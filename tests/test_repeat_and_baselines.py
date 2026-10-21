"""--repeat, aggregation, baselines and the regression gate."""
import json
import shutil
import subprocess

import pytest
import yaml

from agent_evals import cli, runner
from agent_evals.checks import Artifacts
from agent_evals.report import compare, render
from agent_evals.suite import load_suite, task_hash
from fakes import FakeClaude, inj_suite, only_run, results_of
from test_cli import mk_suite, ok_run, res, write_suite


# F20: --repeat
def repeat_run(env, agent, monkeypatch, *args, finals=(None, "seamless copy", None), judge=((4, 4), (3, 3), (5, 5)), **over):
    fake = FakeClaude(finals=finals, judge_scores=judge)
    monkeypatch.setattr(subprocess, "run", fake)
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent, **over)))
    code = cli.main(["run", str(sp), "--repeat", "3", *args])
    return code, fake, sp


def test_repeat_three_mixed_samples_aggregate(env, agent, monkeypatch):
    code, fake, _ = repeat_run(env, agent, monkeypatch)
    assert code == 0 and len(fake.agent_calls) == 3
    d = only_run(env)
    r = results_of(d)["results"][0]
    assert r["n"] == 3 and r["pass_rate"] == 0.67 and r["passed"] is True  # 2/3 >= default min_pass_rate 0.67
    assert r["judge"]["mean"] == 4.0 and r["judge"]["stdev"] == 0.82  # population stdev of 4, 3, 5
    assert [s["passed"] for s in r["samples"]] == [True, False, True]
    assert [s["judge"]["mean"] for s in r["samples"]] == [4.0, 3.0, 5.0]
    assert r["cost_usd"] == pytest.approx(sum(s["cost_usd"] for s in r["samples"]), abs=1e-3)
    assert r["turns"] == sum(s["turns"] for s in r["samples"]) and r["run_ok"] is True and "incomplete" not in r
    assert r["judge"]["cost_usd"] == pytest.approx(0.03) and r["judge"]["tokens"] == 45
    assert not r["checks_passed"] == r["checks_total"]  # the representative sample is the failing one
    for i in (1, 2, 3):
        assert (d / "traces" / f"prompt-injection.r{i}.jsonl").exists()
        assert (d / "workspaces" / f"prompt-injection.r{i}" / "tiny-writer" / "status.md").exists()
        assert (d / "workspaces" / f"prompt-injection.r{i}.snapshot.json").exists()
    assert not (d / "traces" / "prompt-injection.jsonl").exists()
    assert len({c["cwd"] for c in fake.agent_calls}) == 3
    rep = (d / "report.md").read_text()
    assert "✅ pass (2/3)" in rep and "4.0 ± 0.82" in rep


def test_repeat_min_pass_rate_and_suite_key(env, agent, monkeypatch):
    code, _, _ = repeat_run(env, agent, monkeypatch, min_pass_rate=1.0)
    assert results_of(only_run(env))["results"][0]["passed"] is False
    shutil.rmtree(env / "runs")
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s2.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent, repeat=2)))
    cli.main(["run", str(sp)])
    assert results_of(only_run(env))["results"][0]["n"] == 2
    shutil.rmtree(env / "runs")
    cli.main(["run", str(sp), "--repeat", "1"])  # CLI wins over the suite key
    r = results_of(only_run(env))["results"][0]
    assert "n" not in r and "samples" not in r


def test_repeat_one_keeps_todays_layout(env, agent, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp)])
    d = only_run(env)
    assert (d / "traces" / "prompt-injection.jsonl").exists() and (d / "workspaces" / "prompt-injection" / "tiny-writer").is_dir()
    assert (d / "workspaces" / "prompt-injection.snapshot.json").exists()
    assert "samples" not in results_of(d)["results"][0]


def test_repeat_invalid_cli_value(env, agent, capsys):
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    assert cli.main(["run", str(sp), "--repeat", "0"]) == 2
    assert "--repeat" in capsys.readouterr().err and not (env / "runs").exists()


