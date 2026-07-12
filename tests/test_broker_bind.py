# SPDX-License-Identifier: Apache-2.0
"""Broker bind-host policy: ``resolve_bind_host`` enforces the ``bind_loopback``
guard (R1). Pure — no server is started — so the policy is unit-testable."""

from __future__ import annotations

import pytest

from cheapskate.broker import app
from cheapskate.config import BrokerConfig, Config


def _cfg(**broker) -> Config:
    return Config(broker=BrokerConfig(**broker))


def test_non_loopback_host_with_loopback_guard_raises():
    # host 0.0.0.0, bind_lan off, bind_loopback on (default) → hard error
    with pytest.raises(RuntimeError) as e:
        app.resolve_bind_host(_cfg(host="0.0.0.0", bind_lan=False, bind_loopback=True))
    msg = str(e.value)
    assert "bind_lan" in msg and "bind_loopback" in msg  # actionable remedy


def test_loopback_host_with_defaults_ok():
    # host 127.0.0.1, defaults (bind_loopback True) → allowed as-is
    assert app.resolve_bind_host(_cfg(host="127.0.0.1")) == "127.0.0.1"


def test_bind_lan_widens_non_loopback():
    # bind_lan opt-in → configured host used (loopback host widened to 0.0.0.0)
    assert app.resolve_bind_host(_cfg(host="0.0.0.0", bind_lan=True)) == "0.0.0.0"
    assert app.resolve_bind_host(_cfg(host="127.0.0.1", bind_lan=True)) == "0.0.0.0"


def test_bind_loopback_false_allows_non_loopback():
    # bind_loopback explicitly off → configured host used as-is
    assert app.resolve_bind_host(_cfg(host="0.0.0.0", bind_loopback=False)) == "0.0.0.0"


def test_localhost_and_ipv6_loopback_are_loopback():
    assert app.resolve_bind_host(_cfg(host="localhost")) == "localhost"
    assert app.resolve_bind_host(_cfg(host="::1")) == "::1"
