from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from pathlib import Path
from typing import Any

import yaml

try:
    from cwl_utils.parser import load_document_by_uri
except Exception:  # pragma: no cover - optional runtime dependency guard
    load_document_by_uri = None

BITS_PER_BYTE = 8.0
GB_TO_MB = 1000.0


def _load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_cwl(path: str | Path) -> dict[str, Any]:
    file_path = Path(path).resolve()
    if load_document_by_uri is not None:
        try:
            load_document_by_uri(file_path.as_uri())
        except Exception:
            pass
    with file_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _parse_dependencies(step_data: dict[str, Any]) -> set[str]:
    dependencies: set[str] = set()
    inputs = step_data.get("in", {})

    def add_source(source: Any) -> None:
        if isinstance(source, str):
            dependencies.add(source.split("/")[0])
        elif isinstance(source, list):
            for item in source:
                add_source(item)

    if isinstance(inputs, dict):
        for value in inputs.values():
            if isinstance(value, str):
                add_source(value)
            elif isinstance(value, dict):
                add_source(value.get("source"))
            elif isinstance(value, list):
                add_source(value)
    elif isinstance(inputs, list):
        for value in inputs:
            if isinstance(value, dict):
                add_source(value.get("source"))

    return dependencies


def _resource_requirement(step_data: dict[str, Any]) -> dict[str, float]:
    req: dict[str, float] = {
        "cpu_cores": 1.0,
        "ram_gb": 1.0,
        "disk_gb": 1.0,
        "network_mbps": 100.0,
        "workload": 10.0,
        "input_data_size_gb": 1.0,
    }

    requirements = step_data.get("requirements", {})
    resource_requirement: dict[str, Any] = {}
    if isinstance(requirements, dict):
        resource_requirement = requirements.get("ResourceRequirement", {})
    elif isinstance(requirements, list):
        for item in requirements:
            if isinstance(item, dict) and item.get("class") == "ResourceRequirement":
                resource_requirement = item
                break

    if isinstance(resource_requirement, dict):
        if "coresMin" in resource_requirement:
            req["cpu_cores"] = float(resource_requirement["coresMin"])
        if "ramMin" in resource_requirement:
            req["ram_gb"] = float(resource_requirement["ramMin"]) / 1024.0

        tmp = float(resource_requirement.get("tmpdirMin", 0.0))
        out = float(resource_requirement.get("outdirMin", 0.0))
        disk = (tmp + out) / 1024.0
        if disk > 0:
            req["disk_gb"] = disk

        if "networkMin" in resource_requirement:
            req["network_mbps"] = float(resource_requirement["networkMin"])

        if "workload" in resource_requirement:
            req["workload"] = float(resource_requirement["workload"])

        if "inputDataSizeGb" in resource_requirement:
            req["input_data_size_gb"] = float(resource_requirement["inputDataSizeGb"])

    return req


def _scatter_count(step_data: dict[str, Any]) -> int:
    if "scatter" not in step_data:
        return 1
    explicit = step_data.get("scatter_count", step_data.get("scatterCount"))
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    scatter = step_data.get("scatter")
    if isinstance(scatter, list):
        return max(2, len(scatter))
    return 2


def _duration(host: dict[str, Any], storage: dict[str, Any], req: dict[str, float]) -> float:
    cpu_speed = float(host.get("cpu_speed_ghz", 2.5))
    cpu_component = req["workload"] / max(req["cpu_cores"] * cpu_speed, 0.001)
    ram_component = req["workload"] / max(float(host["ram_gb"]) * 0.1, 0.001)
    disk_component = req["input_data_size_gb"] / max(float(storage["read_speed_mbps"]) / 1000.0, 0.001)
    network_component = req["input_data_size_gb"] / max(float(host["network_bandwidth_mbps"]) / BITS_PER_BYTE, 0.001)
    network_component += float(host.get("network_latency_ms", 0.0)) / 1000.0
    return max(cpu_component + ram_component + disk_component + network_component, 0.01)


