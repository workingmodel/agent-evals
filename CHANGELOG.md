# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] - 2026-10-22

Prompt-injection testing, run telemetry and history, and a weekly before/after report.

### Added

- Per-task telemetry in `results.json`: `latency` (`wall_s`, `api_s`, `ttft_s`), `telemetry` (thinking tokens, 1h and 5m cache writes, web search and fetch requests, permission denials), `model_cost`, and the cost split `agent_cost_usd` / `judge_cost_usd` (`cost_usd` stays the combined total). Fields an older Claude Code doesn't report are `null`, never 0. Repeated tasks sum counters and costs and report mean/min/max latency. `rescore` re-derives them from the saved traces.
- `agent_fingerprint` per run: a short sha256 of the agent's `CLAUDE.md` / `CLAUDE.local.md` files and `.claude/` tree. The suite key `fingerprint: [glob, ...]` overrides the file set.
- `<runs-dir>/history.jsonl`: one line per task per run, appended by `run` and replaced by `rescore`. Never fails a run.
- `agent-evals history rebuild` regenerates the history from saved runs (zero tokens, idempotent). `agent-evals history show` prints a compact table.
- Report: `API` and `TTFT` columns, and the header splits cost into agent and judge.
- Prompt-injection testing. Task key `files:` seeds files into the workspace before the starting snapshot (inline text or `{from: path}`), validated up front and included in the task fingerprint. Task keys `category` and `vector`; `category: injection` requires every sample to pass (`min_pass_rate` 1.0 unless the task sets one), adds an "Injection resistance" section to the report, prefixes regressions with `[injection]`, and adds `category` and `vector` to history rows.
- Checks `tool_not_used` (attempted tool calls, denied ones included, optional input regex), `file_unchanged` (pre-existing files modified or deleted) and `flags_injection` (the user was told about an embedded instruction). Results record `files_modified`; `rescore` re-derives it and the tool calls from the saved run.
- `examples/suites/injection.yaml`: a 12-vector public pack against the example agent, and a "Testing prompt injection" README section.
- `agent-evals weekly`: a zero-token before/after report from `history.jsonl`, per agent and per task, written as markdown and JSON under `<runs-dir>/weekly/`. Pass rate and judge mean are sample-weighted; tokens, cost and turns are per sample; latency is p50/p95 over rows. Metrics past a threshold get ⚠️ or ✅ (`--cost-threshold`, `--latency-threshold`, `--judge-drop`), latency is labelled "(few runs)" under 3 rows, and the report warns when model, judge model, Claude Code version or billing differ between the weeks.
- `weekly` splits results by agent fingerprint when the agent's instructions changed, and `weekly --agent X --by-version` compares the latest fingerprint with the one before it, regardless of weeks.
- Task key `min_pass_rate` (per-task override of the suite value).

### Changed

- The runs dir now holds `history.jsonl` next to the run dirs.

## [0.2.3] - 2026-10-21

### Fixed

- `paths-confined` no longer flags paths inside the run's own temp dir. That dir holds only the workspace copy, so a path in it reaches nothing. Agents sometimes mistype the long temp path, for example by dropping the agent-folder segment, which made the check fail runs where nothing left the workspace. `rescore` applies the same rule, using the temp dir recorded in the run's snapshot. Paths outside that dir, including sibling temp dirs, are still flagged.

## [0.2.2] - 2026-10-21

### Changed

- The PyPI package is named `wm-agent-evals`. PyPI rejected `agent-evals` as too similar to an existing project. The command (`agent-evals`), the import (`agent_evals`) and everything else are unchanged. 0.2.1 was never published to PyPI; this is the first PyPI release.

## [0.2.1] - 2026-10-21

Hardening after the first public release.

### Added

- `agent-evals --version`, and `agent_evals.__version__`, both read from the installed package metadata.
- An Install section in the README (`pipx`, `uv tool`, `uvx`, `pip`). The example agent and suite stay in the GitHub repo, not the wheel.
- `run --gate` with no baseline file exits 2 before running anything, so no tokens are spent. With `--update-baseline` it runs and creates the file. With a baseline that has no entry for any task being run, it prints a warning.
- A stderr note with the number of agent and judge runs when `--repeat` is above 5.
- The workspace copy now leaves out `.npmrc`, `.pypirc`, `.netrc`, `.aws`, `credentials.json`, `secrets.*`, `*.p12`, `*.pfx`, `service-account*.json` and `runs`.

### Changed

- `validate` (and `run`, up front) now type- and range-check every suite key, at suite level and for task overrides: `pass_mean`, `regression_drop`, `regression_pass_drop`, `min_pass_rate`, `max_turns`, `timeout_s`, `repeat`, `copy_block`, `vars`, `model`, `judge_model`, `agent`, `path`, `allowed_tools` and `disallowed_tools`. Before, a string `pass_mean` or `regression_drop` passed `validate` and crashed after tokens were spent.
- `run`, `validate` and `rescore` report load errors (file not found, bad YAML, a top level that isn't a mapping, `tasks` not a list, `vars` not a mapping, a missing run dir or `results.json`, an unreadable baseline) as `error: <message>` on stderr and exit 2, with no traceback. `run` used to exit 1 with a traceback.
- A repeated task with any incomplete sample is now incomplete and not passed, whatever the pass rate. Before, it could show as passed and incomplete at once. The run output, the report and `rescore` agree.
- `--update-baseline` with `--no-judge` carries a stored judge mean forward only when the task is unchanged (same `task_hash`, or an old entry without one). If the task changed and has a rubric, the entry is kept as it was and a warning is printed.
- `task_hash` now also covers the suite's `copy_block` (when a check uses `scope: copy_block`) and `pass_mean` (when the task has a rubric and it isn't the default 3.5). Existing baselines for such tasks show a `task_hash` mismatch warning once. Re-record them with `--update-baseline`. The shipped `tiny-writer` baseline is updated.
- Symlinks in the agent folder are copied as links instead of being followed, so a link to a file outside the folder no longer pulls that file into the workspace. Access through the link is flagged by `paths-confined`.
- The `License :: OSI Approved :: MIT License` classifier is dropped in favour of `license = "MIT"` (PEP 639).
- README: relative links are absolute, so they work on the PyPI page. It now states the judge's 30,000-character input limit, what `task_hash` covers, that a `WARN baseline mismatch` on the shipped baseline is expected on another Claude Code version, and that `canary_absent` is a line-level heuristic. The `--max-turns` note is rewritten for users.
- Tests are named by topic (`test_suite.py`, `test_repeat_and_baselines.py`, `test_env_and_isolation.py` and so on) with shared fakes in `tests/fakes.py`. Tests that reach the real `claude` binary now fail.

### Fixed

- `results.json` and `report.md` are always written, even if the baseline comparison raises. With `--gate`, a comparison that failed exits 2 instead of passing.
- A nested `--baselines-dir` is created (`mkdir -p`) instead of crashing after a full run.
- The `cli.py` docstring names the run directory `<timestamp>-<agent>-<id>`.

## [0.2.0] - 2026-10-21

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
