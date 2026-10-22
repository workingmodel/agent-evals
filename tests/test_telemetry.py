"""Run telemetry (latency, thinking, cache writes, per-model cost), the agent fingerprint and run history."""
import json
import subprocess
from datetime import datetime

import pytest
import yaml

from agent_evals import cli, history, telemetry
from agent_evals.report import render
from agent_evals.runner import agent_fingerprint
from agent_evals.suite import validate_suite
from fakes import FakeClaude, inj_suite, only_run, results_of, run_dirs

NEW = {"duration_ms": 26240, "duration_api_ms": 26086, "ttft_ms": 1981, "total_cost_usd": 0.12,
       "usage": {"output_tokens_details": {"thinking_tokens": 1194}, "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 2},
                 "cache_creation": {"ephemeral_1h_input_tokens": 20000, "ephemeral_5m_input_tokens": 22}},
       "modelUsage": {"claude-sonnet-5-5": {"costUSD": 0.1, "thinkingTokens": 1194}, "claude-haiku-5-5": {"costUSD": 0.02}},
       "permission_denials": [{"tool_name": "Bash"}, {"tool_name": "Bash"}]}


def strip_new(ev):
    """Make the fixture's result event look like an older Claude Code: none of the optional fields."""
    for k in ("ttft_ms", "duration_api_ms", "modelUsage", "permission_denials"):
        ev.pop(k, None)
    for k in ("output_tokens_details", "server_tool_use", "cache_creation"):
        ev["usage"].pop(k, None)


def run_suite(env, agent, fake, *args, **over):
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent, **over)))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "run", fake)
        assert cli.main(["run", str(sp), *args]) == 0
    return sp


# T1
def test_from_result_new_format():
    m = telemetry.from_result(NEW, 30.0)
    assert m["latency"] == {"wall_s": 30.0, "api_s": 26.086, "ttft_s": 1.981}
    assert m["telemetry"] == {"thinking_tokens": 1194, "cache_write_1h": 20000, "cache_write_5m": 22,
                              "web_search_requests": 1, "web_fetch_requests": 2, "permission_denials": 2}
    assert m["model_cost"] == {"claude-sonnet-5-5": 0.1, "claude-haiku-5-5": 0.02}


def test_from_result_old_format_has_none_not_zero():
    m = telemetry.from_result({"total_cost_usd": 0.1, "usage": {"input_tokens": 3}}, 4.2)
    assert m["latency"] == {"wall_s": 4.2, "api_s": None, "ttft_s": None}
    assert set(m["telemetry"].values()) == {None} and m["model_cost"] is None
    assert telemetry.from_result({}, None)["latency"] == {"wall_s": None, "api_s": None, "ttft_s": None}
    odd = telemetry.from_result({"usage": "x", "modelUsage": {"m": 3}, "permission_denials": None, "ttft_ms": True}, 1.0)
    assert odd["latency"]["ttft_s"] is None and odd["model_cost"] == {}


def test_thinking_tokens_fall_back_to_model_usage():
    m = telemetry.from_result({"modelUsage": {"a": {"thinkingTokens": 5}, "b": {"thinkingTokens": 7}}}, 1.0)
    assert m["telemetry"]["thinking_tokens"] == 12


def test_percentile():
    assert telemetry.percentile([], 50) is None
    assert telemetry.percentile([4.0], 95) == 4.0
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert telemetry.percentile(xs, 50) == 3.0 and telemetry.percentile(xs, 0) == 1.0 and telemetry.percentile(xs, 100) == 5.0
    assert telemetry.percentile([1.0, 2.0], 50) == 1.5 and telemetry.percentile([None, 2.0, 1.0], 50) == 1.5


