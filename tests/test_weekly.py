"""Weekly before/after report: reads only history.jsonl, never spawns claude."""
import json

import pytest

from agent_evals import cli, history, weekly

TH = weekly.thresholds()


def row(week="2026-W43", agent="tiny", task="t1", **kw):
    r = {"schema": 1, "run": f"run-{week}", "timestamp": "2026-10-20T10:00:00+00:00", "iso_week": week, "agent": agent, "task": task,
         "category": None, "vector": None, "model": "m1", "judge_model": "j1", "claude_version": "2.1.0", "billing": "subscription",
         "agent_fingerprint": "aaaaaaaaaaaa", "n": 1, "passed": True, "pass_rate": 1.0, "incomplete": False, "judge_mean": 4.0,
         "tokens": {"input": 100, "output": 50, "cache_read": 900, "cache_creation": 0}, "agent_cost_usd": 0.10, "judge_cost_usd": 0.01,
         "latency": {"wall_s": 10.0, "api_s": 8.0, "ttft_s": 1.0}, "turns": 4, "run_ok": True}
    r.update(kw)
    return r


@pytest.fixture
def runs(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def write(runs, rows):
    (runs / history.NAME).write_text("".join(json.dumps(r) + "\n" for r in rows))


def run_weekly(runs, *args):
    return cli.main(["weekly", "--runs-dir", str(runs), *args])


def metric(data, key, agent="tiny", task=None):
    rep = data["agents"][agent]
    return (rep["tasks"][task] if task else rep)["metrics"][key]


# ---- week arithmetic -------------------------------------------------------------------------------------------

def test_week_validation_and_year_boundary():
    assert weekly.previous_week("2027-W01") == "2026-W53"
    assert weekly.previous_week("2026-W43") == "2026-W42"
    assert weekly.previous_week("2026-W01") == "2025-W52"
    for bad in ("2026-43", "2026-W4", "2026-W00", "2027-W53", "nope", ""):
        with pytest.raises(weekly.WeeklyError):
            weekly.parse_week(bad)


def test_year_end_weeks_and_stored_iso_week_is_trusted():
    # 2026-12-31 23:30 at -05:00 is already 2027-01-01 in UTC, but the stored iso_week (W53) is what counts
    rows = [row("2026-W53", timestamp="2026-12-31T23:30:00-05:00", pass_rate=0.5, passed=False), row("2027-W01", timestamp="2027-01-04T09:00:00+00:00")]
    d = weekly.build_weeks(rows, None, None, None, TH)
    assert (d["week"], d["vs"]) == ("2027-W01", "2026-W53")
    assert d["rows"] == {"before": 1, "after": 1}
    assert metric(d, "pass_rate")["before"] == 0.5


# ---- statistics ------------------------------------------------------------------------------------------------

def test_cost_per_sample_is_the_same_for_a_repeat_row_and_single_rows():
    rep3 = row(n=3, agent_cost_usd=0.30, judge_cost_usd=0.03, tokens={"input": 300, "output": 150, "cache_read": 2700, "cache_creation": 0})
    singles = [row(), row(), row()]
    a, b = weekly.stats([rep3]), weekly.stats(singles)
    assert a["samples"] == b["samples"] == 3
    for k in ("cost_ps", "cost_agent_ps", "cost_judge_ps", "tokens_in_ps", "tokens_out_ps"):
        assert a[k] == pytest.approx(b[k])
    assert a["cost_ps"] == pytest.approx(0.11)
    assert a["tokens_in_ps"] == pytest.approx(1000)
    assert a["total_cost"] == pytest.approx(0.33)
    assert (a["runs"], b["runs"]) == (1, 3)


def test_pass_rate_is_sample_weighted_and_judge_skips_nulls():
    rows = [row(n=3, pass_rate=1 / 3, passed=False, judge_mean=3.0), row(n=1, pass_rate=1.0, judge_mean=None), row(n=1, judge_mean=5.0)]
    s = weekly.stats(rows)
    assert s["pass_rate"] == pytest.approx((1 + 1 + 1) / 5)
    assert s["judge_mean"] == pytest.approx((3.0 * 3 + 5.0) / 4)


def test_injection_resistance_counts_injection_rows_only():
    rows = [row(category="injection", vector="a"), row(category="injection", vector="b", passed=False, pass_rate=0.0),
            row(category="injection", vector="c", n=2, pass_rate=1.0), row(task="plain", passed=False, pass_rate=0.0)]
    s = weekly.stats(rows)
    assert s["resistance"] == pytest.approx(3 / 4)  # sample-weighted: 1 + 0 + 2 of 4 samples
    assert (s["vectors"], s["vectors_held"]) == (3, 2)
    assert weekly.stats([row()])["resistance"] is None


def test_null_fields_never_crash_or_become_zero():
    bare = row(judge_mean=None, tokens=None, agent_cost_usd=None, judge_cost_usd=None, latency=None, turns=None, agent_fingerprint=None, pass_rate=None)
    s = weekly.stats([bare])
    for k in ("judge_mean", "tokens_in_ps", "tokens_out_ps", "cost_ps", "total_cost", "wall_p50", "api_p95", "turns_ps", "resistance"):
        assert s[k] is None
    assert s["pass_rate"] == 1.0
    d = weekly.build_weeks([bare, row("2026-W42")], None, None, None, TH)
    md = weekly.render(d)
    assert "—" in md
    assert metric(d, "cost_ps")["delta"] is None and metric(d, "cost_ps")["flag"] is None


def test_latency_percentiles_use_row_values_and_a_repeat_row_counts_once():
    rows = [row(latency={"wall_s": w, "api_s": 1.0, "ttft_s": None}) for w in (10.0, 20.0, 30.0)]
    s = weekly.stats(rows + [row(n=5, latency={"wall_s": 40.0, "api_s": 1.0, "ttft_s": None})])
    assert s["wall_p50"] == pytest.approx(25.0)  # 4 rows, not 8 samples
    assert s["ttft_p50"] is None


# ---- flags -----------------------------------------------------------------------------------------------------

def _pair(before, after, th=TH):
    return weekly.build_weeks(before + after, None, "2026-W43", "2026-W42", th)


def test_flags_in_both_directions():
    worse = _pair([row("2026-W42")], [row(agent_cost_usd=0.20, tokens={"input": 100, "output": 50, "cache_read": 1900, "cache_creation": 0},
                                          passed=False, pass_rate=0.0, judge_mean=3.5)])
    for k in ("cost_ps", "tokens_in_ps", "pass_rate", "judge_mean"):
        assert metric(worse, k)["flag"] == "warn", k
    assert metric(worse, "tokens_out_ps")["flag"] is None
    better = _pair([row("2026-W42", agent_cost_usd=0.20, passed=False, pass_rate=0.0, judge_mean=3.0)], [row()])
    for k in ("cost_ps", "pass_rate", "judge_mean"):
        assert metric(better, k)["flag"] == "good", k
    md = weekly.render(worse)
    assert "⚠️" in md and "## Flagged" in md
    assert "## Improved" in weekly.render(better)


def test_small_moves_are_not_flagged_and_resistance_drop_is():
    d = _pair([row("2026-W42", category="injection")], [row(category="injection", agent_cost_usd=0.105, judge_mean=3.9)])
    assert [k for k, m in d["agents"]["tiny"]["metrics"].items() if m["flag"]] == []
    d = _pair([row("2026-W42", category="injection", n=2), row("2026-W42", category="injection", task="t2")],
              [row(category="injection", n=2, pass_rate=0.5, passed=False), row(category="injection", task="t2")])
    assert metric(d, "resistance")["flag"] == "warn"


def test_cli_threshold_overrides(runs, capsys):
    write(runs, [row("2026-W42"), row(agent_cost_usd=0.115)])  # +14% total cost
    assert metric(_pair([row("2026-W42")], [row(agent_cost_usd=0.115)]), "cost_ps")["flag"] is None
    assert run_weekly(runs, "--cost-threshold", "0.1") == 0
    data = json.loads((runs / "weekly" / "2026-W43.json").read_text())
    assert data["agents"]["tiny"]["metrics"]["cost_ps"]["flag"] == "warn"
    assert data["thresholds"]["cost"] == 0.1
    # judge and latency overrides
    rows = [row("2026-W42", latency={"wall_s": 10.0, "api_s": 8.0, "ttft_s": 1.0}) for _ in range(3)]
    now = [row(latency={"wall_s": 11.5, "api_s": 8.0, "ttft_s": 1.0}, judge_mean=3.9) for _ in range(3)]
    base = _pair(rows, now)
    assert metric(base, "wall_p95")["flag"] is None and metric(base, "judge_mean")["flag"] is None
    tight = _pair(rows, now, weekly.thresholds(latency=0.1, judge_drop=0.05))
    assert metric(tight, "wall_p95")["flag"] == "warn" and metric(tight, "judge_mean")["flag"] == "warn"


def test_few_runs_labels_latency_instead_of_flagging():
    slow = {"wall_s": 100.0, "api_s": 80.0, "ttft_s": 1.0}
    few = _pair([row("2026-W42"), row("2026-W42")], [row(latency=slow) for _ in range(5)])
    m = metric(few, "wall_p95")
    assert m["flag"] is None and m["note"] == "few runs"
    assert "wall p95 (few runs)" in weekly.render(few)
    enough = _pair([row("2026-W42") for _ in range(3)], [row(latency=slow) for _ in range(3)])
    assert metric(enough, "wall_p95")["flag"] == "warn" and metric(enough, "wall_p95")["note"] is None


# ---- comparison week selection ---------------------------------------------------------------------------------

def test_missing_comparison_week_prints_current_and_suggests_one(runs, capsys):
    write(runs, [row("2026-W40"), row("2026-W43")])
    assert run_weekly(runs) == 0
    out = capsys.readouterr().out
    assert "Nothing to compare against" in out and "2026-W42" in out and "--vs 2026-W40" in out
    md = (runs / "weekly" / "2026-W43.md").read_text()
    assert "Nothing to compare against" in md
    assert "$0.1100" in md  # current numbers still shown
    data = json.loads((runs / "weekly" / "2026-W43.json").read_text())
    assert data["agents"]["tiny"]["metrics"]["cost_ps"]["delta"] is None


def test_agent_without_rows_in_comparison_week(runs):
    d = weekly.build_weeks([row("2026-W42"), row("2026-W43"), row("2026-W41", agent="new"), row("2026-W43", agent="new")], None, None, None, TH)
    assert any("most recent earlier week is 2026-W41" in n for n in d["agents"]["new"]["notes"])
    assert d["agents"]["tiny"]["notes"] == []


def test_explicit_weeks_and_agent_filter():
    rows = [row("2026-W41", judge_mean=2.0), row("2026-W42"), row("2026-W43"), row("2026-W43", agent="other")]
    d = weekly.build_weeks(rows, "tiny", "2026-W43", "2026-W41", TH)
    assert list(d["agents"]) == ["tiny"] and d["vs"] == "2026-W41"
    assert metric(d, "judge_mean")["flag"] == "good"
    with pytest.raises(weekly.WeeklyError):
        weekly.build_weeks(rows, "ghost", None, None, TH)
    with pytest.raises(weekly.WeeklyError):
        weekly.build_weeks(rows, None, "2026-W43", "2026-W43", TH)


def test_bad_arguments_and_missing_history_exit_2(runs, tmp_path, capsys):
    write(runs, [row()])
    assert run_weekly(runs, "--week", "2026-43") == 2
    assert run_weekly(runs, "--vs", "2027-W53") == 2
    assert run_weekly(runs, "--agent", "ghost") == 2
    assert run_weekly(runs, "--cost-threshold", "-1") == 2
    capsys.readouterr()
    assert run_weekly(tmp_path / "nowhere") == 2
    assert "history rebuild" in capsys.readouterr().err
    (runs / history.NAME).write_text("")
    assert run_weekly(runs) == 2


def test_unreadable_week_rows_are_ignored_with_a_warning():
    d = weekly.build_weeks([row(), row("2026-W42"), row(iso_week=None), row(iso_week="garbage")], None, None, None, TH)
    assert d["rows"] == {"before": 1, "after": 1} and "2 row(s)" in d["warnings"][0]


# ---- comparability and versions --------------------------------------------------------------------------------

def test_comparability_warnings():
    d = _pair([row("2026-W42")], [row(model="m2", claude_version="2.2.0", billing="api")])
    cmp = d["agents"]["tiny"]["comparability"]
    assert "model differs: m1 → m2" in cmp and "Claude Code version differs: 2.1.0 → 2.2.0" in cmp and "billing differs: subscription → api" in cmp
    assert not any("judge model" in c for c in cmp)
    md = weekly.render(d)
    assert "## Comparability" in md and "tiny: model differs" in md
    assert "No differences" in weekly.render(_pair([row("2026-W42")], [row()]))
    assert "unknown → 2.1.0" in _pair([row("2026-W42", claude_version=None)], [row()])["agents"]["tiny"]["comparability"][0]


def test_version_change_section_spans_both_weeks_in_order():
    rows = [row("2026-W42", agent_fingerprint=None, timestamp="2026-10-13T10:00:00+00:00"),
            row("2026-W42", agent_fingerprint="bbbbbbbbbbbb", timestamp="2026-10-15T10:00:00+00:00", pass_rate=0.0, passed=False),
            row("2026-W43", agent_fingerprint="bbbbbbbbbbbb", timestamp="2026-10-20T10:00:00+00:00"),
            row("2026-W43", agent_fingerprint="cccccccccccc", timestamp="2026-10-22T10:00:00+00:00", agent_cost_usd=0.4)]
    d = weekly.build_weeks(rows, None, None, None, TH)
    vc = d["agents"]["tiny"]["version_change"]
    assert [v["fingerprint"] for v in vc] == [weekly.UNKNOWN_FP, "bbbbbbbbbbbb", "cccccccccccc"]
    assert vc[1]["stats"]["runs"] == 2 and vc[1]["stats"]["pass_rate"] == 0.5
    md = weekly.render(d)
    assert "## Version change — tiny" in md and "unknown (pre-0.3 run)" in md and "`cccccccccccc`" in md
    same = weekly.build_weeks([row("2026-W42"), row()], None, None, None, TH)
    assert same["agents"]["tiny"]["version_change"] == [] and "Version change" not in weekly.render(same)


def _versions():
    return [row("2026-W30", agent_fingerprint="aaaaaaaaaaaa", timestamp="2026-07-20T10:00:00+00:00"),
            row("2026-W35", agent_fingerprint="bbbbbbbbbbbb", timestamp="2026-08-25T10:00:00+00:00", agent_cost_usd=0.30),
            row("2026-W36", agent_fingerprint="bbbbbbbbbbbb", timestamp="2026-09-01T10:00:00+00:00", agent_cost_usd=0.30)]


def test_by_version_compares_latest_fingerprint_to_the_one_before(runs, capsys):
    write(runs, _versions())
    assert run_weekly(runs, "--agent", "tiny", "--by-version") == 0
    out = capsys.readouterr().out
    assert "by-version-tiny.md" in out
    data = json.loads((runs / "weekly" / "by-version-tiny.json").read_text())
    assert data["mode"] == "version" and data["labels"] == {"before": "aaaaaaaaaaaa", "after": "bbbbbbbbbbbb"}
    assert data["rows"] == {"before": 1, "after": 2}
    assert data["agents"]["tiny"]["metrics"]["cost_ps"]["flag"] == "warn"
    assert "`aaaaaaaaaaaa` → `bbbbbbbbbbbb`" in (runs / "weekly" / "by-version-tiny.md").read_text()


def test_by_version_when_the_agent_reverted_uses_the_last_different_fingerprint():
    rows = _versions() + [row("2026-W40", agent_fingerprint="aaaaaaaaaaaa", timestamp="2026-10-01T10:00:00+00:00")]
    d = weekly.build_by_version(rows, "tiny", TH)
    assert d["labels"] == {"before": "bbbbbbbbbbbb", "after": "aaaaaaaaaaaa"} and d["rows"] == {"before": 2, "after": 2}


def test_by_version_single_fingerprint_and_missing_agent(runs, capsys):
    write(runs, [row("2026-W42"), row()])
    assert run_weekly(runs, "--agent", "tiny", "--by-version") == 0
    assert "only one agent fingerprint" in capsys.readouterr().out
    assert not (runs / "weekly").exists()
    assert run_weekly(runs, "--by-version") == 2
    assert "--by-version needs --agent" in capsys.readouterr().err
    assert run_weekly(runs, "--agent", "ghost", "--by-version") == 2


# ---- outputs ---------------------------------------------------------------------------------------------------

def test_outputs_markdown_json_and_stdout_summary(runs, capsys):
    rows = [row("2026-W42", task="a"), row("2026-W42", task="b", category="injection", vector="v"),
            row(task="a", agent_cost_usd=0.5), row(task="b", category="injection", vector="v"), row(agent="other")]
    write(runs, rows)
    assert run_weekly(runs) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(str(runs / "weekly" / "2026-W43.md"))
    assert 3 <= len(out) <= 6
    assert any(ln.startswith("tiny: pass 100.0%") and "cost/sample" in ln and "wall p50" in ln and "resistance 100.0%" in ln and "⚠️" in ln for ln in out)
    md = (runs / "weekly" / "2026-W43.md").read_text()
    for section in ("# Weekly report — 2026-W43 vs 2026-W42", "## Summary", "## Flagged", "## Comparability", "## tiny", "### a", "### b", "## other"):
        assert section in md, section
    data = json.loads((runs / "weekly" / "2026-W43.json").read_text())
    assert (data["week"], data["vs"], data["rows"]) == ("2026-W43", "2026-W42", {"before": 2, "after": 3})
    assert set(data["agents"]) == {"tiny", "other"}
    m = data["agents"]["tiny"]["tasks"]["a"]["metrics"]["cost_ps"]
    assert set(m) >= {"before", "after", "delta", "delta_unit", "flag", "note", "label"} and m["flag"] == "warn"
    assert data["agents"]["tiny"]["metrics"]["resistance"]["after"] == 1.0
    assert data["warnings"] == [] and isinstance(data["thresholds"], dict)


def test_agent_filter_names_the_file_and_out_overrides(runs, tmp_path, capsys):
    write(runs, [row("2026-W42"), row()])
    assert run_weekly(runs, "--agent", "tiny") == 0
    assert (runs / "weekly" / "2026-W43-tiny.md").is_file() and (runs / "weekly" / "2026-W43-tiny.json").is_file()
    target = tmp_path / "elsewhere" / "report.md"
    assert run_weekly(runs, "--out", str(target)) == 0
    assert target.is_file() and target.with_suffix(".json").is_file()
    assert json.loads(target.with_suffix(".json").read_text())["agents"]["tiny"]


def test_empty_requested_week_does_not_crash(runs):
    write(runs, [row("2026-W42"), row()])
    assert run_weekly(runs, "--week", "2026-W20", "--vs", "2026-W19") == 0
    data = json.loads((runs / "weekly" / "2026-W20.json").read_text())
    assert data["agents"] == {} and any("No rows in 2026-W20" in n for n in data["notes"])
