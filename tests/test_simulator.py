import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from simulator import simulate


class SimulatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)

        resources = {
            "hosts": [
                {
                    "id": "worker-1",
                    "cpu_cores": 8,
                    "cpu_speed_ghz": 2.5,
                    "ram_gb": 16,
                    "disk_gb": 100,
                    "network_bandwidth_mbps": 1000,
                    "network_latency_ms": 10,
                },
                {
                    "id": "worker-2",
                    "cpu_cores": 4,
                    "cpu_speed_ghz": 2.2,
                    "ram_gb": 8,
                    "disk_gb": 50,
                    "network_bandwidth_mbps": 500,
                    "network_latency_ms": 20,
                },
            ],
            "storage_devices": [
                {
                    "id": "local-ssd",
                    "type": "ssd",
                    "capacity_gb": 500,
                    "read_speed_mbps": 3000,
                    "write_speed_mbps": 2000,
                }
            ],
        }
        self.resources_path = self.work_dir / "resources.json"
        self.resources_path.write_text(json.dumps(resources), encoding="utf-8")

        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  preprocess:
    run: preprocess.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        coresMin: 2
        ramMin: 2048
        tmpdirMin: 1024
        outdirMin: 1024
        networkMin: 120
        workload: 12
        inputDataSizeGb: 2
  process:
    run: process.cwl
    in:
      input_data:
        source: preprocess/out
    out: [out]
    scatter: input_data
    scatter_count: 3
    requirements:
      ResourceRequirement:
        coresMin: 1
        ramMin: 1024
        tmpdirMin: 512
        outdirMin: 512
        networkMin: 80
        workload: 9
        inputDataSizeGb: 1
