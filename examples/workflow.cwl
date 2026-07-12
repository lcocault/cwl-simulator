cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  stage-inputs:
    run: stage-inputs.cwl
    in: {}
    out: [prepared]
    requirements:
      ResourceRequirement:
        coresMin: 2
        ramMin: 2048
        tmpdirMin: 2048
        outdirMin: 1024
        networkMin: 250
        workload: 10
        inputDataSizeGb: 4
  align-samples:
    run: align-samples.cwl
    in:
      prepared:
        source: stage-inputs/prepared
    out: [aligned]
    scatter: prepared
    scatter_count: 3
    requirements:
      ResourceRequirement:
        coresMin: 4
        ramMin: 4096
        tmpdirMin: 2048
        outdirMin: 2048
        networkMin: 500
        workload: 18
        inputDataSizeGb: 8
  summarize-results:
    run: summarize-results.cwl
    in:
      aligned:
        source: align-samples/aligned
    out: [report]
    requirements:
      ResourceRequirement:
        coresMin: 1
        ramMin: 1024
        tmpdirMin: 512
        outdirMin: 512
        networkMin: 100
        workload: 6
        inputDataSizeGb: 1
