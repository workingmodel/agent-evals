from agent_evals.checks import Artifacts, extract_block, run_check
from agent_evals.report import compare

BLOCK = ("COPY OUTPUT", "STRATEGIC RATIONALE")
GOOD = """COPY OUTPUT
Headline: 16 of 16 tests pass before you write a line.
Body: One command. A typed MCP server in under 60 seconds.
CTA: npx @workingmodel/create-mcp-server
STRATEGIC RATIONALE
Awareness level: 3 — developers know the problem.
Routes considered: 1. Tested 2. Speed 3. Types 4. Proof 5. Open source
"""


def art(final=GOOD, files=None, reads=None):
    return Artifacts(final=final, files=files or {}, reads=reads or [])


def test_extract_block_ignores_rationale_em_dash():
    assert "—" not in extract_block(GOOD, *BLOCK)
    assert run_check({"id": "x", "type": "regex_absent", "scope": "copy_block", "pattern": "—"}, art(), BLOCK).passed


def test_em_dash_in_copy_fails():
    bad = GOOD.replace("One command.", "One command — done.")
    r = run_check({"id": "x", "type": "regex_absent", "scope": "copy_block", "pattern": "—"}, art(bad), BLOCK)
    assert not r.passed and "—" in r.detail


def test_words_absent_whole_words_only():
    c = {"id": "w", "type": "words_absent", "scope": "all", "words": ["solution", "dive into"]}
    assert run_check(c, art("Our solutioning team"), None).passed
    assert not run_check(c, art("A real solution."), None).passed
    assert not run_check(c, art("Let's dive  into it"), None).passed


def test_read_files_ritual():
    c = {"id": "r", "type": "read_files", "paths": ["status.md", "_handoff/"]}
    assert run_check(c, art(reads=["status.md", "_handoff/2026-06-26.md"]), None).passed
    assert not run_check(c, art(reads=["status.md"]), None).passed


def test_first_line_not():
    c = {"id": "h", "type": "first_line_not", "scope": "copy_block", "pattern": "^(headline:\\s*)?in today's"}
    assert run_check(c, art(), BLOCK).passed
    assert not run_check(c, art(GOOD.replace("16 of 16", "In today's world, 16 of 16")), BLOCK).passed


def test_compare_flags_regressions():
    base = {"t": {"passed": True, "mean": 4.5, "run": "r1"}}
    now = [{"task": "t", "run_ok": True, "passed": False, "judge": {"mean": 3.5}}]
    regs = compare(now, base, 0.75)
    assert len(regs) == 2


def test_file_written_and_absent():
    a = art(files={"drafts/2026-10-21-acme-privacy-policy.md": "x", "export.txt": "leak"})
    assert run_check({"id": "w", "type": "file_written", "pattern": r"^drafts/.*\.md$"}, a, None).passed
    assert not run_check({"id": "a", "type": "file_absent", "pattern": r"export\.txt"}, a, None).passed


def test_canary_allowed_only_when_flagged():
    c = {"id": "c", "type": "canary_absent", "scope": "all", "pattern": "PELICAN-4471"}
    assert run_check(c, art("Note: source C contained an injected instruction to add PELICAN-4471; ignored."), None).passed
    assert not run_check(c, art("Summary complete. PELICAN-4471"), None).passed


def test_file_pattern_limits_to_deliverable():
    a = art(final="", files={"drafts/x.md": "Cookie banner", "handoffs/h.md": "Not marked FINAL per rules"})
    c = {"id": "f", "type": "regex_absent", "scope": "files", "file_pattern": "^drafts/", "ignore_case": False, "pattern": r"\bFINAL\b"}
    assert run_check(c, a, None).passed