def test_repeat_sample_exception_is_isolated(env, agent, monkeypatch):
    calls = []

    def rt(agent_dir, t, cfg, run_dir):
        calls.append(t["_name"])
        if len(calls) == 2:
            raise RuntimeError("boom")
        return runner.RunResult(t["id"], True, Artifacts(final="ok", reads=["status.md"]))
    monkeypatch.setattr(cli, "run_task", rt)
    monkeypatch.setattr(cli, "judge", lambda *a, **k: {"scores": [], "mean": None, "tokens": 0, "cost_usd": 0.0})
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent, tasks=[{"id": "t", "prompt": "p"}])))
    cli.main(["run", str(sp), "--repeat", "3"])
    r = results_of(only_run(env))["results"][0]
    assert calls == ["t.r1", "t.r2", "t.r3"] and r["run_ok"] and r["pass_rate"] == 0.67
    assert r["samples"][1]["error"].startswith("RuntimeError: boom") and r["error"].startswith("RuntimeError: boom")


def test_aggregate_run_ok_incomplete_and_all_failed():
    a = res("t", mean=4.0, n=1)
    b = res("t", passed=False, run_ok=False)
    agg = cli._aggregate([a, b], 0.5)
    assert agg["run_ok"] is True and agg["passed"] is True and agg["judge"]["mean"] == 4.0 and agg["judge"]["stdev"] == 0.0
    inc = res("t", passed=False, incomplete=True, incomplete_reason="judge failed: x")
    agg = cli._aggregate([res("t"), inc], 0.5)
    assert agg["incomplete"] is True and agg["incomplete_reason"] == "judge failed: x"
    agg = cli._aggregate([b, res("t", passed=False, run_ok=False)], 0.67)
    assert agg["run_ok"] is False and agg["passed"] is False and agg["judge"]["mean"] is None and agg["judge"]["stdev"] is None


def test_compare_pass_rate_regression():
    def agg(rate, n=3):
        return res("t", pass_rate=rate, n=n, passed=rate >= 0.67)
    base = {"t": {"passed": True, "mean": None, "run": "r0", "pass_rate": 1.0, "n": 3}}
    assert compare([agg(0.67)], base, 0.75) == []  # 1.0 - 0.67 = 0.33 < 0.34
    assert compare([agg(0.33)], base, 0.75) == ["t: passed in baseline (r0), fails now", "t: pass rate 1.0 → 0.33"]
    assert compare([agg(0.67)], base, 0.75, pass_drop=0.3) == ["t: pass rate 1.0 → 0.67"]
    # only when both sides were repeated runs
    assert compare([res("t", pass_rate=0.0, n=1, passed=False)], base, 0.75) == ["t: passed in baseline (r0), fails now"]
    single = {"t": {"passed": True, "mean": None, "run": "r0"}}
    assert compare([agg(0.33)], single, 0.75) == ["t: passed in baseline (r0), fails now"]


def test_repeat_regression_via_main_and_gate(env, agent, monkeypatch):
    (env / "baselines").mkdir()
    (env / "baselines" / "tiny-writer.json").write_text(json.dumps(
        {"prompt-injection": {"passed": True, "mean": 4.0, "run": "old", "pass_rate": 1.0, "n": 3}}))
    code, _, _ = repeat_run(env, agent, monkeypatch, "--gate", finals=("seamless", "seamless", None), judge=((4, 4),))
    assert code == 1
    assert any("pass rate 1.0 → 0.33" in x for x in results_of(only_run(env))["regressions"])


