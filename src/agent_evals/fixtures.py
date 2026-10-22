"""Workspace fixtures: the per-task `files:` key seeds files into the temp workspace before the run.

    files:
      status.md: "inline file content"
      notes/brief.txt: {from: fixtures/brief.txt}     # path relative to the suite file
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from .runner import make_ignore


def _path_error(rel: str, ignore) -> str:
    p = PurePosixPath(rel.replace("\\", "/"))
    if not rel.strip() or p.is_absolute() or (len(rel) > 1 and rel[1] == ":"):
        return "must be a relative path inside the workspace"
    if ".." in p.parts:
        return "must not contain `..`"
    if any(ignore("", [part]) for part in p.parts):
        return "hits the workspace skip list (secrets, VCS and binary files never enter the workspace)"
    return ""


def load(suite: dict, task: dict) -> tuple[dict[str, str], list[str]]:
    """(path -> resolved text, problems) for a task's `files:`. Never raises: validation reports the problems."""
    spec = task.get("files")
    if spec is None:
        return {}, []
    if not isinstance(spec, dict):
        return {}, ["`files` must be a mapping of workspace path to content"]
    ignore, base = make_ignore(suite.get("exclude")), Path(suite.get("_dir") or ".")
    out, errs = {}, []
    for rel, v in spec.items():
        if not isinstance(rel, str) or (e := _path_error(rel, ignore)):
            errs.append(f"files: `{rel}` {e or 'must be a string path'}")
        elif isinstance(v, str):
            out[rel] = v
        elif isinstance(v, dict) and set(v) == {"from"} and isinstance(v["from"], str) and v["from"].strip():
            src = Path(v["from"]).expanduser()
            src = src if src.is_absolute() else base / src
            try:
                out[rel] = src.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                errs.append(f"files: `{rel}`: cannot read from: {v['from']} ({type(e).__name__})")
        else:
            errs.append(f"files: `{rel}` must be a string or {{from: <path>}}")
    return out, errs


def seed(ws: Path, files: dict[str, str]) -> None:
    """Write fixtures into the workspace copy, replacing any existing file. Refuses to write through a symlink."""
    root = os.path.realpath(ws)
    for rel, text in files.items():
        dest = ws / rel
        real = os.path.realpath(dest.parent)
        if real != root and not real.startswith(root + os.sep):
            raise ValueError(f"fixture {rel} would be written outside the workspace")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        dest.write_text(text, encoding="utf-8")
