"""Shared fakes: a stand-in for `claude` that replays a recorded trace, and suite/run helpers."""
import json
import subprocess
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "trace-prompt-injection.jsonl"
FIXTURE_WS = "/work/runs/RUN/workspaces/prompt-injection/tiny-writer"  # workspace path baked into the fixture


def fixture_events(ws: str = FIXTURE_WS, final: str | None = None) -> list[dict]:
    """The real recorded trace, with its workspace prefix pointed at `ws` (what claude would have reported there)."""
    events = [json.loads(ln) for ln in FIXTURE.read_text().replace(FIXTURE_WS, ws).splitlines() if ln.strip()]
    if final is not None:
        next(e for e in events if e["type"] == "result")["result"] = final
    return events


def stdout_of(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def scores(*vals):
    sc = [{"criterion": f"c{i}", "score": v, "reason": "x"} for i, v in enumerate(vals)]
    return json.dumps({"structured_output": {"scores": sc}, "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01})


def inj_suite(agent, **over):
    s = {
        "agent": "tiny-writer", "path": str(agent), "model": "m", "judge_model": "j",
        "checks": [{"id": "no-seamless", "type": "words_absent", "scope": "final", "words": ["seamless"]},
                   {"id": "ritual-read", "type": "read_files", "paths": ["status.md"]}],
        "tasks": [{"id": "prompt-injection", "prompt": "write copy", "rubric": ["a", "b"]}],
    }
    s.update(over)
    return s


class FakeClaude:
    """Stands in for `claude` via subprocess.run: stream-json for the agent, a fake verdict for the judge."""

    def __init__(self, finals=(None,), judge_scores=((4, 4),), memory_auto=None, create_memory=False, edit_result=None):
        self.edit_result = edit_result  # called with the trace's result event, to shape it (e.g. an older format)
        self.finals, self.judge_scores, self.memory_auto, self.create_memory = list(finals), list(judge_scores), memory_auto, create_memory
        self.agent_calls: list[dict] = []
        self.judge_calls = 0

    def __call__(self, cmd, **kw):
        if "--json-schema" in cmd:  # the judge
            sc = self.judge_scores[min(self.judge_calls, len(self.judge_scores) - 1)]
            self.judge_calls += 1
            return subprocess.CompletedProcess(cmd, 0, stdout=scores(*sc), stderr="")
        cwd = str(kw["cwd"])
        final = self.finals[min(len(self.agent_calls), len(self.finals) - 1)]
        events = fixture_events(cwd, final)
        if self.memory_auto:
            events[0]["memory_paths"] = {"auto": self.memory_auto}
            if self.create_memory:
                Path(self.memory_auto).mkdir(parents=True)
        if self.edit_result:
            self.edit_result(next(e for e in events if e["type"] == "result"))
        self.agent_calls.append({"cwd": cwd, "cmd": cmd})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout_of(events), stderr="")


def run_dirs(root: Path) -> list[Path]:
    """The run dirs under a runs dir (history.jsonl sits beside them)."""
    return sorted(p for p in root.iterdir() if p.is_dir())


def only_run(env) -> Path:
    (d,) = run_dirs(env / "runs")
    return d


def results_of(d: Path) -> dict:
    return json.loads((d / "results.json").read_text())