def test_aggregate_over_samples():
    a = {**telemetry.from_result(NEW, 30.0), "agent_cost_usd": 0.12, "judge_cost_usd": 0.01}
    b = {**telemetry.from_result({**NEW, "duration_api_ms": 10000, "ttft_ms": None}, 10.0), "agent_cost_usd": 0.1, "judge_cost_usd": 0.0}
    c = {**telemetry.from_result({}, None), "agent_cost_usd": 0.0, "judge_cost_usd": 0.0}  # a sample that never ran
    agg = telemetry.aggregate([a, b, c])
    assert agg["latency"]["wall_s"] == 20.0 and agg["latency"]["api_s"] == 18.043 and agg["latency"]["ttft_s"] == 1.981
    assert agg["latency"]["min"]["wall_s"] == 10.0 and agg["latency"]["max"]["wall_s"] == 30.0
    assert agg["telemetry"]["thinking_tokens"] == 2388 and agg["telemetry"]["permission_denials"] == 4
    assert agg["model_cost"] == {"claude-haiku-5-5": 0.04, "claude-sonnet-5-5": 0.2}
    assert agg["agent_cost_usd"] == 0.22 and agg["judge_cost_usd"] == 0.01
    none = telemetry.aggregate([c])
    assert none["latency"]["wall_s"] is None and none["latency"]["min"]["wall_s"] is None and none["telemetry"]["thinking_tokens"] is None


def test_run_records_telemetry_and_cost_split(env, agent):
    run_suite(env, agent, FakeClaude())
    r = results_of(only_run(env))["results"][0]
    assert r["latency"]["api_s"] == 8.288 and r["latency"]["ttft_s"] == 3.543 and r["latency"]["wall_s"] == r["duration_s"]
    assert r["telemetry"]["thinking_tokens"] == 12 and r["telemetry"]["cache_write_1h"] == 4277 and r["telemetry"]["permission_denials"] == 0
    assert r["model_cost"] == {"claude-sonnet-5-5": 0.0233641}
    assert r["agent_cost_usd"] == 0.0234 and r["judge_cost_usd"] == 0.01 and r["cost_usd"] == 0.0334


def test_run_with_old_format_trace_does_not_crash(env, agent):
    run_suite(env, agent, FakeClaude(edit_result=strip_new))
    r = results_of(only_run(env))["results"][0]
    assert r["latency"]["api_s"] is None and r["latency"]["ttft_s"] is None and r["latency"]["wall_s"] is not None
    assert r["telemetry"]["thinking_tokens"] is None and r["model_cost"] is None and r["agent_cost_usd"] > 0


def test_repeat_aggregates_telemetry_and_keeps_samples(env, agent):
    run_suite(env, agent, FakeClaude(judge_scores=((4, 4),)), "--repeat", "3")
    r = results_of(only_run(env))["results"][0]
    assert [s["latency"]["api_s"] for s in r["samples"]] == [8.288] * 3 and r["latency"]["api_s"] == 8.288
    assert r["latency"]["min"]["api_s"] == r["latency"]["max"]["api_s"] == 8.288
    assert r["telemetry"]["thinking_tokens"] == 36 and r["model_cost"] == {"claude-sonnet-5-5": 0.070092}
    assert r["agent_cost_usd"] == 0.0702 and r["judge_cost_usd"] == 0.03 and r["cost_usd"] == 0.1002


def test_error_result_has_empty_not_zero_latency(env, agent, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp)])
    r = results_of(only_run(env))["results"][0]
    assert r["latency"] == {"wall_s": None, "api_s": None, "ttft_s": None} and r["agent_cost_usd"] == 0.0


def test_rescore_fills_telemetry_for_an_older_run(env, agent, monkeypatch):
    sp = run_suite(env, agent, FakeClaude())
    d = only_run(env)
    data = results_of(d)
    for k in ("latency", "telemetry", "model_cost", "agent_cost_usd", "judge_cost_usd"):
        del data["results"][0][k]
    (d / "results.json").write_text(json.dumps(data))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("rescore must not call claude"))
    assert cli.main(["rescore", str(d), "--suite", str(sp)]) == 0
    r = results_of(d)["results"][0]
    assert r["latency"]["api_s"] == 8.288 and r["telemetry"]["thinking_tokens"] == 12
    assert r["agent_cost_usd"] == 0.0234 and r["judge_cost_usd"] == 0.01 and r["latency"]["wall_s"] == r["duration_s"]


def test_rescore_repeat_run_preserves_telemetry(env, agent, monkeypatch):
    sp = run_suite(env, agent, FakeClaude(judge_scores=((4, 4),)), "--repeat", "2")
    d = only_run(env)
    assert cli.main(["rescore", str(d), "--suite", str(sp)]) == 0
    r = results_of(d)["results"][0]
    assert r["telemetry"]["thinking_tokens"] == 24 and len(r["samples"]) == 2 and r["samples"][1]["latency"]["ttft_s"] == 3.543


