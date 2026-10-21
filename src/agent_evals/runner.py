"""Run one golden task against an agent, isolated, and capture a full trace.

The agent's folder is copied into a throwaway workspace (minus .git and binaries), so its
real status files, handoffs and craft memory are never touched. The agent runs headless via
`claude -p` from that workspace, so it loads its own CLAUDE.md exactly as in daily use.
The workspace lives in a fresh temp dir outside any repo (so no parent CLAUDE.md and no shared
auto-memory folder), then moves into the run dir once the run is over.

Billing guard: by default ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are stripped from the child
environment, so runs only use the Claude subscription (login or CLAUDE_CODE_OAUTH_TOKEN).
`--billing api` leaves the environment as-is and bills the API key.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .checks import Artifacts

_SKIP = (".git", "node_modules", ".venv", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.pdf", "*.mp4", "*.zip",
         ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "settings.local.json")
_KEEP = (".env.example",)  # matches `.env.*` but is a safe template
FILE_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"}


def make_ignore(extra: list[str] | None = None):
    """copytree ignore function: secrets, binaries and VCS junk stay out of the workspace."""
    pats = _SKIP + tuple(extra or ())

    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n not in _KEEP and any(fnmatch.fnmatchcase(n, p) for p in pats)}
    return ignore


IGNORE = make_ignore()
TEXT_SUFFIXES = {".md", ".txt", ".html", ".json", ".yml", ".yaml", ".csv", ".ts", ".js", ".mjs", ".py", ".css"}


@dataclass
class RunResult:
    task_id: str
    ok: bool  # the run itself completed (not a judgement of quality)
    artifacts: Artifacts
    cost_usd: float = 0.0
    tokens: dict = field(default_factory=dict)
    duration_s: float = 0.0
    num_turns: int = 0
    error: str = ""


def _names(items) -> list[str]:
    return [i.get("name", "?") if isinstance(i, dict) else str(i) for i in items or []]


def parse_env(events: list[dict]) -> dict:
    """The environment Claude Code reports in its stream-json init event; {} if the trace has none."""
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    if not init:
        return {}
    return {
        "claude_code_version": init.get("claude_code_version"), "model": init.get("model"),
        "apiKeySource": init.get("apiKeySource"), "mcp_servers": _names(init.get("mcp_servers")),
        "plugins": _names(p for p in init.get("plugins") or [] if not isinstance(p, dict) or p.get("path") != "builtin"),
        "memory_paths": init.get("memory_paths") or {}, "cwd": init.get("cwd"),
    }


def _projects_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "projects"


def _project_dirs() -> set[str]:
    root = _projects_root()
    return {p.name for p in root.iterdir()} if root.is_dir() else set()


def cleanup_auto_memory(auto: str | None, existed_before: set[str]) -> bool:
    """Claude Code makes an auto-memory dir per working directory. Remove ours only if the run created it
    and it holds no files: empty dirs only, nothing under ~/.claude is ever deleted otherwise."""
    if not auto:
        return False
    mem = Path(auto.rstrip("/"))
    if os.path.realpath(mem.parent.parent) != os.path.realpath(_projects_root()) or mem.parent.name in existed_before:
        return False
    if not mem.is_dir() or any(not q.is_dir() for q in mem.rglob("*")):
        return False
    try:
        for d in sorted(mem.rglob("*"), reverse=True):
            d.rmdir()
        mem.rmdir()
        mem.parent.rmdir()  # the per-project dir, if nothing else is in it
    except OSError:
        pass
    return not mem.exists()


def _snapshot(root: Path) -> dict[str, str]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def parse_trace(events: list[dict], ws: Path) -> tuple[list[str], list[str], list[str]]:
    """(reads, tool names, outside) from stream-json events. `outside` is "<Tool>:<path>" for every
    file tool whose target resolves outside the workspace root (the agent-folder copy the run starts in)."""
    root = os.path.realpath(ws)
    reads, names, outside = [], [], []
    for e in events:
        if e.get("type") != "assistant":
            continue
        for b in e.get("message", {}).get("content", []):
            if b.get("type") != "tool_use":
                continue
            name, inp = b.get("name", ""), b.get("input") or {}
            names.append(name)
            fp = inp.get("file_path") or inp.get("path") or ""
            if name == "Read" and fp:
                reads.append(os.path.relpath(os.path.realpath(fp), root) if os.path.isabs(fp) else fp)
            if name not in FILE_TOOLS:
                continue
            for target in (inp.get("file_path"), inp.get("path"), inp.get("notebook_path")):
                if not target:
                    continue
                real = os.path.realpath(os.path.join(ws, os.path.expanduser(target)))
                if real != root and not real.startswith(root + os.sep):
                    outside.append(f"{name}:{real}")
    return reads, names, outside


def _clean_env(billing: str = "subscription") -> dict:
    env = dict(os.environ)
    if billing != "api":
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def run_task(agent_dir: Path, task: dict, cfg: dict, run_dir: Path) -> RunResult:
    name = task.get("_name", task["id"])  # sample name (`<task>.r2`) when repeating; the task id otherwise
    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-evals-")))
    try:
        return _run_in(tmp, agent_dir, task, name, cfg, run_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_in(tmp: Path, agent_dir: Path, task: dict, name: str, cfg: dict, run_dir: Path) -> RunResult:
    ws = tmp / agent_dir.name
    shutil.copytree(agent_dir, ws, ignore=make_ignore(cfg.get("exclude")))
    before = _snapshot(ws)
    # what the run started from: rescore diffs against this, not the (possibly since-edited) agent folder
    snap_path = run_dir / "workspaces" / f"{name}.snapshot.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps({"workspace": os.path.realpath(ws), "files": before}))
    projects_before = _project_dirs()

    tools = cfg.get("allowed_tools", ["Read", "Glob", "Grep", "Write", "Edit"])
    cmd = [
        "claude", "-p", task["prompt"],
        "--model", task.get("model", cfg["model"]),
        "--output-format", "stream-json", "--verbose",
        "--max-turns", str(task.get("max_turns", cfg.get("max_turns", 20))),
        "--permission-mode", "acceptEdits",
        "--allowedTools", ",".join(tools),
        "--disallowedTools", ",".join(cfg.get("disallowed_tools", ["Bash", "WebSearch", "WebFetch", "Task"])),
        "--strict-mcp-config", "--no-session-persistence",
        "--setting-sources", cfg.get("setting_sources", "project"),
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=ws, env=_clean_env(cfg.get("billing", "subscription")), capture_output=True, text=True,
                              timeout=task.get("timeout_s", cfg.get("timeout_s", 900)))
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        stdout, stderr = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout"
    duration = time.monotonic() - t0

    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    trace_path = run_dir / "traces" / f"{name}.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    reads, tool_names, outside = parse_trace(events, ws)

    after = _snapshot(ws)
    changed = {p for p, h in after.items() if before.get(p) != h}
    deleted = sorted(p for p in before if p not in after)
    files = {}
    for p in sorted(changed):
        if Path(p).suffix.lower() in TEXT_SUFFIXES:
            files[p] = (ws / p).read_text(errors="replace")

    env = parse_env(events)
    cleanup_auto_memory((env.get("memory_paths") or {}).get("auto"), projects_before)
    ws_root = run_dir / "workspaces" / name
    if ws_root.exists():
        shutil.rmtree(ws_root)
    ws_root.mkdir(parents=True)
    shutil.move(str(ws), str(ws_root / agent_dir.name))  # the temp dir is removed by the caller

    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    usage = result.get("usage", {}) or {}
    ok = bool(result) and not result.get("is_error", False)
    return RunResult(
        task_id=task["id"],
        ok=ok,
        artifacts=Artifacts(final=result.get("result", "") or "", files=files, reads=reads, tools=tool_names, outside=outside, deleted=deleted, env=env),
        cost_usd=float(result.get("total_cost_usd") or 0),
        tokens={k: usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")},
        duration_s=round(duration, 1),
        num_turns=int(result.get("num_turns") or 0),
        error="" if ok else (result.get("result") or stderr or "no result event")[:500],
    )


def _rebase(events: list[dict], old: str, new: str) -> list[dict]:
    """Rewrite an old absolute workspace prefix in file-tool inputs (the run dir was moved or renamed)."""
    def fix(v):
        return new + v[len(old):] if isinstance(v, str) and (v == old or v.startswith(old + os.sep)) else v
    out = []
    for e in events:
        if e.get("type") == "assistant":
            e = json.loads(json.dumps(e))
            for b in e.get("message", {}).get("content", []):
                if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                    for k in ("file_path", "path", "notebook_path"):
                        if k in b["input"]:
                            b["input"][k] = fix(b["input"][k])
        out.append(e)
    return out


def artifacts_from_saved(run_dir: Path, task_id: str, agent_dir: Path) -> Artifacts:  # task_id: task id or sample name
    """Rebuild a task's Artifacts from its saved trace and workspace (for zero-token rescoring).
    Changed/deleted files are found by diffing the workspace against the snapshot taken when the run
    started; runs without a snapshot fall back to comparing with the agent's current folder."""
    events = [json.loads(ln) for ln in (run_dir / "traces" / f"{task_id}.jsonl").read_text().splitlines() if ln.strip()]
    ws = run_dir / "workspaces" / task_id / agent_dir.name
    snap_path = run_dir / "workspaces" / f"{task_id}.snapshot.json"
    snap = json.loads(snap_path.read_text()) if snap_path.exists() else None
    if snap and snap.get("workspace") and snap["workspace"] != os.path.realpath(ws):
        events = _rebase(events, snap["workspace"], os.path.realpath(ws))
    reads, tools, outside = parse_trace(events, ws)
    files, deleted = {}, []
    if snap:
        now = _snapshot(ws)
        deleted = sorted(p for p in snap["files"] if p not in now)
        changed = {p for p, h in now.items() if snap["files"].get(p) != h}
        files = {p: (ws / p).read_text(errors="replace") for p in sorted(changed) if Path(p).suffix.lower() in TEXT_SUFFIXES}
    else:
        for p in ws.rglob("*"):
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
                rel = p.relative_to(ws)
                orig = agent_dir / rel
                if not orig.exists() or orig.read_bytes() != p.read_bytes():
                    files[str(rel)] = p.read_text(errors="replace")
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    return Artifacts(final=result.get("result", "") or "", files=files, reads=reads, tools=tools, outside=outside, deleted=deleted, env=parse_env(events))
