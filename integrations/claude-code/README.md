# Claude Code integration

Wire cheapskate into Claude Code as a token-saving offload layer. Two files, plus one optional skill.

## Install

1. **Install the MCP extra** so the stdio server can start:

   ```bash
   pip install 'cheapskate[mcp]'
   ```

2. **Register the MCP server.** Either run:

   ```bash
   claude mcp add cheapskate -- cheapskate mcp
   ```

   or copy the `cheapskate` entry from [`cheapskate.mcp.json`](cheapskate.mcp.json) into the
   `mcpServers` object of your project's `.mcp.json` (at the repo root).

3. **Paste the offload guidance.** Copy [`CLAUDE.md.snippet`](CLAUDE.md.snippet) into your
   `CLAUDE.md` (project-level, or `~/.claude/CLAUDE.md` for every project). This is what actually
   tells Claude Code when and how to delegate, and what never to offload.

Restart Claude Code (or reload MCP servers). You should now see the `run_task` and `econ_report`
tools available.

## The two files

- **[`cheapskate.mcp.json`](cheapskate.mcp.json)**: the MCP server registration. Registers
  `cheapskate mcp` as a stdio server named `cheapskate`.
- **[`CLAUDE.md.snippet`](CLAUDE.md.snippet)**: the behavior. Tells Claude Code to hand bulk
  drafting / classification / extraction / summarization / first-pass review / boilerplate to
  `run_task` with explicit acceptance criteria, to verify every result, and to never offload the
  `never_local` classes (`financial`, `legal`, `medical`, `credentials`).

## Optional: the offload skill

[`skills/offload/SKILL.md`](skills/offload/SKILL.md) packages the same behavior as an invokable
Claude Code skill (a `SKILL.md` under a skills directory). Use it if you prefer an explicit
`/offload`-style trigger over always-on CLAUDE.md guidance. The CLAUDE.md snippet and the skill are
complementary; you can ship either or both.
