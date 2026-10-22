import json
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_evals import cli
from agent_evals.checks import Artifacts
from agent_evals.judge import judge
from agent_evals.report import compare, render
from agent_evals.runner import RunResult
from agent_evals.suite import load_suite, validate_suite
from fakes import run_dirs

SUITES = sorted((Path(__file__).parent.parent / "examples" / "suites").glob("*.yaml"))


def mk_suite(**over):
    s = {
        "agent": "a", "path": "/nonexistent", "model": "m", "judge_model": "j",
        "checks": [{"id": "no-x", "type": "regex_absent", "pattern": "XXX"}],
        "tasks": [
            {"id": "t1", "prompt": "p1", "rubric": ["r1", "r2"]},
            {"id": "t2", "prompt": "p2"},
        ],
    }
    s.update(over)
    return s


def write_suite(tmp_path, suite):
    p = tmp_path / "suite.yaml"
    p.write_text(yaml.safe_dump(suite))
    return p


def ok_run(task_id="t1", final="fine"):
    return RunResult(task_id, True, Artifacts(final=final), duration_s=1.0, num_turns=2)


def res(task, passed=True, run_ok=True, mean=None, **kw):
    return {"task": task, "title": task, "run_ok": run_ok, "error": "" if run_ok else "boom", "passed": passed,
            "checks": [], "checks_passed": 0, "checks_total": 0,
            "judge": {"scores": [], "mean": mean, "tokens": 0, "cost_usd": 0.0}, "cost_usd": 0.0, "tokens": {},
            "duration_s": 0, "turns": 0, "tools": [], "reads": [], "files_written": [], **kw}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/claude")
    return tmp_path


# F1
def test_compare_flags_run_error_vs_passing_baseline():
    out = compare([res("t", passed=False, run_ok=False)], {"t": {"passed": True, "run": "r1"}}, 0.75)
    assert out == ["t: passed in baseline (r1), run error now"]


def test_compare_ignores_run_error_when_baseline_failed_or_absent():
    assert compare([res("t", passed=False, run_ok=False)], {"t": {"passed": False}}, 0.75) == []
    assert compare([res("t", passed=False, run_ok=False)], {}, 0.75) == []


def test_compare_judge_drop_still_works():
    out = compare([res("t", mean=3.0)], {"t": {"passed": True, "mean": 4.0}}, 0.75)
    assert out == ["t: judge mean 4.0 → 3.0"]


# F2
def fake_claude(payload):
    def run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout=json.dumps({"structured_output": payload}), stderr="")
    return run


def test_judge_wrong_score_count_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_claude({"scores": [{"criterion": "a", "score": 4, "reason": "x"}]}))
    with pytest.raises(RuntimeError, match="1 scores for 2 criteria"):
        judge("p", ["a", "b"], "out", "m")


def test_judge_ok(monkeypatch):
    sc = [{"criterion": c, "score": 4, "reason": "x"} for c in "ab"]
    monkeypatch.setattr(subprocess, "run", fake_claude({"scores": sc}))
    assert judge("p", ["a", "b"], "out", "m")["mean"] == 4.0


def run_main(env, suite, *extra):
    return cli.main(["run", str(write_suite(env, suite)), *extra])


def load_results(env):
    (d,) = run_dirs(env / "runs")
    return json.loads((d / "results.json").read_text()), d


def test_judge_exception_makes_task_incomplete(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda *a: ok_run())
    def boom(*a, **k):
        raise RuntimeError("judge failed")
    monkeypatch.setattr(cli, "judge", boom)
    run_main(env, mk_suite(), "--task", "t1")
    r = load_results(env)[0]["results"][0]
    assert r["passed"] is False and r["incomplete"] is True and "judge failed" in r["incomplete_reason"]
    _, d = load_results(env)
    assert "⚠️ incomplete" in (d / "report.md").read_text()


def test_no_judge_can_pass_on_checks(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda *a: ok_run())
    monkeypatch.setattr(cli, "judge", lambda *a, **k: pytest.fail("judge must not run"))
    assert run_main(env, mk_suite(), "--task", "t1", "--no-judge") == 0
    r = load_results(env)[0]["results"][0]
    assert r["passed"] is True and "incomplete" not in r


def test_judge_ok_passes(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda *a: ok_run())
    monkeypatch.setattr(cli, "judge", lambda *a, **k: {"scores": [{"criterion": "a", "score": 4, "reason": "x"}] * 2, "mean": 4.0, "tokens": 0, "cost_usd": 0.0})
    run_main(env, mk_suite(), "--task", "t1")
    assert load_results(env)[0]["results"][0]["passed"] is True


def test_rescore_keeps_judge_error_incomplete(env, monkeypatch):
    sp = write_suite(env, mk_suite())
    rd = env / "runs" / "x"
    rd.mkdir(parents=True)
    r = res("t1", passed=False, judge={"scores": [], "mean": None, "error": "bad", "tokens": 0, "cost_usd": 0.0})
    (rd / "results.json").write_text(json.dumps({"agent": "a", "suite": str(sp), "run": "x", "results": [r], "regressions": []}))
    monkeypatch.setattr("agent_evals.runner.artifacts_from_saved", lambda *a: Artifacts(final="fine"))
    cli.rescore(rd, False)
    out = json.loads((rd / "results.json").read_text())["results"][0]
    assert out["passed"] is False and out["incomplete"] is True


