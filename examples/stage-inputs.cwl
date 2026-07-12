cwlVersion: v1.2
class: Workflow
inputs: {}
outputs:
  prepared:
    type: string
    outputSource: prepare/prepared
steps:
  prepare:
    run:
      cwlVersion: v1.2
      class: ExpressionTool
      requirements:
        InlineJavascriptRequirement: {}
      inputs: []
      outputs:
        prepared: string
      expression: |
        ${
          return {"prepared": "prepared-samples"};
        }
    in: {}
    out: [prepared]
