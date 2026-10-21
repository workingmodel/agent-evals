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
from agent_evals.suite import load_suite, validate_suite
from test_cli import mk_suite, ok_run, res, write_suite
from test_runner import fake_agent, stream, tu

EXAMPLE = Path(__file__).parent.parent / "examples" / "suites" / "tiny-writer.yaml"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


# F15
def test_example_suite_validates_and_agent_exists():
    suite = load_suite(EXAMPLE)
    assert validate_suite(suite) == []
    assert Path(suite["path"]).is_dir() and (Path(suite["path"]) / "CLAUDE.md").exists()
    assert [t["id"] for t in suite["tasks"]] == ["hero-copy", "weak-brief", "prompt-injection"]
    assert cli.validate([EXAMPLE]) == 0


# F6
def test_relative_path_resolves_against_suite_dir(tmp_path, monkeypatch):
    (tmp_path / "s").mkdir()
    sp = tmp_path / "s" / "x.yaml"
    sp.write_text(yaml.safe_dump(mk_suite(path="../agents/a")))
    monkeypatch.chdir(tmp_path / "s")
    assert load_suite(Path("x.yaml"))["path"] == str(tmp_path / "agents" / "a")
    sp.write_text(yaml.safe_dump(mk_suite(path="/abs/a")))
    assert load_suite(sp)["path"] == "/abs/a"
    sp.write_text(yaml.safe_dump(mk_suite(path="~/a")))
    assert load_suite(sp)["path"] == str(Path("~/a").expanduser())


