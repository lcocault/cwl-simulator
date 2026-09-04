"""Render a CWL workflow's step graph as Graphviz dot, without invoking cwltool.

The example workflows in this repo use simulator-only fields (networkMin,
workload, inputDataSizeGb, scatter_count) and scatter a scalar output, which
real CWL type-checking rejects. simulator.py already parses these files with
plain YAML, so this reuses that same parsing to draw the step DAG.

With --depth N (N > 0), every step's "run" file reference is recursively
replaced with the full inline content of the referenced CWL document, down
to N levels deep (so a step like "stage-inputs" ends up with the content of
stage-inputs.cwl embedded under its "run" key instead of just the file name,
and --depth 2 would also expand stage-inputs's own steps' "run" references
one level further). That merged document is written to a temporary file,
which is what actually gets loaded and rendered. --depth 0 (the default)
renders every top-level step as a single node with no expansion at all.

Graphviz's "dot" has no per-subgraph rankdir -- one graph, one direction --
so a step whose "run" is an embedded sub-workflow is not drawn as a nested
cluster in the same graph. Instead it is rendered recursively as its own
left-to-right graph (its own further-nested sub-workflows the same way) and
embedded as an image node, so the top-level ("general") workflow always
reads top-to-bottom while every sub-workflow, at any depth, reads as its own
self-contained horizontal lane.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from simulator import _load_cwl


def _merge_run(run_value: Any, base_dir: Path, depth: int) -> Any:
    """Dereference a step's "run" into its full inline content, "depth" levels
    deep. At depth 0 run_value is left untouched -- a bare file-path string,
    or an already-inline dict -- so that step renders as an opaque leaf.
    """
    if depth <= 0:
        return run_value
    if isinstance(run_value, str):
        run_path = (base_dir / run_value).resolve()
        content = _load_cwl(run_path)
        if content.get("class") == "Workflow":
            return _merge_workflow(content, run_path.parent, depth - 1)
        return content
    if isinstance(run_value, dict):
        if run_value.get("class") == "Workflow":
            return _merge_workflow(run_value, base_dir, depth - 1)
        return run_value
    return run_value


def _merge_workflow(workflow: dict[str, Any], base_dir: Path, depth: int) -> dict[str, Any]:
    merged = dict(workflow)
    merged_steps = {}
    for step_id, step_data in workflow.get("steps", {}).items():
        new_step_data = dict(step_data)
        if "run" in step_data:
            new_step_data["run"] = _merge_run(step_data["run"], base_dir, depth)
        merged_steps[step_id] = new_step_data
    merged["steps"] = merged_steps
    return merged


def merge_full_workflow(workflow_path: str, depth: int) -> dict[str, Any]:
    """Replace every step's "run" file reference with the referenced CWL's full content, "depth" levels deep."""
    resolved_path = Path(workflow_path).resolve()
    workflow = _load_cwl(resolved_path)
    return _merge_workflow(workflow, resolved_path.parent, depth)


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


def _sub_workflow(step_data: dict[str, Any]) -> dict[str, Any] | None:
    run_value = step_data.get("run")
    if isinstance(run_value, dict) and run_value.get("class") == "Workflow":
        return run_value
    return None


def _dot_source(
    workflow: dict[str, Any],
    rankdir: str,
    image_nodes: dict[str, tuple[str, float, float]],
    max_columns: int | None = None,
) -> str:
    """Dot source for one workflow scope: every step is a single flat node --
    a plain leaf node, or (per image_nodes) an already-rendered sub-workflow
    image -- connected by edges derived from each step's "in" sources.

    A scope only ever reaches this function with rankdir="LR" when it is a
    leaf (no image_nodes -- see _render_sub_workflow_image/to_dot), i.e. a
    single row of plain nodes. If that row has more than max_columns steps,
    it is wrapped onto several stacked rows instead: rankdir switches to TB
    and steps are chunked, in declaration order, into rank=same groups of at
    most max_columns each, so every group still reads left-to-right but rows
    stack top-to-bottom. This is safe here specifically because a leaf scope
    has no clusters to conflict with -- forcing rank=same across a cluster
    boundary is what breaks (see git history for the rejected approach that
    used this trick to fake per-cluster rankdir at the top level).
    """
    steps = workflow.get("steps", {})
    step_ids = list(steps)
    lines: list[str] = []

    for step_id in step_ids:
        if step_id in image_nodes:
            png_path, width_in, height_in = image_nodes[step_id]
            lines.append(
                f'  "{step_id}" [shape=box, label="{step_id}", labelloc=t, '
                f'image="{png_path}", imagescale=true, fixedsize=true, '
                f"width={width_in + 0.4:.2f}, height={height_in + 0.5:.2f}];"
            )
        else:
            lines.append(f'  "{step_id}" [label="{step_id}"];')

    edges: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for step_id, step_data in steps.items():
        for source in _iter_sources(step_data):
            if "/" not in source:
                continue
            dep_step_id, _ = source.split("/", 1)
            if dep_step_id in steps and (dep_step_id, step_id) not in seen_edges:
                seen_edges.add((dep_step_id, step_id))
                edges.append((dep_step_id, step_id))
    lines.extend(f'  "{source}" -> "{target}";' for source, target in edges)

    if rankdir == "LR" and max_columns and max_columns > 0 and len(step_ids) > max_columns:
        rankdir = "TB"
        for start in range(0, len(step_ids), max_columns):
            row = step_ids[start : start + max_columns]
            if len(row) > 1:
                quoted = "; ".join(f'"{step_id}"' for step_id in row)
                lines.append(f"  {{ rank=same; {quoted}; }}")

    header = ["digraph workflow {", f"  rankdir={rankdir};"]
    return "\n".join(header + lines + ["}"])


def _render_sub_workflow_image(
    sub_workflow: dict[str, Any], tmp_dir: Path, counter: list[int], max_columns: int | None
) -> tuple[str, float, float]:
    """Recursively render a sub-workflow and return (absolute png path, width
    in inches, height in inches) for embedding as an image node in the
    parent scope.

    A scope that itself contains further-nested sub-workflow steps is
    rendered top-to-bottom, same as the top-level ("general") workflow --
    each of those nested steps becomes its own stacked row, recursing the
    same rule to any depth. A scope with no further nesting (a true leaf) is
    rendered left-to-right, since there is nothing beneath it to stack --
    unless it has more than max_columns steps, in which case _dot_source
    wraps it onto multiple rows instead (see its docstring).
    """
    image_nodes = {
        step_id: _render_sub_workflow_image(sub, tmp_dir, counter, max_columns)
        for step_id, step_data in sub_workflow.get("steps", {}).items()
        if (sub := _sub_workflow(step_data)) is not None
    }
    rankdir = "TB" if image_nodes else "LR"

    counter[0] += 1
    dot_path = tmp_dir / f"sub_{counter[0]}.dot"
    png_path = tmp_dir / f"sub_{counter[0]}.png"
    dot_source = _dot_source(sub_workflow, rankdir=rankdir, image_nodes=image_nodes, max_columns=max_columns)
    dot_path.write_text(dot_source, encoding="utf-8")

    plain = subprocess.run(["dot", "-Tplain", str(dot_path)], capture_output=True, text=True, check=True).stdout
    width_in = height_in = 1.0
    for line in plain.splitlines():
        if line.startswith("graph "):
            _, _, width_str, height_str = line.split()
            width_in, height_in = float(width_str), float(height_str)
            break

    subprocess.run(["dot", "-Tpng", str(dot_path), "-o", str(png_path)], check=True)
    return str(png_path), width_in, height_in


def to_dot(workflow: dict[str, Any], rankdir: str = "LR", max_columns: int | None = None) -> str:
    """Top-level entry point: renders "workflow" (already merged to whatever
    depth was requested -- see merge_full_workflow) the same way any nested
    scope would be (see _render_sub_workflow_image's docstring), so the
    top-level ("general") workflow reads top-to-bottom as soon as any of its
    steps got expanded, and falls back to "rankdir" -- a flat left-to-right
    graph by default -- only when nothing was expanded (--depth 0).

    max_columns caps how many steps appear in a single horizontal row of any
    leaf-level (left-to-right) scope, top-level included; longer rows wrap
    onto additional stacked rows instead (see _dot_source).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="cwl_to_dot_"))
    counter = [0]
    image_nodes = {
        step_id: _render_sub_workflow_image(sub, tmp_dir, counter, max_columns)
        for step_id, step_data in workflow.get("steps", {}).items()
        if (sub := _sub_workflow(step_data)) is not None
    }
    # The per-sub-workflow PNGs under tmp_dir are intentionally left on disk:
    # this function only emits dot source referencing them by path, and the
    # `dot -Tpng` pass that actually reads them runs as a separate process
    # after this script exits (see render-cwl.sh).
    if image_nodes:
        rankdir = "TB"
    return _dot_source(workflow, rankdir=rankdir, image_nodes=image_nodes, max_columns=max_columns)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", nargs="?", help="Path to the top-level CWL workflow file")
    parser.add_argument(
        "-d",
        "--depth",
        type=int,
        default=0,
        help=(
            "How many levels of sub-workflow \"run\" nesting to expand inline "
            "(default: 0, every top-level step is a single node). Pass a "
            "number at least as deep as the workflow nests -- e.g. 99 -- to "
            "expand it fully."
        ),
    )
    parser.add_argument(
        "-v",
        "--vertical",
        action="store_true",
        help="Lay out a flat graph (--depth 0) top-to-bottom instead of the default left-to-right; "
        "ignored once --depth expands any sub-workflow, since those always render top-to-bottom",
    )
    parser.add_argument(
        "-c",
        "--max-columns",
        type=int,
        default=None,
        help="Wrap a horizontal row of steps onto additional stacked rows once it exceeds this "
        "many steps (default: no limit, one unbroken row per left-to-right scope)",
    )
    args = parser.parse_args()
    if args.workflow is None:
        parser.print_help()
        return
    rankdir = "TB" if args.vertical else "LR"

    merged_workflow = merge_full_workflow(args.workflow, args.depth)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".full.cwl", delete=False, encoding="utf-8") as temp_file:
        yaml.safe_dump(merged_workflow, temp_file, sort_keys=False)
        temp_path = temp_file.name
    try:
        print(to_dot(_load_cwl(temp_path), rankdir=rankdir, max_columns=args.max_columns))
    finally:
        Path(temp_path).unlink()


if __name__ == "__main__":
    main()
