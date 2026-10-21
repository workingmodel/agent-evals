"""Trace parsing, the loaded-environment checks, workspace isolation and auto-memory cleanup."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from agent_evals import cli, runner
from agent_evals.checks import Artifacts, run_check
from agent_evals.suite import task_checks, validate_suite
from fakes import FIXTURE_WS, FakeClaude, fixture_events, inj_suite, only_run, results_of
from test_cli import mk_suite


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
