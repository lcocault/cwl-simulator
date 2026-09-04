#!/bin/bash
usage() {
    cat <<'EOF'
Usage: render-cwl.sh [OPTIONS] WORKFLOW.cwl

Render a CWL workflow's step graph as a PNG image (WORKFLOW.cwl.png), via
cwl_to_dot.py and graphviz's `dot`.

Options:
  -d, --depth N        How many levels of sub-workflow "run" nesting to
                        expand inline (default: 0, every top-level step is a
                        single node). Pass a number at least as deep as the
                        workflow nests -- e.g. 99 -- to expand it fully.
  -v, --vertical        Lay out a flat graph (depth 0) top-to-bottom instead
                        of the default left-to-right. Ignored once --depth
                        expands any sub-workflow, since those always render
                        top-to-bottom.
  -c, --max-columns N  Wrap a horizontal row of steps onto additional
                        stacked rows once it exceeds this many steps
                        (default: no limit, one unbroken row).
  -h, --help            Show this help and exit.

Examples:
  render-cwl.sh workflow.cwl
  render-cwl.sh --depth 99 workflow.cwl
  render-cwl.sh --depth 99 --max-columns 4 workflow.cwl
EOF
}

if [ $# -eq 0 ]
then
    usage
    exit 0
fi

DEPTH=""
VERTICAL=""
MAX_COLUMNS=""
while true
do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -d|--depth)
            DEPTH="--depth $2"
            shift 2
            ;;
        -v|--vertical)
            VERTICAL="--vertical"
            shift
            ;;
        -c|--max-columns)
            MAX_COLUMNS="--max-columns $2"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

if [ ! -f "$1" ]
then
    echo "Argument file $1 does not exist" >&2
    echo >&2
    usage >&2
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/cwl_to_dot.py" $DEPTH $VERTICAL $MAX_COLUMNS "$1" > /tmp/workflow.dot
dot -Tpng /tmp/workflow.dot -o "$1.png"
rm /tmp/workflow.dot
