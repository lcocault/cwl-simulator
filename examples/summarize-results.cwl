cwlVersion: v1.2
class: Workflow
inputs:
  aligned: string
outputs:
  report:
    type: string
    outputSource: summarize/report
steps:
  summarize:
    run:
      cwlVersion: v1.2
      class: ExpressionTool
      requirements:
        InlineJavascriptRequirement: {}
      inputs:
        aligned: string
      outputs:
        report: string
      expression: |
        ${
          return {"report": "report-for-" + inputs.aligned};
        }
    in:
      aligned: aligned
    out: [report]
