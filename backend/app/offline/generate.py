"""CLI: python -m app.offline.generate [--seed 42] [--out ../data/generated/synthetic-v1]"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.offline.dataset import DEFAULT_SEED, DEFAULT_VERSION, generate_dataset

DEFAULT_OUT = Path(__file__).resolve().parents[3] / "data" / "generated" / DEFAULT_VERSION


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the synthetic incident dataset.")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--version", default=DEFAULT_VERSION)
    args = p.parse_args(argv)
    m = generate_dataset(args.out, seed=args.seed, dataset_version=args.version)
    print(f"[smoke-test / synthetic] wrote {m.counts['total']} incidents to {args.out}")
    print(f"counts: {m.counts}")
    print(f"content_sha256: {m.content_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