"""
        self.workflow_path = self.work_dir / "workflow.cwl"
        self.workflow_path.write_text(workflow, encoding="utf-8")

        # Host allocation is never implicit (see simulator.py's _pick_host):
        # a step relying on the default cores/RAM/workload/network duration
        # formula must have its host pinned via --context's "hosts" map.
        self.hosts_context = {"hosts": {"preprocess": "worker-1", "process": "worker-1"}}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_simulate_generates_json_and_html_outputs(self, mock_loader):
        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(self.workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
            context=self.hosts_context,
        )

        self.assertTrue((self.work_dir / "simulation_results.json").exists())
        self.assertTrue((self.work_dir / "simulation_report.html").exists())
        self.assertIn("simulation_metadata", results)
        self.assertGreaterEqual(len(results["activities"]), 2)
        self.assertEqual(results["simulation_metadata"]["scenario"], "nominal")
        # Called at least once for workflow.cwl itself; also called (and
        # gracefully falls back) while probing each step's "run" target for
        # sub-workflow eligibility, including this fixture's placeholder
        # preprocess.cwl/process.cwl targets that don't exist on disk.
        mock_loader.assert_called()

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_scatter_creates_parallel_activity_instances(self, _):
        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(self.workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
            context=self.hosts_context,
        )

        scatter_items = [item for item in results["activities"] if item.get("scatter_index") is not None]
        self.assertEqual(len(scatter_items), 3)
        self.assertEqual({item["scatter_index"] for item in scatter_items}, {1, 2, 3})
        preprocess_end = next(item["end_time_seconds"] for item in results["activities"] if item["id"] == "preprocess")
        self.assertTrue(all(item["start_time_seconds"] >= preprocess_end for item in scatter_items))
        # All shards share the one host pinned for "process" via context --
        # host allocation is explicit, so it's the same host for every shard.
        host_ids = {item["host_id"] for item in scatter_items}
        self.assertEqual(host_ids, {"worker-1"})

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_failure_scenario_triggers_recovery_activity(self, _):
        scenarios = [
            {
                "trigger_activity_id": "process",
                "failure_mode": "ram_overload",
                "recovery_activity_id": "process-recovery",
            }
        ]

        with patch("simulator.random.random", return_value=0.0):
            results = simulate(
                resources_json_path=str(self.resources_path),
                cwl_workflow_paths=[str(self.workflow_path)],
                failure_scenarios=scenarios,
                output_directory=str(self.work_dir),
                random_seed=1,
                context=self.hosts_context,
            )

        self.assertEqual(results["simulation_metadata"]["scenario"], "failure_recovery")
        self.assertTrue(any(item["id"] == "process-recovery" for item in results["activities"]))
        self.assertTrue(results["failure_scenarios"])

    def test_repository_examples_simulate_failure_recovery(self):
        examples_dir = Path(__file__).resolve().parents[1] / "examples"

        results = simulate(
            resources_json_path=str(examples_dir / "resources.json"),
            cwl_workflow_paths=[str(examples_dir / "workflow.cwl")],
            failure_scenarios=json.loads((examples_dir / "failure_scenarios.json").read_text(encoding="utf-8")),
            failure_probability=1.0,
            output_directory=str(self.work_dir),
            random_seed=7,
            context=json.loads((examples_dir / "context.json").read_text(encoding="utf-8")),
        )

        self.assertEqual(results["simulation_metadata"]["scenario"], "failure_recovery")
        self.assertTrue((self.work_dir / "simulation_results.json").exists())
        self.assertTrue((self.work_dir / "simulation_report.html").exists())
        self.assertEqual(
            len([item for item in results["activities"] if item["id"].startswith("align-samples-scatter-")]),
            3,
        )
        self.assertTrue(any(item["id"] == "align-samples-recovery" for item in results["activities"]))

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_duration_seconds_overrides_resource_formula(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  fixed-step:
    run: fixed-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        coresMin: 4
        ramMin: 4096
        workload: 999
        inputDataSizeGb: 999
        durationSeconds: 12.5
"""
        workflow_path = self.work_dir / "fixed-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
        )

        activity = next(item for item in results["activities"] if item["id"] == "fixed-step")
        self.assertEqual(activity["duration_seconds"], 12.5)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_duration_formula_uses_context_variables(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  transfer-step:
    run: transfer-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationFormula: "product_size_gb / link_throughput_gbps + fixed_overhead_s"
"""
        workflow_path = self.work_dir / "formula-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
            context={"product_size_gb": 8.0, "link_throughput_gbps": 0.5, "fixed_overhead_s": 1.0},
        )

        activity = next(item for item in results["activities"] if item["id"] == "transfer-step")
        self.assertEqual(activity["duration_seconds"], 17.0)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_duration_formula_unknown_variable_raises(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  broken-step:
    run: broken-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationFormula: "undefined_variable * 2"
"""
        workflow_path = self.work_dir / "broken-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        with self.assertRaises(ValueError):
            simulate(
                resources_json_path=str(self.resources_path),
                cwl_workflow_paths=[str(workflow_path)],
                output_directory=str(self.work_dir),
                random_seed=7,
                context={},
            )

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_no_host_and_no_explicit_duration_raises(self, _):
        # "implicit-step" relies on the default cores/RAM/workload/network
        # duration formula and has no host pinned via context -- host
        # allocation is never inferred, so this can't be scheduled.
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  implicit-step:
    run: implicit-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        coresMin: 2
        ramMin: 2048
"""
        workflow_path = self.work_dir / "implicit-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        with self.assertRaises(ValueError):
            simulate(
                resources_json_path=str(self.resources_path),
                cwl_workflow_paths=[str(workflow_path)],
                output_directory=str(self.work_dir),
                random_seed=7,
            )

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_unknown_host_id_in_context_raises(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  fixed-step:
    run: fixed-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 5
"""
        workflow_path = self.work_dir / "unknown-host-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        with self.assertRaises(ValueError):
            simulate(
                resources_json_path=str(self.resources_path),
                cwl_workflow_paths=[str(workflow_path)],
                output_directory=str(self.work_dir),
                random_seed=7,
                context={"hosts": {"fixed-step": "worker-nonexistent"}},
            )

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_no_host_assigned_leaves_host_id_blank_and_unqueued(self, _):
        # Two steps with explicit, host-independent durations and no host
        # pinned via context: host_id stays blank, and with no host to queue
        # behind, both can start as soon as their dependencies allow --
        # here, immediately, since neither depends on the other.
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  first-step:
    run: first-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 5
  second-step:
    run: second-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 5
"""
        workflow_path = self.work_dir / "no-host-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
        )

        first = next(item for item in results["activities"] if item["id"] == "first-step")
        second = next(item for item in results["activities"] if item["id"] == "second-step")
        self.assertIsNone(first["host_id"])
        self.assertIsNone(second["host_id"])
        self.assertEqual(first["start_time_seconds"], 0.0)
        self.assertEqual(second["start_time_seconds"], 0.0)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_latency_seconds_stacks_on_duration_seconds(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  fixed-step:
    run: fixed-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 10
        latencySeconds: 3
"""
        workflow_path = self.work_dir / "latency-seconds-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
        )

        activity = next(item for item in results["activities"] if item["id"] == "fixed-step")
        self.assertEqual(activity["duration_seconds"], 13.0)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_latency_formula_uses_context_variables(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  fixed-step:
    run: fixed-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 10
        latencyFormula: "processor_dispatch_latency_s * 2"
"""
        workflow_path = self.work_dir / "latency-formula-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
            context={"processor_dispatch_latency_s": 1.5},
        )

        activity = next(item for item in results["activities"] if item["id"] == "fixed-step")
        self.assertEqual(activity["duration_seconds"], 13.0)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_latency_seconds_adds_overhead_to_resource_derived_duration(self, _):
        workflow_without_latency = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  plain-step:
    run: plain-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement: {}
"""
        workflow_with_latency = workflow_without_latency.replace(
            "ResourceRequirement: {}", "ResourceRequirement:\n        latencySeconds: 3"
        )

        baseline_path = self.work_dir / "no-latency-workflow.cwl"
        baseline_path.write_text(workflow_without_latency, encoding="utf-8")
        latency_path = self.work_dir / "with-latency-workflow.cwl"
        latency_path.write_text(workflow_with_latency, encoding="utf-8")

        plain_step_context = {"hosts": {"plain-step": "worker-1"}}
        baseline_results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(baseline_path)],
            output_directory=str(self.work_dir / "baseline"),
            random_seed=7,
            context=plain_step_context,
        )
        latency_results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(latency_path)],
            output_directory=str(self.work_dir / "latency"),
            random_seed=7,
            context=plain_step_context,
        )

        baseline_activity = next(item for item in baseline_results["activities"] if item["id"] == "plain-step")
        latency_activity = next(item for item in latency_results["activities"] if item["id"] == "plain-step")
        self.assertEqual(baseline_activity["host_id"], latency_activity["host_id"])
        self.assertAlmostEqual(
            latency_activity["duration_seconds"] - baseline_activity["duration_seconds"], 3.0, places=4
        )

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_input_data_size_kb_feeds_default_duration_formula(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  sized-step:
    run: sized-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        networkMin: 600
        inputDataSizeKb: 2000000
"""
        workflow_path = self.work_dir / "sized-kb-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
            context={"hosts": {"sized-step": "worker-1"}},
        )

        activity = next(item for item in results["activities"] if item["id"] == "sized-step")
        # Host is pinned explicitly via context (worker-1, 1000 Mbps network);
        # 2,000,000 kB == 2 GB, same duration the legacy inputDataSizeGb: 2
        # field would have produced.
        self.assertEqual(activity["host_id"], "worker-1")
        self.assertEqual(activity["duration_seconds"], 26.9267)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_input_data_size_formula_uses_context_variables(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  sized-step:
    run: sized-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        networkMin: 600
        inputDataSizeFormula: "product_size_kb * asset_count"
"""
        workflow_path = self.work_dir / "sized-formula-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
            context={"product_size_kb": 1_000_000.0, "asset_count": 3.0, "hosts": {"sized-step": "worker-1"}},
        )

        activity = next(item for item in results["activities"] if item["id"] == "sized-step")
        # 1,000,000 kB * 3 == 3,000,000 kB == 3 GB.
        self.assertEqual(activity["host_id"], "worker-1")
        self.assertEqual(activity["duration_seconds"], 35.26)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_input_data_size_formula_unknown_variable_raises(self, _):
        workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  broken-step:
    run: broken-step.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        inputDataSizeFormula: "undefined_variable * 2"
