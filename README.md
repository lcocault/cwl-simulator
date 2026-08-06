# cwl-simulator

Simulator of CWL workflows.

## Example scenarios

The repository includes runnable sample inputs in `examples/`:

- `resources.json`: two hosts and one shared SSD storage device
- `workflow.cwl`: a three-step parent workflow that references sub-workflows
- `stage-inputs.cwl`, `align-samples.cwl`, `summarize-results.cwl`: sub-workflow definitions used by `workflow.cwl`
- `failure_scenarios.json`: a recovery scenario triggered on the scattered step
- `context.json`: the `hosts` map pinning each of `workflow.cwl`'s steps to a host (see "Host
  assignment: explicit only" below) -- required, since none of those steps set an explicit
  duration

## Usage

```bash
python simulator.py /path/to/resources.json /path/to/workflow.cwl
```

This generates:
- `simulation_results.json`
- `simulation_report.html`

### Nominal execution example

```bash
python simulator.py \
  examples/resources.json \
  examples/workflow.cwl \
  --context examples/context.json \
  --output-directory /tmp/cwl-simulator-nominal \
  --random-seed 7
```

Use this example to see:

- dependency-aware scheduling
- scatter expansion into parallel activities
- JSON and HTML output generation

`--context examples/context.json` is required here: none of `workflow.cwl`'s steps set an explicit
duration, so each needs its host pinned via that file's `hosts` map (see "Host assignment:
explicit only" below).

### Failure and recovery example

```bash
python simulator.py \
  examples/resources.json \
  examples/workflow.cwl \
  --context examples/context.json \
  --failure-scenarios examples/failure_scenarios.json \
  --failure-probability 1.0 \
  --output-directory /tmp/cwl-simulator-failure \
  --random-seed 7
```

Use this example to see:

- simulated failures on a workflow activity
- automatic insertion of a recovery activity
- failure details in both `simulation_results.json` and `simulation_report.html`

### Named constants, explicit durations, and explicit input sizes

By default, a step's duration is computed from its `ResourceRequirement` (cores, RAM, workload,
input data size) combined with the host and storage specs in `resources.json`. Additional
`ResourceRequirement` fields let a step opt out of parts of that formula:

- `durationSeconds`: a fixed duration, in seconds, regardless of host/storage.
- `durationFormula`: a small arithmetic expression (`+ - * / ()`, plus `min`/`max`/`abs`/`round`)
  evaluated against a set of named constants supplied via `--context`.
- `inputDataSizeKb`: the step's input data size, in kB, as an alternative to `inputDataSizeGb`.
- `inputDataSizeFormula`: same expression mechanism as `durationFormula`, but the result is
  interpreted in kB and only replaces the input-data-size term (`durationFormula`, if also
  present, still overrides duration as a whole).
- `latencySeconds` / `latencyFormula`: a fixed overhead, in seconds, added on top of the step's
  duration regardless of how that duration was computed (default cores/RAM/workload formula,
  `durationSeconds`, or `durationFormula`) -- for a cost that isn't workload-dependent, e.g. job
  dispatch/startup overhead. `latencyFormula` uses the same context-expression mechanism as
  `durationFormula`/`inputDataSizeFormula` and takes precedence over `latencySeconds` if both are
  present. Defaults to 0 when neither is set.

```bash
python simulator.py \
  resources.json \
  workflow.cwl \
  --context context.yaml
```

`--context` points at a flat mapping of numeric constants, parsed as YAML (a superset of
JSON), so `#` comments are allowed regardless of whether the file is named `.yaml`/`.yml`
or `.json`:

```yaml
# Product size in kB, throughput in kB/s.
product_size_kb: 8000000
s3_read_throughput_kb_per_s: 750000
```

```yaml
steps:
  transfer-product:
    run: transfer-product.cwl
    in: {}
    out: [transferred]
    requirements:
      ResourceRequirement:
        durationFormula: "product_size_kb / s3_read_throughput_kb_per_s"

  decipher-product:
    run: decipher-product.cwl
    in: {}
    out: [deciphered]
    requirements:
      ResourceRequirement:
        coresMin: 8
        ramMin: 16384
        # inputDataSizeFormula only replaces the size term; duration is still
        # computed from cores/RAM/workload/size, as with inputDataSizeGb.
        inputDataSizeFormula: "product_size_kb"
        workload: 40
```