def _pick_host(
    hosts: list[dict[str, Any]],
    host_available: dict[str, float],
    dependency_ready_at: float,
    req: dict[str, float],
    storage: dict[str, Any],
) -> tuple[dict[str, Any], float, float]:
    best: tuple[dict[str, Any], float, float] | None = None

    for host in hosts:
        fits = (
            float(host["cpu_cores"]) >= req["cpu_cores"]
            and float(host["ram_gb"]) >= req["ram_gb"]
            and float(host["disk_gb"]) >= req["disk_gb"]
            and float(host["network_bandwidth_mbps"]) >= req["network_mbps"]
        )
        if not fits:
            continue

        start = max(host_available[host["id"]], dependency_ready_at)
        end = start + _duration(host, storage, req)
        if best is None or end < best[2]:
            best = (host, start, end)

    if best is None:
        fallback = max(hosts, key=lambda host: float(host["cpu_cores"]) + float(host["ram_gb"]))
        start = max(host_available[fallback["id"]], dependency_ready_at)
        end = start + _duration(fallback, storage, req)
        return fallback, start, end

    return best


def _build_html_report(results: dict[str, Any]) -> str:
    timeline_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td>"
        f"<td>{item['name']}</td>"
        f"<td>{item['host_id']}</td>"
        f"<td>{item['start_time_seconds']:.2f}</td>"
        f"<td>{item['end_time_seconds']:.2f}</td>"
        f"<td>{item['status']}</td>"
        f"<td>{item['failure_mode']}</td>"
        "</tr>"
        for item in results["activities"]
    )

    utilization = results["resource_utilization"]
    labels = [item["timestamp_seconds"] for item in utilization]
    cpu_values = [item["cpu_utilization_percent"] for item in utilization]
    ram_values = [item["ram_utilization_percent"] for item in utilization]
    disk_values = [item["disk_io_mbps"] for item in utilization]
    net_values = [item["network_io_mbps"] for item in utilization]

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>CWL Simulation Report</title>
  <script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; background: #f8f9fb; color: #1f2937; }}
    h1, h2 {{ margin-bottom: 0.5rem; }}
    .meta {{ margin-bottom: 1rem; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ border: 1px solid #d1d5db; padding: 0.5rem; text-align: left; }}
    th {{ background: #e5e7eb; }}
    .chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }}
    .card {{ background: #fff; padding: 1rem; border: 1px solid #d1d5db; border-radius: 6px; }}
    ul {{ background: #fff; border: 1px solid #d1d5db; border-radius: 6px; padding: 1rem 1.5rem; }}
  </style>
</head>
<body>
  <h1>CWL Simulation Report</h1>
  <div class=\"meta\">
    <strong>Timestamp:</strong> {results['simulation_metadata']['timestamp']}<br />
    <strong>Scenario:</strong> {results['simulation_metadata']['scenario']}<br />
    <strong>Total Duration:</strong> {results['simulation_metadata']['total_duration_seconds']:.2f}s
  </div>

  <h2>Activity Timeline</h2>
  <table>
    <thead>
      <tr>
        <th>ID</th><th>Name</th><th>Host</th><th>Start (s)</th><th>End (s)</th><th>Status</th><th>Failure</th>
      </tr>
    </thead>
    <tbody>{timeline_rows}</tbody>
  </table>

  <h2>Failure Scenarios</h2>
  <ul>
    {''.join(f"<li>{item['trigger_activity_id']} → {item['failure_mode']} → {item['recovery_activity_id']}</li>" for item in results['failure_scenarios']) or '<li>No failures recorded.</li>'}
  </ul>

  <h2>Resource Utilization</h2>
  <div class=\"chart-grid\">
    <div class=\"card\"><canvas id=\"cpuChart\"></canvas></div>
    <div class=\"card\"><canvas id=\"ramChart\"></canvas></div>
    <div class=\"card\"><canvas id=\"diskChart\"></canvas></div>
    <div class=\"card\"><canvas id=\"networkChart\"></canvas></div>
  </div>

  <script>
    const labels = {json.dumps(labels)};
    const cpu = {json.dumps(cpu_values)};
    const ram = {json.dumps(ram_values)};
    const disk = {json.dumps(disk_values)};
    const network = {json.dumps(net_values)};

    const createChart = (id, label, data, color) => new Chart(document.getElementById(id), {{
      type: 'line',
      data: {{ labels, datasets: [{{ label, data, borderColor: color, fill: false }}] }},
      options: {{ responsive: true, maintainAspectRatio: false }}
    }});

    createChart('cpuChart', 'CPU Utilization (%)', cpu, '#2563eb');
    createChart('ramChart', 'RAM Utilization (%)', ram, '#16a34a');
    createChart('diskChart', 'Disk I/O (Mbps)', disk, '#dc2626');
    createChart('networkChart', 'Network I/O (Mbps)', network, '#7c3aed');
  </script>
</body>
</html>
"""


def simulate(
    resources_json_path: str,
    cwl_workflow_paths: list[str],
    failure_scenarios: list[dict[str, str]] | None = None,
    failure_probability: float = 0.2,
    output_directory: str | None = None,
    random_seed: int | None = None,
) -> dict[str, Any]:
    if random_seed is not None:
        random.seed(random_seed)

    resources = _load_json(resources_json_path)
    hosts = resources.get("hosts", [])
    storage_devices = resources.get("storage_devices", [])
    if not hosts:
        raise ValueError("resources.json must contain at least one host")
    if not storage_devices:
        raise ValueError("resources.json must contain at least one storage device")

    storage = storage_devices[0]
    host_available = {host["id"]: 0.0 for host in hosts}

    definitions: dict[str, dict[str, Any]] = {}
    dependencies: dict[str, set[str]] = {}

    for workflow_path in cwl_workflow_paths:
        workflow = _load_cwl(workflow_path)
        steps = workflow.get("steps", {})
        for step_id, step_data in steps.items():
            if not isinstance(step_data, dict):
                continue
            definitions[step_id] = step_data
            dependencies[step_id] = _parse_dependencies(step_data)

    scheduled: dict[str, dict[str, Any]] = {}
    activities: list[dict[str, Any]] = []
    utilization: list[dict[str, Any]] = []

    while len(scheduled) < len(definitions):
        ready = [
            step_id
            for step_id, deps in dependencies.items()
            if step_id not in scheduled and deps.issubset(scheduled.keys())
        ]
        if not ready:
            unscheduled = [step_id for step_id in definitions if step_id not in scheduled]
            raise ValueError(f"Unable to resolve workflow dependencies for: {unscheduled}")

        for step_id in sorted(ready):
            step_data = definitions[step_id]
            dep_ready_at = max((scheduled[dep]["end_time_seconds"] for dep in dependencies[step_id]), default=0.0)
            req = _resource_requirement(step_data)
            scatter_count = _scatter_count(step_data)

            shard_records = []
            for index in range(scatter_count):
                host, start, end = _pick_host(hosts, host_available, dep_ready_at, req, storage)
                host_available[host["id"]] = end

                activity_id = step_id if scatter_count == 1 else f"{step_id}-scatter-{index + 1}"
                record = {
                    "id": activity_id,
                    "base_id": step_id,
                    "name": step_data.get("label") or step_data.get("name") or step_id,
                    "start_time_seconds": round(start, 4),
                    "end_time_seconds": round(end, 4),
                    "duration_seconds": round(end - start, 4),
                    "host_id": host["id"],
                    "resources_used": {
                        "cpu_cores": req["cpu_cores"],
                        "ram_gb": req["ram_gb"],
                        "disk_gb": req["disk_gb"],
                        "network_mbps": req["network_mbps"],
                    },
                    "status": "completed",
                    "failure_mode": "none",
                }
                shard_records.append(record)
                activities.append(record)

                duration = max(end - start, 0.001)
                utilization.append(
                    {
                        "host_id": host["id"],
                        "timestamp_seconds": round(start, 4),
                        "cpu_utilization_percent": round(min((req["cpu_cores"] / float(host["cpu_cores"])) * 100.0, 100.0), 2),
                        "ram_utilization_percent": round(min((req["ram_gb"] / float(host["ram_gb"])) * 100.0, 100.0), 2),
                        "disk_io_mbps": round(min(float(storage["read_speed_mbps"]), req["input_data_size_gb"] * GB_TO_MB / duration), 2),
                        "network_io_mbps": round(min(float(host["network_bandwidth_mbps"]), req["network_mbps"]), 2),
                    }
                )

            scheduled[step_id] = {
                "end_time_seconds": max(item["end_time_seconds"] for item in shard_records),
            }

    failure_scenarios = failure_scenarios or []
    scenario_by_trigger = {item["trigger_activity_id"]: item for item in failure_scenarios}
    failure_records: list[dict[str, str]] = []

    for activity in list(activities):
        trigger_id = activity["base_id"]
        scenario = scenario_by_trigger.get(trigger_id)

        should_fail = bool(scenario) and random.random() < failure_probability
        if not should_fail:
            continue

        activity["status"] = "failed"
        activity["failure_mode"] = scenario.get("failure_mode", "simulated_failure")
        recovery_id = scenario.get("recovery_activity_id", f"{trigger_id}-recovery")

        recovery_duration = max(activity["duration_seconds"] * 0.5, 0.01)
        recovery_start = activity["end_time_seconds"]
        recovery_end = recovery_start + recovery_duration

        activities.append(
            {
                "id": recovery_id,
                "base_id": recovery_id,
                "name": f"Recovery for {trigger_id}",
                "start_time_seconds": round(recovery_start, 4),
                "end_time_seconds": round(recovery_end, 4),
                "duration_seconds": round(recovery_duration, 4),
                "host_id": activity["host_id"],
                "resources_used": activity["resources_used"],
                "status": "completed",
                "failure_mode": "none",
            }
        )

        failure_records.append(
            {
                "trigger_activity_id": activity["id"],
                "failure_mode": activity["failure_mode"],
                "recovery_activity_id": recovery_id,
            }
        )

    for activity in activities:
        activity.pop("base_id", None)

    total_duration = max((item["end_time_seconds"] for item in activities), default=0.0)
    scenario_name = "failure_recovery" if failure_records else "nominal"

    results = {
        "simulation_metadata": {
            "timestamp": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "total_duration_seconds": round(total_duration, 4),
            "scenario": scenario_name,
        },
        "activities": sorted(activities, key=lambda item: (item["start_time_seconds"], item["id"])),
        "resource_utilization": sorted(utilization, key=lambda item: item["timestamp_seconds"]),
        "failure_scenarios": failure_records,
    }

    output_path = Path(output_directory) if output_directory else Path.cwd()
    output_path.mkdir(parents=True, exist_ok=True)

    with (output_path / "simulation_results.json").open("w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)

    with (output_path / "simulation_report.html").open("w", encoding="utf-8") as handle:
        handle.write(_build_html_report(results))

    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate CWL workflow execution")
    parser.add_argument("resources_json_path", help="Path to resources.json")
    parser.add_argument("cwl_workflows", nargs="+", help="Path(s) to CWL workflow files")
    parser.add_argument("--output-directory", default=".", help="Output directory for report files")
    parser.add_argument("--failure-scenarios", default=None, help="Optional path to failure_scenarios JSON file")
    parser.add_argument("--failure-probability", type=float, default=0.2, help="Failure probability for scenario triggers")
    parser.add_argument("--random-seed", type=int, default=None, help="Random seed for deterministic runs")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    scenarios = None
    if args.failure_scenarios:
        scenarios = _load_json(args.failure_scenarios)
    simulate(
        resources_json_path=args.resources_json_path,
        cwl_workflow_paths=args.cwl_workflows,
        failure_scenarios=scenarios,
        failure_probability=args.failure_probability,
        output_directory=args.output_directory,
        random_seed=args.random_seed,
    )


if __name__ == "__main__":
    main()