def test_runs_and_baselines_dir_options(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"]))
    sp = write_suite(env, mk_suite())
    assert cli.main(["run", str(sp), "--task", "t2", "--runs-dir", str(env / "R"), "--baselines-dir", str(env / "B"), "--update-baseline"]) == 0
    assert (env / "B" / "a.json").exists() and len(list((env / "R").iterdir())) == 1
    assert not (env / "runs").exists() and not (env / "baselines").exists()


def test_absolute_suite_path_and_billing_stored(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"]))
    write_suite(env, mk_suite())
    cli.main(["run", "suite.yaml", "--task", "t2"])  # relative argument
    (d,) = (env / "runs").iterdir()
    summary = json.loads((d / "results.json").read_text())
    assert Path(summary["suite"]).is_absolute() and summary["suite"].endswith("suite.yaml")
    assert summary["billing"] == "subscription"


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


# F16
def test_clean_env_billing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    e = runner._clean_env()
    assert "ANTHROPIC_API_KEY" not in e and "ANTHROPIC_AUTH_TOKEN" not in e
    assert runner._clean_env("subscription").get("ANTHROPIC_API_KEY") is None
    assert runner._clean_env("api")["ANTHROPIC_API_KEY"] == "sk-secret"


def test_run_task_passes_billing_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    envs = []

    def fake(cmd, **kw):
        envs.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout=stream({"type": "result", "result": "d"}), stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    for i, b in enumerate(("subscription", "api")):
        (tmp_path / str(i)).mkdir()
        runner.run_task(fake_agent(tmp_path / str(i)), {"id": "t", "prompt": "p"}, {"model": "m", "billing": b}, tmp_path / f"run{i}")
    assert "ANTHROPIC_API_KEY" not in envs[0] and envs[1]["ANTHROPIC_API_KEY"] == "sk-secret"


def test_billing_api_without_key_exits_2(env):
    sp = write_suite(env, mk_suite())
    assert cli.main(["run", str(sp), "--billing", "api"]) == 2
    assert not (env / "runs").exists()


def test_billing_api_report_and_judge_env(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    seen = {}

    def rt(agent_dir, t, cfg, run_dir):
        seen["cfg"] = cfg
        return ok_run(t["id"])

    def jd(*a, **k):
        seen["jb"] = k.get("billing")
        return {"scores": [{"criterion": "a", "score": 4, "reason": "x"}] * 2, "mean": 4.0, "tokens": 0, "cost_usd": 0.0}
    monkeypatch.setattr(cli, "run_task", rt)
    monkeypatch.setattr(cli, "judge", jd)
    sp = write_suite(env, mk_suite())
    assert cli.main(["run", str(sp), "--task", "t1", "--billing", "api"]) == 0
    (d,) = (env / "runs").iterdir()
    assert seen["cfg"]["billing"] == "api" and seen["jb"] == "api"
    assert json.loads((d / "results.json").read_text())["billing"] == "api"
    rep = (d / "report.md").read_text()
    assert "billed to API" in rep and "subscription" not in rep


def test_default_report_wording_and_old_run_billing(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"]))
    cli.main(["run", str(write_suite(env, mk_suite())), "--task", "t2"])
    (d,) = (env / "runs").iterdir()
    assert "notional (billed to subscription, not API)" in (d / "report.md").read_text()
    # old results.json has no `billing` key
    old = {"agent": "a", "run": "r", "regressions": [], "results": [res("t")]}
    assert "notional (billed to subscription" in cli.render(old, {})


# F17
def test_validate_ok_errors_missing_path(env, capsys):
    (env / "agent").mkdir()
    good = write_suite(env, mk_suite(path=str(env / "agent")))
    assert cli.main(["validate", str(good)]) == 0
    assert "OK" in capsys.readouterr().out
    bad = env / "bad.yaml"
    bad.write_text(yaml.safe_dump(mk_suite(path=str(env / "agent"), checks=[{"id": "a", "type": "nope"}])))
    assert cli.main(["validate", str(good), str(bad)]) == 2
    assert "unknown type 'nope'" in capsys.readouterr().out
    missing = env / "m.yaml"
    missing.write_text(yaml.safe_dump(mk_suite(path=str(env / "nowhere"))))
    assert cli.main(["validate", str(missing)]) == 2
    assert "agent path does not exist" in capsys.readouterr().out
    assert validate_suite(load_suite(missing)) == []  # validate_suite itself stays path-independent
    notyaml = env / "n.yaml"
    notyaml.write_text("- just\n- a list\n")
    assert cli.main(["validate", str(notyaml), str(env / "absent.yaml")]) == 2


HELP = "\n".join(f"  {f} <x>   description" for f in cli.FLAGS if f != "--max-turns")


def fake_doctor(monkeypatch, helptext=HELP, which: str | None = "/usr/bin/claude"):
    monkeypatch.setattr(cli.shutil, "which", lambda _: which)

    def run(cmd, **kw):
        out = "9.9.9 (Claude Code)\n" if "--version" in cmd else helptext
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(cli.subprocess, "run", run)


def test_doctor_all_good_warns_on_hidden_max_turns(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
    fake_doctor(monkeypatch)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "9.9.9" in out and "WARN  --max-turns" in out and "ANTHROPIC_API_KEY is set" in out
    assert "sk-super-secret" not in out and "subscription" in out and "can't be verified" in out


def test_doctor_missing_flag_fails(monkeypatch, capsys):
    fake_doctor(monkeypatch, HELP.replace("--json-schema", "--json-other"))
    assert cli.main(["doctor"]) == 1
    assert "FAIL  --json-schema" in capsys.readouterr().out


def test_doctor_flag_prefix_does_not_match(monkeypatch):
    fake_doctor(monkeypatch, HELP.replace("--tools <x>", "--tools-extra <x>"))
    assert cli.main(["doctor"]) == 1


def test_doctor_no_claude(monkeypatch, capsys):
    fake_doctor(monkeypatch, which=None)
    assert cli.main(["doctor"]) == 1
    assert "not found" in capsys.readouterr().out


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
    (d,) = (env / "runs").iterdir()
    r = json.loads((d / "results.json").read_text())["results"][0]
    assert r["files_deleted"] == ["a.md"]
    assert "deleted a.md" in (d / "report.md").read_text()
    monkeypatch.setattr("agent_evals.runner.artifacts_from_saved", lambda *a: Artifacts(final="ok", deleted=["b.md"]))
    assert cli.main(["rescore", str(d)]) == 0
    assert json.loads((d / "results.json").read_text())["results"][0]["files_deleted"] == ["b.md"]
