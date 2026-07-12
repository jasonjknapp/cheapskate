# SPDX-License-Identifier: Apache-2.0
"""Power: never invokes sudo or a real process; parses powermetrics text via an
injected runner; degrades honestly to estimate / unknown modes."""

from __future__ import annotations

from cheapskate.econ import power

# A realistic powermetrics fragment (milliwatts).
_PM_SAMPLE = """
*** Sampled system activity ***
CPU Power: 8200 mW
GPU Power: 12100 mW
ANE Power: 0 mW
Combined Power (CPU + GPU + ANE): 20300 mW
"""


def test_parse_watts_from_powermetrics_text():
    watts = power.parse_powermetrics_watts(_PM_SAMPLE)
    # combined 20300 mW = 20.3 W, the largest package-level figure
    assert watts == 20.3


def test_parse_returns_none_without_power_line():
    assert power.parse_powermetrics_watts("no power here") is None
    assert power.parse_powermetrics_watts("") is None


def test_is_apple_silicon_detection():
    assert power.is_apple_silicon(system="Darwin", machine="arm64") is True
    assert power.is_apple_silicon(system="Darwin", machine="x86_64") is False
    assert power.is_apple_silicon(system="Linux", machine="arm64") is False


def test_measured_mode_uses_injected_runner_never_sudo():
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(cmd)
        return _PM_SAMPLE

    reading = power.read_power(
        runner=runner, allow_measure=True, system="Darwin", machine="arm64"
    )
    assert reading.mode == "measured"
    assert reading.watts == 20.3
    assert reading.known is True
    # the command we asked to run must NOT contain sudo — the runner owns privilege
    assert calls and "sudo" not in calls[0]
    assert calls[0][0] == "powermetrics"


def test_no_measure_by_default_even_with_runner():
    """allow_measure defaults False: no probe unless explicitly opted in."""
    called = False

    def runner(cmd):
        nonlocal called
        called = True
        return _PM_SAMPLE

    reading = power.read_power(runner=runner, watts_estimate=15.0)
    assert called is False
    assert reading.mode == "estimate"
    assert reading.watts == 15.0


def test_static_estimate_mode():
    reading = power.read_power(watts_estimate=30.0)
    assert reading.mode == "estimate"
    assert reading.watts == 30.0
    assert reading.known is True


def test_unknown_mode_omits_watts():
    reading = power.read_power()
    assert reading.mode == "unknown"
    assert reading.watts is None
    assert reading.known is False


def test_non_apple_silicon_skips_measurement_falls_to_estimate():
    reading = power.read_power(
        runner=lambda cmd: _PM_SAMPLE,
        allow_measure=True,
        watts_estimate=25.0,
        system="Linux",
        machine="x86_64",
    )
    # not apple silicon → no measurement → falls back to the estimate
    assert reading.mode == "estimate"
    assert reading.watts == 25.0


def test_measure_failure_degrades_to_estimate():
    def boom(cmd):
        raise RuntimeError("powermetrics not permitted")

    reading = power.read_power(
        runner=boom, allow_measure=True, watts_estimate=18.0,
        system="Darwin", machine="arm64",
    )
    assert reading.mode == "estimate"
    assert reading.watts == 18.0


def test_measure_failure_no_estimate_is_unknown():
    def boom(cmd):
        raise RuntimeError("nope")

    reading = power.read_power(
        runner=boom, allow_measure=True, system="Darwin", machine="arm64",
    )
    assert reading.mode == "unknown"
    assert reading.watts is None


def test_command_shape_has_no_sudo():
    cmd = power._powermetrics_cmd()
    assert "sudo" not in cmd
    assert cmd[0] == "powermetrics"
