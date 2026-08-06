"""Render a CWL workflow's step graph as Graphviz dot, without invoking cwltool.

The example workflows in this repo use simulator-only fields (networkMin,
workload, inputDataSizeGb, scatter_count) and scatter a scalar output, which
real CWL type-checking rejects. simulator.py already parses these files with
plain YAML, so this reuses that same parsing to draw the step DAG.

With --full, every step's "run" file reference is recursively replaced with
the full inline content of the referenced CWL document (so a step like
"stage-inputs" ends up with the entire content of stage-inputs.cwl embedded
under its "run" key instead of just the file name). That merged document is
written to a temporary file, which is what actually gets loaded and rendered
-- steps whose "run" is an embedded sub-workflow are expanded into a nested
cluster in the resulting image.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any

import yaml

from simulator import _load_cwl


def _merge_run(run_value: Any, base_dir: Path) -> Any:
    """Recursively dereference a step's "run" into its full inline content."""
    if isinstance(run_value, str):
        run_path = (base_dir / run_value).resolve()
        content = _load_cwl(run_path)
        if content.get("class") == "Workflow":
            return _merge_workflow(content, run_path.parent)
        return content
    if isinstance(run_value, dict):
        if run_value.get("class") == "Workflow":
            return _merge_workflow(run_value, base_dir)
        return run_value
    return run_value


def _merge_workflow(workflow: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    merged = dict(workflow)
    merged_steps = {}
    for step_id, step_data in workflow.get("steps", {}).items():
        new_step_data = dict(step_data)
        if "run" in step_data:
            new_step_data["run"] = _merge_run(step_data["run"], base_dir)
        merged_steps[step_id] = new_step_data
    merged["steps"] = merged_steps
    return merged


def merge_full_workflow(workflow_path: str) -> dict[str, Any]:
    """Replace every step's "run" file reference with the referenced CWL's full content, recursively."""
    resolved_path = Path(workflow_path).resolve()
    workflow = _load_cwl(resolved_path)
    return _merge_workflow(workflow, resolved_path.parent)


def _out_names(step_data: dict[str, Any]) -> list[str]:
    out = step_data.get("out", [])
    if isinstance(out, dict):
        return list(out.keys())
    if isinstance(out, list):
        return list(out)
    return []


def _iter_sources(step_data: dict[str, Any]) -> list[str]:
    """Raw "stepid/outputname" (or bare workflow-input name) strings from a step's "in"."""
    sources: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            sources.append(value)
        elif isinstance(value, list):
            for item in value:
                add(item)

    inputs = step_data.get("in", {})
    if isinstance(inputs, dict):
        for value in inputs.values():
            if isinstance(value, dict):
                add(value.get("source"))
            else:
                add(value)
    elif isinstance(inputs, list):
        for value in inputs:
            if isinstance(value, dict):
                add(value.get("source"))

    return sources


def _build_graph(
    workflow: dict[str, Any], prefix: str
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Returns (dot lines, entry node ids, exits-by-output-name) for this workflow scope.

    Exits are tracked per output name (not flattened) so that a downstream
    reference like "stage-inputs/prepared" only routes through whichever
    inner step actually produces "prepared", not every output the
    sub-workflow happens to expose.

    A step is only expanded into a nested cluster when its "run" is already an
    embedded dict with class Workflow (i.e. after merge_full_workflow ran) --
    a bare file-path string is always drawn as a single leaf node.
    """
    steps = workflow.get("steps", {})
    lines: list[str] = []
    entries: dict[str, list[str]] = {}
    exits: dict[str, dict[str, list[str]]] = {}

    for step_id, step_data in steps.items():
        node_id = f"{prefix}{step_id}"
        run_value = step_data.get("run")
        sub_workflow = run_value if isinstance(run_value, dict) and run_value.get("class") == "Workflow" else None

        if sub_workflow is not None:
            sub_lines, sub_entry_ids, sub_exits_by_output = _build_graph(sub_workflow, f"{node_id}/")
            cluster_name = node_id.replace("/", "_")
            lines.append(f'  subgraph "cluster_{cluster_name}" {{')
            lines.append(f'    label="{step_id}";')
            lines.extend(f"  {line}" for line in sub_lines)
            lines.append("  }")
            entries[step_id] = sub_entry_ids
            exits[step_id] = sub_exits_by_output
        else:
            lines.append(f'  "{node_id}" [label="{step_id}"];')
            entries[step_id] = [node_id]
            exits[step_id] = {name: [node_id] for name in _out_names(step_data)}

    has_internal_dep: set[str] = set()
    for step_id, step_data in steps.items():
        for source in _iter_sources(step_data):
            if "/" not in source:
                continue
            dep_step_id, dep_output_name = source.split("/", 1)
            if dep_step_id not in steps:
                continue
            has_internal_dep.add(step_id)
            for source_node in exits[dep_step_id].get(dep_output_name, []):
                for target_node in entries[step_id]:
                    lines.append(f'  "{source_node}" -> "{target_node}";')

    entry_ids = [
        node_id
        for step_id in steps
        if step_id not in has_internal_dep
        for node_id in entries[step_id]
    ]

    exits_by_output: dict[str, list[str]] = {}
    outputs = workflow.get("outputs", {})
    if isinstance(outputs, dict):
        for out_name, out_def in outputs.items():
            source = out_def.get("outputSource") if isinstance(out_def, dict) else None
            if isinstance(source, str) and "/" in source:
                dep_step_id, dep_output_name = source.split("/", 1)
                if dep_step_id in exits:
                    exits_by_output[out_name] = exits[dep_step_id].get(dep_output_name, [])

    if not exits_by_output:
        depended_on = {
            source.split("/", 1)[0]
            for step_data in steps.values()
            for source in _iter_sources(step_data)
            if "/" in source and source.split("/", 1)[0] in steps
        }
        for step_id in steps.keys() - depended_on:
            exits_by_output[step_id] = [node for nodes in exits[step_id].values() for node in nodes]

    return lines, entry_ids, exits_by_output


def to_dot(workflow: dict[str, Any], full: bool, rankdir: str = "LR") -> str:
    lines, _, _ = _build_graph(workflow, "")
    header = ["digraph workflow {", f"  rankdir={rankdir};"]
    if full:
        header.append("  compound=true;")
    return "\n".join(header + lines + ["}"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", help="Path to the top-level CWL workflow file")
    parser.add_argument(
        "-f",
        "--full",
        action="store_true",
        help="Merge sub-workflows into a temporary full workflow before rendering",
    )
    parser.add_argument(
        "-v",
        "--vertical",
        action="store_true",
        help="Lay out the graph top-to-bottom instead of the default left-to-right",
    )
    args = parser.parse_args()
    rankdir = "TB" if args.vertical else "LR"

    if not args.full:
        print(to_dot(_load_cwl(args.workflow), full=False, rankdir=rankdir))
        return

    full_workflow = merge_full_workflow(args.workflow)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".full.cwl", delete=False, encoding="utf-8") as temp_file:
        yaml.safe_dump(full_workflow, temp_file)
        temp_path = temp_file.name
    try:
        print(to_dot(_load_cwl(temp_path), full=True, rankdir=rankdir))
    finally:
        Path(temp_path).unlink()


if __name__ == "__main__":
    main()
