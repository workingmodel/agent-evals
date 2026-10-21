# agent-evals

Unit tests for Claude Code agents. Run an agent headless on golden tasks, check its output with rules and an LLM judge, save baselines, and fail the build when something regresses.

## Why we built this

[Working Model](https://www.workingmodel.co/) is a studio that runs its own fleet of Claude Code agents. They do copywriting, art direction, research, legal review and devops.

Every agent is defined by a `CLAUDE.md`, a set of rules and a model. When we change any of those, we need to know what broke. Reading a few outputs by hand doesn't scale, and it misses the quiet regressions. So we wrote tests for agents: the same idea as unit tests, applied to prompts and rules. We use agent-evals to catch regressions when we change an agent's `CLAUDE.md`, rules or model.

It's small on purpose. It does one job and doesn't need a service.

## What it does

For each golden task in a suite, agent-evals:

1. **Runs the agent headless.** It copies the agent's folder into a temp workspace and runs `claude -p` from there. Your real files are never touched.
2. **Applies rule checks.** Zero tokens. Banned words, required format, files read, files written, and more. See [Check types](#check-types).
3. **Asks an LLM judge.** A separate model scores the output 1 to 5 against a per-task rubric. The agent's output is treated as untrusted data.
4. **Saves a trace.** Every event goes to `runs/<run>/traces/`. Cost, tokens, turns and wall-clock time are recorded per task.
5. **Compares with a baseline.** `--update-baseline` saves the current results. `--gate` exits non-zero if a later run regressed.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- The [Claude Code](https://claude.com/claude-code) CLI, installed and logged in

The harness enforces no minimum Claude Code version. `doctor` prints the version and checks that the CLI lists every flag the harness passes. The code notes that `--max-turns` is accepted but missing from `claude --help`, checked against Claude Code 2.1.306, so `doctor` only warns about it.

## Quickstart

From a fresh clone:

```bash
uv sync
uv run agent-evals doctor                                          # claude CLI and flags. Zero tokens.
uv run agent-evals validate examples/suites/tiny-writer.yaml       # suite and agent path. Zero tokens.
uv run agent-evals run examples/suites/tiny-writer.yaml            # runs the agent and the judge
uv run agent-evals run examples/suites/tiny-writer.yaml --repeat 3 # each task three times
uv run agent-evals run examples/suites/tiny-writer.yaml --repeat 3 --gate  # exit 1 on regression
```

`doctor` and `validate` cost nothing. The `run` commands spend tokens. At today's notional prices a task costs about $0.09 per sample, judge included. That varies with the model, the task and the agent's folder, so treat it as a ballpark. To try the rule checks alone, add `--no-judge`.

The example agent in `examples/agents/tiny-writer` is fictional and self-contained. A suite's relative `path` resolves against the suite file's directory, so the commands work from any CWD.

Each run writes `runs/<timestamp>-<agent>-<id>/` with `report.md`, `results.json`, `traces/` and `workspaces/`. Open `report.md` first.

## Billing

By default a run bills to your Claude subscription. The harness removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the environment of every `claude` process it starts (agent and judge).

`run --billing api` keeps them and bills the API instead. It exits 2 if `ANTHROPIC_API_KEY` isn't set.

Costs in reports are notional on a subscription ("billed to subscription, not API") and real with `--billing api`.

## Suite files

A suite is one YAML file. Run `validate` to check it before spending tokens.

### Suite-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `agent` | string | required | Agent name. Names the run dir and the baseline file (`<agent>.json`). |
| `path` | string | required | Agent folder to copy. A relative path resolves against the suite file's directory. `~` is expanded. |
| `model` | string | required | Model the agent runs on. |
| `tasks` | list | required | The golden tasks. See below. |
| `judge_model` | string | `claude-haiku-5-5` | Model for the LLM judge. |
| `max_turns` | int | `20` | `--max-turns` for the agent. |
| `timeout_s` | int | `900` | Wall-clock limit per agent run, in seconds. |
| `pass_mean` | number | `3.5` | Minimum judge mean for a task to pass. |
| `checks` | list | none | Rule checks applied to every task. |
| `copy_block` | `[start, end]` | none | Marker pair for the `copy_block` scope. |
| `vars` | mapping | none | Replaces `{name}` in every task prompt with the value. |
| `repeat` | int >= 1 | `1` | Samples per task. `--repeat` on the command line wins. |
| `min_pass_rate` | number 0 to 1 | `0.67` | Pass rate a repeated task needs to pass. |
| `regression_drop` | number | `0.75` | Judge-mean drop vs baseline that counts as a regression. |
| `regression_pass_drop` | number 0 to 1 | `0.34` | Pass-rate drop vs baseline that counts as a regression. |
| `setting_sources` | string | `project` | Comma-separated list from `user`, `project`, `local`. Passed to `--setting-sources`. |
| `confine_paths` | bool | `true` | `false` turns off the built-in `paths-confined` check. |
| `allow_outside` | list of strings | `[]` | Path prefixes `paths-confined` tolerates. `~` is expanded. |
| `check_env` | bool | `true` | `false` turns off the built-in `clean-env` check. |
| `exclude` | list of strings | `[]` | Extra file-name globs left out of the workspace copy. |
| `allowed_tools` | list | `Read, Glob, Grep, Write, Edit` | Tools the agent may use. |
| `disallowed_tools` | list | `Bash, WebSearch, WebFetch, Task` | Tools the agent may not use. |

`allowed_tools`, `disallowed_tools`, `max_turns` and `timeout_s` are read by the runner. `validate` doesn't check them, so a typo there fails at run time.

### Task-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | required | Unique within the suite. Used in file names, `--task` and baselines. |
| `prompt` | string | required | The task, sent to the agent. |
| `title` | string | the id | Display name in reports. |
| `rubric` | list of strings | none | Judge criteria, each scored 1 to 5. No rubric means no judge for this task. |
| `checks` | list | none | Rule checks added to the suite-level ones. |
| `skip_checks` | list of ids | none | Suite-level check ids to skip, including `paths-confined` and `clean-env`. An unknown id is a validation error. |
| `model` | string | the suite's | Overrides `model`. |
| `max_turns` | int | the suite's | Overrides `max_turns`. |
| `timeout_s` | int | the suite's | Overrides `timeout_s`. |

See `examples/suites/tiny-writer.yaml` for a complete suite.

### Check types

Every check is a mapping with an `id` and a `type`. These optional fields work on every type:

- `scope`: which text the check reads.
  - `all` (default): the final message plus every file the agent created or changed.
  - `final`: the final message only.
  - `files`: created or changed files only.
  - `copy_block`: the text between the suite's `copy_block` markers. It falls back to `all` if the markers are absent. This lets "no em dashes in copy" ignore the agent's own notes.
- `file_pattern`: a regex. Only files whose path matches are read. Use it to check a deliverable folder and skip the agent's own notes.
- `ignore_case`: default `true`. Applies to regex-based checks.

Only text files count as created or changed: `.md .txt .html .json .yml .yaml .csv .ts .js .mjs .py .css`.

| Type | Fields | Passes when |
|---|---|---|
| `regex_absent` | `pattern` | The regex doesn't match. |
| `regex_present` | `pattern` | The regex matches. |
| `words_absent` | `words` | None of the words appear. Whole words, case-insensitive. A space in a phrase matches any whitespace. |
| `min_matches` | `pattern`, `min` | The regex matches at least `min` times. |
| `first_line_not` | `pattern` | The first content line doesn't match. Blank lines and lines starting with `#`, `─`, `-` or `*` are skipped. |
| `read_files` | `paths` | The agent read every path. Each path is a substring match against the paths it read. Checks the session-start ritual. |
| `file_written` | `pattern` | A file whose path matches the regex was created or changed. |
| `file_absent` | `pattern` | No created or changed file path matches the regex. |
| `canary_absent` | `pattern`, optional `allow_context` | The canary only appears on lines that flag it. See below. |
| `no_file_writes` | optional `allow` | No file was created or changed, except paths starting with an `allow` prefix. |
| `no_file_deletes` | optional `allow` | No file present at the start is gone, except paths starting with an `allow` prefix. |
| `paths_confined` | optional `allow` | No file tool touched a path outside the workspace. |
| `clean_env` | none | No plugin or MCP server loaded, and no API key was seen on a subscription run. |

`canary_absent` is for prompt-injection tests. You plant a unique string in the task, and the agent must not use it. Quoting it to warn the user is fine. A line with the canary passes if it matches `allow_context`, a regex that defaults to phrases like "ignored", "did not follow", "embedded instruction", "prompt injection" and "suspicious". Set your own `allow_context` to replace the default.

### Built-in checks

Two checks are added to every task automatically:

- `paths-confined` (a `paths_confined` check): fails if a file tool (Read, Write, Edit, Glob, Grep, NotebookEdit) targeted a path outside the workspace. Reads count. Turn off with `confine_paths: false` or `skip_checks: [paths-confined]`. Tolerate specific prefixes with `allow_outside`.
- `clean-env` (a `clean_env` check): reads the environment Claude Code reports in its init event (version, model, `apiKeySource`, MCP servers, non-builtin plugins, memory paths, cwd). It fails if a plugin or MCP server loaded, or if a subscription-billed run saw an API key. A trace with no init event passes. Turn off with `check_env: false` or `skip_checks: [clean-env]`.

## What "pass" means

A task sample passes when both of these hold:

1. Every rule check passes.
2. The judge mean is at least `pass_mean`. A task with no rubric, or a run with `--no-judge`, has no judge mean and this part is satisfied.

Other outcomes:

- **Run error.** The agent run didn't produce a result, or Claude reported an error. The task fails and `run_ok` is false.
- **Incomplete.** The task has a rubric, the judge ran, but it errored or returned the wrong number of scores. The task is not a pass, and it is never counted as one. It's shown as `INCOMPLETE`. Incomplete results are not written to baselines.

A failing task doesn't change the exit code of `run`. Only `--gate` does that.

### Repeats

`run --repeat N` (or the suite key `repeat`) runs every task N times. Agents are not deterministic, and one sample can mislead.

- Sample `i` saves `traces/<task>.r<i>.jsonl` and `workspaces/<task>.r<i>/`. With N=1 the names have no suffix.
- A task passes when its pass rate is at least `min_pass_rate` (default `0.67`, so 2 of 3).
- The result reports `pass_rate`, `n`, the judge mean and its standard deviation (`stdev`) across samples, summed cost, tokens and time, and the per-sample results under `samples`.

## The regression gate

`run --gate` compares the results with `<baselines-dir>/<agent>.json` and exits 1 if any task regressed. Without `--gate`, regressions are printed and written to the report, but the exit code stays 0.

A task regressed when any of these is true:

- It passed in the baseline and now fails. This includes a crashed run and an incomplete judge, which can never pass the gate.
- Its judge mean fell by `regression_drop` (default `0.75`) or more.
- Both the baseline and this run are multi-sample (n > 1), and the pass rate fell by `regression_pass_drop` (default `0.34`) or more. With a single sample on either side, only the first two rules apply.

A task with no baseline entry can't regress.

### Exit codes

| Command | Code | Meaning |
|---|---|---|
| `run` | 0 | Finished. No regression, or `--gate` wasn't given. |
| `run` | 1 | `--gate` and at least one regression. |
| `run` | 2 | Invalid suite or arguments, `claude` not on PATH, or `--billing api` without `ANTHROPIC_API_KEY`. |
| `rescore` | 0 / 2 | Done / suite file not found. |
| `validate` | 0 / 2 | All suites valid / at least one invalid. |
| `doctor` | 0 / 1 | Everything found / `claude` missing or a required flag absent. |

## Baselines

`run --update-baseline` writes `<baselines-dir>/<agent>.json` (default `./baselines`). Commit it. Each task entry holds:

- `passed`, `mean` (judge mean) and `run` (the run that produced it)
- `pass_rate`, `n` and `stdev`, when the task was repeated
- `model`, `judge_model`, `claude_version` and `task_hash`

`task_hash` is a short fingerprint of the prompt, the effective checks, the rubric and the model. A run with no judge mean keeps the baseline's stored mean. Runs that errored or are incomplete aren't stored.

If a later `run` or `rescore` differs from the baseline in `model`, `judge_model` (rubric tasks only), `claude_version` or `task_hash`, it prints `WARN baseline mismatch` and adds a "Baseline mismatch" section to the report. The mismatch never fails the gate. It tells you the comparison isn't like for like. Baselines written before these fields existed don't warn.

## Commands

### `run <suite>`

| Flag | Meaning |
|---|---|
| `--task ID` | Run only this task. Repeatable. |
| `--gate` | Exit 1 if any task regresses against the baseline. |
| `--update-baseline` | Save the results as the new baseline. |
| `--repeat N` | Run each task N times. Default 1, or the suite's `repeat`. |
| `--no-judge` | Rule checks only. No judge tokens. |
| `--billing subscription\|api` | Default `subscription`. See [Billing](#billing). |
| `--runs-dir DIR` | Where runs are written. Default `./runs`. |
| `--baselines-dir DIR` | Where baselines live. Default `./baselines`. |

### `rescore <run_dir>`

Re-applies the rule checks to a saved run. Zero tokens. Judge scores are kept. Use it after you change a check. It recomputes pass/fail, the aggregate for repeated runs, regressions against the current baseline, and the report.

| Flag | Meaning |
|---|---|
| `--suite PATH` | Suite to score against. Default is the one stored in the run. Needed for old runs that stored a relative path. |
| `--update-baseline` | Save the rescored results as the baseline. |
| `--baselines-dir DIR` | Where baselines live. Default `./baselines`. |

Changed and deleted files are diffed against a snapshot taken when the run started, so rescoring still works after the agent folder changes or the run dir moves. Older runs without a snapshot are compared with the agent folder instead.

### `validate <suite>...`

Checks suite files and that each agent `path` exists. Zero tokens. Exit 0 or 2.

### `doctor`

Checks that `claude` is on PATH, prints its version, checks that the flags the harness uses are listed in `claude --help`, and shows the billing mode. Login can't be verified without spending tokens, so if `run` reports an auth error, run `claude` once to log in.

## Isolation and its limits

Be clear about what this is. The workspace is a copy of the agent folder, and the agent runs with real tool access on your machine. It is **not a sandbox**.

What the harness does:

- **Temp-dir workspace.** The copy is made in a fresh temp dir outside any repo. No parent `CLAUDE.md` gets picked up, and runs don't share one auto-memory folder. It moves into the run dir afterwards.
- **`--setting-sources project`.** Only the agent's own `.claude/settings.json` and `CLAUDE.md` load. Your user settings, hooks, plugins and `~/.claude/CLAUDE.md` stay out. `--strict-mcp-config` keeps MCP servers out too.
- **Secrets stay out of the copy.** Excluded: `.git`, `node_modules`, `.venv`, `.env` and `.env.*` (but `.env.example` is kept), `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `settings.local.json`, and images, PDFs, video and zips. Add more with `exclude`.
- **Restricted tools.** By default the agent gets Read, Glob, Grep, Write and Edit. Bash, web tools and Task are off. The agent runs with `--permission-mode acceptEdits`, which is meant to auto-approve edits inside the working directory only. The harness doesn't rely on that: `paths-confined` flags any write outside the workspace.
- **Judge isolation.** The judge runs with no tools, from an empty temp dir. The agent output sits inside a random per-call delimiter, so it can't close its own block.

What it doesn't do:

- **Out-of-workspace access is flagged, not blocked.** The `paths-confined` check reads the trace after the run. It tells you the agent reached outside the workspace. It didn't stop it.
- **Known gap.** Glob and Grep calls with an absolute `pattern` (as opposed to `path`) aren't checked.
- **Auto-memory can't be disabled.** Claude Code makes a memory dir per working directory. The harness removes the run's memory dir only if the run created it and it holds no files.
- **Traces can hold sensitive data.** Files under `runs/` include everything the agent read and wrote. `runs/` is gitignored. Don't commit it, and think before sharing a run.

If your agent can do real damage with Read, Write and Edit, review the suite before you run it. See [SECURITY.md](SECURITY.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The test suite never calls `claude` and costs no tokens.

## License

MIT. See [LICENSE](LICENSE).

---

Built by [Working Model Inc](https://www.workingmodel.co/).
