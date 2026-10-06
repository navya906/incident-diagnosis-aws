# Contributing

## Before you start
- [ ] `git pull` and read `PROGRESS.md`, `DECISIONS.md`, the current phase in `BRIEF.md`
- [ ] venv active (`backend/.venv`), `pip install -e ".[dev]"` done
- [ ] `pytest -q` and `ruff check .` pass on a clean checkout

## While working
- Branch name: `phaseN/<topic>` (e.g. `phase1/scenario-generator`).
- Implement, then test, then fix, then document. No TODOs, pseudocode or empty stubs in core paths.
- Anything that cannot run here (real AWS, real LLM) gets a full interface plus a deterministic
  `LOCAL-ONLY` implementation, documented as such.
- Tune rules, prompts, weights and thresholds on the **dev** split only. The test split is touched only
  by the final experiment runner.
- Changing `app/contracts/` or `app/interfaces/`: add a `DECISIONS.md` row and update tests in the same PR.
- Changing DB models: add an Alembic migration (`alembic revision --autogenerate`) and review it.

## Before opening a PR
- [ ] `ruff check .` and `ruff format --check .` clean, `pytest -q` green
- [ ] `PROGRESS.md` updated (done / next / unverified)
- [ ] `DECISIONS.md` updated for non-obvious choices
- [ ] No secrets, `.env`, or generated data committed
- [ ] Synthetic or stub-LLM outputs are labelled `smoke-test / synthetic`

## Commit style
Short imperative subject, optional body explaining why. Reference the phase, e.g.
`Phase 1: add seeded scenario generator`.
