from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from familytrade.simulation.engine import initialize_engine, run_engine, step_engine
from familytrade.simulation.state import ZoneState
from familytrade.strategies.definitions import (
    BreakoutRetestSetup,
    ConfirmedPivotZonesSetup,
    ConstantNode,
    ExitPolicy,
    ReversalSetup,
    SetupStop,
    SetupTarget,
)
from familytrade.strategies.validation import canonical_definition_sha256

from .conftest import BASE, make_bar, make_config

_FIXTURE = Path(__file__).parents[2] / "docs" / "strategies" / "reversal-breakout-examples-v1.json"
_FIXTURE_SHA = "fd62857956ffae7eb9b5cd7b07be10054ad4b16b8654e629c8efdaba3aa7ac56"


def _case(case_id: str) -> dict[str, object]:
    payload = _FIXTURE.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == _FIXTURE_SHA
    document = json.loads(payload)
    return next(item for item in document["cases"] if item["id"] == case_id)


def _configured(
    *,
    breakout: bool = True,
    reversal: bool = False,
    sides: str = "both",
    strict: bool = False,
    expiry: int = 24,
    directional: bool = False,
    recent_peak: bool = False,
    entry_filter_fail: bool = False,
    target_mode: str = "measured_move",
):
    config = make_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "side_policy": sides,
            "nodes": (
                (
                    ConstantNode(
                        kind="constant",
                        node_id="entry-filter",
                        value_type="boolean",
                        unit="boolean",
                        value=False,
                    ),
                )
                if entry_filter_fail
                else ()
            ),
            "entry_rules": config.strategy_version.definition.entry_rules.model_copy(
                update={"long_root": None, "short_root": None}
            ),
            "entry_combination": "setups_only",
            "exit_policy": ExitPolicy(
                kind="bracket_exit_v1",
                stop=SetupStop(kind="setup_price", field="stop"),
                target=SetupTarget(kind="setup_price", field="target"),
            ),
            "setup_modules": (
                ConfirmedPivotZonesSetup(
                    kind="confirmed_pivot_zones_v1",
                    zone_interval_seconds=300,
                    use_atr=False,
                    atr_length=2,
                    pivot_left=1,
                    pivot_right=1,
                    merge_multiple="0.25",
                    max_width_multiple="0.5",
                    minimum_touches=1,
                    max_zones=12,
                    zone_max_age_bars=500,
                    cooldown_execution_bars=1,
                ),
                ReversalSetup(
                    kind="reversal_setup_v1",
                    enabled=reversal,
                    sides=sides,
                    approach_multiple="6",
                    require_directional_approach=directional,
                    stop_buffer_multiple="1",
                    recent_peak_stop=recent_peak,
                    peak_lookback=3,
                    target_mode="r_multiple" if target_mode == "measured_move" else target_mode,
                    measured_move_multiple="1",
                    r_multiple="3",
                    filter_root=None,
                ),
                BreakoutRetestSetup(
                    kind="breakout_retest_v1",
                    enabled=breakout,
                    sides=sides,
                    confirmation_mode="strict_cross" if strict else "beyond",
                    break_multiple="1",
                    pullback_multiple="0.5",
                    setup_expiry_execution_bars=expiry,
                    stop_buffer_multiple="0.8",
                    use_vwap_stop=False,
                    target_mode=target_mode,
                    measured_move_multiple="1",
                    r_multiple="3",
                    arm_filter_root=None,
                    entry_filter_root="entry-filter" if entry_filter_fail else None,
                ),
            ),
        }
    )
    strategy = config.strategy_version.model_copy(
        update={
            "definition": definition,
            "canonical_definition_sha256": canonical_definition_sha256(definition),
        }
    )
    return config.model_copy(update={"strategy_version": strategy})


def _zone(*, kind: str, low: str, high: str, sequence: int = 0) -> ZoneState:
    return ZoneState(
        zone_id=f"018f4c00-0000-7{sequence + 100:03x}-8000-{sequence + 100:012x}",
        kind=kind,
        low=Decimal(low),
        high=Decimal(high),
        creation_sequence=sequence,
        touch_count=1,
        created_zone_index=0,
        last_touch_zone_index=0,
        last_filled_execution_index=None,
        pivot_bar_end=BASE,
        confirmation_bar_end=BASE,
        known_at=BASE,
        source_bar_record_ids=(),
        source_dataset_provenance=(),
    )


def _seed(config, *zones: ZoneState):
    return initialize_engine(config).model_copy(
        update={"zones": zones, "next_zone_sequence": len(zones)}
    )


