# Gemini CLI integration

Wire cheapskate into Gemini CLI as a token-saving offload layer.

## Install

1. **Install the MCP extra** so the stdio server can start:

   ```bash
   pip install 'cheapskate[mcp]'
   ```

2. **Register the MCP server.** Merge the `cheapskate` entry from
   [`settings.snippet.json`](settings.snippet.json) into the `mcpServers` object of your Gemini CLI
   `settings.json` (`~/.gemini/settings.json` for all projects, or `.gemini/settings.json` in a
   project). If your `settings.json` has no `mcpServers` key yet, add it.

3. **Paste the offload guidance.** Copy [`GEMINI.md.snippet`](GEMINI.md.snippet) into your
   `GEMINI.md` (project root, or `~/.gemini/GEMINI.md`). This is what tells Gemini CLI when to
   delegate and what never to offload.

Restart Gemini CLI. The `run_task` and `econ_report` tools should now be available.

## The two files

- **[`settings.snippet.json`](settings.snippet.json)**: the MCP server registration
  (`mcpServers.cheapskate`, running `cheapskate mcp` over stdio).
- **[`GEMINI.md.snippet`](GEMINI.md.snippet)**: the behavior: hand bulk drafting / classification /
  extraction / summarization / first-pass review / boilerplate to `run_task`, verify every result,
  never offload the `never_local` classes (`financial`, `legal`, `medical`, `credentials`).

## A note on the config format

The `mcpServers` object and its `command` / `args` / `cwd` / `env` / `timeout` / `trust` fields
(with `$VAR` env expansion) are the documented Gemini CLI MCP settings shape. If a future Gemini CLI
release renames a field, the value stays the same: run `cheapskate mcp` as a stdio server. Adjust
the surrounding key names to match your installed version's docs if needed.