"""
        workflow_path = self.work_dir / "broken-sized-workflow.cwl"
        workflow_path.write_text(workflow, encoding="utf-8")

        with self.assertRaises(ValueError):
            simulate(
                resources_json_path=str(self.resources_path),
                cwl_workflow_paths=[str(workflow_path)],
                output_directory=str(self.work_dir),
                random_seed=7,
                context={},
            )

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_subworkflow_without_own_cost_is_inlined_and_timed_from_inner_steps(self, _):
        sub_workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs:
  final:
    type: string
    outputSource: second/out
steps:
  first:
    run: first.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 5
  second:
    run: second.cwl
    in:
      out: first/out
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 7
"""
        (self.work_dir / "sub-workflow.cwl").write_text(sub_workflow, encoding="utf-8")

        container_workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  container-step:
    run: sub-workflow.cwl
    in: {}
    out: [final]
"""
        workflow_path = self.work_dir / "container-workflow.cwl"
        workflow_path.write_text(container_workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
        )

        activity_ids = {item["id"] for item in results["activities"]}
        # The container step itself never becomes an activity -- it's fully
        # replaced by its inner steps, prefixed by its own id.
        self.assertNotIn("container-step", activity_ids)
        self.assertEqual(activity_ids, {"container-step/first", "container-step/second"})

        first = next(item for item in results["activities"] if item["id"] == "container-step/first")
        second = next(item for item in results["activities"] if item["id"] == "container-step/second")
        self.assertEqual(first["duration_seconds"], 5.0)
        self.assertEqual(second["duration_seconds"], 7.0)
        # "second" depends (via the rewired cross-step source) on "first".
        self.assertGreaterEqual(second["start_time_seconds"], first["end_time_seconds"])
        self.assertEqual(results["simulation_metadata"]["total_duration_seconds"], 12.0)

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_subworkflow_with_own_cost_stays_atomic(self, _):
        sub_workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs:
  final:
    type: string
    outputSource: inner/out
steps:
  inner:
    run: inner.cwl
    in: {}
    out: [out]
    requirements:
      ResourceRequirement:
        durationSeconds: 999
"""
        (self.work_dir / "costed-sub-workflow.cwl").write_text(sub_workflow, encoding="utf-8")

        container_workflow = """
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  container-step:
    run: costed-sub-workflow.cwl
    in: {}
    out: [final]
    requirements:
      ResourceRequirement:
        durationSeconds: 3
"""
        workflow_path = self.work_dir / "costed-container-workflow.cwl"
        workflow_path.write_text(container_workflow, encoding="utf-8")

        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
        )

        activity_ids = {item["id"] for item in results["activities"]}
        # The step defines its own durationSeconds, so it stays atomic --
        # its sub-workflow's inner steps are never scheduled independently.
        self.assertEqual(activity_ids, {"container-step"})
        self.assertEqual(results["activities"][0]["duration_seconds"], 3.0)

    def test_example_subworkflow_references_exist(self):
        examples_dir = Path(__file__).resolve().parents[1] / "examples"
        workflow = yaml.safe_load((examples_dir / "workflow.cwl").read_text(encoding="utf-8"))
        steps = workflow.get("steps", {})

        for step in steps.values():
            run_target = step.get("run")
            self.assertIsNotNone(run_target, "Each example workflow step must define a run target")
            if isinstance(run_target, str) and run_target.endswith(".cwl"):
                subworkflow_path = examples_dir / run_target
                self.assertTrue(subworkflow_path.exists(), f"Missing referenced file: {run_target}")
                subworkflow = yaml.safe_load(subworkflow_path.read_text(encoding="utf-8"))
                self.assertEqual(subworkflow.get("class"), "Workflow")


if __name__ == "__main__":
    unittest.main()
