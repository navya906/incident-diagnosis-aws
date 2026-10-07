"""CLI for experiments.

python -m app.experiments run --config ../experiments/smoke-dev.yaml
python -m app.experiments run --config ../experiments/real-test-openai.yaml --split test --final
python -m app.experiments plan --config ../experiments/real-test-openai.yaml
python -m app.experiments verify exp-0123456789ab
python -m app.experiments list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.experiments.config import ExperimentConfig
from app.experiments.runner import (
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    DEFAULT_RUNS,
    ExperimentRunner,
    SplitGuardError,
    plan,
    save_to_db,
    verify,
)
from app.offline.dataset import DEFAULT_SEED, DatasetLoader, generate_dataset


def _ensure_dataset(path: Path) -> None:
    if not (path / "manifest.json").is_file():
        print(f"dataset not found at {path}; generating it (seed {DEFAULT_SEED})")
        generate_dataset(path, seed=DEFAULT_SEED)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.experiments")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run an experiment config")
    r.add_argument("--config", type=Path, required=True)
    r.add_argument("--split", choices=["dev", "test"], help="overrides the config's split")
    r.add_argument("--final", action="store_true", help="required to run on the test split")
    r.add_argument("--limit", type=int, help="first N incidents (smoke checks)")
    r.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    r.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    r.add_argument("--out", type=Path, default=DEFAULT_RUNS)
    r.add_argument("--db", action="store_true", help="also store in the configured database")
    v = sub.add_parser("verify", help="re-run an experiment by id and compare results")
    v.add_argument("experiment_id")
    v.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    v.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    v.add_argument("--out", type=Path, default=DEFAULT_RUNS)
    pl = sub.add_parser("plan", help="count LLM calls and estimate cost before running")
    pl.add_argument("--config", type=Path, required=True)
    pl.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    pl.add_argument("--incidents", type=int, help="default: size of the config's split")
    pl.add_argument("--input-tokens", type=int, default=6500, help="per call (pilot average)")
    pl.add_argument("--output-tokens", type=int, default=1300, help="per call (pilot average)")
    ls = sub.add_parser("list", help="list stored experiments")
    ls.add_argument("--out", type=Path, default=DEFAULT_RUNS)
    args = p.parse_args(argv)

    if args.cmd == "list":
        for m in sorted(args.out.glob("*/manifest.json")):
            d = json.loads(m.read_text(encoding="utf-8"))
            print(
                f"{d['experiment_id']}  {d['name']:28s} {d['split']:4s} {d['model']['model']:28s}"
                f" {d['records']:6d} records  {d['started_at'][:19]}  [{d['label']}]"
            )
        return 0
    if args.cmd == "plan":
        config = ExperimentConfig.load(args.config)
        n = args.incidents
        if n is None:
            _ensure_dataset(args.dataset)
            n = len(DatasetLoader(args.dataset).incident_ids(config.split)[: config.limit])
        llm = config.settings.get("llm", {})
        result = plan(
            config,
            n,
            args.input_tokens,
            args.output_tokens,
            float(llm.get("input_cost_per_1k", 0.0)),
            float(llm.get("output_cost_per_1k", 0.0)),
        )
        for row in result["per_condition"]:
            print(
                f"  {row['condition']:24s} {row['family']:9s} runs={row['runs']} "
                f"calls/incident={row['calls_per_incident']}"
            )
        print(
            f"{config.name}: {n} incidents, {result['calls']} LLM calls, "
            f"~{result['input_tokens'] / 1e6:.1f}M input + {result['output_tokens'] / 1e6:.1f}M "
            f"output tokens, ~${result['cost_usd']} at the config's prices (repairs excluded)"
        )
        return 0
    _ensure_dataset(args.dataset)
    if args.cmd == "verify":
        report = verify(args.experiment_id, args.out, args.dataset, args.corpus)
        print(json.dumps(report, indent=2))
        return 0 if report["reproduced"] else 1

    config = ExperimentConfig.load(args.config)
    updates = {}
    if args.split:
        updates["split"] = args.split
    if args.limit:
        updates["limit"] = args.limit
    if updates:
        config = config.model_copy(update=updates)
    runner = ExperimentRunner(config, args.dataset, args.corpus, args.out)
    try:
        run = runner.run(final=args.final)
    except SplitGuardError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.db:
        from app.db.session import make_engine, make_session_factory

        with make_session_factory(make_engine())() as session:
            save_to_db(run, session)
    print(
        f"[{run.manifest['label']}] {run.experiment_id}: {run.manifest['records']} diagnoses, "
        f"{len(run.summary)} conditions -> {run.directory}"
    )
    for row in run.summary:
        print(
            f"  {row['condition']:24s} acc={row['label_correct']} rc={row['root_cause_correct']}"
            f" evidR={row['cited_recall']} halluc={row['hallucination_rate']} "
            f"rejected={row['rejected']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