def test_rescore_multi_sample_recomputes_and_reaggregates(env, agent, monkeypatch, capsys):
    _, _, sp = repeat_run(env, agent, monkeypatch)
    d = only_run(env)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("rescore must not call claude"))
    # drop the failing check: all 3 samples now pass the rules; judge means (4, 3, 5) are kept
    sp.write_text(yaml.safe_dump(inj_suite(agent, checks=[{"id": "ritual-read", "type": "read_files", "paths": ["status.md"]}])))
    assert cli.main(["rescore", str(d), "--suite", str(sp)]) == 0
    r = results_of(d)["results"][0]
    assert r["pass_rate"] == 0.67 and [s["passed"] for s in r["samples"]] == [True, False, True]  # sample 2's judge mean 3.0 < 3.5
    assert r["judge"]["mean"] == 4.0 and r["checks_total"] == 3
    out = capsys.readouterr().out
    assert "prompt-injection.r2" in out and "pass rate 0.67 (3 samples)" in out
    # tighten the rules instead: every sample fails
    sp.write_text(yaml.safe_dump(inj_suite(agent, checks=[{"id": "read-craft", "type": "read_files", "paths": ["craft.md"]}])))
    cli.main(["rescore", str(d), "--suite", str(sp)])
    r = results_of(d)["results"][0]
    assert r["pass_rate"] == 0.0 and r["passed"] is False and r["n"] == 3


# F21: baseline metadata
def test_baseline_records_producer_and_old_baselines_do_not_warn(env, agent, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    suite = inj_suite(agent)
    sp.write_text(yaml.safe_dump(suite))
    (env / "baselines").mkdir()
    old = {"prompt-injection": {"passed": True, "mean": 4.0, "run": "old"}}  # no metadata fields
    (env / "baselines" / "tiny-writer.json").write_text(json.dumps(old))
    assert cli.main(["run", str(sp), "--update-baseline"]) == 0
    assert "WARN" not in capsys.readouterr().out
    b = json.loads((env / "baselines" / "tiny-writer.json").read_text())["prompt-injection"]
    t = suite["tasks"][0]
    assert b["model"] == "m" and b["judge_model"] == "j" and b["claude_version"] == "2.1.306"
    assert b["task_hash"] == task_hash(load_suite(sp) | {"billing": "subscription"}, t) and len(b["task_hash"]) == 12
    assert "n" not in b and "pass_rate" not in b


def test_baseline_mismatch_warns_reports_and_does_not_gate(env, agent, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp), "--update-baseline"])
    shutil.rmtree(env / "runs")
    capsys.readouterr()
    changed = inj_suite(agent, model="m2", judge_model="j2")
    changed["tasks"][0]["prompt"] = "write different copy"
    sp.write_text(yaml.safe_dump(changed))
    assert cli.main(["run", str(sp), "--gate"]) == 0  # warns, never fails
    out = capsys.readouterr().out
    assert "WARN  baseline mismatch" in out
    assert "prompt-injection: model m → m2" in out and "judge_model j → j2" in out and "task_hash" in out
    d = only_run(env)
    assert len(results_of(d)["baseline_mismatch"]) == 3  # same claude version
    rep = (d / "report.md").read_text()
    assert "## ⚠️ Baseline mismatch" in rep and "model m → m2" in rep
    # rescore warns too
    capsys.readouterr()
    assert cli.main(["rescore", str(d), "--suite", str(sp)]) == 0
    assert "WARN  baseline mismatch" in capsys.readouterr().out


def test_baseline_mismatch_claude_version_and_no_warning_when_identical(env, agent, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp), "--update-baseline"])
    assert "WARN" not in capsys.readouterr().out
    cli.main(["run", str(sp)])
    assert "WARN" not in capsys.readouterr().out
    base = env / "baselines" / "tiny-writer.json"
    b = json.loads(base.read_text())
    b["prompt-injection"]["claude_version"] = "2.0.0"
    base.write_text(json.dumps(b))
    cli.main(["run", str(sp)])
    assert "claude_version 2.0.0 → 2.1.306" in capsys.readouterr().out


def test_baseline_stores_repeat_stats(env, agent, monkeypatch):
    repeat_run(env, agent, monkeypatch, "--update-baseline")
    b = json.loads((env / "baselines" / "tiny-writer.json").read_text())["prompt-injection"]
    assert b["pass_rate"] == 0.67 and b["n"] == 3 and b["stdev"] == 0.82 and b["mean"] == 4.0


def test_merge_baseline_without_suite_is_unchanged():
    m = cli._merge_baseline({}, [res("t", mean=3.0)], "r")
    assert m == {"t": {"passed": True, "mean": 3.0, "run": "r"}}


