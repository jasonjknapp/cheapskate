# OpenAI-compatible integration

The universal path. Anything that speaks the OpenAI Chat Completions API (LangChain, LlamaIndex,
aider, a custom script) offloads to cheapskate by pointing its `base_url` at the broker. No SDK of
ours, no lock-in: it is your existing OpenAI client with one URL changed.

## Install

1. **Start the broker:**

   ```bash
   cheapskate serve
   ```

   It binds `127.0.0.1:4747` by default (loopback-only unless you opt into LAN).

2. **Set a broker key.** The broker uses bearer auth. Keys live in the broker-keys file under
   cheapskate's state dir (`XDG_STATE_HOME/cheapskate/broker-keys.json`, i.e.
   `~/.local/state/cheapskate/broker-keys.json` by default). Put a valid key in the environment:

   ```bash
   export CHEAPSKATE_KEY=<your-broker-key>
   ```

## The drop-in

Change two things in your OpenAI client and nothing else:

- `base_url` -> `http://127.0.0.1:4747/v1`
- `api_key`  -> your `CHEAPSKATE_KEY` (sent as the bearer token)

That alone makes the broker a plain OpenAI-compatible proxy to your local models.

## The `task_type` opt-in

To opt a request into econ routing (route to the cheapest model that clears the bar, local first,
cloud only on escalation, fail-closed on `never_local` classes), add a **`task_type`** field to the
request body. With the OpenAI Python SDK that is `extra_body={"task_type": "summarize"}`; with curl
it is just another JSON key. Drop the field and the call is a plain proxy.

One constraint: **do not combine `task_type` with `stream: true`.** Econ routing is non-streaming
and the broker returns a 400 for that combination. Stream a concrete `role:`/model without a
`task_type` if you need streaming.

## Files

- **[`python-openai-sdk.py`](python-openai-sdk.py)**: a runnable ~20-line example using the
  official `openai` SDK, opting into `summarize` routing via `extra_body`.
- **[`curl-examples.sh`](curl-examples.sh)**: the curl equivalents: a call with `task_type`, one
  without, and a `/v1/models` listing.

## Safety

Never send the `never_local` task types (`financial`, `legal`, `medical`, `credentials`) here to
save tokens; the broker fails closed on them (HTTP 422). Verify any offloaded output against your
acceptance criteria before trusting it.
