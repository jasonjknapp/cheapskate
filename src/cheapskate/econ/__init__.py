# SPDX-License-Identifier: Apache-2.0
"""The economics + judgment layer: measured (not estimated) cost of every route.

Modules:
  * ``pricing``  — load/lookup cloud model prices from a bundled snapshot.
  * ``power``    — sample local power draw (Apple Silicon) or fall back honestly.
  * ``costmath`` — pure, deterministic cost formulas (the quality bar of the repo).
  * ``report``   — consume telemetry → per-task-type receipts + recommendations.
  * ``governor`` — per-user monthly budget caps that auto-tighten the dial.

The honest differentiator lives in ``costmath``: the true cost of a task type
includes its measured retry/escalation multiplier — a task that retried locally
then escalated to the cloud is charged BOTH. Nobody else models that.
"""

from __future__ import annotations