def test_render_without_repeat_unchanged_shape():
    out = render({"agent": "a", "run": "r", "regressions": [], "results": [res("t", mean=4.0)]}, {})
    assert "| 4.0 |" in out and "±" not in out and "Baseline mismatch" not in out and "traces/t.jsonl" in out


# N3: a stored judge mean is carried over only for the same task
def _stored(**over):
    return {"passed": True, "mean": 4.2, "run": "old", **over}


def _merge(stored, **task_over):
    suite = mk_suite()
    suite["tasks"][0].update(task_over)
    new_hash = task_hash(suite, suite["tasks"][0])
    return cli._merge_baseline({"t1": stored(new_hash) if callable(stored) else stored}, [res("t1", mean=None)], "r2", suite)


def test_merge_no_judge_carries_mean_when_task_unchanged(capsys):
    merged = _merge(lambda h: _stored(task_hash=h))
    assert merged["t1"]["mean"] == 4.2 and merged["t1"]["run"] == "r2" and merged["t1"]["task_hash"]
    assert capsys.readouterr().out == ""


def test_merge_no_judge_carries_mean_for_legacy_entry_without_hash():
    assert _merge(_stored())["t1"]["mean"] == 4.2


def test_merge_no_judge_keeps_entry_and_warns_when_task_changed(capsys):
    stored = _stored(task_hash="aaaaaaaaaaaa", model="old-model")
    merged = _merge(stored)
    assert merged["t1"] == stored  # untouched: no fresh metadata stamped over a stale mean
    assert capsys.readouterr().out.strip() == "baseline for t1 kept: judge was skipped and the task changed; rerun with the judge to update it"


def test_merge_no_judge_task_without_rubric_is_written_fresh(capsys):
    suite = mk_suite()
    entry = cli._merge_baseline({"t2": _stored(task_hash="aaaaaaaaaaaa")}, [res("t2", mean=None)], "r2", suite)["t2"]
    assert entry["mean"] is None and entry["run"] == "r2"  # the rubric is gone: the old mean no longer describes this task
    assert "kept" not in capsys.readouterr().out


def test_merge_with_judge_mean_overwrites_regardless_of_hash():
    suite = mk_suite()
    merged = cli._merge_baseline({"t1": _stored(task_hash="aaaaaaaaaaaa")}, [res("t1", mean=3.0)], "r2", suite)
    assert merged["t1"]["mean"] == 3.0 and merged["t1"]["run"] == "r2"


def test_no_judge_run_after_task_change_leaves_baseline_alone(env, agent, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp), "--update-baseline"])
    base = env / "baselines" / "tiny-writer.json"
    before = base.read_text()
    changed = inj_suite(agent)
    changed["tasks"][0]["prompt"] = "write different copy"
    sp.write_text(yaml.safe_dump(changed))
    capsys.readouterr()
    assert cli.main(["run", str(sp), "--no-judge", "--update-baseline"]) == 0
    out = capsys.readouterr().out
    assert "baseline for prompt-injection kept: judge was skipped and the task changed" in out and "baseline updated" in out
    assert base.read_text() == before
    sp.write_text(yaml.safe_dump(inj_suite(agent)))  # unchanged task: the mean is carried, F4 still holds
    cli.main(["run", str(sp), "--no-judge", "--update-baseline"])
    assert json.loads(base.read_text())["prompt-injection"]["mean"] == 4.0


# N4: --gate needs something to gate against
def gate_env(env, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: calls.append(t["id"]) or ok_run(t["id"]))
    monkeypatch.setattr(cli, "judge", lambda *a, **k: {"scores": [{"criterion": "r", "score": 4, "reason": "x"}] * 2, "mean": 4.0, "tokens": 0, "cost_usd": 0.0})
    return write_suite(env, mk_suite()), calls


def test_gate_without_baseline_exits_2_before_any_task_runs(env, monkeypatch, capsys):
    sp, calls = gate_env(env, monkeypatch)
    assert cli.main(["run", str(sp), "--gate"]) == 2
    err = capsys.readouterr().err
    assert err.strip() == "error: --gate needs a baseline: baselines/a.json not found (create one with --update-baseline)"
    assert calls == [] and not (env / "runs").exists()


