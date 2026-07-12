---
name: offload
description: Offload cheap, bulk subtasks (drafting, classification, extraction, summarization, first-pass code review, boilerplate) to cheapskate's run_task MCP tool instead of spending premium tokens on them. Use when you are about to do repetitive, low-judgement work by hand and a local model could clear the bar. Never offload financial, legal, medical, or credential work.
---

# Offload to cheapskate

Hand volume to cheapskate; keep your tokens for judgement. This skill assumes the cheapskate MCP
server is registered (tools `run_task` and `econ_report`). If it is not, tell the user to install it
per `integrations/claude-code/README.md`.

## When to use this

You are about to produce, by hand, a batch of low-judgement output: drafts, labels, extracted
fields, a summary of a long file, a first-pass review, or boilerplate. Offload it instead.

## How to offload

1. **Pick the task type**: one of `summarize`, `draft`, `classify`, `extract`, `review`,
   `boilerplate`.
2. **Write explicit acceptance criteria**: the exact shape, length, and constraints the output
   must meet. Vague criteria get vague output; specific criteria let cheapskate verify and repair.
3. **Call the tool**:

   ```
   run_task(task_type="<type>", criteria="<precise acceptance criteria>", payload="<the input>")
   ```

4. **Verify the result.** Read `output` against your `criteria`. Use it only if it passes. If `ok`
   is false, `route` is `refused`, or the output misses the criteria, do the task yourself.

## Hard rule: never offload never_local classes

`financial`, `legal`, `medical`, and `credentials` are `never_local`. cheapskate refuses them by
design, and you must not try to route money, legal, medical, or secret-handling work through
`run_task` to save tokens. That work stays with you on the premium tier.

## Check the savings

Call `econ_report()` to show the user the monthly receipt: how much work was routed, the quality
pass rate, and the true local-vs-cloud cost.
