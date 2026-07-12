#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# cheapskate broker via curl. The broker speaks the OpenAI Chat Completions API,
# so these are ordinary OpenAI calls with base_url pointed at the broker.
#
# Prereqs:
#   cheapskate serve                 # start the broker on 127.0.0.1:4747
#   export CHEAPSKATE_KEY=<key>      # a broker key (bearer token); keys live in
#                                    # broker-keys.json under cheapskate's state dir
set -euo pipefail

BASE="http://127.0.0.1:4747/v1"
AUTH="Authorization: Bearer ${CHEAPSKATE_KEY}"

# 1) With task_type: opt into econ routing (local first, cloud only on escalation,
#    fail-closed on never_local classes). task_type is a body field. Keep
#    stream=false (the default) with task_type: the broker rejects streaming +
#    task_type. Do NOT send financial/legal/medical/credentials task types here.
curl -sS "${BASE}/chat/completions" \
  -H "${AUTH}" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "role:reasoning",
        "task_type": "summarize",
        "messages": [
          {"role": "system", "content": "You summarize tersely. No preamble."},
          {"role": "user", "content": "Summarize this in three bullets: <text>"}
        ]
      }'
echo

# 2) Without task_type: a plain OpenAI-compatible proxy to the resolved role/model.
curl -sS "${BASE}/chat/completions" \
  -H "${AUTH}" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "role:reasoning",
        "messages": [
          {"role": "user", "content": "Say hi in one word."}
        ]
      }'
echo

# 3) List the registry roles the broker exposes as OpenAI models (role:<name>).
curl -sS "${BASE}/models" -H "${AUTH}"
echo
