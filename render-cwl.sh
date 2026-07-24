#!/bin/bash
FULL=""
if [ "$1" = "-f" ] || [ "$1" = "--full" ]
then
    FULL="--full"
    shift
fi

if [ ! -f "$1" ]
then
    echo "Argument file $1 does not exist"
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/cwl_to_dot.py" $FULL "$1" > /tmp/workflow.dot
dot -Tpng /tmp/workflow.dot -o "$1.png"
rm /tmp/workflow.dot