# cheapskate

**Routes every task to the cheapest model that passes your quality bar — local or cloud — and
shows you the receipts.**

Cheapskate is the economics + judgment layer above your serving engines (Ollama, MLX, LM Studio)
and cloud APIs. It is not a gateway and not a serving engine: it decides *where each task should
run* based on a spend dial, per-task-type routing rules, hard safety classes (never-local AND
never-cloud, both fail closed), and — coming in the next milestones — measured-on-your-hardware
economics with monthly savings receipts.

> **Status: v0.1 under construction.** Core extraction is in progress; the econ engine,
> cloud adapters, eval harness, and full docs land in the next milestones. Not ready for use yet.

- Architecture and conventions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- License: Apache-2.0
