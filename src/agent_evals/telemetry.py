"""Per-run telemetry from the final `result` event, and its aggregation over repeated samples.

Every field is optional: older Claude Code versions omit some, and a missing measurement is None, never 0.
Fields on a sample (and on the aggregated task):
  latency        {"wall_s", "api_s", "ttft_s"}; for a repeated task, the mean of each, plus "min" / "max" mappings
  telemetry      {"thinking_tokens", "cache_write_1h", "cache_write_5m", "web_search_requests",
                  "web_fetch_requests", "permission_denials"}  (denials are a count); sums over samples
  model_cost     {model: cost_usd} from the result event's `modelUsage`
  agent_cost_usd / judge_cost_usd   the two halves of the combined `cost_usd`
"""
from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path

LATENCY_KEYS = ("wall_s", "api_s", "ttft_s")
COUNT_KEYS = ("thinking_tokens", "cache_write_1h", "cache_write_5m", "web_search_requests", "web_fetch_requests", "permission_denials")


def percentile(values: Sequence[float | None], p: float) -> float | None:
    """The p-th percentile (0-100) by linear interpolation; None for no values."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _secs(ms, wall: float | None = None) -> float | None:
    n = _num(ms)
    return round(n / 1000, 3) if n is not None else wall


def _dig(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def from_result(result: dict, wall_s: float | None) -> dict:
    """{latency, telemetry, model_cost} from a `result` event ({} gives all-None fields)."""
    usage = result.get("usage")
    thinking = _dig(usage, "output_tokens_details", "thinking_tokens")
    model_usage = result.get("modelUsage")
    if thinking is None and isinstance(model_usage, dict):
        parts = [_num(m.get("thinkingTokens")) for m in model_usage.values() if isinstance(m, dict)]
        thinking = sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None
    denials = result.get("permission_denials")
    tele = {
        "thinking_tokens": thinking,
        "cache_write_1h": _dig(usage, "cache_creation", "ephemeral_1h_input_tokens"),
        "cache_write_5m": _dig(usage, "cache_creation", "ephemeral_5m_input_tokens"),
        "web_search_requests": _dig(usage, "server_tool_use", "web_search_requests"),
        "web_fetch_requests": _dig(usage, "server_tool_use", "web_fetch_requests"),
        "permission_denials": len(denials) if isinstance(denials, list) else None,
    }
    cost = {m: _num(u.get("costUSD")) for m, u in model_usage.items() if isinstance(u, dict) and _num(u.get("costUSD")) is not None} \
        if isinstance(model_usage, dict) else None
    return {"latency": {"wall_s": wall_s, "api_s": _secs(result.get("duration_api_ms")), "ttft_s": _secs(result.get("ttft_ms"))},
            "telemetry": {k: tele[k] for k in COUNT_KEYS}, "model_cost": cost}


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def aggregate(samples: list[dict]) -> dict:
    """Fold per-sample telemetry: sums for counts and costs, mean/min/max for latency (None when no sample measured it)."""
    lat: dict = {}
    lo: dict = {}
    hi: dict = {}
    for k in LATENCY_KEYS:
        xs = [x["latency"][k] for x in samples if isinstance(x.get("latency"), dict) and x["latency"].get(k) is not None]
        lat[k], lo[k], hi[k] = _mean(xs), (min(xs) if xs else None), (max(xs) if xs else None)
    tele = {}
    for k in COUNT_KEYS:
        xs = [x["telemetry"][k] for x in samples if isinstance(x.get("telemetry"), dict) and x["telemetry"].get(k) is not None]
        tele[k] = sum(xs) if xs else None
    costs = [x["model_cost"] for x in samples if isinstance(x.get("model_cost"), dict)]
    model_cost = {m: round(sum(c.get(m, 0) for c in costs), 6) for m in sorted({m for c in costs for m in c})} if costs else None
    return {"latency": {**lat, "min": lo, "max": hi}, "telemetry": tele, "model_cost": model_cost,
            "agent_cost_usd": round(sum(x.get("agent_cost_usd") or 0 for x in samples), 4),
            "judge_cost_usd": round(sum(x.get("judge_cost_usd") or 0 for x in samples), 4)}


def read_result_event(trace: Path) -> dict | None:
    """The final `result` event of a saved trace; None if the trace is missing, unreadable or has none."""
    try:
        events = [json.loads(ln) for ln in trace.read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return None
    return next((e for e in reversed(events) if isinstance(e, dict) and e.get("type") == "result"), None)


def fill_sample(r: dict, trace: Path, refresh: bool = False) -> None:
    """Add the telemetry fields to a saved sample result from its trace. Without `refresh`, a sample that already
    has them is left alone; with it (rescore), they are re-derived whenever the trace has a result event."""
    if "latency" in r and not refresh:
        return
    judge_cost = (r.get("judge") or {}).get("cost_usd") or 0
    ev = read_result_event(trace)
    if ev is None:
        if "latency" not in r:
            r["latency"] = {"wall_s": r.get("duration_s") if r.get("run_ok") else None, "api_s": None, "ttft_s": None}
            r["telemetry"] = {k: None for k in COUNT_KEYS}
            r["model_cost"] = None
            r["agent_cost_usd"] = round(max((r.get("cost_usd") or 0) - judge_cost, 0), 4)
            r["judge_cost_usd"] = round(judge_cost, 4)
        return
    r.update(from_result(ev, r.get("duration_s") if r.get("run_ok") else None))
    r["agent_cost_usd"] = round(_num(ev.get("total_cost_usd")) or 0, 4)
    r["judge_cost_usd"] = round(judge_cost, 4)


def fill_result(r: dict, run_dir: Path, refresh: bool = False) -> None:
    """fill_sample for a saved task result: each repeat sample from `traces/<task>.rN.jsonl`, then the fold."""
    traces = run_dir / "traces"
    if r.get("samples"):
        for i, s in enumerate(r["samples"], 1):
            fill_sample(s, traces / f"{r['task']}.r{i}.jsonl", refresh)
        r.update(aggregate(r["samples"]))
    else:
        fill_sample(r, traces / f"{r['task']}.jsonl", refresh)