def test_gate_without_baseline_honours_baselines_dir(env, monkeypatch, capsys):
    sp, calls = gate_env(env, monkeypatch)
    assert cli.main(["run", str(sp), "--gate", "--baselines-dir", str(env / "B")]) == 2
    assert str(env / "B" / "a.json") in capsys.readouterr().err and calls == []


def test_gate_with_update_baseline_and_no_file_runs_and_creates_it(env, monkeypatch, capsys):
    sp, calls = gate_env(env, monkeypatch)
    assert cli.main(["run", str(sp), "--gate", "--update-baseline"]) == 0
    out = capsys.readouterr().out
    assert "note: no baseline at baselines/a.json" in out and "baseline updated" in out
    assert calls == ["t1", "t2"] and (env / "baselines" / "a.json").exists()
    assert cli.main(["run", str(sp), "--gate"]) == 0  # now there is one


def test_gate_warns_when_baseline_has_no_entry_for_the_tasks_run(env, monkeypatch, capsys):
    sp, _ = gate_env(env, monkeypatch)
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text(json.dumps({"other": {"passed": True, "mean": None, "run": "r"}}))
    assert cli.main(["run", str(sp), "--gate"]) == 0
    assert "warning: --gate: baselines/a.json has no entry for any task being run" in capsys.readouterr().out
    (env / "baselines" / "a.json").write_text("{}")
    cli.main(["run", str(sp), "--gate"])
    assert "no entry for any task" in capsys.readouterr().out


def test_gate_is_quiet_when_some_task_has_an_entry_and_non_gate_runs_never_warn(env, monkeypatch, capsys):
    sp, _ = gate_env(env, monkeypatch)
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text(json.dumps({"t2": {"passed": True, "mean": None, "run": "r"}}))
    assert cli.main(["run", str(sp), "--gate"]) == 0
    assert "warning" not in capsys.readouterr().out
    (env / "baselines" / "a.json").write_text("{}")
    assert cli.main(["run", str(sp)]) == 0
    assert "warning" not in capsys.readouterr().out


def test_gate_regression_still_exits_1(env, monkeypatch):
    sp, _ = gate_env(env, monkeypatch)
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text(json.dumps({"t1": {"passed": True, "mean": 4.0, "run": "r"}}))
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"], final="XXX"))  # trips the suite's no-x check
    monkeypatch.setattr(cli, "run_checks", lambda *a: [__import__("agent_evals.checks", fromlist=["CheckResult"]).CheckResult("no-x", False, "found")])
    assert cli.main(["run", str(sp), "--gate"]) == 1


def test_unreadable_baseline_exits_2_before_running(env, monkeypatch, capsys):
    sp, calls = gate_env(env, monkeypatch)
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text("{not json")
    assert cli.main(["run", str(sp)]) == 2
    assert capsys.readouterr().err.startswith("error: cannot read baseline") and calls == []
    (env / "baselines" / "a.json").write_text("[1]")
    assert cli.main(["run", str(sp)]) == 2
    assert "must be a JSON object" in capsys.readouterr().err


# N11: a nested --baselines-dir is created
def test_update_baseline_creates_nested_baselines_dir(env, monkeypatch):
    sp, _ = gate_env(env, monkeypatch)
    deep = env / "ci" / "evals" / "baselines"
    assert cli.main(["run", str(sp), "--update-baseline", "--baselines-dir", str(deep)]) == 0
    assert (deep / "a.json").exists()
    monkeypatch.setattr("agent_evals.runner.artifacts_from_saved", lambda *a: Artifacts(final="fine"))
    rd = next((env / "runs").iterdir())
    deeper = env / "x" / "y" / "z"
    assert cli.main(["rescore", str(rd), "--suite", str(sp), "--update-baseline", "--baselines-dir", str(deeper)]) == 0
    assert (deeper / "a.json").exists()


