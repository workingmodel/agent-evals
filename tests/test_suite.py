"""Suite loading, validation and the task hash."""
from pathlib import Path

import yaml

from agent_evals import cli
from agent_evals.suite import load_suite, task_hash, validate_suite
from test_cli import mk_suite, write_suite

EXAMPLE = Path(__file__).parent.parent / "examples" / "suites" / "tiny-writer.yaml"


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


def test_validate_repeat_and_thresholds():
    assert validate_suite(mk_suite(repeat=3, min_pass_rate=0.5, regression_pass_drop=1)) == []
    for bad in ({"repeat": 0}, {"repeat": 1.5}, {"repeat": True}, {"min_pass_rate": 1.5}, {"min_pass_rate": "x"},
                {"regression_pass_drop": -0.1}, {"regression_pass_drop": True}):
        assert validate_suite(mk_suite(**bad)), bad


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
