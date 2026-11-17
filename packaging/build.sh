#!/usr/bin/env bash
set -euo pipefail
python3 -m compileall -q fm1zero
python3 -m unittest discover -s tests -t .
