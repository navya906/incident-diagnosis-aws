"""Report ids for the component evaluation reports in docs/experiments (D110).

The condition matrix has experiment ids (`app.experiments`, D82). The component reports
(anomaly detectors, evidence ranking, retrieval, the diagnosis smoke run, the verifier audit)
get a content-addressed id instead:

    report_id = "rpt-" + first 12 hex of sha256(canonical JSON of the report's .json payload)

The payload already records the dataset version and content hash, the configuration and the
command, so the id names exactly those inputs and results. Every evaluation CLI is
deterministic: re-running the command printed in the report must give the same id.

    python -m app.evaluation.provenance check          # ids in docs/experiments match their files
    python -m app.evaluation.provenance list
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_DIR = REPO / "docs" / "experiments"
PREFIX = "rpt-"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def report_id(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "report_id"}
    # Round-trip through JSON so the id is computed on what is written (str() for dates, etc.).
    body = json.loads(_canonical(body))
    return PREFIX + hashlib.sha256(_canonical(body).encode()).hexdigest()[:12]


def stamp(payload: dict) -> dict:
    """The payload with its `report_id` as the first key."""
    body = {k: v for k, v in payload.items() if k != "report_id"}
    return {"report_id": report_id(body), **body}


def with_report_id(md: list[str], rid: str) -> list[str]:
    """Insert the id line after the report's `- Command:` line (or after the title)."""
    line = (
        f"- Report id: `{rid}` (content hash of the .json results; "
        "`python -m app.evaluation.provenance check`)"
    )
    out = [x for x in md if not x.startswith("- Report id:")]
    at = next((i + 1 for i, x in enumerate(out) if x.startswith("- Command:")), 1)
    return out[:at] + [line] + out[at:]


def check(directory: Path = DEFAULT_DIR) -> list[str]:
    """Problems with the report ids in `directory` (empty list = all consistent)."""
    problems = []
    for path in sorted(Path(directory).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "meta" not in payload:
            continue  # not a component report
        rid = payload.get("report_id")
        if not rid:
            problems.append(f"{path.name}: no report_id")
            continue
        if rid != report_id(payload):
            problems.append(f"{path.name}: report_id {rid} does not match its content")
        md = path.with_suffix(".md")
        if not md.is_file() or rid not in md.read_text(encoding="utf-8"):
            problems.append(f"{md.name}: does not state {rid}")
    return problems


def reports(directory: Path = DEFAULT_DIR) -> dict[str, str]:
    """report_id -> file name for every stamped report in `directory`."""
    out = {}
    for path in sorted(Path(directory).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("report_id"):
            out[payload["report_id"]] = path.name
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check or list component report ids.")
    p.add_argument("cmd", choices=["check", "list"])
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = p.parse_args(argv)
    if args.cmd == "list":
        for rid, name in reports(args.dir).items():
            print(f"{rid}  {name}")
        return 0
    problems = check(args.dir)
    for line in problems:
        print(line)
    print("report ids consistent" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
