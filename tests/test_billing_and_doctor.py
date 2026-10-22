"""Run options, billing and the doctor command."""
import json
import subprocess
from pathlib import Path

from agent_evals import cli, runner
from fakes import run_dirs
from test_cli import mk_suite, ok_run, res, write_suite
from test_runner import fake_agent, stream


def test_runs_and_baselines_dir_options(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"]))
    sp = write_suite(env, mk_suite())
    assert cli.main(["run", str(sp), "--task", "t2", "--runs-dir", str(env / "R"), "--baselines-dir", str(env / "B"), "--update-baseline"]) == 0
    assert (env / "B" / "a.json").exists() and len(list(run_dirs(env / "R"))) == 1
    assert not (env / "runs").exists() and not (env / "baselines").exists()


def test_absolute_suite_path_and_billing_stored(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"]))
    write_suite(env, mk_suite())
    cli.main(["run", "suite.yaml", "--task", "t2"])  # relative argument
    (d,) = run_dirs(env / "runs")
    summary = json.loads((d / "results.json").read_text())
    assert Path(summary["suite"]).is_absolute() and summary["suite"].endswith("suite.yaml")
    assert summary["billing"] == "subscription"


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
    (d,) = run_dirs(env / "runs")
    assert seen["cfg"]["billing"] == "api" and seen["jb"] == "api"
    assert json.loads((d / "results.json").read_text())["billing"] == "api"
    rep = (d / "report.md").read_text()
    assert "billed to API" in rep and "subscription" not in rep


def test_default_report_wording_and_old_run_billing(env, monkeypatch):
    monkeypatch.setattr(cli, "run_task", lambda agent_dir, t, *a: ok_run(t["id"]))
    cli.main(["run", str(write_suite(env, mk_suite())), "--task", "t2"])
    (d,) = run_dirs(env / "runs")
    assert "notional (billed to subscription, not API)" in (d / "report.md").read_text()
    # old results.json has no `billing` key
    old = {"agent": "a", "run": "r", "regressions": [], "results": [res("t")]}
    assert "notional (billed to subscription" in cli.render(old, {})


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
