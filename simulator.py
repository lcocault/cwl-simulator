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
    from schema_salad.exceptions import ValidationException
except ImportError:  # pragma: no cover - optional runtime dependency guard
    load_document_by_uri = None
    ValidationException = ValueError

BITS_TO_BYTES_DIVISOR = 8.0
GB_TO_MB_DECIMAL = 1000.0
MB_PER_GIB = 1024.0
RAM_WORKLOAD_FACTOR = 0.1
KB_PER_GB_DECIMAL = 1_000_000.0


def _load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_context(path: str | Path) -> dict[str, Any]:
    # YAML is a superset of JSON, so a strict JSON context file still parses
    # fine here -- this just additionally allows "#" comments in it.
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_cwl(path: str | Path) -> dict[str, Any]:
    file_path = Path(path).resolve()
    if load_document_by_uri is not None:
        try:
            load_document_by_uri(file_path.as_uri())
        except (ValidationException, ValueError, OSError):
            pass
    with file_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _parse_dependencies(step_data: dict[str, Any]) -> set[str]:
    dependencies: set[str] = set()
    inputs = step_data.get("in", {})

    def add_source(source: Any) -> None:
        if isinstance(source, str):
            # rsplit, not split: a flattened sub-workflow step id can itself
            # contain "/" (e.g. "pf/l0-product-transfer-dmz-to-dr").
            dependencies.add(source.rsplit("/", 1)[0])
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


def _resource_requirement_block(step_data: dict[str, Any]) -> dict[str, Any]:
    requirements = step_data.get("requirements", {})
    if isinstance(requirements, dict):
        resource_requirement = requirements.get("ResourceRequirement", {})
        return resource_requirement if isinstance(resource_requirement, dict) else {}
    if isinstance(requirements, list):
        for item in requirements:
            if isinstance(item, dict) and item.get("class") == "ResourceRequirement":
                return item
    return {}


_FORMULA_FUNCTIONS: dict[str, Any] = {"min": min, "max": max, "abs": abs, "round": round}