# T2
def instr_agent(tmp_path):
    ag = tmp_path / "ag"
    (ag / ".claude" / "skills" / "s").mkdir(parents=True)
    (ag / "sub").mkdir()
    (ag / "CLAUDE.md").write_text("rules")
    (ag / "sub" / "CLAUDE.md").write_text("sub rules")
    (ag / ".claude" / "skills" / "s" / "SKILL.md").write_text("skill")
    (ag / "status.md").write_text("status")
    return ag


def test_fingerprint_stable_and_tracks_instruction_files(tmp_path):
    ag = instr_agent(tmp_path)
    fp = agent_fingerprint(ag)
    assert len(fp) == 12 and fp == agent_fingerprint(ag)
    (ag / "status.md").write_text("changed")
    (ag / "drafts").mkdir()
    (ag / "drafts" / "d.md").write_text("draft")
    assert agent_fingerprint(ag) == fp  # not an instruction file
    (ag / "CLAUDE.md").write_text("new rules")
    changed = agent_fingerprint(ag)
    assert changed != fp
    (ag / ".claude" / "skills" / "s" / "SKILL.md").write_text("new skill")
    assert agent_fingerprint(ag) != changed
    third = agent_fingerprint(ag)
    (ag / "sub" / "CLAUDE.md").write_text("x")
    assert agent_fingerprint(ag) != third


def test_fingerprint_depends_on_paths_and_ignores_secrets_and_skips(tmp_path):
    ag = instr_agent(tmp_path)
    fp = agent_fingerprint(ag)
    (ag / ".claude" / "settings.local.json").write_text("{secret}")
    (ag / ".claude" / ".env").write_text("KEY=1")
    (ag / ".claude" / "logo.png").write_bytes(b"\x89PNG")
    (ag / "node_modules").mkdir()
    (ag / "node_modules" / "CLAUDE.md").write_text("vendored")
    assert agent_fingerprint(ag) == fp
    (ag / "sub" / "CLAUDE.md").rename(ag / "sub" / "CLAUDE.local.md")  # a rename is a change; so is a local file
    assert agent_fingerprint(ag) != fp


def test_fingerprint_exclude_matches_workspace_copy(tmp_path):
    a, b = instr_agent(tmp_path / "a"), instr_agent(tmp_path / "b")
    (b / "sub" / "CLAUDE.md").write_text("different")
    assert agent_fingerprint(a) != agent_fingerprint(b)
    assert agent_fingerprint(a, exclude=["sub"]) == agent_fingerprint(b, exclude=["sub"])


def test_fingerprint_override_globs(tmp_path):
    ag = instr_agent(tmp_path)
    fp = agent_fingerprint(ag, ["status.md", "prompts/*.md"])
    assert fp != agent_fingerprint(ag)
    (ag / "CLAUDE.md").write_text("other")
    assert agent_fingerprint(ag, ["status.md", "prompts/*.md"]) == fp  # CLAUDE.md is no longer in the set
    (ag / "status.md").write_text("changed")
    assert agent_fingerprint(ag, ["status.md", "prompts/*.md"]) != fp
    (ag / ".env").write_text("K=1")
    assert agent_fingerprint(ag, [".env"]) == agent_fingerprint(ag, ["nothing-matches"])  # secrets stay out even if named


def test_fingerprint_suite_key_validation():
    s = inj_suite("/x", fingerprint=["a*.md"])
    assert validate_suite(s) == []
    assert any("`fingerprint` must be a list of glob strings" in e for e in validate_suite(inj_suite("/x", fingerprint="CLAUDE.md")))


def test_run_stores_fingerprint(env, agent):
    run_suite(env, agent, FakeClaude())
    fp = results_of(only_run(env))["agent_fingerprint"]
    assert fp == agent_fingerprint(agent)
    (agent / "CLAUDE.md").write_text("changed")
    run_suite(env, agent, FakeClaude())
    assert {results_of(d)["agent_fingerprint"] for d in run_dirs(env / "runs")} == {fp, agent_fingerprint(agent)}


# T3 / T4
def lines(env):
    return [json.loads(ln) for ln in (env / "runs" / "history.jsonl").read_text().splitlines()]


