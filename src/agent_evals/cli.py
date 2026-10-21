"""agent-evals — run golden tasks against a Claude Code agent and score them.

    agent-evals run examples/suites/tiny-writer.yaml                     # all tasks
    agent-evals run examples/suites/tiny-writer.yaml --task weak-brief
    agent-evals run examples/suites/tiny-writer.yaml --gate              # exit 1 on regression vs baseline
    agent-evals run examples/suites/tiny-writer.yaml --update-baseline
    agent-evals run examples/suites/tiny-writer.yaml --repeat 3          # each task 3 times; pass rate + stdev
    agent-evals rescore runs/<run>      # re-apply rule checks, zero tokens
    agent-evals validate suites/*.yaml  # check suites, zero tokens
    agent-evals doctor                  # check the claude CLI, zero tokens

Outputs <runs-dir>/<timestamp>-<agent>/{results.json, report.md, traces/*.jsonl} (default ./runs).
Baselines live in <baselines-dir>/<agent>.json (default ./baselines; commit them).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .checks import run_checks
from .judge import judge
from .report import compare, render
from .runner import run_task
from .suite import load_suite, task_checks, task_hash, validate_suite

FLAGS = ("--json-schema", "--setting-sources", "--strict-mcp-config", "--tools", "--no-session-persistence",
         "--output-format", "--allowedTools", "--disallowedTools", "--permission-mode", "--max-turns")
# listed nowhere in `claude --help` but accepted (checked against claude 2.1.306): warn, don't fail
HIDDEN_OK = {"--max-turns"}


def _run_dir_name(agent: str) -> str:
    # random suffix: two runs in the same second must not collide
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{agent}-{secrets.token_hex(2)}"


def _task_passed(checks_ok: bool, verdict: dict, rubric: list, judged: bool, pass_mean: float) -> tuple[bool, str]:
    """(passed, incomplete_reason). A rubric task whose judge failed is incomplete, never a pass."""
    if rubric and judged:
        if verdict.get("error"):
            return False, f"judge failed: {verdict['error']}"
        if len(verdict.get("scores", [])) != len(rubric):
            return False, f"judge returned {len(verdict.get('scores', []))} scores for {len(rubric)} criteria"
    judge_ok = verdict["mean"] is None or verdict["mean"] >= pass_mean
    return checks_ok and judge_ok, ""


def _meta(suite: dict, t: dict, env: dict | None) -> dict:
    """What produced a result, stored in the baseline so a later comparison can say if it's like for like."""
    m = {"model": t.get("model", suite.get("model")), "judge_model": suite.get("judge_model", "claude-haiku-5-5"),
         "claude_version": (env or {}).get("claude_code_version"), "task_hash": task_hash(suite, t)}
    return {k: v for k, v in m.items() if v}


def _merge_baseline(baseline: dict, results: list[dict], run_name: str, suite: dict | None = None) -> dict:
    """Merge results into the baseline; never lose a stored judge mean, never store incomplete tasks."""
    merged = dict(baseline)
    tasks = {t["id"]: t for t in (suite or {}).get("tasks", [])}
    for r in results:
        if not r["run_ok"] or r.get("incomplete"):
            continue
        mean = r["judge"]["mean"]
        if mean is None and (merged.get(r["task"]) or {}).get("mean") is not None:
            mean = merged[r["task"]]["mean"]
        entry = {"passed": r["passed"], "mean": mean, "run": run_name}
        if r.get("n", 1) > 1:
            entry |= {"pass_rate": r["pass_rate"], "n": r["n"], "stdev": r["judge"].get("stdev")}
        if suite and r["task"] in tasks:
            entry |= _meta(suite, tasks[r["task"]], r.get("env"))
        merged[r["task"]] = entry
    return merged


def _baseline_mismatches(results: list[dict], baseline: dict, suite: dict) -> list[str]:
    """Fields where the baseline was produced differently (old baselines lack the fields: no warning)."""
    tasks = {t["id"]: t for t in suite.get("tasks", [])}
    out = []
    for r in results:
        b, t = baseline.get(r["task"]), tasks.get(r["task"])
        if not b or not t:
            continue
        cur = _meta(suite, t, r.get("env"))
        for k in ("model", "judge_model", "claude_version", "task_hash"):
            if k == "judge_model" and not t.get("rubric"):
                continue
            if k in b and k in cur and b[k] != cur[k]:
                out.append(f"{r['task']}: {k} {b[k]} → {cur[k]}")
    return out