def _evaluate_formula(field_name: str, formula: str, context: dict[str, Any]) -> float:
    namespace: dict[str, Any] = {**_FORMULA_FUNCTIONS, **context}
    try:
        result = eval(formula, {"__builtins__": {}}, namespace)  # noqa: S307 - restricted namespace
    except NameError as exc:
        raise ValueError(f"Unknown variable in {field_name} {formula!r}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface any expression error with context
        raise ValueError(f"Could not evaluate {field_name} {formula!r}: {exc}") from exc
    return float(result)


def _resource_requirement(step_data: dict[str, Any], context: dict[str, Any]) -> dict[str, float]:
    req: dict[str, float] = {
        "cpu_cores": 1.0,
        "ram_gb": 1.0,
        "disk_gb": 1.0,
        "network_mbps": 100.0,
        "workload": 10.0,
        "input_data_size_gb": 1.0,
        "latency_seconds": 0.0,
    }

    resource_requirement = _resource_requirement_block(step_data)

    if isinstance(resource_requirement, dict):
        if "coresMin" in resource_requirement:
            req["cpu_cores"] = float(resource_requirement["coresMin"])
        if "ramMin" in resource_requirement:
            req["ram_gb"] = float(resource_requirement["ramMin"]) / MB_PER_GIB

        tmp = float(resource_requirement.get("tmpdirMin", 0.0))
        out = float(resource_requirement.get("outdirMin", 0.0))
        disk = (tmp + out) / MB_PER_GIB
        if disk > 0:
            req["disk_gb"] = disk

        if "networkMin" in resource_requirement:
            req["network_mbps"] = float(resource_requirement["networkMin"])

        if "workload" in resource_requirement:
            req["workload"] = float(resource_requirement["workload"])

        if "inputDataSizeFormula" in resource_requirement:
            size_kb = _evaluate_formula(
                "inputDataSizeFormula", str(resource_requirement["inputDataSizeFormula"]), context
            )
            req["input_data_size_gb"] = size_kb / KB_PER_GB_DECIMAL
        elif "inputDataSizeKb" in resource_requirement:
            req["input_data_size_gb"] = float(resource_requirement["inputDataSizeKb"]) / KB_PER_GB_DECIMAL
        elif "inputDataSizeGb" in resource_requirement:
            req["input_data_size_gb"] = float(resource_requirement["inputDataSizeGb"])

        if "latencyFormula" in resource_requirement:
            req["latency_seconds"] = _evaluate_formula(
                "latencyFormula", str(resource_requirement["latencyFormula"]), context
            )
        elif "latencySeconds" in resource_requirement:
            req["latency_seconds"] = float(resource_requirement["latencySeconds"])

    return req


def _duration_override(step_data: dict[str, Any], context: dict[str, Any]) -> float | None:
    resource_requirement = _resource_requirement_block(step_data)

    if "durationSeconds" in resource_requirement:
        return float(resource_requirement["durationSeconds"])

    if "durationFormula" in resource_requirement:
        return _evaluate_formula("durationFormula", str(resource_requirement["durationFormula"]), context)

    return None


def _host_assignment(step_id: str, context: dict[str, Any]) -> str | None:
    """A step's host, if pinned via --context's "hosts" map; None otherwise.

    Host allocation is never inferred from resource fit or availability --
    see _pick_host(). "hosts" maps a (flattened) step id to a host_id from
    resources.json, e.g. {"hosts": {"decipher-l0-product": "dr-processing-a"}}.
    """
    hosts_map = context.get("hosts")
    if not isinstance(hosts_map, dict):
        return None
    return hosts_map.get(step_id)


_COST_DEFINING_FIELDS = {
    "durationSeconds",
    "durationFormula",
    "workload",
    "inputDataSizeGb",
    "inputDataSizeKb",
    "inputDataSizeFormula",
    "latencySeconds",
    "latencyFormula",
}


def _step_defines_own_cost(step_data: dict[str, Any]) -> bool:
    resource_requirement = _resource_requirement_block(step_data)
    return any(field in resource_requirement for field in _COST_DEFINING_FIELDS)


def _resolve_run(run_value: Any, base_dir: Path) -> tuple[dict[str, Any] | None, Path]:
    if isinstance(run_value, str):
        run_path = (base_dir / run_value).resolve()
        try:
            return _load_cwl(run_path), run_path.parent
        except OSError:
            return None, base_dir
    if isinstance(run_value, dict):
        return run_value, base_dir
    return None, base_dir


def _out_names(step_data: dict[str, Any]) -> list[str]:
    out = step_data.get("out", [])
    if isinstance(out, dict):
        return list(out.keys())
    if isinstance(out, list):
        return list(out)
    return []


def _iter_in_pairs(step_data: dict[str, Any]) -> list[tuple[str, Any]]:
    inputs = step_data.get("in", {})
    pairs: list[tuple[str, Any]] = []
    if isinstance(inputs, dict):
        for name, value in inputs.items():
            source = value.get("source") if isinstance(value, dict) else value
            pairs.append((name, source))
    elif isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                pairs.append((item.get("id"), item.get("source")))
    return pairs


def _resolve_in_source(
    source: Any,
    local_steps: dict[str, Any],
    local_exits: dict[str, dict[str, list[str]]],
    external_sources: dict[str, str],
) -> Any:
    if not isinstance(source, str):
        return source
    if "/" in source:
        dep_step_id, dep_output_name = source.split("/", 1)
        if dep_step_id in local_steps:
            candidates = local_exits.get(dep_step_id, {}).get(dep_output_name, [])
            if candidates:
                return f"{candidates[0]}/{dep_output_name}"
        return source
    return external_sources.get(source, source)


def _flatten_steps(
    steps: dict[str, Any],
    outputs: dict[str, Any],
    base_dir: Path,
    prefix: str,
    external_sources: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Recursively inline sub-workflow steps into real, schedulable leaf steps.

    A step whose "run" resolves to another CWL Workflow is inlined -- its
    duration/size become emergent from its own inner steps' schedule --
    unless it scatters or already defines its own cost (durationSeconds/
    durationFormula/workload/inputDataSize*), in which case it stays atomic
    as before (this is what keeps examples/workflow.cwl's stage-inputs/
    align-samples/summarize-results, which all set their own workload and
    inputDataSizeGb, scheduled exactly as before).

    Nested leaf step ids are prefixed by their containing step path (e.g.
    "pf/l0-product-transfer-dmz-to-dr") so they can't collide with sibling
    ids and so failure/scatter/report output stays traceable to its subsystem.
    """
    flat_steps: dict[str, dict[str, Any]] = {}
    local_exits: dict[str, dict[str, list[str]]] = {}

    for step_id, step_data in steps.items():
        if not isinstance(step_data, dict):
            continue
        node_id = f"{prefix}{step_id}"
        wf_dict, child_base_dir = _resolve_run(step_data.get("run"), base_dir)
        eligible = (
            wf_dict is not None
            and wf_dict.get("class") == "Workflow"
            and "scatter" not in step_data
            and not _step_defines_own_cost(step_data)
        )

        if eligible:
            child_external_sources = {
                name: _resolve_in_source(source, steps, local_exits, external_sources)
                for name, source in _iter_in_pairs(step_data)
            }
            child_flat_steps, child_exits = _flatten_steps(
                wf_dict.get("steps", {}),
                wf_dict.get("outputs", {}),
                child_base_dir,
                f"{node_id}/",
                child_external_sources,
            )
            flat_steps.update(child_flat_steps)
            local_exits[step_id] = child_exits
        else:
            rewired = dict(step_data)
            rewired["in"] = {
                name: _resolve_in_source(source, steps, local_exits, external_sources)
                for name, source in _iter_in_pairs(step_data)
            }
            flat_steps[node_id] = rewired
            local_exits[step_id] = {name: [node_id] for name in _out_names(step_data)}

    exits: dict[str, list[str]] = {}
    if isinstance(outputs, dict):
        for out_name, out_def in outputs.items():
            source = out_def.get("outputSource") if isinstance(out_def, dict) else None
            if isinstance(source, str) and "/" in source:
                dep_step_id, dep_output_name = source.split("/", 1)
                if dep_step_id in local_exits:
                    exits[out_name] = local_exits[dep_step_id].get(dep_output_name, [])

    return flat_steps, exits


def _scatter_count(step_data: dict[str, Any]) -> int:
    if "scatter" not in step_data:
        return 1
    explicit = step_data.get("scatter_count")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    scatter = step_data.get("scatter")
    if isinstance(scatter, list):
        return max(1, len(scatter))
    return 2


def _duration(host: dict[str, Any], storage: dict[str, Any], req: dict[str, float]) -> float:
    cpu_speed = float(host.get("cpu_speed_ghz", 2.5))
    cpu_component = req["workload"] / max(req["cpu_cores"] * cpu_speed, 0.001)
    ram_component = req["workload"] / max(float(host["ram_gb"]) * RAM_WORKLOAD_FACTOR, 0.001)
    disk_component = req["input_data_size_gb"] / max(float(storage["read_speed_mbps"]) / GB_TO_MB_DECIMAL, 0.001)
    network_component = req["input_data_size_gb"] * GB_TO_MB_DECIMAL / max(
        float(host["network_bandwidth_mbps"]) / BITS_TO_BYTES_DIVISOR, 0.001
    )
    network_component += float(host.get("network_latency_ms", 0.0)) / 1000.0
    return max(cpu_component + ram_component + disk_component + network_component, 0.01)


def _pick_host(
    hosts: list[dict[str, Any]],
    host_available: dict[str, float],
    dependency_ready_at: float,
    req: dict[str, float],
    storage: dict[str, Any],
    duration_override: float | None,
    host_id: str | None,
) -> tuple[dict[str, Any] | None, float, float]:
    """Resolve an activity's host, start, and end time.

    Host assignment is never inferred from resource fit or host availability
    -- host_id comes straight from --context's "hosts" map (see
    _host_assignment()). With no host_id, the activity isn't queued behind
    any host's availability: start is purely dependency-driven. That branch
    is only reachable when duration_override is set, since simulate()
    requires either an explicit host or an explicit duration -- a
    resource-formula duration needs a specific host's cpu/ram/network specs
    to even be computable.
    """
    if host_id is None:
        start = dependency_ready_at
        end = start + duration_override + req["latency_seconds"]
        return None, start, end

    host = next((item for item in hosts if item["id"] == host_id), None)
    if host is None:
        raise ValueError(f"Unknown host_id {host_id!r} in --context's \"hosts\" map (check resources.json)")

    start = max(host_available[host_id], dependency_ready_at)
    base_duration = duration_override if duration_override is not None else _duration(host, storage, req)
    end = start + base_duration + req["latency_seconds"]
    return host, start, end


def _utilization_timeline(
    utilization: list[dict[str, Any]],
) -> tuple[list[float], list[float], list[float], list[float], list[float], list[list[str]]]:
    """Piecewise-constant, overlap-aware timeline for the resource charts.

    Breakpoints are every sample's start and end time. Each segment's
    values are summed across whichever activities are actually active
    during it (so activities running in parallel on different hosts are
    correctly combined instead of just alternating), alongside the list of
    contributing activity names per segment for tooltips -- a single name
    when everything is sequential, several when activities overlap. The
    final breakpoint is always some activity's end time with nothing else
    still active, so it naturally closes the last step back to zero.
    """
    if not utilization:
        return [], [], [], [], [], []

    breakpoints = sorted({item["timestamp_seconds"] for item in utilization} | {item["end_time_seconds"] for item in utilization})

    timestamps: list[float] = []
    cpu_values: list[float] = []
    ram_values: list[float] = []
    disk_values: list[float] = []
    net_values: list[float] = []
    task_lists: list[list[str]] = []

    for t in breakpoints:
        active = [item for item in utilization if item["timestamp_seconds"] <= t < item["end_time_seconds"]]
        timestamps.append(t)
        cpu_values.append(round(sum(item["cpu_cores_used"] for item in active), 4))
        ram_values.append(round(sum(item["ram_gb_used"] for item in active), 4))
        disk_values.append(round(sum(item["disk_io_mbps"] for item in active), 2))
        net_values.append(round(sum(item["network_io_mbps"] for item in active), 2))
        task_lists.append([item["activity_id"] for item in active])

    return timestamps, cpu_values, ram_values, disk_values, net_values, task_lists


_HOST_COLOR_PALETTE = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]
_FAILED_BORDER_COLOR = "#d03b3b"  # status/critical, reserved -- never a host slot
_UNASSIGNED_HOST_COLOR = "#898781"  # muted ink, reserved -- never a host slot
_UNASSIGNED_HOST_LABEL = "(unassigned)"


def _gantt_data(activities: list[dict[str, Any]]) -> dict[str, Any]:
    """Chart.js floating-bar Gantt series, one row per activity.

    Bars are colored by host_id (identity, so a fixed categorical slot per
    host in first-seen order -- never re-derived from status or position).
    An activity with no host_id (host allocation is never inferred -- see
    _pick_host()) always gets the reserved muted-gray swatch instead of a
    categorical slot, so "no host assigned" reads as a distinct, deliberate
    state rather than competing for identity color with real hosts. A
    failed activity keeps its host fill but gets the reserved status-red
    border, so failure is a secondary cue layered on top of identity rather
    than competing with it for the same channel.
    """
    host_color_map: dict[str, str] = {}
    for item in activities:
        host_id = item["host_id"]
        if host_id is None:
            continue
        host_color_map.setdefault(host_id, _HOST_COLOR_PALETTE[len(host_color_map) % len(_HOST_COLOR_PALETTE)])

    def color_for(host_id: str | None) -> str:
        return _UNASSIGNED_HOST_COLOR if host_id is None else host_color_map[host_id]

    legend_map = dict(host_color_map)
    if any(item["host_id"] is None for item in activities):
        legend_map[_UNASSIGNED_HOST_LABEL] = _UNASSIGNED_HOST_COLOR

    return {
        "labels": [item["id"] for item in activities],
        "ranges": [[item["start_time_seconds"], item["end_time_seconds"]] for item in activities],
        "names": [item["name"] for item in activities],
        "hosts": [item["host_id"] or _UNASSIGNED_HOST_LABEL for item in activities],
        "statuses": [item["status"] for item in activities],
        "failure_modes": [item["failure_mode"] for item in activities],
        "colors": [color_for(item["host_id"]) for item in activities],
        "borders": [_FAILED_BORDER_COLOR if item["status"] == "failed" else "rgba(0,0,0,0)" for item in activities],
        "host_color_map": legend_map,
    }


def _build_html_report(results: dict[str, Any]) -> str:
    timeline_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td>"
        f"<td>{item['name']}</td>"
        f"<td>{item['host_id'] or _UNASSIGNED_HOST_LABEL}</td>"
        f"<td>{item['start_time_seconds']:.2f}</td>"
        f"<td>{item['end_time_seconds']:.2f}</td>"
        f"<td>{item['duration_seconds']:.2f}</td>"
        f"<td>{item['status']}</td>"
        f"<td>{item['failure_mode']}</td>"
        "</tr>"
        for item in results["activities"]
    )

    labels, cpu_values, ram_values, disk_values, net_values, task_lists = _utilization_timeline(
        results["resource_utilization"]
    )

    gantt = _gantt_data(results["activities"])
    gantt_height = max(200, len(gantt["labels"]) * 28 + 60)
    gantt_legend = "".join(
        f'<span class="legend-item"><span class="swatch" style="background:{color}"></span>{host}</span>'
        for host, color in gantt["host_color_map"].items()
    )
    if any(status == "failed" for status in gantt["statuses"]):
        gantt_legend += (
            f'<span class="legend-item"><span class="swatch" '
            f'style="background:transparent;border:2px solid {_FAILED_BORDER_COLOR}"></span>Failed activity</span>'
        )

    failure_scenarios_section = ""
    if results["failure_scenarios"]:
        failure_items = "".join(
            f"<li>{item['trigger_activity_id']} → {item['failure_mode']} → {item['recovery_activity_id']}</li>"
            for item in results["failure_scenarios"]
        )
        failure_scenarios_section = f"""
  <h2>Failure Scenarios</h2>
  <ul>
    {failure_items}
  </ul>
"""

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
    .controls {{ margin: 0.5rem 0 1rem; }}
    .controls label {{ margin-right: 1.5rem; cursor: pointer; }}
    .gantt-legend {{ margin: 0.75rem 0; display: flex; flex-wrap: wrap; gap: 1rem; }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.9rem; color: #52514e; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}
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
        <th>ID</th><th>Name</th><th>Host</th><th>Start (s)</th><th>End (s)</th><th>Duration (s)</th><th>Status</th><th>Failure</th>
      </tr>
    </thead>
    <tbody>{timeline_rows}</tbody>
  </table>

  <h2>Activity Gantt Chart</h2>
  <div class=\"gantt-legend\">{gantt_legend}</div>
  <div class=\"card\" style=\"height: {gantt_height}px;\"><canvas id=\"ganttChart\"></canvas></div>

  {failure_scenarios_section}
  <h2>Resource Utilization</h2>
  <div class=\"controls\">
    <label><input type=\"radio\" name=\"timescale\" value=\"regular\" checked /> Regular (one scale unit per step)</label>
    <label><input type=\"radio\" name=\"timescale\" value=\"actual\" /> Actual time spent</label>
  </div>
  <div class=\"chart-grid\">
    <div class=\"card\"><canvas id=\"cpuChart\"></canvas></div>
    <div class=\"card\"><canvas id=\"ramChart\"></canvas></div>
    <div class=\"card\"><canvas id=\"diskChart\"></canvas></div>
    <div class=\"card\"><canvas id=\"networkChart\"></canvas></div>
  </div>

  <script>
    const ganttLabels = {json.dumps(gantt["labels"])};
    const ganttRanges = {json.dumps(gantt["ranges"])};
    const ganttNames = {json.dumps(gantt["names"])};
    const ganttHosts = {json.dumps(gantt["hosts"])};
    const ganttStatuses = {json.dumps(gantt["statuses"])};
    const ganttFailureModes = {json.dumps(gantt["failure_modes"])};
    const ganttColors = {json.dumps(gantt["colors"])};
    const ganttBorders = {json.dumps(gantt["borders"])};

    new Chart(document.getElementById('ganttChart'), {{
      type: 'bar',
      data: {{
        labels: ganttLabels,
        datasets: [{{
          label: 'Activities',
          data: ganttRanges,
          backgroundColor: ganttColors,
          borderColor: ganttBorders,
          borderWidth: 2,
          borderSkipped: false,
          borderRadius: 4,
          barPercentage: 0.6,
          categoryPercentage: 0.85,
        }}]
      }},
      options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        scales: {{
          x: {{ type: 'linear', position: 'top', min: 0, title: {{ display: true, text: 'Time (s)' }} }},
          y: {{ ticks: {{ autoSkip: false }} }}
        }},
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{
              title: (items) => ganttNames[items[0].dataIndex],
              label: (item) => {{
                const i = item.dataIndex;
                const [start, end] = ganttRanges[i];
                const lines = [
                  `Host: ${{ganttHosts[i]}}`,
                  `Start: ${{start.toFixed(2)}}s   End: ${{end.toFixed(2)}}s`,
                  `Duration: ${{(end - start).toFixed(2)}}s`,
                  `Status: ${{ganttStatuses[i]}}`,
                ];
                if (ganttStatuses[i] === 'failed') {{
                  lines.push(`Failure: ${{ganttFailureModes[i]}}`);
                }}
                return lines;
              }}
            }}
          }}
        }}
      }}
    }});

    const timestamps = {json.dumps(labels)};
    const cpu = {json.dumps(cpu_values)};
    const ram = {json.dumps(ram_values)};
    const disk = {json.dumps(disk_values)};
    const network = {json.dumps(net_values)};
    const contributingTasks = {json.dumps(task_lists)};

    const series = [
      {{ id: 'cpuChart', label: 'CPU Cores Used', data: cpu, color: '#2563eb' }},
      {{ id: 'ramChart', label: 'RAM (GB) Used', data: ram, color: '#16a34a' }},
      {{ id: 'diskChart', label: 'Disk I/O (Mbps)', data: disk, color: '#dc2626' }},
      {{ id: 'networkChart', label: 'Network I/O (Mbps)', data: network, color: '#7c3aed' }},
    ];

    const tooltipTasksPlugin = {{
      callbacks: {{
        afterBody: (items) => {{
          const tasks = contributingTasks[items[0].dataIndex] || [];
          if (tasks.length === 0) {{
            return ['(no active task)'];
          }}
          const heading = tasks.length === 1 ? 'Contributing task:' : `Contributing tasks (${{tasks.length}}):`;
          return [heading, ...tasks.map((t) => '- ' + t)];
        }}
      }}
    }};

    // Chart.js's built-in 'nearest'/'index' interaction picks whichever data
    // point is closest by x-distance -- for a stepped:'before' line (value
    // holds from a point until the next one, which is what "before" actually
    // means in Chart.js's own _steppedLineTo: it draws the *current* point's
    // value across the segment and jumps at the *end* -- 'after' jumps
    // immediately and is wrong for this), that's incorrect for anything past
    // a wide segment's midpoint: the line drawn under the cursor is still at
    // the *earlier* point's value, but "nearest point" has already flipped to
    // the *later* one. This mode instead finds the last point at or before
    // the cursor's pixel position, matching what's actually drawn. Reading
    // each point element's own rendered .x (its actual pixel position after
    // layout) rather than re-deriving it via scale.getPixelForValue works
    // identically for both the category ('regular') and linear ('actual')
    // x-scales, since Chart.js always populates .x on point elements the
    // same way regardless of scale type.
    Chart.Interaction.modes.currentStep = (chart, e) => {{
      const results = [];
      chart.data.datasets.forEach((dataset, datasetIndex) => {{
        const points = chart.getDatasetMeta(datasetIndex).data;
        if (!points || points.length === 0) {{
          return;
        }}
        let bestIndex = 0;
        for (let i = 0; i < points.length; i++) {{
          if (points[i].x <= e.x) {{
            bestIndex = i;
          }} else {{
            break;
          }}
        }}
        results.push({{ element: points[bestIndex], datasetIndex, index: bestIndex }});
      }});
      return results;
    }};

    let charts = {{}};

    function renderCharts(mode) {{
      const actual = mode === 'actual';
      for (const key in charts) {{
        charts[key].destroy();
      }}
      charts = {{}};

      for (const s of series) {{
        charts[s.id] = new Chart(document.getElementById(s.id), {{
          type: 'line',
          data: actual
            ? {{ datasets: [{{ label: s.label, data: timestamps.map((t, i) => ({{ x: t, y: s.data[i] }})), borderColor: s.color, fill: false, stepped: 'before' }}] }}
            : {{ labels: timestamps, datasets: [{{ label: s.label, data: s.data, borderColor: s.color, fill: false, stepped: 'before' }}] }},
          options: {{
            responsive: true,
            maintainAspectRatio: false,
            interaction: {{ mode: 'currentStep', intersect: false }},
            scales: {{
              x: actual
                ? {{ type: 'linear', title: {{ display: true, text: 'Time (s)' }} }}
                : {{ type: 'category', title: {{ display: true, text: 'Step' }} }}
            }},
            plugins: {{ tooltip: tooltipTasksPlugin }}
          }}
        }});
      }}
    }}

    renderCharts('regular');

    document.querySelectorAll('input[name="timescale"]').forEach((el) => {{
      el.addEventListener('change', (e) => renderCharts(e.target.value));
    }});
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
    timestamp: dt.datetime | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if random_seed is not None:
        random.seed(random_seed)

    context = context or {}

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
        workflow_path_obj = Path(workflow_path).resolve()
        workflow = _load_cwl(workflow_path_obj)
        flat_steps, _exits = _flatten_steps(
            workflow.get("steps", {}), workflow.get("outputs", {}), workflow_path_obj.parent, "", {}
        )
        for step_id, step_data in flat_steps.items():
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
            req = _resource_requirement(step_data, context)
            scatter_count = _scatter_count(step_data)
            duration_override = _duration_override(step_data, context)
            host_id = _host_assignment(step_id, context)

            if host_id is None and duration_override is None:
                raise ValueError(
                    f"Step {step_id!r} has no host (add it to --context's \"hosts\" map) and no "
                    "explicit duration (durationSeconds/durationFormula); its duration can't be "
                    "computed without a specific host's resource profile, and host allocation is "
                    "never inferred."
                )

            shard_records = []
            for index in range(scatter_count):
                host, start, end = _pick_host(
                    hosts, host_available, dep_ready_at, req, storage, duration_override, host_id
                )
                if host is not None:
                    host_available[host["id"]] = end

                activity_id = step_id if scatter_count == 1 else f"{step_id}-scatter-{index + 1}"
                record = {
                    "id": activity_id,
                    "base_id": step_id,
                    "name": step_data.get("label") or step_data.get("name") or step_id,
                    "start_time_seconds": round(start, 4),
                    "end_time_seconds": round(end, 4),
                    "duration_seconds": round(end - start, 4),
                    "host_id": host["id"] if host is not None else None,
                    "resources_used": {
                        "cpu_cores": req["cpu_cores"],
                        "ram_gb": req["ram_gb"],
                        "disk_gb": req["disk_gb"],
                        "network_mbps": req["network_mbps"],
                    },
                    "status": "completed",
                    "failure_mode": "none",
                }
                if scatter_count > 1:
                    record["scatter_index"] = index + 1
                shard_records.append(record)
                activities.append(record)

                duration = max(end - start, 0.001)
                # Workers have no local disk: every byte moved is read from
                # or written to a network-attached disk bay, so "disk I/O"
                # and "network I/O" are the same physical transfer, bounded
                # by whichever is more restrictive -- the disk bay's own
                # throughput (storage) or this host's own network link.
                # Deriving both from the same implied throughput (actual
                # data moved / actual duration), rather than disk from that
                # and network from the step's declared networkMin (a static
                # config value unrelated to how much was actually moved or
                # how long it took), is what keeps them consistent instead
                # of reporting two unrelated numbers for one real event. With
                # no host assigned there's no host-side network link to cap
                # against, so only the shared storage device's throughput
                # (and what was actually moved) bound it.
                implied_io_mbps = req["input_data_size_gb"] * GB_TO_MB_DECIMAL * BITS_TO_BYTES_DIVISOR / duration
                io_caps = [float(storage["read_speed_mbps"]), implied_io_mbps]
                if host is not None:
                    io_caps.append(float(host["network_bandwidth_mbps"]))
                effective_io_mbps = round(min(io_caps), 2)
                utilization.append(
                    {
                        "activity_id": activity_id,
                        "activity_name": record["name"],
                        "host_id": host["id"] if host is not None else None,
                        "timestamp_seconds": round(start, 4),
                        "end_time_seconds": round(end, 4),
                        "cpu_cores_used": round(
                            req["cpu_cores"] if host is None else min(req["cpu_cores"], float(host["cpu_cores"])), 4
                        ),
                        "ram_gb_used": round(
                            req["ram_gb"] if host is None else min(req["ram_gb"], float(host["ram_gb"])), 4
                        ),
                        "disk_io_mbps": effective_io_mbps,
                        "network_io_mbps": effective_io_mbps,
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
    current_time = timestamp or dt.datetime.now(dt.timezone.utc)

    results = {
        "simulation_metadata": {
            "timestamp": current_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    parser.add_argument(
        "--context",
        default=None,
        help="Optional path to a JSON file of named numeric constants usable in durationFormula expressions",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    scenarios = None
    if args.failure_scenarios:
        scenarios = _load_json(args.failure_scenarios)
    context = _load_context(args.context) if args.context else None
    simulate(
        resources_json_path=args.resources_json_path,
        cwl_workflow_paths=args.cwl_workflows,
        failure_scenarios=scenarios,
        failure_probability=args.failure_probability,
        output_directory=args.output_directory,
        random_seed=args.random_seed,
        context=context,
    )


if __name__ == "__main__":
    main()