def test_iso_week_across_year_boundary():
    assert history.iso_week(datetime(2026, 12, 31)) == "2026-W53"
    assert history.iso_week(datetime(2027, 1, 4)) == "2027-W01"
    assert history.iso_week(datetime(2027, 1, 1)) == "2026-W53"
    assert history.iso_week(datetime(2026, 10, 22)) == "2026-W43"


def test_run_timestamp_from_name():
    ts = history.run_timestamp("20261231-235959-a-1234")
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second) == (2026, 12, 31, 23, 59, 59) and ts.utcoffset() is not None
    assert history.run_timestamp("garbage", fallback=1_000_000_000).year == 2001
    with pytest.raises(ValueError):
        history.run_timestamp("garbage")


def test_run_appends_history_line_per_task(env, agent):
    run_suite(env, agent, FakeClaude())
    (row,) = lines(env)
    d = only_run(env)
    assert row["schema"] == 1 and row["run"] == d.name and row["agent"] == "tiny-writer" and row["task"] == "prompt-injection"
    assert row["model"] == "m" and row["judge_model"] == "j" and row["claude_version"] == "2.1.306" and row["billing"] == "subscription"
    assert row["agent_fingerprint"] == results_of(d)["agent_fingerprint"] and row["n"] == 1
    assert row["passed"] is True and row["pass_rate"] == 1.0 and row["incomplete"] is False and row["run_ok"] is True
    assert row["judge_mean"] == 4.0 and row["turns"] == 2
    assert row["tokens"] == {"input": 4, "output": 394, "cache_read": 23081, "cache_creation": 4277}
    assert row["agent_cost_usd"] == 0.0234 and row["judge_cost_usd"] == 0.01
    assert row["latency"] == {"wall_s": results_of(d)["results"][0]["duration_s"], "api_s": 8.288, "ttft_s": 3.543}
    assert datetime.fromisoformat(row["timestamp"]).utcoffset() is not None
    assert row["iso_week"] == history.iso_week(datetime.fromisoformat(row["timestamp"]))
    run_suite(env, agent, FakeClaude())
    assert len(lines(env)) == 2


def test_rescore_replaces_run_lines(env, agent):
    sp = run_suite(env, agent, FakeClaude())
    run_suite(env, agent, FakeClaude())
    first, other = run_dirs(env / "runs")
    before = lines(env)
    sp.write_text(yaml.safe_dump(inj_suite(agent, checks=[{"id": "read-craft", "type": "read_files", "paths": ["craft.md"]}])))
    assert cli.main(["rescore", str(first), "--suite", str(sp)]) == 0
    after = lines(env)
    assert len(after) == 2 and sorted(r["run"] for r in after) == sorted([first.name, other.name])
    assert {r["run"]: r["passed"] for r in before}[first.name] is True and {r["run"]: r["passed"] for r in after}[first.name] is False
    assert [r for r in after if r["run"] == other.name] == [r for r in before if r["run"] == other.name]


def test_rescore_does_not_create_history_file(env, agent):
    sp = run_suite(env, agent, FakeClaude())
    (env / "runs" / "history.jsonl").unlink()
    assert cli.main(["rescore", str(only_run(env)), "--suite", str(sp)]) == 0
    assert not (env / "runs" / "history.jsonl").exists()


