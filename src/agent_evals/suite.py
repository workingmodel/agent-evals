"""Suite loading and up-front validation, so a typo fails before any tokens are spent."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

from .checks import CHECK_FIELDS


class SuiteError(ValueError):
    """The suite file can't be read or isn't shaped like a suite. The message is meant for the user."""


def load_suite(path: Path) -> dict:
    try:
        suite = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise SuiteError(f"suite file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise SuiteError(f"cannot read suite file {path}: {e}") from None
    except yaml.YAMLError as e:
        raise SuiteError(f"invalid YAML in {path}: {' '.join(str(e).split())}") from None
    if not isinstance(suite, dict):
        raise SuiteError(f"{path}: suite must be a YAML mapping")
    if suite.get("tasks") is not None and not isinstance(suite["tasks"], list):
        raise SuiteError(f"{path}: `tasks` must be a list")
    if suite.get("vars") is not None and not isinstance(suite["vars"], dict):
        raise SuiteError(f"{path}: `vars` must be a mapping of name to value")
    if isinstance(suite.get("path"), str):
        p = Path(suite["path"]).expanduser()
        # relative agent paths are relative to the suite file, so a suite runs from any CWD
        suite["path"] = str(p if p.is_absolute() else os.path.normpath(path.resolve().parent / p))
    # suite-level `vars` fill {name} placeholders in task prompts (e.g. shared source excerpts)
    for task in suite.get("tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("prompt"), str):
            for k, v in (suite.get("vars") or {}).items():
                task["prompt"] = task["prompt"].replace("{" + str(k) + "}", str(v).rstrip())
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
    """Short fingerprint of what defines a task's result: prompt, effective checks, rubric, model, and the
    suite's `copy_block` / `pass_mean` when they apply to this task (so default suites keep their hashes)."""
    checks = [{k: v for k, v in c.items() if k != "billing"} for c in task_checks(suite, task)]  # billing is a run option
    blob = {"prompt": task.get("prompt"), "checks": checks, "rubric": task.get("rubric") or [], "model": task.get("model", suite.get("model"))}
    if suite.get("copy_block") and any(c.get("scope") == "copy_block" for c in checks):
        blob["copy_block"] = suite["copy_block"]
    if task.get("rubric") and suite.get("pass_mean", DEFAULT_PASS_MEAN) != DEFAULT_PASS_MEAN:
        blob["pass_mean"] = suite["pass_mean"]
    return hashlib.sha256(json.dumps(blob, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


DEFAULT_PASS_MEAN = 3.5
SETTING_SOURCES = {"user", "project", "local"}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _is_setting_sources(v: Any) -> bool:
    return isinstance(v, str) and all(x.strip() in SETTING_SOURCES for x in v.split(","))


def _is_copy_block(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_is_text(x) for x in v)


def _is_vars(v: Any) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, (str, int, float, bool)) for k, x in v.items())


# key -> (predicate, what is expected). Suite-level keys; the first three can also be set per task.
_KEY_RULES: dict[str, tuple[Callable[[Any], bool], str]] = {
    "agent": (_is_text, "a non-empty string"),
    "path": (_is_text, "a non-empty string"),
    "model": (_is_text, "a non-empty string"),
    "judge_model": (_is_text, "a non-empty string"),
    "max_turns": (lambda v: _is_int(v) and v >= 1, "an integer >= 1"),
    "timeout_s": (lambda v: _is_int(v) and v >= 1, "an integer >= 1"),
    "repeat": (lambda v: _is_int(v) and v >= 1, "an integer >= 1"),
    "pass_mean": (lambda v: _is_num(v) and 1 <= v <= 5, "a number between 1 and 5"),
    "regression_drop": (lambda v: _is_num(v) and v >= 0, "a number >= 0"),
    "regression_pass_drop": (lambda v: _is_num(v) and 0 <= v <= 1, "a number between 0 and 1"),
    "min_pass_rate": (lambda v: _is_num(v) and 0 <= v <= 1, "a number between 0 and 1"),
    "copy_block": (_is_copy_block, "a list of two non-empty strings [start, end]"),
    "vars": (_is_vars, "a mapping of string names to string or number values"),
    "allowed_tools": (_is_str_list, "a list of strings"),
    "disallowed_tools": (_is_str_list, "a list of strings"),
    "allow_outside": (_is_str_list, "a list of strings"),
    "exclude": (_is_str_list, "a list of strings"),
    "setting_sources": (_is_setting_sources, "a comma-separated string of user, project, local"),
    "confine_paths": (lambda v: isinstance(v, bool), "true or false"),
    "check_env": (lambda v: isinstance(v, bool), "true or false"),
}
_TASK_KEYS = ("model", "max_turns", "timeout_s")


def _key_errors(d: dict, keys: Iterable[str], where: str) -> list[str]:
    """Type- and range-check the given keys of a suite or task mapping (a key present but null is invalid: the runner would read it as a value)."""
    return [f"{where}`{k}` must be {_KEY_RULES[k][1]}" for k in keys if k in d and not _KEY_RULES[k][0](d[k])]


def _suite_key_errors(suite: dict) -> list[str]:
    return _key_errors(suite, _KEY_RULES, "")


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
        elif not isinstance(t["prompt"], str):
            errs.append(f"{where}: prompt must be a string")
        errs += _key_errors(t, _TASK_KEYS, f"{where}: ")
        errs += [e for c in t.get("checks") or [] for e in _check_errors(c, where)]
        errs += [f"{where}: skip_checks refers to unknown suite check `{s}`" for s in t.get("skip_checks") or [] if s not in suite_ids]
        rubric = t.get("rubric")
        if rubric is not None and not (isinstance(rubric, list) and all(isinstance(x, str) for x in rubric)):
            errs.append(f"{where}: rubric must be a list of strings")
    return errs
