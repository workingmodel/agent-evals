"""Baseline comparison and the markdown report."""
from __future__ import annotations


def compare(results: list[dict], baseline: dict, drop: float, pass_drop: float = 0.34) -> list[str]:
    """A regression is: a task that passed in the baseline now fails, its judge mean fell by >= drop, or (when both
    were repeated runs) its pass rate fell by >= pass_drop."""
    out = []
    for r in results:
        b = baseline.get(r["task"])
        if not b:
            continue
        if not r["run_ok"]:
            if b.get("passed"):
                out.append(f"{r['task']}: passed in baseline ({b.get('run')}), run error now")
            continue
        if b.get("passed") and not r["passed"]:
            out.append(f"{r['task']}: passed in baseline ({b.get('run')}), fails now")
        m, bm = r["judge"].get("mean"), b.get("mean")
        if m is not None and bm is not None and bm - m >= drop:
            out.append(f"{r['task']}: judge mean {bm} → {m}")
        if r.get("n", 1) > 1 and b.get("n", 1) > 1 and b["pass_rate"] - r["pass_rate"] >= pass_drop - 1e-9:
            out.append(f"{r['task']}: pass rate {b['pass_rate']} → {r['pass_rate']}")
    return out


def _judge_cell(r: dict) -> str:
    m = r["judge"]["mean"]
    if m is None:
        return "—"
    return f"{m} ± {r['judge']['stdev']}" if r.get("n", 1) > 1 and r["judge"].get("stdev") is not None else str(m)


def render(summary: dict, suite: dict) -> str:
    rs = summary["results"]
    passed = sum(r["passed"] for r in rs)
    cost = sum(r["cost_usd"] for r in rs)
    secs = sum(r["duration_s"] for r in rs)
    cost_note = "billed to API" if summary.get("billing") == "api" else "notional (billed to subscription, not API)"
    lines = [
        f"# Eval report — {summary['agent']}",
        "",
        f"Run `{summary['run']}` · **{passed}/{len(rs)} tasks passed** · {secs:.0f}s total · "
        f"${cost:.2f} {cost_note}",
        "",
    ]
    if summary["regressions"]:
        lines += ["## ⚠️ Regressions vs baseline", ""] + [f"- {x}" for x in summary["regressions"]] + [""]
    if summary.get("baseline_mismatch"):
        lines += ["## ⚠️ Baseline mismatch", "", "The baseline was produced by something different; the comparison may not be like for like.", ""]
        lines += [f"- {x}" for x in summary["baseline_mismatch"]] + [""]
    lines += ["| Task | Result | Rule checks | Judge (1–5) | Turns | Time | Tokens in/out |", "|---|---|---|---|---|---|---|"]
    for r in rs:
        tok = r["tokens"]
        tin = tok.get("input_tokens", 0) + tok.get("cache_read_input_tokens", 0) + tok.get("cache_creation_input_tokens", 0)
        res = "✅ pass" if r["passed"] else ("💥 run error" if not r["run_ok"] else "⚠️ incomplete" if r.get("incomplete") else "❌ fail")
        if r.get("n", 1) > 1:
            res += f" ({round(r['pass_rate'] * r['n'])}/{r['n']})"
        lines.append(f"| {r['title']} | {res} | {r['checks_passed']}/{r['checks_total']} | {_judge_cell(r)} "
                     f"| {r['turns']} | {r['duration_s']}s | {tin:,}/{tok.get('output_tokens', 0):,} |")
    for r in rs:
        lines += ["", f"## {r['title']}", ""]
        if r["error"]:
            lines += [f"Run error: `{r['error']}`", ""]
        if r.get("incomplete"):
            lines += [f"Incomplete: {r.get('incomplete_reason', 'judge did not complete')}", ""]
        failed = [c for c in r["checks"] if not c["passed"]]
        lines += [f"**Rule checks:** {r['checks_passed']}/{r['checks_total']} passed"]
        lines += [f"- ❌ `{c['id']}` {c['detail']}" for c in failed]
        if r["judge"]["scores"]:
            lines += ["", "**Judge:**"] + [f"- {s['score']}/5 {s['criterion']}: {s['reason']}" for s in r["judge"]["scores"]]
        if r["judge"].get("error"):
            lines += [f"- judge error: {r['judge']['error']}"]
        lines += ["", f"**Trace:** {len(r['tools'])} tool calls ({', '.join(sorted(set(r['tools']))) or 'none'}); "
                  f"read {len(r['reads'])} file(s); wrote {', '.join(r['files_written']) or 'nothing'}"
                  + (f"; deleted {', '.join(r['files_deleted'])}" if r.get('files_deleted') else "")
                  + f". Full trace: `traces/{r['task']}{'.r*' if r.get('n', 1) > 1 else ''}.jsonl`"]
    return "\n".join(lines) + "\n"