def test_history_failure_only_warns(env, agent, monkeypatch, capsys):
    monkeypatch.setattr(history, "update", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    run_suite(env, agent, FakeClaude())
    assert "warning: could not update history.jsonl: OSError: disk full" in capsys.readouterr().err
    assert (only_run(env) / "results.json").exists()


def test_history_update_keeps_foreign_lines_and_drops_garbage(env, agent, capsys):
    (env / "runs").mkdir()
    foreign = {"schema": 1, "run": "20250101-000000-x-aaaa", "timestamp": "2025-01-01T00:00:00+00:00", "task": "t"}
    (env / "runs" / "history.jsonl").write_text(json.dumps(foreign) + "\nnot json\n")
    run_suite(env, agent, FakeClaude())
    got = lines(env)
    assert got[0] == foreign and len(got) == 2 and "dropped unreadable line" in capsys.readouterr().err


def test_rebuild_is_idempotent_and_byte_identical(env, agent, capsys):
    run_suite(env, agent, FakeClaude())
    run_suite(env, agent, FakeClaude(judge_scores=((4, 4),)), "--repeat", "2")
    hist = env / "runs" / "history.jsonl"
    live = hist.read_bytes()
    hist.unlink()
    assert cli.main(["history", "rebuild"]) == 0
    assert "2 rows from 2 runs" in capsys.readouterr().out
    first = hist.read_bytes()
    assert cli.main(["history", "rebuild", "--runs-dir", str(env / "runs")]) == 0
    assert hist.read_bytes() == first == live  # what `run` wrote is what rebuild regenerates
    assert [r["run"] for r in lines(env)] == sorted(r["run"] for r in lines(env))


def test_rebuild_skips_corrupt_and_foreign_runs(env, agent, capsys):
    run_suite(env, agent, FakeClaude())
    (env / "runs" / "20250101-000000-bad-aaaa").mkdir()
    (env / "runs" / "20250101-000000-bad-aaaa" / "results.json").write_text("{not json")
    (env / "runs" / "20250101-000001-odd-bbbb").mkdir()
    (env / "runs" / "20250101-000001-odd-bbbb" / "results.json").write_text("[1]")
    (env / "runs" / "empty").mkdir()
    assert cli.main(["history", "rebuild"]) == 0
    cap = capsys.readouterr()
    assert "skipped 20250101-000000-bad-aaaa" in cap.err and "skipped 20250101-000001-odd-bbbb" in cap.err and "1 rows from 1 runs" in cap.out
    assert len(lines(env)) == 1
    assert cli.main(["history", "rebuild", "--runs-dir", "nope"]) == 2


def test_rebuild_fills_older_run_from_traces(env, agent):
    run_suite(env, agent, FakeClaude())
    d = only_run(env)
    data = results_of(d)
    for k in ("latency", "telemetry", "model_cost", "agent_cost_usd", "judge_cost_usd", "model", "judge_model"):
        data["results"][0].pop(k, None)
    data.pop("agent_fingerprint")
    (d / "results.json").write_text(json.dumps(data))
    assert cli.main(["history", "rebuild"]) == 0
    (row,) = lines(env)
    assert row["latency"]["api_s"] == 8.288 and row["agent_cost_usd"] == 0.0234 and row["model"] == "claude-sonnet-5-5"
    assert row["agent_fingerprint"] is None and row["judge_model"] is None
    (d / "traces" / "prompt-injection.jsonl").unlink()  # no trace either: missing, not zero
    assert cli.main(["history", "rebuild"]) == 0
    (row,) = lines(env)
    assert row["latency"]["api_s"] is None and row["latency"]["wall_s"] is not None and row["agent_cost_usd"] == 0.0234


def test_history_show(env, agent, capsys):
    run_suite(env, agent, FakeClaude())
    run_suite(env, agent, FakeClaude())
    capsys.readouterr()
    assert cli.main(["history", "show", "--last", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:2] == ["run", "task"] and len(out) == 2 and "prompt-injection" in out[1] and "pass" in out[1] and "$0.03" in out[1]
    assert "8.3s" in out[1] and "3.5s" in out[1]
    assert cli.main(["history", "show", "--agent", "other"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


# T5
def summary_of(*results):
    return {"agent": "a", "run": "r", "regressions": [], "results": list(results)}


def base_result(**kw):
    from test_cli import res
    return res("t", **kw)


def test_report_columns_with_and_without_data():
    with_data = base_result(cost_usd=0.07, agent_cost_usd=0.05, judge_cost_usd=0.02,
                            latency={"wall_s": 9.0, "api_s": 8.25, "ttft_s": 1.5, "min": {}, "max": {}})
    out = render(summary_of(with_data), {})
    assert "| Time | API | TTFT | Tokens in/out |" in out and "| 8.2s | 1.5s |" in out.replace("8.25s", "8.2s")
    assert "$0.05 agent + $0.02 judge" in out
    old = render(summary_of(base_result(cost_usd=0.07, judge={"scores": [], "mean": None, "tokens": 0, "cost_usd": 0.02})), {})
    assert "| — | — |" in old and "$0.05 agent + $0.02 judge" in old
    partial = render(summary_of(base_result(latency={"wall_s": 1.0, "api_s": None, "ttft_s": 2.0})), {})
    assert "| — | 2.0s |" in partial
    assert "$0.00 agent + $0.00 judge" in render(summary_of(), {})
