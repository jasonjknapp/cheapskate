# SPDX-License-Identifier: Apache-2.0
"""Routes: pure route_decision, both fail-closed classes, dial gating."""

from __future__ import annotations

from cheapskate.config import Config
from cheapskate.router import routes


def _cfg(**over):
    cfg = Config()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_never_local_fails_closed_at_every_dial():
    cfg = _cfg(never_local=["financial"])
    for lvl in (0, 1, 3):
        dec = routes.route_decision("financial", (lvl, None), cfg)
        assert dec["route"] == routes.REFUSE_NEVER_LOCAL
    dec = routes.route_decision("financial", (2, "max"), cfg)
    assert dec["route"] == routes.REFUSE_NEVER_LOCAL
    # never a silent cloud fallback: the route is a refusal, not CLOUD
    assert dec["route"] != routes.CLOUD


def test_never_cloud_forced_local_when_local_possible():
    cfg = _cfg(never_cloud=["secrets"])
    # level 1/2/3 keep it on-machine
    for lvl, sub in ((1, None), (2, "std"), (3, None)):
        dec = routes.route_decision("secrets", (lvl, sub), cfg)
        assert dec["route"] == routes.LOCAL
        assert "never_cloud" in dec["reason"]


def test_never_cloud_refuses_at_level_0_rather_than_leaving_machine():
    cfg = _cfg(never_cloud=["secrets"])
    dec = routes.route_decision("secrets", (0, None), cfg)
    assert dec["route"] == routes.REFUSE_NEVER_CLOUD


def test_never_local_wins_if_listed_in_both():
    cfg = _cfg(never_local=["x"], never_cloud=["x"])
    dec = routes.route_decision("x", (2, "std"), cfg)
    assert dec["route"] == routes.REFUSE_NEVER_LOCAL


def test_unknown_task_type():
    cfg = Config()
    dec = routes.route_decision("no-such-type", (2, "std"), cfg)
    assert dec["route"] == routes.UNKNOWN


def test_level_0_sends_local_capable_work_to_cloud():
    cfg = Config()  # summarize is local-first, tier safe
    dec = routes.route_decision("summarize", (0, None), cfg)
    assert dec["route"] == routes.CLOUD_DOWNGRADED


def test_level_3_forces_local():
    cfg = Config()
    dec = routes.route_decision("boilerplate", (3, None), cfg)  # min_level 2
    assert dec["route"] == routes.LOCAL


def test_min_level_floor():
    cfg = Config()  # boilerplate has min_level=2
    assert routes.route_decision("boilerplate", (1, None), cfg)["route"] == routes.CLOUD_DOWNGRADED
    assert routes.route_decision("boilerplate", (2, "std"), cfg)["route"] == routes.LOCAL


def test_lite_sub_dial_only_routes_safe_tier_locally():
    cfg = Config()
    # summarize is tier 'safe' → local even under lite
    assert routes.route_decision("summarize", (2, "lite"), cfg)["route"] == routes.LOCAL
    # review is tier 'std' → downgraded under lite
    assert routes.route_decision("review", (2, "lite"), cfg)["route"] == routes.CLOUD_DOWNGRADED
    # but local under std
    assert routes.route_decision("review", (2, "std"), cfg)["route"] == routes.LOCAL


def test_escalation_patience_signal():
    cfg = Config()
    assert routes.route_decision("summarize", (2, "max"), cfg)["escalation_patience"] == "tolerate-retry"
    assert routes.route_decision("summarize", (2, "std"), cfg)["escalation_patience"] == "escalate-fast"
    assert routes.route_decision("summarize", (3, None), cfg)["escalation_patience"] == "escalate-fast"


def test_decision_is_pure():
    cfg = Config()
    a = routes.route_decision("summarize", (2, "std"), cfg)
    b = routes.route_decision("summarize", (2, "std"), cfg)
    assert a == b
