# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - Unreleased

First public release, under the MIT license.

### Added

- `examples/`: a self-contained fictional agent (`tiny-writer`) and a three-task suite, so a fresh clone can run without any private files.
- `doctor` command: checks the `claude` CLI, its version and the flags the harness uses, and shows the billing mode. Zero tokens.
- `validate` command: checks suite files and that each agent `path` exists. Zero tokens. Suites are also validated up front by `run`, so a typo fails before any tokens are spent.
- `--billing subscription|api`. Subscription is the default and strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from child processes.
- `--runs-dir` and `--baselines-dir` for `run`, and `--baselines-dir` for `rescore`.
- `--repeat N` (and suite key `repeat`) with pass-rate gating: `min_pass_rate`, `regression_pass_drop`, judge mean and stdev across samples, per-sample results and traces.
- Baseline metadata (`model`, `judge_model`, `claude_version`, `task_hash`, plus `pass_rate`/`n`/`stdev` for repeats) and a `WARN baseline mismatch` message and report section when a later run differs.
- Check types `no_file_deletes` and `clean_env`, and the built-in `clean-env` check, which reads the environment from Claude Code's init event (plugins, MCP servers, API key source).
- Run snapshots: the workspace file hashes are saved when a run starts, so `rescore` finds changed and deleted files even after the agent folder changes or the run dir moves.
- `rescore --suite` to score a saved run against a given suite file.
- ruff and pyright configuration, and a CI test workflow (ruff, pyright and pytest on Python 3.11 to 3.13).
- `LICENSE` (MIT), `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, and package metadata in `pyproject.toml`.

### Changed

- Gate correctness: a crashed run counts as a regression when the baseline passed. A judge failure, or a wrong number of judge scores, leaves the task incomplete instead of passing it. Incomplete and crashed tasks are never written to baselines, and a stored judge mean is not lost when a run has no judge.
- One broken task no longer loses the rest of the run.
- Run directories get a random suffix, so two runs in the same second don't collide.
- Isolation: agents run with `--setting-sources project`, so user settings, hooks and plugins stay out. File-tool access outside the workspace is detected and fails the built-in `paths-confined` check. Secret files are left out of the workspace copy. The judge wraps the agent output in a random delimiter, and the canary check's default allow-context is tighter.
- Workspaces are created in a temp dir outside any repo, then moved into the run dir. The harness removes the run's Claude Code auto-memory dir if the run created it and it is empty.
- Suite `path` resolves relative to the suite file, so suites run from any CWD.
- The `tiny-writer` example's hero-copy task is tighter: the agent rule requires proof points to be used exactly as written, and the rubric states the house tone directly.
- The README is rewritten for a public audience.

### Fixed

- `rescore` recomputes `regressions` against the current baseline, stores them in `results.json` and `report.md`, and prints them.
- `rescore` of a repeated run prints `INCOMPLETE` per task, matching the single-sample path.

## [0.1.0]

Initial internal version.
