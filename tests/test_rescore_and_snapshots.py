"""Run snapshots, deleted files and rescoring saved runs."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_evals import cli, runner
from agent_evals.checks import Artifacts, run_check
from agent_evals.runner import RunResult
from agent_evals.suite import validate_suite
from fakes import FakeClaude, inj_suite, only_run, results_of, run_dirs
from test_cli import mk_suite, res, write_suite
from test_runner import fake_agent, stream, tu


def make_run(env, suite_ref):
    rd = env / "runs" / "x"
    rd.mkdir(parents=True)
    (rd / "results.json").write_text(json.dumps({"agent": "a", "suite": suite_ref, "run": "x", "results": [res("t1")], "regressions": []}))
    return rd


def test_rescore_suite_override_and_missing_suite_error(env, monkeypatch, capsys):
    monkeypatch.setattr("agent_evals.runner.artifacts_from_saved", lambda *a: Artifacts(final="fine"))
    rd = make_run(env, "gone/suite.yaml")  # old-style relative path that no longer exists
    assert cli.main(["rescore", str(rd)]) == 2
    err = capsys.readouterr().err
    assert "suite file not found" in err and "--suite" in err
    sp = write_suite(env, mk_suite())
    assert cli.main(["rescore", str(rd), "--suite", str(sp)]) == 0
    assert "PASS  t1" in capsys.readouterr().out


def test_rescore_baselines_dir(env, monkeypatch):
    monkeypatch.setattr("agent_evals.runner.artifacts_from_saved", lambda *a: Artifacts(final="fine"))
    sp = write_suite(env, mk_suite())
    rd = make_run(env, str(sp))
    assert cli.main(["rescore", str(rd), "--update-baseline", "--baselines-dir", str(env / "B")]) == 0
    assert (env / "B" / "a.json").exists()


# F5 + F8
def agent_with_files(tmp_path):
    ag = fake_agent(tmp_path)
    (ag / "keep.md").write_text("keep")
    (ag / "gone.md").write_text("bye")
    return ag


def run_script(monkeypatch, tmp_path, mutate, events: Any = ()):
    """Fake claude that applies `mutate` to the workspace; returns (agent dir, run dir)."""
    def fake(cmd, **kw):
        mutate(Path(kw["cwd"]))
        evs = list(events) if isinstance(events, list | tuple) else events(Path(kw["cwd"]))
        out = stream(*evs, {"type": "result", "result": "done", "num_turns": 1, "usage": {}})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    ag = agent_with_files(tmp_path)
    return ag, tmp_path / "run"


def mutate(ws):
    (ws / "gone.md").unlink()
    (ws / "drafts").mkdir()
    (ws / "drafts" / "new.md").write_text("new")
    (ws / "keep.md").write_text("changed")


def test_snapshot_saved_and_deletes_detected(monkeypatch, tmp_path):
    ag, rd = run_script(monkeypatch, tmp_path, mutate)
    rr = runner.run_task(ag, {"id": "t", "prompt": "p"}, {"model": "m"}, rd)
    snap = json.loads((rd / "workspaces" / "t.snapshot.json").read_text())
    ws = rd / "workspaces" / "t" / "agent"
    assert snap["workspace"] != os.path.realpath(ws) and snap["workspace"].endswith("/agent")  # recorded where the run happened (temp)
    assert set(snap["files"]) >= {"CLAUDE.md", "keep.md", "gone.md"} and all(len(h) == 40 for h in snap["files"].values())
    assert rr.artifacts.deleted == ["gone.md"] and set(rr.artifacts.files) == {"drafts/new.md", "keep.md"}


def test_rescore_uses_snapshot_not_live_agent_folder(monkeypatch, tmp_path):
    ag, rd = run_script(monkeypatch, tmp_path, mutate)
    runner.run_task(ag, {"id": "t", "prompt": "p"}, {"model": "m"}, rd)
    (ag / "keep.md").write_text("changed")  # agent folder drifted to match the run's output
    (ag / "CLAUDE.md").write_text("edited since the run")
    art = runner.artifacts_from_saved(rd, "t", ag)
    assert set(art.files) == {"drafts/new.md", "keep.md"} and art.deleted == ["gone.md"]


def test_rescore_without_snapshot_falls_back(monkeypatch, tmp_path):
    ag, rd = run_script(monkeypatch, tmp_path, mutate)
    runner.run_task(ag, {"id": "t", "prompt": "p"}, {"model": "m"}, rd)
    (rd / "workspaces" / "t.snapshot.json").unlink()
    art = runner.artifacts_from_saved(rd, "t", ag)
    assert set(art.files) == {"drafts/new.md", "keep.md"} and art.deleted == []


def test_rescore_moved_run_dir(monkeypatch, tmp_path):
    events = lambda ws: [tu("Read", file_path=str(ws / "status.md")), tu("Write", file_path=str(ws / "drafts" / "new.md"))]  # noqa: E731
    ag, rd = run_script(monkeypatch, tmp_path, mutate, events)
    runner.run_task(ag, {"id": "t", "prompt": "p"}, {"model": "m"}, rd)
    # the trace holds the temp-dir paths the run used; the snapshot records that workspace so rescore can rebase them
    moved = tmp_path / "elsewhere" / "renamed-run"
    moved.parent.mkdir()
    shutil.move(str(rd), str(moved))
    art = runner.artifacts_from_saved(moved, "t", ag)
    assert art.outside == [] and art.reads == ["status.md"]
    assert set(art.files) == {"drafts/new.md", "keep.md"} and art.deleted == ["gone.md"]
    # without the rebase the same trace would be flagged
    ev = [json.loads(ln) for ln in (moved / "traces" / "t.jsonl").read_text().splitlines()]
    assert runner.parse_trace(ev, moved / "workspaces" / "t" / "agent")[2]


def test_no_file_deletes_check():
    c = {"id": "d", "type": "no_file_deletes"}
    assert run_check(c, Artifacts(final=""), None).passed
    r = run_check(c, Artifacts(final="", deleted=["a.md", "drafts/b.md"]), None)
    assert not r.passed and r.detail == "a.md, drafts/b.md"
    assert run_check({**c, "allow": ["drafts/"]}, Artifacts(final="", deleted=["drafts/b.md"]), None).passed
    assert validate_suite(mk_suite(checks=[c])) == []
    assert validate_suite(mk_suite(checks=[{**c, "allow": "drafts/"}]))


def test_deleted_survives_file_pattern_filter():
    c = {"id": "d", "type": "no_file_deletes", "file_pattern": "^drafts/"}
    assert not run_check(c, Artifacts(final="", deleted=["x.md"]), None).passed


def test_files_deleted_in_results_and_rescore(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: RunResult(t["id"], True, Artifacts(final="ok", deleted=["a.md"])))
    cli.main(["run", str(write_suite(env, mk_suite())), "--task", "t2"])
    (d,) = run_dirs(env / "runs")
    r = json.loads((d / "results.json").read_text())["results"][0]
    assert r["files_deleted"] == ["a.md"]
    assert "deleted a.md" in (d / "report.md").read_text()
    monkeypatch.setattr("agent_evals.runner.artifacts_from_saved", lambda *a: Artifacts(final="ok", deleted=["b.md"]))
    assert cli.main(["rescore", str(d)]) == 0
    assert json.loads((d / "results.json").read_text())["results"][0]["files_deleted"] == ["b.md"]


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


# N2: rescore load failures are one line and exit 2
def test_rescore_missing_dir_or_results_exits_2(env, capsys):
    assert cli.main(["rescore", str(env / "nonexistent")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: cannot read") and "results.json" in err and "Traceback" not in err
    (env / "empty").mkdir()
    assert cli.main(["rescore", str(env / "empty")]) == 2


def test_rescore_invalid_results_json_exits_2(env, capsys):
    rd = env / "runs" / "x"
    rd.mkdir(parents=True)
    for text in ("{not json", "[1]", json.dumps({"agent": "a"})):
        (rd / "results.json").write_text(text)
        assert cli.main(["rescore", str(rd)]) == 2
        assert capsys.readouterr().err.startswith("error: ")


def test_rescore_bad_suite_or_baseline_exits_2(env, monkeypatch, capsys):
    monkeypatch.setattr("agent_evals.runner.artifacts_from_saved", lambda *a: Artifacts(final="fine"))
    rd = make_run(env, "x")
    broken = env / "broken.yaml"
    broken.write_text("agent: [unclosed\n")
    assert cli.main(["rescore", str(rd), "--suite", str(broken)]) == 2
    assert capsys.readouterr().err.startswith("error: invalid YAML")
    bad_key = write_suite(env, mk_suite(pass_mean="high"))
    assert cli.main(["rescore", str(rd), "--suite", str(bad_key)]) == 2
    assert "`pass_mean`" in capsys.readouterr().err
    good = write_suite(env, mk_suite())
    (env / "baselines").mkdir()
    (env / "baselines" / "a.json").write_text("{nope")
    assert cli.main(["rescore", str(rd), "--suite", str(good)]) == 2
    assert capsys.readouterr().err.startswith("error: cannot read baseline")