def _bars(config, first: int, last: int, **prices):
    if "close" in prices:
        prices.setdefault("open_price", prices["close"])
        prices.setdefault("high", prices["close"])
        prices.setdefault("low", prices["close"])
    return tuple(make_bar(config, index, **prices) for index in range(first, last))


def test_reversal_breakout_long_breakout_levels_and_next_event() -> None:
    case = _case("long_breakout_levels_and_next_event")
    config = _configured(sides="long")
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    armed = run_engine(
        config,
        _bars(config, 0, 5, high="2006", low="2004", close="2005"),
        initial_state=state,
    )
    assert armed.decisions[-1].decision_type == "ARM"
    assert armed.decisions[-1].reason_code == case["expected"][0].split(" at")[0]
    snapshot = armed.decisions[-1].evidence.selected_setup
    assert snapshot is not None
    assert (snapshot.entry, snapshot.stop, snapshot.target) == (
        Decimal("2001.0"),
        Decimal("1999.2"),
        Decimal("2005.0"),
    )
    retest = run_engine(
        config,
        _bars(config, 5, 10, high="2004", low="2000.9", close="2003"),
        initial_state=armed.state,
    )
    assert retest.decisions[-1].decision_type == "ENTRY"
    assert retest.state.pending_entry is not None
    waiting = step_engine(
        config,
        retest.state,
        make_bar(config, 10, open_price="2001", high="2002", low="2000.9", close="2001"),
    )
    assert waiting.fills == ()
    filled = step_engine(
        config,
        waiting.state,
        make_bar(config, 11, open_price="2001", high="2002", low="2000.9", close="2001"),
    )
    assert filled.fills[0].fill_price == Decimal("2001.0")
    assert filled.state.position is not None


def test_reversal_breakout_short_breakout_levels_and_next_event() -> None:
    _case("short_breakout_levels_and_next_event")
    config = _configured(sides="short")
    state = _seed(config, _zone(kind="resistance", low="2010", high="2010.5"))
    armed = run_engine(
        config,
        _bars(config, 0, 5, high="2007", low="2005", close="2005.5"),
        initial_state=state,
    )
    snapshot = armed.decisions[-1].evidence.selected_setup
    assert snapshot is not None and armed.decisions[-1].reason_code == "ARM_SHORT"
    assert (snapshot.entry, snapshot.stop, snapshot.target) == (
        Decimal("2009.5"),
        Decimal("2011.3"),
        Decimal("2005.5"),
    )
    entered = run_engine(
        config,
        _bars(config, 5, 10, high="2009.6", low="2007", close="2008"),
        initial_state=armed.state,
    )
    assert entered.decisions[-1].decision_type == "ENTRY"
    assert entered.state.pending_entry is not None


def test_reversal_rejected_when_not_approaching() -> None:
    _case("reversal_rejected_when_not_approaching")
    config = _configured(breakout=False, reversal=True, sides="long", directional=True)
    state = _seed(config, _zone(kind="support", low="1999", high="2000"))
    first = run_engine(config, _bars(config, 0, 5, close="2003"), initial_state=state)
    second = run_engine(config, _bars(config, 5, 10, close="2004"), initial_state=first.state)
    assert second.decisions[-1].decision_type == "HOLD"
    assert second.decisions[-1].reason_code == "NOT_APPROACHING"
    assert second.state.pending_entry is None


def test_breakout_expiration_boundary() -> None:
    _case("breakout_expiration_boundary")
    config = _configured(sides="long", expiry=1)
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    armed = run_engine(
        config, _bars(config, 0, 5, low="2004", high="2006", close="2005"), initial_state=state
    )
    eligible = run_engine(
        config,
        _bars(config, 5, 10, low="2002", high="2004", close="2003"),
        initial_state=armed.state,
    )
    assert eligible.decisions[-1].reason_code == "NOT_RETESTED"
    expired = run_engine(
        config,
        _bars(config, 10, 15, low="2002", high="2004", close="2003"),
        initial_state=eligible.state,
    )
    assert (expired.decisions[-1].decision_type, expired.decisions[-1].reason_code) == (
        "CANCEL",
        "SETUP_EXPIRED",
    )
    assert expired.state.setups[0].status == "EXPIRED"


def test_beyond_mode_is_not_strict_cross() -> None:
    _case("beyond_mode_is_not_strict_cross")
    base = _configured(sides="long")
    strict = _configured(sides="long", strict=True)
    base_state = _seed(base, _zone(kind="support", low="2000", high="2000.5"))
    strict_state = _seed(strict, _zone(kind="support", low="2000", high="2000.5"))
    warm_base = run_engine(base, _bars(base, 0, 5, close="2005"), initial_state=base_state)
    warm_strict = run_engine(strict, _bars(strict, 0, 5, close="2005"), initial_state=strict_state)
    base_result = run_engine(
        base, _bars(base, 5, 10, close="2005.2"), initial_state=warm_base.state
    )
    strict_result = run_engine(
        strict, _bars(strict, 5, 10, close="2005.2"), initial_state=warm_strict.state
    )
    assert warm_base.decisions[-1].decision_type == "ARM"
    assert base_result.decisions[-1].reason_code == "NOT_RETESTED"
    assert strict_result.decisions[-1].decision_type == "HOLD"


