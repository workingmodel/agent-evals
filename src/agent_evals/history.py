"""Run history: one JSON line per task per run in `<runs-dir>/history.jsonl`, for trends across runs.

The file lives next to the run dirs (gitignored, private). It is derived data: `history rebuild` regenerates it
from the saved results.json files with zero tokens, and re-running or rescoring a run replaces that run's lines.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .telemetry import fill_result

SCHEMA = 1
NAME = "history.jsonl"


def run_timestamp(*names: str, fallback: float | None = None) -> datetime:
    """Local start time with UTC offset, from the first `YYYYMMDD-HHMMSS-...` run name that parses (else the fallback epoch)."""
    for name in names:
        try:
            return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").astimezone()
        except ValueError:
            pass
    if fallback is None:
        raise ValueError(f"cannot read a start time from run name {names[0]!r}")
    return datetime.fromtimestamp(fallback).astimezone()


def iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _tokens(r: dict) -> dict:
    t = r.get("tokens") or {}
    return {"input": t.get("input_tokens"), "output": t.get("output_tokens"),
            "cache_read": t.get("cache_read_input_tokens"), "cache_creation": t.get("cache_creation_input_tokens")}


def rows_for(summary: dict, run_dir: Path) -> list[dict]:
    """History rows for one run's results.json. Results saved before telemetry existed are filled from their traces."""
    run = summary.get("run") or run_dir.name
    ts = run_timestamp(run, run_dir.name, fallback=_mtime(run_dir))
    rows = []
    for r in copy.deepcopy(summary["results"]):
        fill_result(r, run_dir)
        env, lat = r.get("env") or {}, r.get("latency") or {}
        judge = r.get("judge") or {}
        rows.append({
            "schema": SCHEMA, "run": run, "timestamp": ts.isoformat(timespec="seconds"), "iso_week": iso_week(ts),
            "agent": summary.get("agent"), "task": r["task"],
            "model": r.get("model") or env.get("model"), "judge_model": r.get("judge_model"),
            "claude_version": env.get("claude_code_version"), "billing": summary.get("billing"),
            "agent_fingerprint": summary.get("agent_fingerprint"),
            "n": r.get("n", 1), "passed": bool(r.get("passed")),
            "pass_rate": r.get("pass_rate", 1.0 if r.get("passed") else 0.0), "incomplete": bool(r.get("incomplete")),
            "judge_mean": judge.get("mean"), "tokens": _tokens(r),
            "agent_cost_usd": r.get("agent_cost_usd"), "judge_cost_usd": r.get("judge_cost_usd"),
            "latency": {k: lat.get(k) for k in ("wall_s", "api_s", "ttft_s")},
            "turns": r.get("turns"), "run_ok": bool(r.get("run_ok")),
        })
    return rows


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _dump(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _order(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (datetime.fromisoformat(r["timestamp"]), r["run"]))  # stable: task order within a run is kept


def update(runs_dir: Path, summary: dict, run_dir: Path) -> None:
    """Replace this run's lines in history.jsonl (appending if it has none). Lines that aren't ours are kept."""
    path = runs_dir / NAME
    keep: list[dict] = []
    if path.exists():
        for ln in path.read_text().splitlines():
            try:
                row = json.loads(ln)
                datetime.fromisoformat(row["timestamp"])
                if row.get("run") != (summary.get("run") or run_dir.name):
                    keep.append(row)
            except (ValueError, KeyError, TypeError):
                print(f"warning: dropped unreadable line from {path}", file=sys.stderr)
    _write_atomic(path, _dump(_order(keep + rows_for(summary, run_dir))))


def rebuild(runs_dir: Path) -> tuple[int, int, list[str]]:
    """Regenerate history.jsonl from every <runs-dir>/*/results.json. Returns (runs, rows, warnings); unreadable runs are skipped."""
    rows, warnings, runs = [], [], 0
    for rd in sorted(p for p in runs_dir.iterdir() if (p / "results.json").is_file()):
        try:
            summary = json.loads((rd / "results.json").read_text())
            if not isinstance(summary, dict) or not isinstance(summary.get("results"), list):
                raise ValueError("not a results file written by `run`")
            got = rows_for(summary, rd)
        except Exception as e:  # a corrupt run must not stop the rest
            warnings.append(f"skipped {rd.name}: {type(e).__name__}: {e}")
            continue
        rows += got
        runs += 1
    _write_atomic(runs_dir / NAME, _dump(_order(rows)))
    return runs, len(rows), warnings


def load(runs_dir: Path) -> list[dict]:
    path = runs_dir / NAME
    out = []
    for ln in path.read_text().splitlines() if path.exists() else []:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _f(v, fmt: str = "{:.1f}s") -> str:
    return fmt.format(v) if isinstance(v, (int, float)) else "—"


def show(rows: list[dict], agent: str | None = None, last: int = 20) -> str:
    rows = [r for r in rows if not agent or r.get("agent") == agent][-last:] if last > 0 else []
    head = ["run", "task", "pass", "judge", "cost", "wall/api/ttft", "tokens in/out"]
    table = [head]
    for r in rows:
        lat, tok = r.get("latency") or {}, r.get("tokens") or {}
        cost = (r.get("agent_cost_usd") or 0) + (r.get("judge_cost_usd") or 0)
        tin = sum(tok.get(k) or 0 for k in ("input", "cache_read", "cache_creation"))
        mark = "inc" if r.get("incomplete") else ("pass" if r.get("passed") else ("ERR" if not r.get("run_ok") else "fail"))
        table.append([r.get("run", ""), r.get("task", ""), mark, _f(r.get("judge_mean"), "{}"), f"${cost:.2f}",
                      "/".join(_f(lat.get(k)) for k in ("wall_s", "api_s", "ttft_s")), f"{tin:,}/{tok.get('output') or 0:,}"])
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in table) + "\n"
