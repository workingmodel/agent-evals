"""Weekly before/after report from `<runs-dir>/history.jsonl`. Zero tokens: it only reads the history file.

Two ISO weeks are compared per agent and per task (or, with by-version, the agent's latest fingerprint against the one
before it). All figures are normalised per sample so a `--repeat 3` week doesn't look three times as expensive.
Every field in a history row may be null; a metric with no data is None, never 0, and shows as "—".

A repeated task is one history row holding the mean latency of its samples, so latency percentiles are taken over rows:
a repeated row contributes its mean once.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from .telemetry import percentile

SCHEMA = 1
FEW_RUNS = 3  # fewer rows than this on either side: latency percentiles aren't flagged
UNKNOWN_FP = "unknown (pre-0.3 run)"
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_EPS = 1e-9


class WeeklyError(ValueError):
    """A bad argument or unusable history; the CLI prints it and exits 2."""


def parse_week(s: str) -> tuple[int, int]:
    m = _WEEK_RE.match(s or "")
    if not m:
        raise WeeklyError(f"bad week {s!r}: expected YYYY-Www, e.g. 2026-W43")
    y, w = int(m[1]), int(m[2])
    try:
        date.fromisocalendar(y, w, 1)
    except ValueError:
        raise WeeklyError(f"bad week {s!r}: {y} has no week {w:02d}") from None
    return y, w


def previous_week(week: str) -> str:
    y, w = parse_week(week)
    d = date.fromisocalendar(y, w, 1)
    py, pw, _ = date.fromordinal(d.toordinal() - 7).isocalendar()
    return f"{py}-W{pw:02d}"


# key, label, unit, better ("up"/"down"/None), shown as a row when it has data
METRICS = [
    ("runs", "runs", "count", None), ("samples", "samples", "count", None),
    ("pass_rate", "pass rate", "rate", "up"), ("judge_mean", "judge mean", "score", "up"),
    ("resistance", "injection resistance", "rate", "up"),
    ("tokens_in_ps", "tokens in / sample", "tokens", "down"), ("tokens_out_ps", "tokens out / sample", "tokens", "down"),
    ("cost_agent_ps", "agent cost / sample", "money", "down"), ("cost_judge_ps", "judge cost / sample", "money", "down"),
    ("cost_ps", "total cost / sample", "money", "down"), ("total_cost", "total spend", "money", None),
    ("wall_p50", "wall p50", "secs", "down"), ("wall_p95", "wall p95", "secs", "down"),
    ("api_p50", "API p50", "secs", "down"), ("api_p95", "API p95", "secs", "down"),
    ("ttft_p50", "TTFT p50", "secs", "down"), ("ttft_p95", "TTFT p95", "secs", "down"),
    ("turns_ps", "turns / sample", "num", "down"),
]
LABELS = {k: lbl for k, lbl, _, _ in METRICS}
UNITS = {k: u for k, _, u, _ in METRICS}
LATENCY_KEYS = {"wall_p50", "wall_p95", "api_p50", "api_p95", "ttft_p50", "ttft_p95"}
COST_FLAGGED = {"cost_ps", "tokens_in_ps", "tokens_out_ps"}
LATENCY_FLAGGED = {"wall_p95", "api_p95"}
VERSION_KEYS = ["pass_rate", "judge_mean", "resistance", "tokens_in_ps", "tokens_out_ps", "cost_ps", "wall_p50"]
SUMMARY_KEYS = ["pass_rate", "judge_mean", "resistance", "cost_ps", "wall_p50", "total_cost"]


# ---- statistics over rows ---------------------------------------------------------------------------------------

def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _n(r: dict) -> int:
    n = r.get("n")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 1 else 1


def _rate(r: dict) -> float:
    v = _num(r.get("pass_rate"))
    return v if v is not None else (1.0 if r.get("passed") else 0.0)


def _tokens_in(r: dict) -> float | None:
    t = r.get("tokens") if isinstance(r.get("tokens"), dict) else {}
    parts = [_num(t.get(k)) for k in ("input", "cache_read", "cache_creation")]
    return sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None


def _tokens_out(r: dict) -> float | None:
    return _num((r.get("tokens") if isinstance(r.get("tokens"), dict) else {}).get("output"))


def _cost(r: dict, which: str = "total") -> float | None:
    a, j = _num(r.get("agent_cost_usd")), _num(r.get("judge_cost_usd"))
    if which == "agent":
        return a
    if which == "judge":
        return j
    return (a or 0) + (j or 0) if a is not None or j is not None else None


def _per_sample(rows: list[dict], getter) -> float | None:
    """Sum of the values over sum(n) of the rows that have a value."""
    pairs = [(v, _n(r)) for r in rows if (v := getter(r)) is not None]
    return sum(v for v, _ in pairs) / sum(n for _, n in pairs) if pairs else None


def _lat(r: dict, key: str) -> float | None:
    return _num((r.get("latency") if isinstance(r.get("latency"), dict) else {}).get(key))


def stats(rows: list[dict]) -> dict:
    """All metrics for a set of history rows (None where there is no data)."""
    out: dict = {"runs": len(rows), "samples": sum(_n(r) for r in rows)}
    out["pass_rate"] = sum(_rate(r) * _n(r) for r in rows) / out["samples"] if rows else None
    judged = [(_num(r.get("judge_mean")), _n(r)) for r in rows]
    judged = [(m, n) for m, n in judged if m is not None]
    out["judge_mean"] = sum(m * n for m, n in judged) / sum(n for _, n in judged) if judged else None
    inj = [r for r in rows if r.get("category") == "injection"]
    out["resistance"] = sum(_rate(r) * _n(r) for r in inj) / sum(_n(r) for r in inj) if inj else None
    out["vectors"] = len(inj)
    out["vectors_held"] = sum(1 for r in inj if r.get("passed"))
    out["tokens_in_ps"] = _per_sample(rows, _tokens_in)
    out["tokens_out_ps"] = _per_sample(rows, _tokens_out)
    out["cost_agent_ps"] = _per_sample(rows, lambda r: _cost(r, "agent"))
    out["cost_judge_ps"] = _per_sample(rows, lambda r: _cost(r, "judge"))
    out["cost_ps"] = _per_sample(rows, _cost)
    costs = [c for r in rows if (c := _cost(r)) is not None]
    out["total_cost"] = sum(costs) if costs else None
    for key, name in (("wall_s", "wall"), ("api_s", "api"), ("ttft_s", "ttft")):
        vals = [_lat(r, key) for r in rows]
        out[f"{name}_p50"], out[f"{name}_p95"] = percentile(vals, 50), percentile(vals, 95)
    out["turns_ps"] = _per_sample(rows, lambda r: _num(r.get("turns")))
    return out


# ---- deltas and flags -------------------------------------------------------------------------------------------

def delta(key: str, before: float | None, after: float | None) -> tuple[float | None, str]:
    """(delta, unit): points for rates, absolute for scores and counts, percent for everything else."""
    unit = UNITS[key]
    if before is None or after is None:
        return None, "pct"
    if unit == "rate":
        return (after - before) * 100, "points"
    if unit in ("score", "count"):
        return after - before, "abs"
    if before == 0:
        return (0.0 if after == 0 else None), "pct"
    return (after - before) / before * 100, "pct"


def flag(key: str, d: float | None, th: dict, few: bool = False) -> str | None:
    """"warn" / "good" when the metric moved past its threshold, else None."""
    if d is None:
        return None
    if key in ("pass_rate", "resistance"):  # any change
        return "warn" if d < -_EPS else "good" if d > _EPS else None
    if key == "judge_mean":
        return "warn" if d <= -th["judge_drop"] + _EPS else "good" if d >= th["judge_drop"] - _EPS else None
    limit = th["cost"] if key in COST_FLAGGED else th["latency"] if key in LATENCY_FLAGGED and not few else None
    if limit is None:
        return None
    return "warn" if d / 100 >= limit - _EPS else "good" if d / 100 <= -limit + _EPS else None


def compare_stats(before: dict, after: dict, th: dict) -> dict:
    out = {}
    few = before["runs"] < FEW_RUNS or after["runs"] < FEW_RUNS
    for key, label, _, _ in METRICS:
        b, a = before.get(key), after.get(key)
        d, unit = delta(key, b, a)
        note = "few runs" if few and key in LATENCY_KEYS and (b is not None or a is not None) else None
        out[key] = {"key": key, "label": label, "before": b, "after": a, "delta": d, "delta_unit": unit,
                    "flag": flag(key, d, th, few), "note": note}
    return out


def compare_rows(before: list[dict], after: list[dict], th: dict) -> dict:
    return {"before": stats(before), "after": stats(after), "metrics": compare_stats(stats(before), stats(after), th)}


# ---- grouping, comparability, versions --------------------------------------------------------------------------

def _agent(r: dict) -> str:
    return r.get("agent") or "(unknown)"


def _ts(r: dict) -> datetime:
    try:
        t = datetime.fromisoformat(r["timestamp"])
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _fp(r: dict) -> str:
    return r.get("agent_fingerprint") or UNKNOWN_FP


def _week_of(r: dict) -> str | None:
    w = r.get("iso_week")
    if isinstance(w, str) and _WEEK_RE.match(w):
        try:
            parse_week(w)
            return w
        except WeeklyError:
            pass
    return None


def comparability(before: list[dict], after: list[dict]) -> list[str]:
    """One line per field (model, judge model, Claude Code version, billing) whose values differ between the sides."""
    out = []
    if not before or not after:
        return out
    for key, label in (("model", "model"), ("judge_model", "judge model"), ("claude_version", "Claude Code version"), ("billing", "billing")):
        b = sorted({str(r.get(key)) if r.get(key) is not None else "unknown" for r in before})
        a = sorted({str(r.get(key)) if r.get(key) is not None else "unknown" for r in after})
        if a != b:
            out.append(f"{label} differs: {', '.join(b)} → {', '.join(a)}")
    return out


def version_blocks(rows: list[dict]) -> list[dict]:
    """Per distinct fingerprint, chronologically by first appearance: row range and stats over all its rows."""
    groups: dict[str, list[dict]] = {}
    for r in sorted(rows, key=_ts):
        groups.setdefault(_fp(r), []).append(r)
    return [{"fingerprint": fp, "first": rs[0].get("timestamp"), "last": rs[-1].get("timestamp"), "stats": stats(rs)}
            for fp, rs in groups.items()]


def _agent_report(name: str, before: list[dict], after: list[dict], th: dict) -> dict:
    rep = compare_rows(before, after, th)
    tasks = {}
    for t in sorted({r.get("task") or "(unknown)" for r in before + after}):
        tasks[t] = compare_rows([r for r in before if (r.get("task") or "(unknown)") == t],
                                [r for r in after if (r.get("task") or "(unknown)") == t], th)
    rep.update(agent=name, tasks=tasks, comparability=comparability(before, after), notes=[])
    return rep


# ---- build ------------------------------------------------------------------------------------------------------

def thresholds(cost: float = 0.2, latency: float = 0.25, judge_drop: float = 0.25) -> dict:
    return {"cost": cost, "latency": latency, "judge_drop": judge_drop}


def build_weeks(rows: list[dict], agent: str | None, week: str | None, vs: str | None, th: dict) -> dict:
    if agent:
        rows = [r for r in rows if _agent(r) == agent]
        if not rows:
            raise WeeklyError(f"no history rows for agent {agent!r}")
    dated = [r for r in rows if _week_of(r)]
    warnings = [f"{len(rows) - len(dated)} row(s) without a valid iso_week were ignored"] if len(dated) != len(rows) else []
    if not dated:
        raise WeeklyError("history has no rows with an iso_week")
    if week:
        parse_week(week)
    else:
        week = max((w for r in dated if (w := _week_of(r))), key=parse_week)
    if vs:
        parse_week(vs)
        if vs == week:
            raise WeeklyError("--week and --vs are the same week")
    else:
        vs = previous_week(week)
    cur = [r for r in dated if _week_of(r) == week]
    prev = [r for r in dated if _week_of(r) == vs]
    earlier = [w for r in dated if (w := _week_of(r)) and parse_week(w) < parse_week(week)]
    latest_earlier = max(earlier, key=parse_week) if earlier else None
    notes = []
    if not cur:
        notes.append(f"No rows in {week}.")
    if not prev:
        hint = f"; the most recent earlier week with data is {latest_earlier} (try --vs {latest_earlier})" if latest_earlier else ""
        notes.append(f"Nothing to compare against: no rows in {vs}{hint}.")
    agents = {}
    for name in sorted({_agent(r) for r in cur + prev}):
        b, a = [r for r in prev if _agent(r) == name], [r for r in cur if _agent(r) == name]
        rep = _agent_report(name, b, a, th)
        if prev and not b:
            own = [w for r in dated if _agent(r) == name and (w := _week_of(r)) and parse_week(w) < parse_week(week)]
            rep["notes"].append(f"No rows for this agent in {vs}"
                                + (f"; its most recent earlier week is {max(own, key=parse_week)} (try --vs)." if own else "."))
        if cur and not a:
            rep["notes"].append(f"No rows for this agent in {week}.")
        fps = version_blocks(b + a)
        rep["version_change"] = fps if len(fps) > 1 else []
        agents[name] = rep
    return {"schema": SCHEMA, "mode": "weeks", "week": week, "vs": vs, "labels": {"before": vs, "after": week},
            "agent_filter": agent, "thresholds": th, "rows": {"before": len(prev), "after": len(cur)},
            "notes": notes, "warnings": warnings, "agents": agents}


def build_by_version(rows: list[dict], agent: str, th: dict) -> dict:
    rows = sorted((r for r in rows if _agent(r) == agent), key=_ts)
    if not rows:
        raise WeeklyError(f"no history rows for agent {agent!r}")
    latest = _fp(rows[-1])
    prev_fp = next((_fp(r) for r in reversed(rows) if _fp(r) != latest), None)
    base = {"schema": SCHEMA, "mode": "version", "agent_filter": agent, "thresholds": th, "warnings": [], "week": None, "vs": None}
    if prev_fp is None:
        return {**base, "single": latest, "notes": [], "agents": {}, "rows": {"before": 0, "after": len(rows)},
                "labels": {"before": "", "after": latest}}
    b, a = [r for r in rows if _fp(r) == prev_fp], [r for r in rows if _fp(r) == latest]
    rep = _agent_report(agent, b, a, th)
    rep["version_change"] = []
    return {**base, "labels": {"before": prev_fp, "after": latest}, "rows": {"before": len(b), "after": len(a)},
            "notes": [], "agents": {agent: rep}}


# ---- formatting -------------------------------------------------------------------------------------------------

def fmt(key: str, v: float | None) -> str:
    if v is None:
        return "—"
    return {"rate": f"{v * 100:.1f}%", "score": f"{v:.2f}", "count": f"{v:,.0f}", "tokens": f"{v:,.0f}", "money": f"${v:.4f}",
            "secs": f"{v:.1f}s", "num": f"{v:.1f}"}[UNITS[key]]


def fmt_delta(m: dict) -> str:
    d = m["delta"]
    if d is None:
        return "—"
    return {"points": f"{d:+.1f} pts", "abs": f"{d:+.2f}" if UNITS[m["key"]] == "score" else f"{d:+,.0f}", "pct": f"{d:+.0f}%"}[m["delta_unit"]]


def _mark(m: dict) -> str:
    return {"warn": "⚠️", "good": "✅"}.get(m["flag"] or "", "")


def _rows_md(metrics: dict, keys=None) -> list[str]:
    lines = ["| Metric | Before | After | Δ | |", "|---|---|---|---|---|"]
    for key, m in metrics.items():
        if (keys and key not in keys) or (m["before"] is None and m["after"] is None):
            continue
        label = m["label"] + (f" ({m['note']})" if m["note"] else "")
        lines.append(f"| {label} | {fmt(key, m['before'])} | {fmt(key, m['after'])} | {fmt_delta(m)} | {_mark(m)} |")
    return lines


def flags_of(data: dict) -> list[tuple[str, str, str, str, dict]]:
    """(flag, agent, task or "", key, metric) for every flagged metric, agent level first."""
    out = []
    for name, rep in data["agents"].items():
        for scope, block in [("", rep)] + list(rep["tasks"].items()):
            out += [(m["flag"], name, scope, k, m) for k, m in block["metrics"].items() if m["flag"]]
    return out


def _flag_line(f: tuple) -> str:
    _, agent, task, key, m = f
    return f"- {_mark(m)} {agent}{' / ' + task if task else ''}: {m['label']} {fmt(key, m['before'])} → {fmt(key, m['after'])} ({fmt_delta(m)})"


def render(data: dict) -> str:
    by_version = data["mode"] == "version"
    lb, la = data["labels"]["before"], data["labels"]["after"]
    title = f"Version report — {data['agent_filter']}: `{lb}` → `{la}`" if by_version else f"Weekly report — {la} vs {lb}"
    lines = [f"# {title}", "", f"Agents: {', '.join(data['agents']) or 'none'} · rows: {data['rows']['before']} before, {data['rows']['after']} after "
             f"· read from history.jsonl (zero tokens)", ""]
    th = data["thresholds"]
    lines += [f"Flags: cost or tokens per sample ±{th['cost']:.0%}, p95 wall/API latency ±{th['latency']:.0%}, any drop in pass rate or "
              f"injection resistance, judge mean drop ≥ {th['judge_drop']:g}. Per-sample figures divide by samples, so repeats don't inflate them.", ""]
    lines += [f"> {n}" for n in data["notes"] + data["warnings"]] + ([""] if data["notes"] + data["warnings"] else [])
    if not data["agents"]:
        return "\n".join(lines) + "\n"
    lines += ["## Summary", "", "| Agent | Pass rate | Judge | Resistance | Cost / sample | Wall p50 | Spend | Flags |", "|---|---|---|---|---|---|---|---|"]
    for name, rep in data["agents"].items():
        ms = rep["metrics"]
        cell = lambda k: f"{fmt(k, ms[k]['before'])} → {fmt(k, ms[k]['after'])}"  # noqa: E731
        w = sum(1 for f in flags_of(data) if f[1] == name and f[0] == "warn")
        lines.append(f"| {name} | {cell('pass_rate')} | {cell('judge_mean')} | {cell('resistance')} | {cell('cost_ps')} "
                     f"| {cell('wall_p50')} | {cell('total_cost')} | {w} ⚠️ |")
    fl = flags_of(data)
    lines += ["", "## Flagged", ""] + ([_flag_line(f) for f in fl if f[0] == "warn"] or ["Nothing flagged."])
    good = [_flag_line(f) for f in fl if f[0] == "good"]
    if good:
        lines += ["", "## Improved", ""] + good
    lines += ["", "## Comparability", ""]
    cmp_lines = [f"- ⚠️ {name}: {c}" for name, rep in data["agents"].items() for c in rep["comparability"]]
    lines += cmp_lines or ["No differences in model, judge model, Claude Code version or billing."]
    for name, rep in data["agents"].items():
        if rep["version_change"]:
            lines += ["", f"## Version change — {name}", "", "The agent's instructions changed (fingerprint), over both weeks:", ""]
            vb = rep["version_change"]
            lines += ["| Metric | " + " | ".join(f"`{v['fingerprint']}`" for v in vb) + " |", "|---|" + "---|" * len(vb)]
            lines.append("| rows (first → last) | " + " | ".join(f"{v['stats']['runs']} ({(v['first'] or '?')[:10]} → {(v['last'] or '?')[:10]})" for v in vb) + " |")
            for k in VERSION_KEYS:
                if any(v["stats"][k] is not None for v in vb):
                    lines.append(f"| {LABELS[k]} | " + " | ".join(fmt(k, v["stats"][k]) for v in vb) + " |")
    for name, rep in data["agents"].items():
        lines += ["", f"## {name}", ""] + [f"> {n}" for n in rep["notes"]] + ([""] if rep["notes"] else [])
        lines += _rows_md(rep["metrics"])
        for task, block in rep["tasks"].items():
            lines += ["", f"### {task}", ""] + _rows_md(block["metrics"], keys={k for k, *_ in METRICS if k != "total_cost"})
    return "\n".join(lines) + "\n"


def summary_lines(data: dict) -> list[str]:
    if data["mode"] == "version":
        head = f"{data['agent_filter']}: {data['labels']['before']} → {data['labels']['after']}"
    else:
        head = f"{data['labels']['after']} vs {data['labels']['before']}"
    out = [head] + data["notes"]
    for name, rep in data["agents"].items():
        ms = rep["metrics"]
        parts = [f"pass {fmt('pass_rate', ms['pass_rate']['after'])} ({fmt_delta(ms['pass_rate'])})",
                 f"cost/sample {fmt('cost_ps', ms['cost_ps']['after'])} ({fmt_delta(ms['cost_ps'])})",
                 f"wall p50 {fmt('wall_p50', ms['wall_p50']['after'])}"]
        if ms["resistance"]["after"] is not None:
            parts.append(f"resistance {fmt('resistance', ms['resistance']['after'])}")
        w = sum(1 for f in flags_of(data) if f[1] == name and f[0] == "warn")
        out.append(f"{name}: " + ", ".join(parts) + f", {w} ⚠️")
    return out
