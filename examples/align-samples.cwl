cwlVersion: v1.2
class: Workflow
inputs:
  prepared: string
outputs:
  aligned:
    type: string
    outputSource: align/aligned
steps:
  align:
    run:
      cwlVersion: v1.2
      class: ExpressionTool
      requirements:
        InlineJavascriptRequirement: {}
      inputs:
        prepared: string
      outputs:
        aligned: string
      expression: |
        ${
          return {"aligned": inputs.prepared + "-aligned"};
        }
    in:
      prepared: prepared
    out: [aligned]