def _aggregate(samples: list[dict], min_pass_rate: float) -> dict:
    """Fold per-sample results into one task result: top-level keys as for a single run, plus n/pass_rate/samples."""
    n = len(samples)
    rate = round(sum(bool(x["passed"]) for x in samples) / n, 2)
    rep = next((x for x in samples if not x["passed"]), samples[0])  # the detail the report shows: a failure if any
    means = [x["judge"]["mean"] for x in samples if x["judge"]["mean"] is not None]
    tok_keys = {k for x in samples for k in x["tokens"]}
    res = {
        **rep,
        "run_ok": any(x["run_ok"] for x in samples),
        "error": next((x["error"] for x in samples if x["error"]), ""),
        "passed": rate >= min_pass_rate - 1e-9, "pass_rate": rate, "n": n,
        "judge": {**rep["judge"], "mean": round(statistics.fmean(means), 2) if means else None,
                  "stdev": round(statistics.pstdev(means), 2) if means else None,
                  "tokens": sum(x["judge"].get("tokens", 0) for x in samples),
                  "cost_usd": round(sum(x["judge"].get("cost_usd", 0) for x in samples), 4)},
        "cost_usd": round(sum(x["cost_usd"] for x in samples), 4),
        "tokens": {k: sum(x["tokens"].get(k, 0) for x in samples) for k in sorted(tok_keys)},
        "judge_tokens": sum(x.get("judge_tokens", 0) for x in samples),
        "duration_s": round(sum(x["duration_s"] for x in samples), 1), "turns": sum(x["turns"] for x in samples),
        "samples": samples,
    }
    res.pop("incomplete", None)
    res.pop("incomplete_reason", None)
    inc = next((x for x in samples if x.get("incomplete")), None)
    if inc:
        res["incomplete"], res["incomplete_reason"] = True, inc.get("incomplete_reason", "")
    return res


