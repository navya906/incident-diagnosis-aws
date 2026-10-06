#!/usr/bin/env bash
# Creates backend/.venv and installs dev dependencies. Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")/../backend"

if [ ! -d .venv ]; then
  if command -v python3.11 >/dev/null; then python3.11 -m venv .venv; else python3 -m venv .venv; fi
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
echo "Done. Activate with: source backend/.venv/bin/activate"
