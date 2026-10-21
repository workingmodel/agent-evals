"""Eval harness for Claude Code agents."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wm-agent-evals")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
