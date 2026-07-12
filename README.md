# cwl-simulator

Simulator of CWL workflows.

## Usage

```bash
python simulator.py /path/to/resources.json /path/to/workflow.cwl
```

This generates:
- `simulation_results.json`
- `simulation_report.html`

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
    ]
)
```
