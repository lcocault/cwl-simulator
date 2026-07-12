import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_simulate_generates_json_and_html_outputs(self, mock_loader):
        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(self.workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
        )

        self.assertTrue((self.work_dir / "simulation_results.json").exists())
        self.assertTrue((self.work_dir / "simulation_report.html").exists())
        self.assertIn("simulation_metadata", results)
        self.assertGreaterEqual(len(results["activities"]), 2)
        self.assertEqual(results["simulation_metadata"]["scenario"], "nominal")
        mock_loader.assert_called_once()

    @patch("simulator.load_document_by_uri", autospec=True)
    def test_scatter_creates_parallel_activity_instances(self, _):
        results = simulate(
            resources_json_path=str(self.resources_path),
            cwl_workflow_paths=[str(self.workflow_path)],
            output_directory=str(self.work_dir),
            random_seed=7,
        )

        scatter_items = [item for item in results["activities"] if item["id"].startswith("process-scatter-")]
        self.assertEqual(len(scatter_items), 3)
        host_ids = {item["host_id"] for item in scatter_items}
        self.assertGreaterEqual(len(host_ids), 1)

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
            )

        self.assertEqual(results["simulation_metadata"]["scenario"], "failure_recovery")
        self.assertTrue(any(item["id"] == "process-recovery" for item in results["activities"]))
        self.assertTrue(results["failure_scenarios"])


if __name__ == "__main__":
    unittest.main()
