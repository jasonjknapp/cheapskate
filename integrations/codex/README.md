# Codex / ChatGPT CLI integration

Wire cheapskate into OpenAI Codex CLI as a token-saving offload layer.

## Install

1. **Install the MCP extra** so the stdio server can start:

   ```bash
   pip install 'cheapskate[mcp]'
   ```

2. **Register the MCP server.** Either run:

   ```bash
   codex mcp add cheapskate -- cheapskate mcp
   ```

   or append the `[mcp_servers.cheapskate]` table from [`config.toml.snippet`](config.toml.snippet)
   to your `~/.codex/config.toml` (or `.codex/config.toml` in a project).

3. **Paste the offload guidance.** Copy [`AGENTS.md.snippet`](AGENTS.md.snippet) into your
   `AGENTS.md` (project root, or `~/.codex/AGENTS.md`). This tells Codex when to delegate and what
   never to offload.

Restart Codex. The `run_task` and `econ_report` tools should now be available.

## The two files

- **[`config.toml.snippet`](config.toml.snippet)**: the MCP server registration. Adds the
  `[mcp_servers.cheapskate]` table that runs `cheapskate mcp` over stdio.
- **[`AGENTS.md.snippet`](AGENTS.md.snippet)**: the behavior: hand bulk drafting / classification /
  extraction / summarization / first-pass review / boilerplate to `run_task`, verify every result,
  never offload the `never_local` classes (`financial`, `legal`, `medical`, `credentials`).

## A note on the config format

`[mcp_servers.<name>]` with `command` / `args` / `env` (plus optional `startup_timeout_sec` and
`tool_timeout_sec`) is the documented Codex CLI stdio-MCP shape. If a future Codex release changes a
key name, the value stays the same: run `cheapskate mcp` as a stdio server. Match your installed
version's config reference if a field has moved.
