# cwl-simulator

Simulator of CWL workflows.

## Example scenarios

The repository includes runnable sample inputs in `examples/`:

- `resources.json`: two hosts and one shared SSD storage device
- `workflow.cwl`: a three-step workflow that includes a scatter stage
- `failure_scenarios.json`: a recovery scenario triggered on the scattered step

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
  --output-directory /tmp/cwl-simulator-nominal \
  --random-seed 7
```

Use this example to see:

- dependency-aware scheduling
- scatter expansion into parallel activities
- JSON and HTML output generation

### Failure and recovery example

```bash
python simulator.py \
  examples/resources.json \
  examples/workflow.cwl \
  --failure-scenarios examples/failure_scenarios.json \
  --failure-probability 1.0 \
  --output-directory /tmp/cwl-simulator-failure \
  --random-seed 7
```

Use this example to see:

- simulated failures on a workflow activity
- automatic insertion of a recovery activity
- failure details in both `simulation_results.json` and `simulation_report.html`

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
)
```
