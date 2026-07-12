# Agent offload kit

Drop-in files that wire cheapskate in as a **token-saving offload layer** for the coding agents
you already run (Claude Code, Gemini CLI, Codex/ChatGPT, and anything that speaks the OpenAI API).

The point: your expensive cloud agent stops burning premium tokens on cheap, bulk subtasks. It
hands the drafting, classifying, extracting, first-pass reviewing, and boilerplate to cheapskate,
which routes each one to the cheapest local model that clears your quality bar (verify-and-repair,
with a cloud escalation only when local can't pass). The agent keeps its own tokens for the work
that actually needs them.

## The mental model

**Let the expensive agent DECIDE and REVIEW. Let cheapskate DRAFT and CLASSIFY.**

- The premium agent owns judgement: architecture, the plan, final synthesis, the last review pass,
  anything load-bearing.
- cheapskate owns volume: turn N drafts, classify M items, extract fields from a blob, summarize a
  long file, produce boilerplate, do a first-pass code review the agent then sanity-checks.
- Every offloaded result comes back with acceptance criteria already applied (cheapskate verifies
  and repairs before returning), and the agent still checks it before trusting it. Two cheap passes
  plus one expensive glance beats one expensive pass on everything.

## Two integration paths

| Path | For | How |
|---|---|---|
| **MCP server** | MCP-capable agents (Claude Code, Gemini CLI, Codex) | Register `cheapskate mcp` (stdio). The agent gets a `run_task` tool (route + verify-and-repair + fail-closed safety) and an `econ_report` tool (the monthly savings receipt). Needs the `mcp` extra: `pip install 'cheapskate[mcp]'`. |
| **OpenAI-compatible endpoint** | Everything else (LangChain, LlamaIndex, aider, custom scripts) | Point the client's `base_url` at the broker's `/v1` (`http://127.0.0.1:4747/v1`), bearer auth. Add a `task_type` field to opt a request into econ routing; drop it and it's a plain proxy. Run `cheapskate serve` first. |

## Which drop-in file to use

| Tool | Files |
|---|---|
| **Claude Code** | [claude-code/](claude-code/): `cheapskate.mcp.json`, `CLAUDE.md.snippet`, optional offload skill |
| **Gemini CLI** | [gemini-cli/](gemini-cli/): `settings.snippet.json`, `GEMINI.md.snippet` |
| **Codex / ChatGPT CLI** | [codex/](codex/): `config.toml.snippet`, `AGENTS.md.snippet` |
| **Any OpenAI-API tool** | [openai-compatible/](openai-compatible/): `python-openai-sdk.py`, `curl-examples.sh` |

## Safety (read this before you offload anything)

cheapskate ships four **`never_local`** classes that must never be answered by a weak local model
and have no silent cloud fallback: **`financial`, `legal`, `medical`, `credentials`**. The MCP tool
and the endpoint both **fail closed** on these (a hard refusal, not a downgrade), and your offload
instructions tell the agent to keep that work on the premium tier. Do not try to route it through
cheapskate to save tokens. For everything you do offload, the agent's job is unchanged: it states
explicit acceptance criteria and **verifies the returned output against them before using it.** That
is not optional politeness, it is the honest usage pattern, and it matches cheapskate's own
verify-and-repair design.

## Task-type vocabulary

The `task_type` you pass (to `run_task` or in the OpenAI body) selects the routing rule. The shipped
set: `summarize`, `draft`, `classify`, `extract`, `review`, `boilerplate`. The four `never_local`
classes above are refused by design. You can add or retune task types in `config.yaml`; these are
the defaults every drop-in here uses.