# F3
def test_validate_catches_problems():
    s = mk_suite(checks=[{"id": "a", "type": "nope"}, {"id": "b", "type": "regex_absent"}, {"id": "c", "type": "regex_present", "pattern": "("},
                         {"id": "d", "type": "min_matches", "pattern": "x"}, {"type": "no_file_writes"}])
    s["tasks"] = [{"id": "t", "prompt": "p", "skip_checks": ["ghost"], "rubric": "str"}, {"id": "t", "prompt": "p"}, {"id": "u"}]
    errs = "\n".join(validate_suite(s))
    for needle in ("unknown type 'nope'", "requires `pattern`", "bad regex", "requires `min`", "missing id",
                   "duplicate task id", "unknown suite check `ghost`", "rubric must be a list", "missing prompt"):
        assert needle in errs, needle


def test_validate_top_level_keys():
    assert len(validate_suite({})) == 4
    assert validate_suite(mk_suite()) == []


def test_invalid_suite_returns_2_and_no_run_dir(env):
    assert run_main(env, mk_suite(checks=[{"id": "a", "type": "nope"}])) == 2
    assert not (env / "runs").exists()


def test_unknown_task_filter_returns_2(env, capsys):
    assert run_main(env, mk_suite(), "--task", "t1", "--task", "zzz") == 2
    assert "zzz" in capsys.readouterr().err and not (env / "runs").exists()


def test_missing_claude_returns_2(env, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    assert run_main(env, mk_suite()) == 2
    assert not (env / "runs").exists()


def test_task_exception_isolated_and_results_written(env, monkeypatch):
    def rt(agent_dir, t, *a):
        if t["id"] == "t1":
            raise FileNotFoundError("claude gone")
        return ok_run("t2")
    monkeypatch.setattr(cli, "run_task", rt)
    run_main(env, mk_suite())
    rs = load_results(env)[0]["results"]
    assert [r["task"] for r in rs] == ["t1", "t2"]
    assert rs[0]["run_ok"] is False and rs[0]["passed"] is False and rs[0]["error"].startswith("FileNotFoundError: claude gone")
    assert rs[1]["passed"] is True


def test_keyboard_interrupt_still_writes_results(env, monkeypatch):
    def rt(agent_dir, t, *a):
        if t["id"] == "t2":
            raise KeyboardInterrupt
        return ok_run()
    monkeypatch.setattr(cli, "run_task", rt)
    monkeypatch.setattr(cli, "judge", lambda *a, **k: {"scores": [{"criterion": "a", "score": 4, "reason": "x"}] * 2, "mean": 4.0, "tokens": 0, "cost_usd": 0.0})
    with pytest.raises(KeyboardInterrupt):
        run_main(env, mk_suite())
    rs, d = load_results(env)
    assert [r["task"] for r in rs["results"]] == ["t1"] and (d / "report.md").exists()


def test_all_crashed_gate_fails(env, monkeypatch):
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text(json.dumps({"t2": {"passed": True, "mean": None, "run": "old"}}))
    monkeypatch.setattr(cli, "run_task", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert run_main(env, mk_suite(), "--gate") == 1


# F4
def test_merge_baseline_preserves_mean_and_skips_incomplete():
    base = {"t1": {"passed": True, "mean": 4.2, "run": "old"}, "t2": {"passed": True, "mean": 4.0, "run": "old"}}
    new = [res("t1", passed=True, mean=None), res("t2", passed=False, incomplete=True), res("t3", mean=3.0), res("t4", run_ok=False)]
    m = cli._merge_baseline(base, new, "newrun")
    assert m["t1"] == {"passed": True, "mean": 4.2, "run": "newrun"}
    assert m["t2"]["run"] == "old"
    assert m["t3"]["mean"] == 3.0 and "t4" not in m


def test_update_baseline_via_main_keeps_mean(env, monkeypatch):
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text(json.dumps({"t2": {"passed": True, "mean": 4.5, "run": "old"}}))
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"]))
    run_main(env, mk_suite(), "--task", "t2", "--update-baseline")
    assert json.loads((env / "baselines" / "a.json").read_text())["t2"]["mean"] == 4.5


# F9
def test_run_dir_names_do_not_collide():
    assert len({cli._run_dir_name("a") for _ in range(20)}) > 1


# suites
@pytest.mark.parametrize("path", SUITES, ids=lambda p: p.name)
def test_example_suites_validate(path):
    assert validate_suite(load_suite(path)) == []


def test_render_incomplete_reason():
    summary = {"agent": "a", "run": "r", "regressions": [], "results": [res("t", passed=False, incomplete=True, incomplete_reason="judge failed: x")]}
    out = render(summary, {})
    assert "⚠️ incomplete" in out and "judge failed: x" in out
