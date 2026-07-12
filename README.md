# cheapskate

**Routes every task to the cheapest model that passes your quality bar — local or cloud — and
shows you the receipts.**

Cheapskate is the economics + judgment layer above your serving engines (Ollama, MLX, LM Studio)
and cloud APIs. It is not a gateway and not a serving engine: it decides *where each task should
run* based on a spend dial, per-task-type routing rules, hard safety classes (never-local AND
never-cloud, both fail closed), and — coming in the next milestones — measured-on-your-hardware
economics with monthly savings receipts.

> **Status: v0.1 under construction.** Core extraction, the econ engine, and the cloud tier
> are in place; the eval harness and full docs land in the next milestones. Not ready for use yet.

Surfaces available today:

- **Local routing + verify-and-repair** — `cheapskate task` runs a supervised subtask, routed by the spend dial and fail-closed safety classes.
- **Cloud tier (thin adapters, OFF by default)** — BYO keys via env; `openai-compat` covers OpenAI / OpenRouter / Gemini-compat, `anthropic` covers Claude.
- **Budget governor** — per-user monthly cloud-spend caps that auto-tighten the dial toward local before a request reaches the cloud.
- **Econ receipts** — `cheapskate econ` / `cheapskate report [--share]` show the measured local-vs-cloud cost and monthly savings.
- **OpenAI-compatible endpoint** — point any OpenAI-client tool's `base_url` at the broker's `/v1/chat/completions`; a `task_type` field opts the request into econ routing.
- **MCP server** — `cheapskate mcp` (stdio) exposes `run_task` and `econ_report` to any MCP client (needs the `mcp` extra).

- Architecture and conventions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- License: Apache-2.0