def test_same_bar_break_and_retest_is_not_causal_entry() -> None:
    _case("same_bar_break_and_retest_is_not_causal_entry")
    config = _configured(sides="long")
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    result = run_engine(
        config,
        _bars(config, 0, 5, low="2000.8", high="2006", close="2005"),
        initial_state=state,
    )
    assert result.decisions[-1].decision_type == "ARM"
    assert result.state.pending_entry is None
    assert result.state.setups[0].snapshot.arm_execution_index == 0


def test_opposing_breakout_arms_are_atomic_conflict() -> None:
    _case("opposing_breakout_arms_are_atomic_conflict")
    config = _configured(sides="both")
    state = _seed(
        config,
        _zone(kind="support", low="1980", high="1981", sequence=0),
        _zone(kind="resistance", low="2020", high="2021", sequence=1),
    )
    result = run_engine(config, _bars(config, 0, 5, close="2000"), initial_state=state)
    assert result.decisions[-1].decision_type == "REJECT"
    assert result.decisions[-1].reason_code == "CONFLICT_OPPOSING_ARMS"
    assert result.state.setups == () and result.state.pending_entry is None


def test_entry_intent_filter_rejection_cancels_arm() -> None:
    case = _case("entry_intent_filter_rejection_cancels_arm")
    config = _configured(sides="long", entry_filter_fail=True)
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    armed = run_engine(
        config, _bars(config, 0, 5, low="2004", high="2006", close="2005"), initial_state=state
    )
    assert case["given"]["filter"]["minimum"] == "1.20"
    rejected = run_engine(
        config,
        _bars(config, 5, 10, high="2004", low="2000.9", close="2003"),
        initial_state=armed.state,
    )
    assert (
        rejected.decisions[-1].decision_type,
        rejected.decisions[-1].reason_code,
        rejected.state.setups[0].status,
    ) == (
        "REJECT",
        "FILTER_FAIL",
        "CANCELLED",
    )
    assert rejected.decisions[-1].evidence.results[0].result == "FAIL"
    assert rejected.state.pending_entry is None


def test_decision_atr_reference_missing_and_atr_off() -> None:
    _case("decision_atr_reference_missing_and_atr_off")
    config = _configured(sides="long")
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    result = run_engine(config, _bars(config, 0, 5, close="2005"), initial_state=state)
    snapshot = result.decisions[-1].evidence.selected_setup
    assert snapshot is not None and snapshot.unit == "1"
    assert all(value.known_at <= snapshot.known_at for value in snapshot.frozen_feature_values)


def test_cooldown_and_daily_cap_are_fill_counted() -> None:
    _case("cooldown_and_daily_cap_are_fill_counted")
    config = _configured(sides="long")
    zone = _zone(kind="support", low="2000", high="2000.5").model_copy(
        update={"last_filled_execution_index": 0}
    )
    state = _seed(config, zone).model_copy(update={"last_execution_slot_index": 0})
    blocked = run_engine(config, _bars(config, 0, 5, close="2005"), initial_state=state)
    assert blocked.decisions[-1].reason_code == "NO_ELIGIBLE_ZONE"
    eligible = run_engine(config, _bars(config, 5, 10, close="2005"), initial_state=blocked.state)
    assert eligible.decisions[-1].decision_type == "ARM"
    assert eligible.state.risk.filled_entries_in_trading_day == 0


def test_recent_peak_window_includes_signal_bar() -> None:
    _case("recent_peak_window_includes_signal_bar")
    config = _configured(breakout=False, reversal=True, sides="long", recent_peak=True)
    state = _seed(config, _zone(kind="support", low="2000", high="2000"))
    one = run_engine(
        config,
        _bars(config, 0, 5, low="1998", high="2001", close="2000"),
        initial_state=state,
    )
    two = run_engine(
        config,
        _bars(config, 5, 10, low="1997", high="2001", close="2000"),
        initial_state=one.state,
    )
    three = run_engine(
        config,
        _bars(config, 10, 15, low="1999", high="2001", close="2000"),
        initial_state=two.state,
    )
    snapshot = three.decisions[-1].evidence.selected_setup
    assert snapshot is not None and snapshot.stop == Decimal("1996.0")
    assert snapshot.signal_execution_index == 2
