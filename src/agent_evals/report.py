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
        tag = "[injection] " if r.get("category") == "injection" else ""  # stands out in the list
        if not r["run_ok"]:
            if b.get("passed"):
                out.append(f"{tag}{r['task']}: passed in baseline ({b.get('run')}), run error now")
            continue
        if b.get("passed") and not r["passed"]:
            out.append(f"{tag}{r['task']}: passed in baseline ({b.get('run')}), fails now")
        m, bm = r["judge"].get("mean"), b.get("mean")
        if m is not None and bm is not None and bm - m >= drop:
            out.append(f"{tag}{r['task']}: judge mean {bm} → {m}")
        if r.get("n", 1) > 1 and b.get("n", 1) > 1 and b["pass_rate"] - r["pass_rate"] >= pass_drop - 1e-9:
            out.append(f"{tag}{r['task']}: pass rate {b['pass_rate']} → {r['pass_rate']}")
    return out


def _injection_section(rs: list[dict]) -> list[str]:
    """`## Injection resistance`: how many injection vectors held, and why each failed one didn't. A vector counts as
    resisted only if its task passed; incomplete and run-error tasks are shown apart from plain failures."""
    inj = [r for r in rs if r.get("category") == "injection"]
    if not inj:
        return []
    lines = ["## Injection resistance", "", f"**{sum(r['passed'] for r in inj)}/{len(inj)} vectors resisted**", ""]
    for r in inj:
        if r["passed"]:
            continue
        failed = ", ".join(c["id"] for c in r["checks"] if not c["passed"])
        rate = f" ({round(r['pass_rate'] * r['n'])}/{r['n']} samples held)" if r.get("n", 1) > 1 else ""
        if not r["run_ok"]:
            why = "💥 run error"
        elif r.get("incomplete"):
            why = "⚠️ incomplete (not scored)"
        else:
            why = "❌ " + (f"failed: {failed}" if failed else "judge below the pass mean")
        lines.append(f"- {r['title']} · vector: {r.get('vector') or 'unlabelled'} · {why}{rate}")
    return lines + [""] if len(lines) > 4 else lines


def _judge_cell(r: dict) -> str:
    m = r["judge"]["mean"]
    if m is None:
        return "—"
    return f"{m} ± {r['judge']['stdev']}" if r.get("n", 1) > 1 and r["judge"].get("stdev") is not None else str(m)


def _secs_cell(r: dict, key: str) -> str:
    v = (r.get("latency") or {}).get(key)
    return f"{v:.1f}s" if isinstance(v, (int, float)) else "—"


def _cost_split(r: dict) -> tuple[float, float]:
    """(agent, judge) cost; results saved before the split derive the agent half from the combined cost."""
    judge = r.get("judge_cost_usd", (r.get("judge") or {}).get("cost_usd") or 0) or 0
    return (r["agent_cost_usd"] if r.get("agent_cost_usd") is not None else r["cost_usd"] - judge), judge


def render(summary: dict, suite: dict) -> str:
    rs = summary["results"]
    passed = sum(r["passed"] for r in rs)
    agent_cost, judge_cost = (sum(x) for x in zip(*map(_cost_split, rs), strict=False)) if rs else (0.0, 0.0)
    secs = sum(r["duration_s"] for r in rs)
    cost_note = "billed to API" if summary.get("billing") == "api" else "notional (billed to subscription, not API)"
    lines = [
        f"# Eval report — {summary['agent']}",
        "",
        f"Run `{summary['run']}` · **{passed}/{len(rs)} tasks passed** · {secs:.0f}s total · "
        f"${agent_cost:.2f} agent + ${judge_cost:.2f} judge {cost_note}",
        "",
    ]
    lines += _injection_section(rs)
    if summary["regressions"]:
        lines += ["## ⚠️ Regressions vs baseline", ""] + [f"- {x}" for x in summary["regressions"]] + [""]
    if summary.get("baseline_mismatch"):
        lines += ["## ⚠️ Baseline mismatch", "", "The baseline was produced by something different; the comparison may not be like for like.", ""]
        lines += [f"- {x}" for x in summary["baseline_mismatch"]] + [""]
    lines += ["| Task | Result | Rule checks | Judge (1–5) | Turns | Time | API | TTFT | Tokens in/out |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rs:
        tok = r["tokens"]
        tin = tok.get("input_tokens", 0) + tok.get("cache_read_input_tokens", 0) + tok.get("cache_creation_input_tokens", 0)
        res = "✅ pass" if r["passed"] else ("💥 run error" if not r["run_ok"] else "⚠️ incomplete" if r.get("incomplete") else "❌ fail")
        if r.get("n", 1) > 1:
            res += f" ({round(r['pass_rate'] * r['n'])}/{r['n']})"
        lines.append(f"| {r['title']} | {res} | {r['checks_passed']}/{r['checks_total']} | {_judge_cell(r)} "
                     f"| {r['turns']} | {r['duration_s']}s | {_secs_cell(r, 'api_s')} | {_secs_cell(r, 'ttft_s')} | {tin:,}/{tok.get('output_tokens', 0):,} |")
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
