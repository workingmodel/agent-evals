"""Sprint 4: workspaces outside any repo, loaded-environment checks, --repeat, baseline metadata, fixture-based runs."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from agent_evals import cli, runner
from agent_evals.checks import Artifacts, run_check
from agent_evals.report import compare, render
from agent_evals.suite import task_checks, task_hash, validate_suite
from test_cli import mk_suite, res

FIXTURE = Path(__file__).parent / "fixtures" / "trace-prompt-injection.jsonl"
FIXTURE_WS = "/work/runs/RUN/workspaces/prompt-injection/tiny-writer"  # workspace path baked into the fixture


def fixture_events(ws: str = FIXTURE_WS, final: str | None = None) -> list[dict]:
    """The real recorded trace, with its workspace prefix pointed at `ws` (what claude would have reported there)."""
    events = [json.loads(ln) for ln in FIXTURE.read_text().replace(FIXTURE_WS, ws).splitlines() if ln.strip()]
    if final is not None:
        next(e for e in events if e["type"] == "result")["result"] = final
    return events


def stdout_of(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def scores(*vals):
    sc = [{"criterion": f"c{i}", "score": v, "reason": "x"} for i, v in enumerate(vals)]
    return json.dumps({"structured_output": {"scores": sc}, "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    return tmp_path


@pytest.fixture
def agent(env):
    ag = env / "agents" / "tiny-writer"
    ag.mkdir(parents=True)
    (ag / "CLAUDE.md").write_text("You write copy.")
    (ag / "status.md").write_text("# Status")
    return ag


def inj_suite(agent, **over):
    s = {
        "agent": "tiny-writer", "path": str(agent), "model": "m", "judge_model": "j",
        "checks": [{"id": "no-seamless", "type": "words_absent", "scope": "final", "words": ["seamless"]},
                   {"id": "ritual-read", "type": "read_files", "paths": ["status.md"]}],
        "tasks": [{"id": "prompt-injection", "prompt": "write copy", "rubric": ["a", "b"]}],
    }
    s.update(over)
    return s


class FakeClaude:
    """Stands in for `claude` via subprocess.run: stream-json for the agent, a fake verdict for the judge."""

    def __init__(self, finals=(None,), judge_scores=((4, 4),), memory_auto=None, create_memory=False):
        self.finals, self.judge_scores, self.memory_auto, self.create_memory = list(finals), list(judge_scores), memory_auto, create_memory
        self.agent_calls: list[dict] = []
        self.judge_calls = 0

    def __call__(self, cmd, **kw):
        if "--json-schema" in cmd:  # the judge
            sc = self.judge_scores[min(self.judge_calls, len(self.judge_scores) - 1)]
            self.judge_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout=scores(*sc), stderr="")
        cwd = str(kw["cwd"])
        final = self.finals[min(len(self.agent_calls), len(self.finals) - 1)]
        events = fixture_events(cwd, final)
        if self.memory_auto:
            events[0]["memory_paths"] = {"auto": self.memory_auto}
            if self.create_memory:
                Path(self.memory_auto).mkdir(parents=True)
        self.agent_calls.append({"cwd": cwd, "cmd": cmd})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout_of(events), stderr="")


def only_run(env) -> Path:
    (d,) = (env / "runs").iterdir()
    return d


def results_of(d: Path) -> dict:
    return json.loads((d / "results.json").read_text())


# F18: parsing the real trace
def test_parse_trace_on_fixture_rebased(tmp_path):
    ws = tmp_path / "ws" / "tiny-writer"
    ws.mkdir(parents=True)
    events = runner._rebase(fixture_events(), FIXTURE_WS, str(ws))
    reads, tools, outside = runner.parse_trace(events, ws)
    assert reads == ["status.md"] and tools == ["Read"] and outside == []
    # unrebased, the recorded paths are outside the new workspace
    assert runner.parse_trace(fixture_events(), ws)[2]


def test_parse_env_from_init_event():
    assert runner.parse_env(fixture_events()) == {
        "claude_code_version": "2.1.306", "model": "claude-sonnet-5-5", "apiKeySource": "none", "mcp_servers": [],
        "plugins": [], "memory_paths": {"auto": "/home/user/.claude/projects/-work/memory/"}, "cwd": FIXTURE_WS,
    }
    assert runner.parse_env([{"type": "result"}]) == {}


def test_parse_env_keeps_only_non_builtin_plugins_and_mcp_names():
    init = {"type": "system", "subtype": "init", "mcp_servers": [{"name": "figma", "status": "connected"}],
            "plugins": [{"name": "a", "path": "builtin"}, {"name": "b", "path": "/home/x/b"}]}
    env = runner.parse_env([init])
    assert env["plugins"] == ["b"] and env["mcp_servers"] == ["figma"]


def env_art(**over):
    base = runner.parse_env(fixture_events())
    return Artifacts(final="", env={**base, **over})


CLEAN = {"id": "clean-env", "type": "clean_env"}


def test_clean_env_variants():
    assert run_check(CLEAN, env_art(), None).passed
    r = run_check(CLEAN, env_art(plugins=["sketchy"]), None)
    assert not r.passed and "sketchy" in r.detail
    r = run_check(CLEAN, env_art(mcp_servers=["figma"]), None)
    assert not r.passed and "figma" in r.detail
    r = run_check(CLEAN, env_art(apiKeySource="ANTHROPIC_API_KEY"), None)
    assert not r.passed and "ANTHROPIC_API_KEY" in r.detail
    # the key is expected when the run was billed to the API
    assert run_check({**CLEAN, "billing": "api"}, env_art(apiKeySource="ANTHROPIC_API_KEY"), None).passed
    assert not run_check({**CLEAN, "billing": "api"}, env_art(mcp_servers=["figma"]), None).passed


def test_clean_env_without_init_event_passes():
    r = run_check(CLEAN, Artifacts(final=""), None)
    assert r.passed and r.detail == "no init event"


def test_clean_env_survives_file_pattern_filter():
    c = {**CLEAN, "file_pattern": "^x"}
    assert not run_check(c, env_art(mcp_servers=["figma"]), None).passed


def test_clean_env_builtin_check_and_opt_outs():
    s = mk_suite()
    t = s["tasks"][0]
    cs = task_checks(s, t)
    assert [c["id"] for c in cs][-2:] == ["clean-env", "paths-confined"] and cs[-2]["billing"] == "subscription"
    assert task_checks({**s, "billing": "api"}, t)[-2]["billing"] == "api"
    assert not any(c["id"] == "clean-env" for c in task_checks({**s, "check_env": False}, t))
    assert not any(c["id"] == "clean-env" for c in task_checks(s, {**t, "skip_checks": ["clean-env"]}))
    assert validate_suite(mk_suite(check_env=False, tasks=[{"id": "t", "prompt": "p", "skip_checks": ["clean-env"]}])) == []
    assert validate_suite(mk_suite(check_env="no"))


def test_validate_repeat_and_thresholds():
    assert validate_suite(mk_suite(repeat=3, min_pass_rate=0.5, regression_pass_drop=1)) == []
    for bad in ({"repeat": 0}, {"repeat": 1.5}, {"repeat": True}, {"min_pass_rate": 1.5}, {"min_pass_rate": "x"},
                {"regression_pass_drop": -0.1}, {"regression_pass_drop": True}):
        assert validate_suite(mk_suite(**bad)), bad


# F23: workspace in a temp dir outside any repo
def test_workspace_runs_in_temp_dir_then_moves_into_run_dir(env, agent, monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(subprocess, "run", fake)
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    assert cli.main(["run", str(sp)]) == 0
    d = only_run(env)
    cwd = Path(fake.agent_calls[0]["cwd"])
    tmp_root = Path(os.path.realpath(tempfile.gettempdir()))
    assert cwd.name == "tiny-writer" and cwd.parent.name.startswith("agent-evals-")
    assert tmp_root in cwd.parents and d.resolve() not in cwd.parents and cwd == Path(os.path.realpath(cwd))
    assert not cwd.parent.exists()  # temp dir removed
    assert (d / "workspaces" / "prompt-injection" / "tiny-writer" / "status.md").exists()
    snap = json.loads((d / "workspaces" / "prompt-injection.snapshot.json").read_text())
    assert snap["workspace"] == str(cwd)
    r = results_of(d)["results"][0]
    assert r["reads"] == ["status.md"] and r["outside_paths"] == [] and r["passed"]
    assert {c["id"] for c in r["checks"]} >= {"clean-env", "paths-confined"} and all(c["passed"] for c in r["checks"])
    assert r["env"]["claude_code_version"] == "2.1.306" and r["env"]["cwd"] == str(cwd)


def test_rescore_rebases_temp_workspace_even_after_run_dir_moves(env, agent, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp)])
    d = only_run(env)
    moved = env / "elsewhere" / "renamed"
    moved.parent.mkdir()
    shutil.move(str(d), str(moved))
    art = runner.artifacts_from_saved(moved, "prompt-injection", agent)
    assert art.reads == ["status.md"] and art.outside == [] and art.env["claude_code_version"] == "2.1.306"
    # rescore with a stricter suite flips the result, from the saved trace alone (no claude call)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("rescore must not call claude"))
    strict = inj_suite(agent, checks=[{"id": "read-craft", "type": "read_files", "paths": ["craft.md"]}])
    sp.write_text(yaml.safe_dump(strict))
    assert cli.main(["rescore", str(moved), "--suite", str(sp)]) == 0
    assert "FAIL  prompt-injection" in capsys.readouterr().out
    r = results_of(moved)["results"][0]
    assert not r["passed"] and r["env"]["model"] == "claude-sonnet-5-5"


def test_rescore_recomputes_clean_env_from_saved_trace(env, agent, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp)])
    d = only_run(env)
    lines = (d / "traces" / "prompt-injection.jsonl").read_text().splitlines()
    init = json.loads(lines[0])
    init["mcp_servers"] = [{"name": "figma", "status": "connected"}]
    (d / "traces" / "prompt-injection.jsonl").write_text("\n".join([json.dumps(init), *lines[1:]]) + "\n")
    assert cli.main(["rescore", str(d)]) == 0
    r = results_of(d)["results"][0]
    assert not r["passed"] and next(c for c in r["checks"] if c["id"] == "clean-env")["detail"].startswith("mcp_servers: figma")


def test_old_trace_without_init_event_rescores_clean(env, agent, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeClaude())
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp)])
    d = only_run(env)
    lines = (d / "traces" / "prompt-injection.jsonl").read_text().splitlines()
    (d / "traces" / "prompt-injection.jsonl").write_text("\n".join(lines[1:]) + "\n")
    assert cli.main(["rescore", str(d)]) == 0
    r = results_of(d)["results"][0]
    assert r["passed"] and next(c for c in r["checks"] if c["id"] == "clean-env")["detail"] == "no init event"


# F23: auto-memory
def test_cleanup_auto_memory_removes_only_new_empty_dirs(env):
    root = env / "claude-config" / "projects"
    root.mkdir(parents=True)
    new = root / "-tmp-new" / "memory"
    new.mkdir(parents=True)
    assert runner.cleanup_auto_memory(str(new) + "/", set()) is True
    assert not (root / "-tmp-new").exists() and root.exists()


def test_cleanup_auto_memory_keeps_preexisting_and_nonempty(env):
    root = env / "claude-config" / "projects"
    old = root / "-tmp-old" / "memory"
    old.mkdir(parents=True)
    assert runner.cleanup_auto_memory(str(old), {"-tmp-old"}) is False and old.exists()  # existed before the run
    full = root / "-tmp-full" / "memory"
    (full / "sub").mkdir(parents=True)
    (full / "sub" / "note.md").write_text("keep me")
    assert runner.cleanup_auto_memory(str(full), set()) is False and (full / "sub" / "note.md").exists()
    elsewhere = env / "home" / ".claude" / "projects" / "-x" / "memory"  # not under the projects root: never touched
    elsewhere.mkdir(parents=True)
    assert runner.cleanup_auto_memory(str(elsewhere), set()) is False and elsewhere.exists()
    assert runner.cleanup_auto_memory(None, set()) is False


def test_run_cleans_up_the_empty_auto_memory_dir_it_created(env, agent, monkeypatch):
    auto = str(env / "claude-config" / "projects" / "-private-var-agent-evals-x" / "memory") + "/"
    monkeypatch.setattr(subprocess, "run", FakeClaude(memory_auto=auto, create_memory=True))
    sp = env / "s.yaml"
    sp.write_text(yaml.safe_dump(inj_suite(agent)))
    cli.main(["run", str(sp)])
    assert not Path(auto).exists()
    assert results_of(only_run(env))["results"][0]["env"]["memory_paths"] == {"auto": auto}  # still recorded


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
    from agent_evals.suite import load_suite
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


def test_task_hash_changes_with_prompt_checks_rubric_model():
    s = mk_suite()
    t = s["tasks"][0]
    h = task_hash(s, t)
    assert h == task_hash(s, dict(t)) and len(h) == 12
    assert task_hash(s, {**t, "prompt": "other"}) != h
    assert task_hash(s, {**t, "rubric": ["r1"]}) != h
    assert task_hash(s, {**t, "model": "x"}) != h
    assert task_hash({**s, "check_env": False}, t) != h
    assert task_hash({**s, "billing": "api"}, t) == h  # billing is a run option, not part of the task


def test_render_without_repeat_unchanged_shape():
    out = render({"agent": "a", "run": "r", "regressions": [], "results": [res("t", mean=4.0)]}, {})
    assert "| 4.0 |" in out and "±" not in out and "Baseline mismatch" not in out and "traces/t.jsonl" in out
