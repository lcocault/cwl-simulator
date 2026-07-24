cwlVersion: v1.2
class: Workflow
inputs: {}
outputs:
  prepared:
    type: string
    outputSource: validate-samples/prepared
  reference_index:
    type: string
    outputSource: index-reference/indexed
steps:
  fetch-samples:
    run:
      cwlVersion: v1.2
      class: ExpressionTool
      requirements:
        InlineJavascriptRequirement: {}
      inputs: {}
      outputs:
        raw: string
      expression: |
        ${
          return {"raw": "raw-samples"};
        }
    in: {}
    out: [raw]
  validate-samples:
    run:
      cwlVersion: v1.2
      class: ExpressionTool
      requirements:
        InlineJavascriptRequirement: {}
      inputs:
        raw: string
      outputs:
        prepared: string
      expression: |
        ${
          return {"prepared": inputs.raw + "-validated"};
        }
    in:
      raw: fetch-samples/raw
    out: [prepared]
  index-reference:
    run:
      cwlVersion: v1.2
      class: ExpressionTool
      requirements:
        InlineJavascriptRequirement: {}
      inputs:
        raw: string
      outputs:
        indexed: string
      expression: |
        ${
          return {"indexed": inputs.raw + "-indexed"};
        }
    in:
      raw: fetch-samples/raw
    out: [indexed]