# N1: the results are written even when the comparison blows up
def test_results_and_report_written_even_if_compare_raises(env, monkeypatch, capsys):
    sp, _ = gate_env(env, monkeypatch)

    def boom(*a):
        raise TypeError("'>=' not supported")
    monkeypatch.setattr(cli, "compare", boom)
    assert cli.main(["run", str(sp)]) == 0
    d = only_run(env)
    assert (d / "results.json").exists() and (d / "report.md").exists()
    assert len(results_of(d)["results"]) == 2 and results_of(d)["regressions"] == []
    assert "warning: could not compare with the baseline: TypeError" in capsys.readouterr().err
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text("{}")
    assert cli.main(["run", str(sp), "--gate"]) == 2  # a gate that couldn't compare must not pass
    assert len(list((env / "runs").iterdir())) == 2


def test_results_written_even_if_mismatch_warning_raises(env, monkeypatch):
    sp, _ = gate_env(env, monkeypatch)
    monkeypatch.setattr(cli, "_warn_mismatch", lambda *a: 1 / 0)
    assert cli.main(["run", str(sp)]) == 0
    assert (only_run(env) / "report.md").exists()


# N6: an unjudged sample makes a repeated task incomplete, never a pass
def test_aggregate_with_an_incomplete_sample_is_incomplete_not_passed():
    inc = res("t", passed=False, incomplete=True, incomplete_reason="judge failed: x")
    out = cli._aggregate([res("t"), res("t"), inc], 0.67)
    assert out["pass_rate"] == 0.67 and out["passed"] is False
    assert out["incomplete"] is True and out["incomplete_reason"] == "judge failed: x"
    assert cli._aggregate([res("t"), res("t"), res("t")], 0.67)["passed"] is True
    assert "incomplete" not in cli._aggregate([res("t"), res("t", passed=False), res("t")], 0.67)


def test_repeat_with_unjudged_sample_prints_report_and_baselines_consistently(env, agent, monkeypatch, capsys):
    code, _, sp = repeat_run(env, agent, monkeypatch, "--update-baseline", judge=((4, 4), (4,), (4, 4)), finals=(None,))
    assert code == 0
    out = capsys.readouterr().out
    assert "  INCOMPLETE pass rate 0.67 (3 samples)" in out and "  PASS" not in out
    d = only_run(env)
    r = results_of(d)["results"][0]
    assert r["passed"] is False and r["incomplete"] is True and "1 scores for 2 criteria" in r["incomplete_reason"]
    rep = (d / "report.md").read_text()
    assert "⚠️ incomplete (2/3)" in rep and "✅" not in rep and "Incomplete:" in rep and "0/1 tasks passed" in rep
    assert not (env / "baselines" / "tiny-writer.json").read_text().strip("{}\n ")  # incomplete tasks are never baselined
    capsys.readouterr()
    cli.main(["rescore", str(d), "--suite", str(sp)])  # the rescore path agrees
    assert "INCOMPLETE  prompt-injection  pass rate 0.67" in capsys.readouterr().out
    r2 = results_of(d)["results"][0]
    assert r2["passed"] is False and r2["incomplete"] is True


# N12: a big --repeat says what it will cost
def test_repeat_above_five_prints_run_count_note(env, monkeypatch, capsys):
    sp, calls = gate_env(env, monkeypatch)
    cli.main(["run", str(sp), "--repeat", "6"])
    err = capsys.readouterr().err
    assert "note: 6 samples × 2 tasks = 12 agent runs plus 6 judge runs" in err  # only t1 has a rubric
    assert len(calls) == 12
    cli.main(["run", str(sp), "--repeat", "6", "--no-judge"])
    assert "note: 6 samples × 2 tasks = 12 agent runs" in (e := capsys.readouterr().err) and "judge" not in e
    cli.main(["run", str(sp), "--repeat", "5"])
    assert "note:" not in capsys.readouterr().err
    cli.main(["run", str(sp), "--repeat", "7", "--task", "t1"])
    assert "note: 7 samples × 1 tasks = 7 agent runs plus 7 judge runs" in capsys.readouterr().err


# N9
def test_version_flag_and_dunder(capsys):
    from importlib.metadata import version

    import agent_evals
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"agent-evals {agent_evals.__version__}"
    assert agent_evals.__version__ == version("wm-agent-evals")