def _error_result(t: dict, error: str) -> dict:
    return {
        "task": t["id"], "title": t.get("title", t["id"]),
        "run_ok": False, "error": error[:500], "passed": False,
        "checks": [], "checks_passed": 0, "checks_total": 0,
        "judge": {"scores": [], "mean": None, "tokens": 0, "cost_usd": 0.0},
        "cost_usd": 0.0, "tokens": {}, "judge_tokens": 0, "duration_s": 0.0, "turns": 0,
        "tools": [], "reads": [], "outside_paths": [], "files_written": [], "files_deleted": [], "final_excerpt": "", "env": {},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-evals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a suite")
    r.add_argument("suite", type=Path)
    r.add_argument("--task", action="append", help="run only these task ids")
    r.add_argument("--gate", action="store_true", help="exit 1 if any task regresses vs baseline")
    r.add_argument("--update-baseline", action="store_true")
    r.add_argument("--repeat", type=int, default=None, metavar="N", help="run each task N times (default 1, or the suite's `repeat`)")
    r.add_argument("--no-judge", action="store_true", help="rule checks only (zero judge tokens)")
    r.add_argument("--billing", choices=("subscription", "api"), default="subscription",
                   help="subscription (default) strips ANTHROPIC_API_KEY; api keeps it and bills the API")
    r.add_argument("--runs-dir", type=Path, default=Path("runs"))
    r.add_argument("--baselines-dir", type=Path, default=Path("baselines"))
    rs = sub.add_parser("rescore", help="re-apply rule checks to a saved run (zero tokens; judge scores kept)")
    rs.add_argument("run_dir", type=Path)
    rs.add_argument("--update-baseline", action="store_true")
    rs.add_argument("--suite", type=Path, help="suite file to score against (default: the one stored in the run)")
    rs.add_argument("--baselines-dir", type=Path, default=Path("baselines"))
    v = sub.add_parser("validate", help="validate suite files (zero tokens)")
    v.add_argument("suites", nargs="+", type=Path)
    sub.add_parser("doctor", help="check the claude CLI and billing setup (zero tokens)")
    args = ap.parse_args(argv)
    if args.cmd == "rescore":
        return rescore(args.run_dir, args.update_baseline, args.suite, args.baselines_dir)
    if args.cmd == "validate":
        return validate(args.suites)
    if args.cmd == "doctor":
        return doctor()

    suite = load_suite(args.suite)
    errors = validate_suite(suite)
    if not errors and args.task:
        valid = [t["id"] for t in suite["tasks"]]
        unknown = [x for x in args.task if x not in valid]
        if unknown:
            errors.append(f"unknown task id(s): {', '.join(unknown)} (valid: {', '.join(valid)})")
    if args.repeat is not None and args.repeat < 1:
        errors.append("--repeat must be >= 1")
    if errors:
        print("invalid suite/arguments:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 2
    if not shutil.which("claude"):
        print("claude CLI not found on PATH", file=sys.stderr)
        return 2
    if args.billing == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("--billing api needs ANTHROPIC_API_KEY set", file=sys.stderr)
        return 2
    suite["billing"] = args.billing  # run_task reads it from the suite config
    agent = suite["agent"]
    tasks = [t for t in suite["tasks"] if not args.task or t["id"] in args.task]
    run_dir = args.runs_dir / _run_dir_name(agent)
    run_dir.mkdir(parents=True)
    run_dir = run_dir.resolve()
    block = tuple(suite["copy_block"]) if suite.get("copy_block") else None
    pass_mean = suite.get("pass_mean", 3.5)
    n = args.repeat if args.repeat is not None else suite.get("repeat", 1)

    results: list[dict] = []
    base_path = args.baselines_dir / f"{agent}.json"
    baseline = json.loads(base_path.read_text()) if base_path.exists() else {}
    regressions: list[str] = []
    try:
        for t in tasks:
            print(f"→ {agent}/{t['id']}: running agent…", flush=True)
            try:
                res = _run_samples(t, n, suite, run_dir, block, pass_mean, args.no_judge)
            except Exception as e:  # one broken task must not lose the rest of the run
                res = _error_result(t, f"{type(e).__name__}: {e}")
            results.append(res)
            mark = "PASS" if res["passed"] else ("INCOMPLETE" if res.get("incomplete") else "FAIL")
            rate = f" pass rate {res['pass_rate']} ({n} samples)" if n > 1 else ""
            print(f"  {mark}{rate}  checks {res['checks_passed']}/{res['checks_total']}  judge {res['judge']['mean']}  "
                  f"{res['duration_s']}s  {res['turns']} turns  ${res['cost_usd']}"
                  + (f"  {res['error']}" if res["error"] else ""), flush=True)
    finally:  # also on Ctrl-C: keep what we have
        regressions = compare(results, baseline, suite.get("regression_drop", 0.75), suite.get("regression_pass_drop", 0.34))
        summary = {"agent": agent, "suite": str(args.suite.resolve()), "billing": args.billing, "run": run_dir.name, "results": results, "regressions": regressions}
        if n > 1:
            summary["repeat"] = n
        _warn_mismatch(summary, baseline, suite)
        (run_dir / "results.json").write_text(json.dumps(summary, indent=2))
        (run_dir / "report.md").write_text(render(summary, suite))
        print(f"\nreport: {run_dir / 'report.md'}")

    if args.update_baseline:
        base_path.parent.mkdir(exist_ok=True)
        base_path.write_text(json.dumps(_merge_baseline(baseline, results, run_dir.name, suite), indent=2) + "\n")
        print(f"baseline updated: {base_path}")
    if regressions:
        print("REGRESSIONS:\n  " + "\n  ".join(regressions))
        if args.gate:
            return 1
    return 0


def _warn_mismatch(summary: dict, baseline: dict, suite: dict) -> None:
    """Warn (never fail) when the baseline was produced by a different model, judge, claude version or task."""
    mism = _baseline_mismatches(summary["results"], baseline, suite)
    summary.pop("baseline_mismatch", None)
    if mism:
        summary["baseline_mismatch"] = mism
        print("WARN  baseline mismatch:\n  " + "\n  ".join(mism))


def _run_samples(t: dict, n: int, suite: dict, run_dir: Path, block, pass_mean: float, no_judge: bool) -> dict:
    """Run a task n times. n=1 is the plain single run (same trace/workspace names as ever); n>1 names samples `<id>.r<i>`."""
    samples = []
    for i in range(1, n + 1):
        name = t["id"] if n == 1 else f"{t['id']}.r{i}"
        if n > 1:
            print(f"  sample {i}/{n}…", flush=True)
        try:
            samples.append(_run_one({**t, "_name": name}, suite, run_dir, block, pass_mean, no_judge))
        except Exception as e:
            if n == 1:
                raise
            samples.append(_error_result(t, f"{type(e).__name__}: {e}"))
    return samples[0] if n == 1 else _aggregate(samples, suite.get("min_pass_rate", 0.67))


def _run_one(t: dict, suite: dict, run_dir: Path, block, pass_mean: float, no_judge: bool) -> dict:
    rr = run_task(Path(suite["path"]), t, suite, run_dir)
    checks = run_checks(task_checks(suite, t), rr.artifacts, block) if rr.ok else []
    verdict = {"scores": [], "mean": None, "tokens": 0, "cost_usd": 0.0}
    if rr.ok and t.get("rubric") and not no_judge:
        print(f"  judging ({suite.get('judge_model')})…", flush=True)
        try:
            verdict = judge(t["prompt"], t["rubric"], rr.artifacts.text("all"), suite.get("judge_model", "claude-haiku-5-5"),
                            billing=suite.get("billing", "subscription"))
        except Exception as e:  # judge failure is reported, and makes the task incomplete
            verdict["error"] = str(e)
    checks_ok = rr.ok and all(c.passed for c in checks)
    if rr.ok:
        passed, reason = _task_passed(checks_ok, verdict, t.get("rubric") or [], not no_judge, pass_mean)
    else:
        passed, reason = False, ""
    res = {
        "task": t["id"], "title": t.get("title", t["id"]),
        "run_ok": rr.ok, "error": rr.error,
        "passed": passed,
        "checks": [c.__dict__ for c in checks],
        "checks_passed": sum(c.passed for c in checks), "checks_total": len(checks),
        "judge": verdict,
        "cost_usd": round(rr.cost_usd + verdict.get("cost_usd", 0), 4),
        "tokens": rr.tokens, "judge_tokens": verdict.get("tokens", 0),
        "duration_s": rr.duration_s, "turns": rr.num_turns,
        "tools": rr.artifacts.tools, "reads": rr.artifacts.reads, "outside_paths": rr.artifacts.outside,
        "files_written": list(rr.artifacts.files), "files_deleted": rr.artifacts.deleted,
        "final_excerpt": rr.artifacts.final[:1500],
        "env": rr.artifacts.env,
    }
    if reason:
        res["incomplete"], res["incomplete_reason"] = True, reason
    return res


def rescore(run_dir: Path, update_baseline: bool, suite_path: Path | None = None, baselines_dir: Path = Path("baselines")) -> int:
    summary = json.loads((run_dir / "results.json").read_text())
    suite_path = suite_path or Path(summary["suite"])  # new runs store an absolute path; old ones are CWD-relative
    if not suite_path.is_file():
        print(f"suite file not found: {suite_path}\n  pass the suite explicitly: agent-evals rescore {run_dir} --suite <path>", file=sys.stderr)
        return 2
    suite = load_suite(suite_path)
    suite["billing"] = summary.get("billing", "subscription")  # clean-env judges apiKeySource against the run's billing
    block = tuple(suite["copy_block"]) if suite.get("copy_block") else None
    pass_mean = suite.get("pass_mean", 3.5)
    tasks = {t["id"]: t for t in suite["tasks"]}
    for r in summary["results"]:
        if not r["run_ok"] or r["task"] not in tasks:
            continue
        t = tasks[r["task"]]
        if r.get("samples"):
            for i, smp in enumerate(r["samples"], 1):
                if smp["run_ok"]:
                    _rescore_sample(smp, f"{r['task']}.r{i}", t, suite, run_dir, block, pass_mean)
            new = _aggregate(r["samples"], suite.get("min_pass_rate", 0.67))
            r.clear()
            r.update(new)
            mark = "PASS" if r["passed"] else ("INCOMPLETE" if r.get("incomplete") else "FAIL")
            print(f"{mark}  {r['task']}  pass rate {r['pass_rate']} ({r['n']} samples)  judge {r['judge']['mean']}")
        else:
            _rescore_sample(r, r["task"], t, suite, run_dir, block, pass_mean)
    base_path = baselines_dir / f"{summary['agent']}.json"
    baseline = json.loads(base_path.read_text()) if base_path.exists() else {}
    # regressions are recomputed against the baseline as it is now (before any --update-baseline below)
    summary["regressions"] = compare(summary["results"], baseline, suite.get("regression_drop", 0.75), suite.get("regression_pass_drop", 0.34))
    _warn_mismatch(summary, baseline, suite)
    (run_dir / "results.json").write_text(json.dumps(summary, indent=2))
    (run_dir / "report.md").write_text(render(summary, suite))
    if summary["regressions"]:
        print("REGRESSIONS:\n  " + "\n  ".join(summary["regressions"]))
    if update_baseline:
        base_path = baselines_dir / f"{summary['agent']}.json"
        base_path.parent.mkdir(exist_ok=True)
        base_path.write_text(json.dumps(_merge_baseline(baseline, summary["results"], run_dir.name, suite), indent=2) + "\n")
        print(f"baseline updated: {base_path}")
    return 0


def _rescore_sample(r: dict, name: str, t: dict, suite: dict, run_dir: Path, block, pass_mean: float) -> None:
    """Re-apply the rule checks to one saved run (a task, or one sample of a repeated task); judge scores are kept."""
    from .runner import artifacts_from_saved
    art = artifacts_from_saved(run_dir, name, Path(suite["path"]))
    checks = run_checks(task_checks(suite, t), art, block)
    r["checks"] = [c.__dict__ for c in checks]
    r["outside_paths"] = art.outside
    r["files_deleted"] = art.deleted
    r["env"] = art.env
    r["checks_passed"], r["checks_total"] = sum(c.passed for c in checks), len(checks)
    m = r["judge"]["mean"]
    # stored runs don't record --no-judge; judge output (scores or error) means it was judged
    judged = bool(r["judge"].get("scores") or r["judge"].get("error"))
    r["passed"], reason = _task_passed(all(c.passed for c in checks), r["judge"], t.get("rubric") or [], judged, pass_mean)
    r.pop("incomplete", None)
    r.pop("incomplete_reason", None)
    if reason:
        r["incomplete"], r["incomplete_reason"] = True, reason
    mark = "PASS" if r["passed"] else ("INCOMPLETE" if r.get("incomplete") else "FAIL")
    print(f"{mark}  {name}  checks {r['checks_passed']}/{r['checks_total']}  judge {m}")
    for c in checks:
        if not c.passed:
            print(f"   ❌ {c.id} {c.detail[:150]}")


def validate(paths: list[Path]) -> int:
    """Load + validate each suite and check its agent folder exists. Zero tokens."""
    bad = 0
    for p in paths:
        try:
            suite = load_suite(p)
            errs = validate_suite(suite)
            if not errs and not Path(suite["path"]).is_dir():
                errs = [f"agent path does not exist: {suite['path']}"]
        except Exception as e:  # unreadable file, bad YAML
            errs = [f"{type(e).__name__}: {e}"]
        if errs:
            bad += 1
            print(f"FAIL  {p}\n  " + "\n  ".join(errs))
        else:
            print(f"OK    {p}  ({len(suite['tasks'])} tasks)")
    return 2 if bad else 0


def doctor() -> int:
    """Check the claude CLI the harness depends on and the billing setup. Zero tokens."""
    exe = shutil.which("claude")
    if not exe:
        print("FAIL  claude CLI not found on PATH (install Claude Code first)")
        return 1
    print(f"OK    claude found: {exe}")
    ok = True
    try:
        ver = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30)
        print(f"OK    version: {(ver.stdout or ver.stderr).strip()}")
        helptext = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"FAIL  could not run claude: {e}")
        return 1
    for f in FLAGS:
        if re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", helptext):
            continue
        if f in HIDDEN_OK:
            print(f"WARN  {f} not listed in `claude --help` (known to be hidden; the harness passes it anyway)")
        else:
            ok = False
            print(f"FAIL  {f} not listed in `claude --help`; this claude version may be incompatible")
    if ok:
        print("OK    flags the harness uses are present")
    print("OK    billing: `run` defaults to subscription (strips ANTHROPIC_API_KEY); use --billing api to bill the API")
    print(f"INFO  ANTHROPIC_API_KEY is {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}"
          " (required for --billing api; ignored in subscription mode)")
    print("NOTE  login can't be verified without spending tokens; if `run` reports an auth error, run `claude` once to log in.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