A step may still declare `coresMin`/`ramMin`/`disk`/`networkMin` alongside `durationSeconds` or
`durationFormula` — those are still reported in `resources_used`, only the timing calculation
itself is overridden; they no longer drive host selection, since host assignment is always
explicit (see "Host assignment: explicit only" below). `disk_io_mbps`/`network_io_mbps` in the
resource-utilization report are always derived
from actual data moved (`inputDataSize*`) over actual duration, capped by both the storage
device's and the host's speed — never from `networkMin` directly, since workers are assumed to
have no local disk: every byte moved is a network-attached-storage access, so disk and network
throughput are the same real transfer, not two independent figures. Precedence when multiple
input-size fields are
present on the same step: `inputDataSizeFormula` > `inputDataSizeKb` > `inputDataSizeGb`. Steps
with none of these fields keep using the original formula/`inputDataSizeGb`, so existing
workflows are unaffected. Referencing a name that isn't in the context (or wasn't passed via
`--context` at all) raises a `ValueError` rather than silently defaulting.

### Host assignment: explicit only

A step's host is never inferred from resource fit or which host happens to be free soonest.
It comes only from `--context`'s `hosts` map, keyed by (flattened) step id:

```yaml
hosts:
  decipher-l0-product: dr-processing-a
  denoise-deconvolution-correlation: dr-processing-b
```

- If a step's id is in `hosts`, it runs on that host: queued behind whatever else is already
  scheduled there, and (for the default cores/RAM/workload/network duration formula) timed using
  that host's specs. Referencing a `host_id` that isn't in `resources.json` raises a `ValueError`.
- If a step's id is **not** in `hosts`, its `host_id` is `null` in `simulation_results.json` (shown
  as "(unassigned)" in the HTML report/Gantt chart), and it isn't queued behind any host's
  availability -- it starts as soon as its dependencies are done. This is only valid when the step
  also has an explicit, host-independent duration (`durationSeconds` or `durationFormula`): the
  default cores/RAM/workload/network formula needs a specific host's specs to be computable at
  all, so a step with neither a `hosts` entry nor an explicit duration raises a `ValueError`.
- A scattered step's `hosts` entry (keyed by the step id, not per-shard) applies to every shard
  alike, so all shards queue behind each other on that one host. With no `hosts` entry (and an
  explicit duration), shards have nothing to queue behind and can all start together.
- `disk_io_mbps`/`network_io_mbps` for an unassigned activity are capped only by the storage
  device's throughput and what was actually moved, not by any host's network link, since there
  is no host to draw that cap from.

### Sub-workflows: automatic vs. explicit cost

A step's `run` can point at another CWL `Workflow` (a sub-workflow), not just a single tool. If
that step defines none of `workload`, `durationSeconds`, `durationFormula`, `inputDataSizeGb`,
`inputDataSizeKb`, `inputDataSizeFormula`, `latencySeconds`, or `latencyFormula` itself, it is
*inlined*: its inner steps are
scheduled directly (recursively, to any depth), and the container step never becomes an activity
of its own — its "duration" is simply whatever its inner steps' schedule works out to. Inner step
ids are prefixed by the container step's id (e.g. `pf/l0-product-transfer-dmz-to-dr`), and
cross-boundary references (a sub-workflow step consuming the container's own declared input, or a
sibling step consuming one of the container's declared outputs) are rewired automatically.

If a step *does* define any of those fields, it stays atomic exactly as before — its sub-workflow,
if any, is not scheduled at all, and the step's own explicit cost is used unchanged. This is what
keeps `examples/workflow.cwl` (whose steps all set `workload`/`inputDataSizeGb`) behaving exactly
as it always has, even though its steps' `run` targets are themselves `Workflow`s. Use this to
force a step atomic (e.g. to model it as a coarse-grained cost estimate) even when it has a real
sub-workflow underneath — set any one of those fields, even to a value you'll refine later.
Scattered steps are never inlined, regardless of their `requirements`.

## Python API

```python
from simulator import simulate

simulate(
    resources_json_path="resources.json",
    cwl_workflow_paths=["workflow.cwl"],
    failure_scenarios=[
        {
            "trigger_activity_id": "process-1",
            "failure_mode": "ram_overload",
            "recovery_activity_id": "process-1-recovery"
        }
    ],
    output_directory="example-output",
    random_seed=7,
    context={
        "product_size_gb": 8,
        "s3_read_throughput_gbps": 0.75,
        "hosts": {"process-1": "worker-1"},
    },
)
```
