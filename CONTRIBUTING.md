# Contributing

## Setup

```bash
uv sync
```

## Checks

Run all three before you open a pull request:

```bash
uv run pytest -q
uv run ruff check
uv run pyright
```

CI runs the same three on Python 3.11, 3.12 and 3.13.

## Tests never spend tokens

The test suite must not call `claude`. Tests mock it, usually by patching `subprocess.run` or `cli.run_task`. See `tests/test_sprint4.py` for a fake `claude` that emits stream-json. A test that would spend tokens is a bug.

Live runs against the example suite are optional and manual. If you do one, say so in the pull request.

## Adding a check type

1. Add the type and its required fields to `CHECK_FIELDS` in `src/agent_evals/checks.py`.
2. Add the logic as a new branch in `run_check` in the same file. Return a `CheckResult`.
3. If it needs new validation, add it to `_check_errors` in `src/agent_evals/suite.py`.
4. Add tests in `tests/test_checks.py`: pass, fail, and the field validation.
5. Add a row to the check types table in `README.md`, and an entry to `CHANGELOG.md`.

## Pull requests

- Keep each change small and focused.
- Add or update tests for any behaviour change.
- Update the README and CHANGELOG when behaviour or flags change.
- Don't commit `runs/`.
