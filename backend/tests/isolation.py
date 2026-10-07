"""Import-isolation check that does not depend on which tests ran first."""

import json
import subprocess
import sys

LLM_PREFIXES = ("app.ai", "openai", "google.generativeai")

_CODE = """
import importlib, json, sys
modules, prefixes = json.loads(sys.argv[1]), tuple(json.loads(sys.argv[2]))
for m in modules:
    importlib.import_module(m)
print(json.dumps(sorted(m for m in sys.modules if m.startswith(prefixes))))
"""


def imported_llm_modules(*modules: str) -> list[str]:
    """LLM-related modules pulled in by importing `modules` in a fresh interpreter."""
    out = subprocess.run(
        [sys.executable, "-c", _CODE, json.dumps(modules), json.dumps(LLM_PREFIXES)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)
