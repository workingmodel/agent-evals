"""Suite loading, validation and the task hash."""
import json
from pathlib import Path

import pytest
import yaml

from agent_evals import cli
from agent_evals.suite import SuiteError, load_suite, task_hash, validate_suite
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


# N7: the hash covers copy_block and pass_mean only where they change this task's result
def test_task_hash_covers_copy_block_and_pass_mean_where_they_apply():
    s = mk_suite(checks=[{"id": "c", "type": "regex_absent", "pattern": "x", "scope": "copy_block"}])
    rubric_task, plain = s["tasks"][0], s["tasks"][1]
    base = task_hash(s, rubric_task)
    assert task_hash({**s, "copy_block": ["A", "B"]}, rubric_task) != base
    assert task_hash({**s, "copy_block": ["A", "B"]}, rubric_task) != task_hash({**s, "copy_block": ["A", "C"]}, rubric_task)
    assert task_hash({**s, "pass_mean": 4.0}, rubric_task) != base
    assert task_hash({**s, "pass_mean": 3.5}, rubric_task) == base  # the default spelled out is not a change
    assert task_hash({**s, "pass_mean": 4.0}, plain) == task_hash(s, plain)  # no rubric: pass_mean can't matter
    no_scope = mk_suite()
    assert task_hash({**no_scope, "copy_block": ["A", "B"]}, no_scope["tasks"][0]) == task_hash(no_scope, no_scope["tasks"][0])
    assert task_hash({**s, "max_turns": 3, "timeout_s": 9}, rubric_task) == base


def test_shipped_baseline_hashes_match_the_example_suite():
    suite = load_suite(EXAMPLE)
    baseline = json.loads((EXAMPLE.parents[2] / "baselines" / "tiny-writer.json").read_text())
    assert {t["id"]: task_hash(suite, t) for t in suite["tasks"]} == {k: v["task_hash"] for k, v in baseline.items()}


# N1: every key is type- and range-checked up front
BAD_SUITE_KEYS = [
    ("pass_mean", "high"), ("pass_mean", 0.5), ("pass_mean", 5.5), ("pass_mean", True), ("pass_mean", None),
    ("regression_drop", "x"), ("regression_drop", -1), ("regression_drop", True), ("regression_drop", float("nan")),
    ("regression_pass_drop", "x"), ("regression_pass_drop", 1.1), ("regression_pass_drop", -0.1),
    ("min_pass_rate", "x"), ("min_pass_rate", 2), ("min_pass_rate", False),
    ("max_turns", 0), ("max_turns", "5"), ("max_turns", 2.5), ("max_turns", True),
    ("timeout_s", 0), ("timeout_s", "900"), ("timeout_s", False),
    ("repeat", 0), ("repeat", "3"), ("repeat", True),
    ("copy_block", ["a"]), ("copy_block", ["a", "b", "c"]), ("copy_block", ["a", ""]), ("copy_block", "ab"), ("copy_block", ["a", 1]),
    ("vars", [1]), ("vars", "x"), ("vars", {"a": [1]}), ("vars", {1: "a"}), ("vars", {"a": None}),
    ("judge_model", ""), ("judge_model", 5), ("model", 5), ("model", ["m"]),
    ("agent", 5), ("path", ["p"]),
    ("allowed_tools", "Read"), ("allowed_tools", ["Read", 1]), ("disallowed_tools", "Bash"), ("disallowed_tools", [None]),
    ("setting_sources", "project,bogus"), ("setting_sources", ["project"]),
    ("exclude", "*.log"), ("allow_outside", [1]),
    ("confine_paths", "no"), ("check_env", 0),
]


@pytest.mark.parametrize(("key", "value"), BAD_SUITE_KEYS, ids=[f"{k}={v!r}" for k, v in BAD_SUITE_KEYS])
def test_validate_rejects_bad_suite_key(key, value):
    errs = validate_suite(mk_suite(**{key: value}))
    assert any(f"`{key}`" in e for e in errs), errs


@pytest.mark.parametrize(("key", "value"), [
    ("pass_mean", 1), ("pass_mean", 5), ("pass_mean", 3.5), ("regression_drop", 0), ("regression_drop", 2),
    ("regression_pass_drop", 0), ("regression_pass_drop", 1), ("min_pass_rate", 0.5),
    ("max_turns", 1), ("timeout_s", 60), ("repeat", 3), ("copy_block", ["<!-- a -->", "<!-- b -->"]),
    ("vars", {"a": "x", "b": 2, "c": 1.5, "d": True}), ("vars", {}), ("judge_model", "j2"),
    ("allowed_tools", []), ("allowed_tools", ["Read"]), ("disallowed_tools", ["Bash"]),
    ("setting_sources", "user,project"), ("exclude", ["*.log"]), ("allow_outside", ["/tmp"]),
])
def test_validate_accepts_good_suite_key(key, value):
    assert validate_suite(mk_suite(**{key: value})) == []


