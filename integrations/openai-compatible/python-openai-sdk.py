#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drop-in cheapskate offload via the official OpenAI Python SDK.

cheapskate's broker speaks the OpenAI Chat Completions API, so any OpenAI-client
tool (LangChain, LlamaIndex, aider, a custom script) offloads to it by pointing
base_url at the broker. Adding a task_type opts the request into econ routing
(local first, cloud only on escalation, fail-closed on never_local classes).

Prereqs:
  * pip install openai
  * cheapskate serve                # start the broker on 127.0.0.1:4747
  * export CHEAPSKATE_KEY=<key>     # a broker key; keys live in the broker-keys
                                    # file under cheapskate's state dir
                                    # (XDG_STATE_HOME/cheapskate/broker-keys.json)
"""
import os

from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:4747/v1",  # the broker, not api.openai.com
    api_key=os.environ["CHEAPSKATE_KEY"],  # sent as a bearer token
)

# task_type is a cheapskate extension field: it goes in the request body via
# extra_body and opts this call into econ routing. Drop it and the broker is a
# plain role/model proxy. (Do NOT set stream=True with a task_type: the broker
# rejects that combination; econ routing is non-streaming.)
resp = client.chat.completions.create(
    model="role:reasoning",  # a registry role; see: cheapskate models list
    messages=[
        {"role": "system", "content": "You summarize tersely. No preamble."},
        {"role": "user", "content": "Summarize this in three bullets: <paste long text here>"},
    ],
    extra_body={"task_type": "summarize"},  # <- the econ-routing opt-in
)

print(resp.choices[0].message.content)

# Verify the result against your acceptance criteria before trusting it, and
# never route financial / legal / medical / credentials work this way: those
# task types are never_local and the broker fails closed (HTTP 422).
