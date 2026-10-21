import pytest

from agent_evals import cli


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