@pytest.mark.parametrize(("key", "value"), [
    ("max_turns", 0), ("max_turns", "5"), ("max_turns", True), ("timeout_s", -1), ("timeout_s", 1.5), ("model", ""), ("model", 7),
])
def test_validate_rejects_bad_task_override(key, value):
    suite = mk_suite()
    suite["tasks"][0][key] = value
    errs = validate_suite(suite)
    assert any(f"task t1: `{key}`" in e for e in errs), errs


def test_validate_accepts_task_overrides_and_rejects_non_string_prompt():
    suite = mk_suite()
    suite["tasks"][0].update(model="m2", max_turns=3, timeout_s=60)
    assert validate_suite(suite) == []
    suite["tasks"][1]["prompt"] = ["not", "a", "string"]
    assert validate_suite(suite) == ["task t2: prompt must be a string"]


def test_bad_pass_mean_is_caught_before_any_task_runs(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_task", lambda *a: pytest.fail("a task ran"))
    sp = write_suite(env, mk_suite(pass_mean="high", regression_drop="x"))
    assert cli.main(["validate", str(sp)]) == 2
    out = capsys.readouterr().out
    assert "`pass_mean` must be a number between 1 and 5" in out and "`regression_drop` must be a number >= 0" in out
    assert cli.main(["run", str(sp)]) == 2
    assert "`pass_mean`" in capsys.readouterr().err
    assert not (env / "runs").exists()


# N2: load errors are one clear message and exit 2
@pytest.mark.parametrize(("text", "msg"), [
    ("agent: [unclosed\n", "invalid YAML"),
    ("- just\n- a list\n", "suite must be a YAML mapping"),
    ("just a string\n", "suite must be a YAML mapping"),
    ("", "suite must be a YAML mapping"),
    ("agent: a\ntasks: nope\n", "`tasks` must be a list"),
    ("agent: a\ntasks: {id: t}\n", "`tasks` must be a list"),
    ("agent: a\nvars: [1]\ntasks: []\n", "`vars` must be a mapping"),
    ("agent: a\nvars: text\ntasks: []\n", "`vars` must be a mapping"),
])
def test_load_suite_raises_one_clear_error(tmp_path, text, msg):
    p = tmp_path / "s.yaml"
    p.write_text(text)
    with pytest.raises(SuiteError, match=msg):
        load_suite(p)


def test_load_suite_missing_file_and_unreadable(tmp_path):
    with pytest.raises(SuiteError, match="suite file not found"):
        load_suite(tmp_path / "nope.yaml")
    bad = tmp_path / "bin.yaml"
    bad.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(SuiteError, match="cannot read suite file"):
        load_suite(bad)
    with pytest.raises(SuiteError, match="cannot read suite file"):
        load_suite(tmp_path)  # a directory


def test_suite_error_is_a_value_error():
    assert issubclass(SuiteError, ValueError)


@pytest.mark.parametrize("text", ["agent: [unclosed\n", "- a\n", "agent: a\nvars: [1]\ntasks: []\n"])
@pytest.mark.parametrize("cmd", ["run", "validate"])
def test_cli_load_errors_exit_2_with_one_line_and_no_traceback(env, monkeypatch, capsys, text, cmd):
    monkeypatch.setattr(cli, "run_task", lambda *a: pytest.fail("a task ran"))
    sp = env / "s.yaml"
    sp.write_text(text)
    assert cli.main([cmd, str(sp)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1 and "Traceback" not in err


@pytest.mark.parametrize("cmd", ["run", "validate"])
def test_cli_missing_suite_file_exits_2(env, capsys, cmd):
    assert cli.main([cmd, str(env / "absent.yaml")]) == 2
    assert capsys.readouterr().err.startswith("error: suite file not found")


def test_validate_reports_every_file_even_after_a_load_error(env, capsys):
    good = write_suite(env, mk_suite(path=str(env)))
    assert cli.main(["validate", str(env / "absent.yaml"), str(good)]) == 2
    cap = capsys.readouterr()
    assert "suite file not found" in cap.err and f"OK    {good}" in cap.out
