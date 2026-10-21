"""Suite loading and up-front validation, so a typo fails before any tokens are spent."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from .checks import CHECK_FIELDS


def load_suite(path: Path) -> dict:
    suite = yaml.safe_load(path.read_text())
    if not isinstance(suite, dict):
        return suite  # validate_suite reports it
    if isinstance(suite.get("path"), str):
        p = Path(suite["path"]).expanduser()
        # relative agent paths are relative to the suite file, so a suite runs from any CWD
        suite["path"] = str(p if p.is_absolute() else os.path.normpath(path.resolve().parent / p))
    # suite-level `vars` fill {name} placeholders in task prompts (e.g. shared source excerpts)
    for task in suite.get("tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("prompt"), str):
            for k, v in (suite.get("vars") or {}).items():
                task["prompt"] = task["prompt"].replace("{" + k + "}", str(v).rstrip())
    return suite


def task_checks(suite: dict, task: dict) -> list[dict]:
    skip = set(task.get("skip_checks", []))
    checks = [c for c in suite.get("checks", []) if c["id"] not in skip] + task.get("checks", [])
    if suite.get("check_env", True) and "clean-env" not in skip:
        checks.append({"id": "clean-env", "type": "clean_env", "billing": suite.get("billing", "subscription")})
    if suite.get("confine_paths", True) and "paths-confined" not in skip:
        checks.append({"id": "paths-confined", "type": "paths_confined", "allow": suite.get("allow_outside", [])})
    return checks


def task_hash(suite: dict, task: dict) -> str:
    """Short fingerprint of what defines a task's result: prompt, effective checks, rubric, model."""
    checks = [{k: v for k, v in c.items() if k != "billing"} for c in task_checks(suite, task)]  # billing is a run option
    blob = {"prompt": task.get("prompt"), "checks": checks, "rubric": task.get("rubric") or [], "model": task.get("model", suite.get("model"))}
    return hashlib.sha256(json.dumps(blob, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


SETTING_SOURCES = {"user", "project", "local"}


def _suite_key_errors(suite: dict) -> list[str]:
    errs = []
    ss = suite.get("setting_sources")
    if ss is not None and not (isinstance(ss, str) and all(x.strip() in SETTING_SOURCES for x in ss.split(","))):
        errs.append("`setting_sources` must be a comma-separated string of user, project, local")
    for k in ("allow_outside", "exclude"):
        v = suite.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            errs.append(f"`{k}` must be a list of strings")
    for k in ("confine_paths", "check_env"):
        if k in suite and not isinstance(suite[k], bool):
            errs.append(f"`{k}` must be true or false")
    if "repeat" in suite and (not isinstance(suite["repeat"], int) or isinstance(suite["repeat"], bool) or suite["repeat"] < 1):
        errs.append("`repeat` must be an integer >= 1")
    for k in ("min_pass_rate", "regression_pass_drop"):
        v = suite.get(k)
        if k in suite and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1):
            errs.append(f"`{k}` must be a number between 0 and 1")
    return errs


def _check_errors(check: object, where: str) -> list[str]:
    if not isinstance(check, dict):
        return [f"{where}: check must be a mapping"]
    errs = []
    cid, kind = check.get("id"), check.get("type")
    where = f"{where} check {cid or '?'}"
    if not cid:
        errs.append(f"{where}: missing id")
    if kind not in CHECK_FIELDS:
        errs.append(f"{where}: unknown type {kind!r} (known: {', '.join(CHECK_FIELDS)})")
        return errs
    for f in CHECK_FIELDS[kind]:
        if f not in check:
            errs.append(f"{where}: type {kind} requires `{f}`")
    if "words" in check and not isinstance(check["words"], list):
        errs.append(f"{where}: `words` must be a list")
    for f in ("paths", "allow"):
        if f in check and not isinstance(check[f], list):
            errs.append(f"{where}: `{f}` must be a list")
    if "min" in check and (not isinstance(check["min"], int) or isinstance(check["min"], bool)):
        errs.append(f"{where}: `min` must be an int")
    for f in ("pattern", "file_pattern", "allow_context"):
        if f in check:
            try:
                re.compile(check[f])
            except (re.error, TypeError) as e:
                errs.append(f"{where}: bad regex in `{f}`: {e}")
    return errs


def validate_suite(suite: dict) -> list[str]:
    """Return a list of human-readable problems; empty means the suite is runnable."""
    if not isinstance(suite, dict):
        return ["suite must be a YAML mapping"]
    errs = [f"missing top-level key `{k}`" for k in ("agent", "path", "model", "tasks") if not suite.get(k)]
    errs += _suite_key_errors(suite)
    suite_checks = suite.get("checks") or []
    errs += [e for c in suite_checks for e in _check_errors(c, "suite")]
    suite_ids = {c["id"] for c in suite_checks if isinstance(c, dict) and c.get("id")} | {"paths-confined", "clean-env"}
    tasks = suite.get("tasks")
    if not isinstance(tasks, list):
        return errs + (["`tasks` must be a list"] if tasks else [])
    seen: set = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errs.append(f"task #{i + 1}: must be a mapping")
            continue
        tid = t.get("id")
        where = f"task {tid}" if tid else f"task #{i + 1}"
        if not tid:
            errs.append(f"{where}: missing id")
        elif tid in seen:
            errs.append(f"{where}: duplicate task id")
        seen.add(tid)
        if not t.get("prompt"):
            errs.append(f"{where}: missing prompt")
        errs += [e for c in t.get("checks") or [] for e in _check_errors(c, where)]
        errs += [f"{where}: skip_checks refers to unknown suite check `{s}`" for s in t.get("skip_checks") or [] if s not in suite_ids]
        rubric = t.get("rubric")
        if rubric is not None and not (isinstance(rubric, list) and all(isinstance(x, str) for x in rubric)):
            errs.append(f"{where}: rubric must be a list of strings")
    return errs
