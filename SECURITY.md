# Security

## Reporting a vulnerability

Please report vulnerabilities privately. Use GitHub's **Report a vulnerability** option (private vulnerability reporting) on the Security tab of this repository. Please don't open a public issue for a security problem.

Include what you found, how to reproduce it, and which version you tested.

## Scope

agent-evals runs a Claude Code agent with real tool access on your machine. Read this before you run a suite you didn't write.

- **The workspace is a copy, not a sandbox.** The agent works in a copy of its folder, but it can still reach other paths. The harness flags out-of-workspace file access after the fact. It doesn't block it. See "Isolation and its limits" in the README.
- **Suites are code-like.** A suite chooses the agent folder, the tools the agent gets and the prompts it runs. Review a suite before you run it.
- **Traces can hold sensitive content.** Files under `runs/` include everything the agent read and wrote, including parts of your agent's files. `runs/` is gitignored. Don't commit or share it without checking.

Reports about these documented limits are welcome as suggestions. Open a normal issue for those.
