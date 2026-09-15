from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from familytrade.simulation.engine import initialize_engine, run_engine, step_engine
from familytrade.simulation.state import ZoneState
from familytrade.strategies.definitions import (
    BreakoutRetestSetup,
    CompareNode,
    ConfirmedPivotZonesSetup,
    ConstantNode,
    ExitPolicy,
    FeatureInstance,
    FeatureNode,
    ReversalSetup,
    SetupStop,
    SetupTarget,
)
from familytrade.strategies.validation import canonical_definition_sha256

from .conftest import BASE, make_bar, make_config
from .test_engine_contract_matrix import _fixture_bar, _fixture_epoch, _quality

_FIXTURE = Path(__file__).parents[2] / "docs" / "strategies" / "reversal-breakout-examples-v1.json"
_FIXTURE_SHA = "fd62857956ffae7eb9b5cd7b07be10054ad4b16b8654e629c8efdaba3aa7ac56"


def _case(case_id: str) -> dict[str, object]:
    payload = _FIXTURE.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == _FIXTURE_SHA
    document = json.loads(payload)
    return next(item for item in document["cases"] if item["id"] == case_id)


def _legacy_fill_scope(*, no_same_bar_fill: bool, next_bar_ft02_fill: bool) -> str:
    """Adapt the old draft's external fill-scope sentence across the FT02 boundary."""
    payload = _FIXTURE.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == _FIXTURE_SHA
    scope = json.loads(payload)["assumptions"]["fill_scope"]
    assert "next-event eligibility only" in scope and "await FT-02" in scope
    assert no_same_bar_fill and next_bar_ft02_fill
    # The draft sentence is caller-authored provenance, not an FT07 output or
    # authority to suppress the final FT02 Fill asserted by the engine tests.
    return (
        "actual fill price/outcome awaits FT-02; no intrabar event order or "
        "same-bar fill is inferred"
    )


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
    execution_interval: int = 300,
    use_atr: bool = False,
    use_vwap_stop: bool = False,
    pivot_radius: int = 1,
    approach_multiple: str = "6",
    reversal_stop_multiple: str = "1",
    entry_filter_minimum: str = "1.20",
):
    config = make_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "side_policy": sides,
            "nodes": (
                (
                    FeatureNode(
                        kind="feature",
                        node_id="filter-volume",
                        feature_id="relative_volume_filter_v1",
                        offset=0,
                    ),
                    ConstantNode(
                        kind="constant",
                        node_id="filter-minimum",
                        value_type="decimal",
                        unit="ratio",
                        value=Decimal(entry_filter_minimum),
                    ),
                    CompareNode(
                        kind="compare",
                        node_id="entry-filter",
                        op="gte",
                        left="filter-volume",
                        right="filter-minimum",
                        tolerance=None,
                    ),
                )
                if entry_filter_fail
                else ()
            ),
            "features": (
                FeatureInstance(
                    feature_id="relative_volume_filter_v1",
                    kind="indicator",
                    name="relative_volume_v1",
                    output_type="decimal",
                    unit="ratio",
                    interval_seconds=execution_interval,
                    parameters={"n": 1},
                ),
            )
            if entry_filter_fail
            else (),
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
                    use_atr=use_atr,
                    atr_length=2,
                    pivot_left=pivot_radius,
                    pivot_right=pivot_radius,
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
                    approach_multiple=approach_multiple,
                    require_directional_approach=directional,
                    stop_buffer_multiple=reversal_stop_multiple,
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
                    use_vwap_stop=use_vwap_stop,
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
            "execution_interval_seconds": execution_interval,
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


def _frozen_breakout_arm(case, *, side: str):
    """Drive draft-indexed 15-minute execution periods from the immutable inputs."""
    given = case["given"]
    config = _configured(
        sides=side,
        execution_interval=900,
        use_atr=True,
        use_vwap_stop=True,
        pivot_radius=50,
    )
    bounds = given["zone"]
    zone = _zone(
        kind="support" if side == "long" else "resistance",
        low=bounds["low"],
        high=bounds["high"],
    )
    arm_index = given["arm_bar"]["index"]
    events = []
    for period in range(arm_index + 1):
        for minute in range(15):
            index = period * 15 + minute
            if side == "long":
                if period < arm_index - 1:
                    high, low, close = "1999", "1995", "1997"
                elif period == arm_index - 1:
                    high, low = ("2004.5", "2003") if 5 <= minute < 10 else ("2006", "2002")
                    close = given["previous_close"]
                else:
                    high, low, close = "2007", "2003", given["arm_bar"]["close"]
                volume = 10 if period < arm_index - 1 else 8 if period == arm_index - 1 else 6
            else:
                if period < arm_index - 1:
                    high, low, close = "2012", "2008", "2010"
                elif period == arm_index - 1:
                    high, low, close = (
                        "2008.5" if 5 <= minute < 10 else "2009",
                        "2005",
                        given["previous_close"],
                    )
                else:
                    high, low, close = "2007.5", "2003.5", given["arm_bar"]["close"]
                volume = 10 if period < arm_index - 1 else 25 if period == arm_index - 1 else 40
            event = make_bar(
                config,
                index,
                open_price=close,
                high=high,
                low=low,
                close=close,
            )
            events.append(
                event.model_copy(
                    update={
                        "selection": event.selection.model_copy(
                            update={
                                "bar": event.selection.bar.model_copy(
                                    update={"volume": Decimal(volume)}
                                )
                            }
                        )
                    }
                )
            )
    armed = run_engine(config, tuple(events), initial_state=_seed(config, zone))
    snapshot = armed.decisions[-1].evidence.selected_setup
    assert snapshot is not None, (
        armed.decisions[-1].reason_code,
        armed.state.zones,
        tuple(
            (value.feature_id, value.value, value.reason_code)
            for value in armed.state.feature_values
            if "atr" in value.feature_id or "vwap" in value.feature_id
        ),
    )
    assert snapshot.arm_execution_index == arm_index
    vwap = next(
        value
        for value in armed.state.feature_values
        if value.feature_id == "__ft07_exec_session_vwap_v1"
    )
    assert vwap.value == Decimal(given["arm_bar"]["vwap"])
    assert Decimal(snapshot.unit) == Decimal(4)
    return config, armed, snapshot


def test_reversal_breakout_long_breakout_levels_and_next_event() -> None:
    case = _case("long_breakout_levels_and_next_event")
    given = case["given"]
    config, armed, snapshot = _frozen_breakout_arm(case, side="long")
    assert armed.decisions[-1].decision_type == "ARM"
    assert (snapshot.entry, snapshot.stop, snapshot.target) == (
        Decimal("2002.50"),
        Decimal("1994.80"),
        Decimal("2005.00"),
    )
    retest_index = given["later_retest_bar"]["index"]
    retest = run_engine(
        config,
        _bars(
            config,
            retest_index * 15,
            (retest_index + 1) * 15,
            open_price="2003",
            high="2004",
            low=given["later_retest_bar"]["low"],
            close="2003",
        ),
        initial_state=armed.state,
    )
    assert retest.decisions[-1].decision_type == "ENTRY"
    assert retest.fills == () and retest.state.pending_entry is not None
    intent = retest.intents[-1]
    fill_index = given["next_fill_bar"]["index"]
    fill_bar = make_bar(
        config,
        fill_index * 15,
        open_price="2003",
        high=given["next_fill_bar"]["high"],
        low=given["next_fill_bar"]["low"],
        close="2003",
    )
    waiting = step_engine(config, retest.state, fill_bar)
    assert waiting.fills == ()
    eligible_bar = make_bar(
        config,
        fill_index * 15 + 1,
        open_price="2003",
        high=given["next_fill_bar"]["high"],
        low=given["next_fill_bar"]["low"],
        close="2003",
    )
    filled = step_engine(config, waiting.state, eligible_bar)
    assert filled.fills[0].fill_price == snapshot.entry and filled.state.position is not None
    assert [
        f"{armed.decisions[-1].reason_code} at bar {snapshot.arm_execution_index} close",
        (
            f"{retest.decisions[-1].reason_code} at bar {retest_index} close because "
            f"{Decimal(given['later_retest_bar']['low']):.2f} <= {snapshot.entry:.2f}"
        ),
        (
            f"first fill eligibility is bar {fill_index} and its range includes limit "
            f"{snapshot.entry:.2f}"
            if intent.active_from == eligible_bar.selection.bar.start_at
            and eligible_bar.selection.bar.low <= snapshot.entry <= eligible_bar.selection.bar.high
            else "first fill eligibility/range not observed"
        ),
        _legacy_fill_scope(
            no_same_bar_fill=retest.fills == () and waiting.fills == (),
            next_bar_ft02_fill=filled.fills[0].fill_price == snapshot.entry,
        ),
    ] == case["expected"]


def test_reversal_breakout_short_breakout_levels_and_next_event() -> None:
    case = _case("short_breakout_levels_and_next_event")
    given = case["given"]
    config, armed, snapshot = _frozen_breakout_arm(case, side="short")
    assert armed.decisions[-1].reason_code == "ARM_SHORT"
    assert (snapshot.entry, snapshot.stop, snapshot.target) == (
        Decimal("2008.00"),
        Decimal("2013.70"),
        Decimal("2005.50"),
    )
    retest_index = given["later_retest_bar"]["index"]
    entered = run_engine(
        config,
        _bars(
            config,
            retest_index * 15,
            (retest_index + 1) * 15,
            open_price="2007",
            high=given["later_retest_bar"]["high"],
            low="2006.5",
            close="2007",
        ),
        initial_state=armed.state,
    )
    assert entered.decisions[-1].decision_type == "ENTRY"
    assert entered.fills == () and entered.state.pending_entry is not None
    intent = entered.intents[-1]
    fill_index = given["next_fill_bar"]["index"]
    fill_bar = make_bar(
        config,
        fill_index * 15,
        open_price="2007",
        high=given["next_fill_bar"]["high"],
        low=given["next_fill_bar"]["low"],
        close="2007",
    )
    waiting = step_engine(config, entered.state, fill_bar)
    assert waiting.fills == ()
    eligible_bar = make_bar(
        config,
        fill_index * 15 + 1,
        open_price="2007",
        high=given["next_fill_bar"]["high"],
        low=given["next_fill_bar"]["low"],
        close="2007",
    )
    filled = step_engine(config, waiting.state, eligible_bar)
    assert filled.fills[0].fill_price == snapshot.entry and filled.state.position is not None
    assert [
        f"{armed.decisions[-1].reason_code} at bar {snapshot.arm_execution_index} close",
        (
            f"{entered.decisions[-1].reason_code} at bar {retest_index} close because "
            f"{Decimal(given['later_retest_bar']['high']):.2f} >= {snapshot.entry:.2f}"
        ),
        (
            f"first fill eligibility is bar {fill_index} and its range includes limit "
            f"{snapshot.entry:.2f}"
            if intent.active_from == eligible_bar.selection.bar.start_at
            and eligible_bar.selection.bar.low <= snapshot.entry <= eligible_bar.selection.bar.high
            else "first fill eligibility/range not observed"
        ),
        _legacy_fill_scope(
            no_same_bar_fill=entered.fills == () and waiting.fills == (),
            next_bar_ft02_fill=filled.fills[0].fill_price == snapshot.entry,
        ),
    ] == case["expected"]


def test_reversal_rejected_when_not_approaching() -> None:
    case = _case("reversal_rejected_when_not_approaching")
    given = case["given"]
    bounds = given["support_zone"]
    config = _configured(
        breakout=False,
        reversal=True,
        sides="long",
        directional=True,
        approach_multiple="1.5",
        use_atr=True,
    )
    zone = _zone(kind="support", low=bounds["low"], high=bounds["high"])
    first = run_engine(
        config,
        _bars(config, 0, 5, close=given["previous_close"], high="2005", low="2001"),
        initial_state=_seed(config, zone),
    )
    second = run_engine(
        config,
        _bars(config, 5, 10, close=given["current_close"], high="2006", low="2002"),
        initial_state=first.state,
    )
    assert second.decisions[-1].decision_type == "HOLD"
    assert second.decisions[-1].reason_code == "NOT_APPROACHING"
    assert second.state.pending_entry is None
    base = _configured(
        breakout=False,
        reversal=True,
        sides="long",
        directional=False,
        approach_multiple="1.5",
        use_atr=True,
    )
    base_first = run_engine(
        base,
        _bars(base, 0, 5, close=given["previous_close"], high="2005", low="2001"),
        initial_state=_seed(base, zone),
    )
    accepted = run_engine(
        base,
        _bars(base, 5, 10, close=given["current_close"], high="2006", low="2002"),
        initial_state=base_first.state,
    )
    assert [
        (
            f"opt-in variant rejects REVERSAL_LONG with reason {second.decisions[-1].reason_code}"
            if second.decisions[-1].decision_type == "HOLD" and second.state.pending_entry is None
            else "opt-in variant did not reject"
        ),
        (
            "base preset leaves the option off and would accept the raw proximity condition"
            if accepted.decisions[-1].decision_type == "ENTRY"
            and accepted.decisions[-1].evidence.selected_setup is not None
            else "base preset did not accept"
        ),
    ] == case["expected"]


def test_breakout_expiration_boundary() -> None:
    case = _case("breakout_expiration_boundary")
    given = case["given"]
    arm_index = given["arm_bar_index"]
    last_eligible = given["no_qualifying_retest_through_index"]
    late_index = given["first_late_retest_index"]
    config = _configured(
        sides="long",
        expiry=given["breakout_expiry_bars"],
        pivot_radius=50,
    )
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    warm = run_engine(
        config,
        _bars(config, 0, arm_index * 5, low="1999", high="2001", close="2000"),
        initial_state=state,
    )
    armed = run_engine(
        config,
        _bars(config, arm_index * 5, (arm_index + 1) * 5, low="2004", high="2006", close="2005"),
        initial_state=warm.state,
    )
    snapshot = armed.decisions[-1].evidence.selected_setup
    assert snapshot is not None and snapshot.arm_execution_index == arm_index
    through = run_engine(
        config,
        _bars(
            config,
            (arm_index + 1) * 5,
            (last_eligible + 1) * 5,
            low="2003",
            high="2004",
            close="2003",
        ),
        initial_state=armed.state,
    )
    assert through.decisions[-1].reason_code == "NOT_RETESTED"
    expired = run_engine(
        config,
        _bars(
            config, late_index * 5, (late_index + 1) * 5, low="2000.9", high="2004", close="2003"
        ),
        initial_state=through.state,
    )
    assert (expired.decisions[-1].decision_type, expired.decisions[-1].reason_code) == (
        "CANCEL",
        "SETUP_EXPIRED",
    )
    assert expired.state.setups[0].status == "EXPIRED"
    assert [
        (
            f"setup remains armed through bar {last_eligible}"
            if through.state.setups[0].status == "ARMED"
            and through.decisions[-1].evidence.selected_setup == snapshot
            else "setup did not remain armed"
        ),
        (
            f"EXPIRED is recorded before evaluating bar {late_index}"
            if expired.state.setups[0].status == "EXPIRED"
            and expired.decisions[-1].reason_code == "SETUP_EXPIRED"
            else "expiry not recorded"
        ),
        (
            f"bar {late_index} cannot create an entry from this setup"
            if expired.state.pending_entry is None and expired.intents == ()
            else "late entry exists"
        ),
    ] == case["expected"]


def test_beyond_mode_is_not_strict_cross() -> None:
    case = _case("beyond_mode_is_not_strict_cross")
    given = case["given"]
    bounds = given["zone"]
    base = _configured(sides="long", use_atr=True)
    strict = _configured(sides="long", strict=True, use_atr=True)
    zone = _zone(kind="support", low=bounds["low"], high=bounds["high"])
    warm_base = run_engine(
        base,
        _bars(base, 0, 5, close=given["previous_close"], high="2007", low="2003"),
        initial_state=_seed(base, zone),
    )
    warm_strict = run_engine(
        strict,
        _bars(strict, 0, 5, close=given["previous_close"], high="2007", low="2003"),
        initial_state=_seed(strict, zone),
    )
    base_result = run_engine(
        base,
        _bars(base, 5, 10, close=given["current_close"], high="2007.2", low="2003.2"),
        initial_state=warm_base.state,
    )
    strict_result = run_engine(
        strict,
        _bars(strict, 5, 10, close=given["current_close"], high="2007.2", low="2003.2"),
        initial_state=warm_strict.state,
    )
    base_snapshot = base_result.decisions[-1].evidence.selected_setup
    assert base_snapshot is not None and Decimal(base_snapshot.unit) == Decimal(4)
    assert [
        (
            "base beyond mode arms a long breakout"
            if base_result.decisions[-1].reason_code == "ARM_LONG"
            else "base did not arm"
        ),
        (
            "optional strict_cross mode does not arm"
            if strict_result.decisions[-1].decision_type == "HOLD"
            and strict_result.state.setups == ()
            else "strict mode armed"
        ),
        (
            "strict_cross remains default off because it changes the source strategy"
            if base.strategy_version.definition.setup_modules[2].confirmation_mode == "beyond"
            and strict.strategy_version.definition.setup_modules[2].confirmation_mode
            == "strict_cross"
            else "default mode mismatch"
        ),
    ] == case["expected"]


def test_same_bar_break_and_retest_is_not_causal_entry() -> None:
    case = _case("same_bar_break_and_retest_is_not_causal_entry")
    given = case["given"]
    bar_index = given["bar"]["index"]
    zone_bounds = given["zone"]
    config = _configured(sides="long", use_atr=True, pivot_radius=50)
    state = _seed(
        config,
        _zone(kind="support", low=zone_bounds["low"], high=zone_bounds["high"]),
    )
    warm = run_engine(
        config,
        _bars(config, 0, bar_index * 5, low="2002", high="2006", close=given["previous_close"]),
        initial_state=state,
    )
    result = run_engine(
        config,
        _bars(
            config,
            bar_index * 5,
            (bar_index + 1) * 5,
            low=given["bar"]["low"],
            high=given["bar"]["high"],
            close=given["bar"]["close"],
        ),
        initial_state=warm.state,
    )
    assert result.decisions[-1].decision_type == "ARM"
    assert result.state.pending_entry is None
    snapshot = result.state.setups[0].snapshot
    assert snapshot is not None and snapshot.arm_execution_index == bar_index
    assert snapshot.entry == Decimal(given["frozen_pullback_level"])
    assert [
        f"{result.decisions[-1].reason_code} at bar {bar_index} close",
        (
            f"no retest and no entry intent at bar {bar_index}"
            if result.state.pending_entry is None and result.intents == ()
            else "same-bar entry exists"
        ),
        (
            f"a qualifying retest may first be observed on bar {bar_index + 1}"
            if snapshot.arm_execution_index == bar_index
            and result.state.setups[0].status == "ARMED"
            else "arm did not persist"
        ),
    ] == case["expected"]


def test_opposing_breakout_arms_are_atomic_conflict() -> None:
    case = _case("opposing_breakout_arms_are_atomic_conflict")
    given = case["given"]
    config = _configured(sides="both", use_atr=True)
    state = _seed(
        config,
        _zone(
            kind="support",
            low=given["support_zone"]["low"],
            high=given["support_zone"]["high"],
            sequence=0,
        ),
        _zone(
            kind="resistance",
            low=given["resistance_zone"]["low"],
            high=given["resistance_zone"]["high"],
            sequence=1,
        ),
    )
    warm = run_engine(
        config,
        _bars(config, 0, 5, close=given["current_close"], high="2002", low="1998"),
        initial_state=state,
    )
    result = run_engine(
        config,
        _bars(config, 5, 10, close=given["current_close"], high="2002", low="1998"),
        initial_state=warm.state,
    )
    assert result.decisions[-1].decision_type == "REJECT"
    assert result.decisions[-1].reason_code == "CONFLICT_OPPOSING_ARMS"
    candidates = tuple(item.snapshot for item in result.state.setups)
    assert len(candidates) == 2 and all(item is not None for item in candidates)
    assert {item.side for item in candidates if item is not None} == {"long", "short"}
    assert all(
        Decimal(item.unit) == Decimal(given["unit"]) for item in candidates if item is not None
    )
    assert [
        (
            "CONFLICT_OPPOSING_ARMS records both candidate snapshots"
            if result.decisions[-1].reason_code == "CONFLICT_OPPOSING_ARMS"
            and len(candidates) == 2
            and all(item is not None for item in candidates)
            else "candidate conflict not recorded"
        ),
        (
            "breakout state remains IDLE and no entry intent exists"
            if all(item.status == "CANCELLED" for item in result.state.setups)
            and result.state.pending_entry is None
            and result.intents == ()
            else "unexpected armed state"
        ),
        (
            "Pine's sequential short-overwrites-long result is not copied"
            if result.decisions[-1].side is None
            and result.decisions[-1].evidence.selected_setup is None
            else "sequential winner copied"
        ),
    ] == case["expected"]


def test_entry_intent_filter_rejection_cancels_arm() -> None:
    case = _case("entry_intent_filter_rejection_cancels_arm")
    given = case["given"]
    retest = given["retest_bar"]
    end = datetime.fromisoformat(retest["end"])
    start = end - timedelta(minutes=(retest["index"] + 1) * 5)
    raw = _configured(
        sides="long",
        entry_filter_fail=True,
        use_atr=True,
        pivot_radius=50,
        entry_filter_minimum=given["filter"]["minimum"],
    )
    config = _fixture_epoch(raw, start, end_minutes=(retest["index"] + 2) * 5)
    close_end = start + timedelta(minutes=(retest["index"] + 2) * 5 + 5)
    window = config.calendar.windows[0].model_copy(update={"end_at": close_end})
    config = config.model_copy(
        update={
            "calendar": config.calendar.model_copy(
                update={"coverage_end": close_end, "windows": (window,)}
            ),
            "contract": config.contract.model_copy(
                update={
                    "entry_cutoff_at": close_end - timedelta(minutes=5),
                    "liquidation_start_at": close_end - timedelta(minutes=3),
                    "last_trade_at": close_end,
                }
            ),
        }
    )
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))

    def fixture_period(period: int, *, low: str, high: str, close: str, volume: str):
        events = []
        for minute in range(5):
            event = _fixture_bar(
                config,
                period * 5 + minute,
                start,
                open_price=close,
                high=high,
                low=low,
                close=close,
            )
            selection = event.selection.model_copy(
                update={"bar": event.selection.bar.model_copy(update={"volume": Decimal(volume)})}
            )
            events.append(event.model_copy(update={"selection": selection}))
        return tuple(events)

    warm = run_engine(
        config,
        tuple(
            event
            for period in range(retest["index"] - 1)
            for event in fixture_period(period, low="2002", high="2006", close="2004", volume="100")
        ),
        initial_state=state,
    )
    armed = run_engine(
        config,
        fixture_period(retest["index"] - 1, low="2003", high="2007", close="2005", volume="100"),
        initial_state=warm.state,
    )
    rejected = run_engine(
        config,
        fixture_period(retest["index"], high="2004", low=retest["low"], close="2003", volume="80"),
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
    actual_feature = next(
        item
        for item in rejected.state.feature_values
        if item.feature_id == given["filter"]["feature"]
    )
    assert actual_feature.value == Decimal(given["filter"]["value"])
    assert actual_feature.evaluation_bar_end == end
    assert actual_feature.known_at == end
    assert rejected.decisions[-1].execution_bar_end == end
    assert rejected.decisions[-1].evidence.evaluation_stage == "entry_intent"
    # Feature-version text is a caller provenance label: the FT07 FeatureValue
    # public shape has feature_id/name/parameters but no feature_version field.
    assert "feature_version" not in type(actual_feature).model_fields
    assert [
        (
            "FILTER_FAIL is recorded with the frozen evidence fields"
            if rejected.decisions[-1].reason_code == "FILTER_FAIL"
            and rejected.decisions[-1].evidence.results[0].result == "FAIL"
            and actual_feature.value == Decimal(given["filter"]["value"])
            and actual_feature.evaluation_bar_end == end
            else "filter evidence mismatch"
        ),
        (
            "the arm is CANCELLED and no entry intent is submitted"
            if rejected.state.setups[0].status == "CANCELLED" and rejected.intents == ()
            else "arm not cancelled"
        ),
        (
            "same-bar re-arming is prohibited"
            if len(rejected.decisions) == 1 and rejected.state.setups[0].status == "CANCELLED"
            else "same-bar re-arm"
        ),
    ] == case["expected"]


def test_decision_atr_reference_missing_and_atr_off() -> None:
    case = _case("decision_atr_reference_missing_and_atr_off")
    given = case["given"]
    decision_end = datetime.fromisoformat(given["execution_bar_end"])
    start = decision_end - timedelta(minutes=15)
    atr = Decimal(given["completed_zone_atr"]["value"])

    def events(config):
        return tuple(
            _fixture_bar(
                config,
                index,
                start,
                open_price="2005",
                high=str(Decimal(2005) + atr / 2),
                low=str(Decimal(2005) - atr / 2),
                close="2005",
            )
            for index in range(15)
        )

    zone = _zone(kind="support", low="2000", high="2000.5")
    on = _fixture_epoch(
        _configured(sides="long", execution_interval=900, use_atr=True, pivot_radius=50),
        start,
        end_minutes=20,
    )
    on_result = run_engine(on, events(on), initial_state=_seed(on, zone))
    on_snapshot = on_result.decisions[-1].evidence.selected_setup
    assert on_snapshot is not None and Decimal(on_snapshot.unit) == atr
    assert on_result.decisions[-1].execution_bar_end == decision_end
    atr_values = tuple(
        item for item in on_result.state.feature_values if item.feature_id == "__ft07_zone_300_atr"
    )
    assert atr_values and atr_values[-1].evaluation_bar_end == decision_end
    assert atr_values[-1].value == atr
    missing = _fixture_epoch(
        _configured(sides="long", execution_interval=300, use_atr=True, pivot_radius=50),
        start,
        end_minutes=20,
    )
    unready = run_engine(missing, events(missing)[:5], initial_state=_seed(missing, zone))
    assert unready.decisions[-1].reason_code == "NOT_READY_ATR"
    assert unready.decisions[-1].evidence.selected_setup is None
    off = _fixture_epoch(
        _configured(sides="long", execution_interval=900, use_atr=False, pivot_radius=50),
        start,
        end_minutes=20,
    )
    off_result = run_engine(off, events(off), initial_state=_seed(off, zone))
    off_snapshot = off_result.decisions[-1].evidence.selected_setup
    assert off_snapshot is not None and Decimal(off_snapshot.unit) == Decimal(1)
    future_end = datetime.fromisoformat(given["future_zone_atr"]["bar_end"])
    assert future_end > decision_end
    future_atr = Decimal(given["future_zone_atr"]["value"])
    # Wilder n=2: ATR 4.20 followed by a completed zone bar with TR 5.40
    # produces the frozen future ATR 4.80.  The later completed bar is real
    # engine input, not merely a timestamp named in the fixture.
    future_range = 2 * future_atr - atr
    future_events = tuple(
        _fixture_bar(
            on,
            index,
            start,
            open_price="2005",
            high=str(Decimal(2005) + future_range / 2),
            low=str(Decimal(2005) - future_range / 2),
            close="2005",
        )
        for index in range(15, 20)
    )
    advanced = run_engine(on, future_events, initial_state=on_result.state)
    completed_future = tuple(
        item for item in advanced.state.feature_values if item.feature_id == "__ft07_zone_300_atr"
    )[-1]
    assert completed_future.evaluation_bar_end == future_end
    assert completed_future.value == future_atr
    assert completed_future.known_at >= future_end
    assert advanced.decisions == () and advanced.intents == ()
    replayed = run_engine(on, (*events(on), *future_events), initial_state=_seed(on, zone))
    assert replayed.decisions == on_result.decisions
    assert replayed.intents == on_result.intents
    assert replayed.decisions[-1].evidence == on_result.decisions[-1].evidence
    assert replayed.decisions[-1].order_intent == on_result.decisions[-1].order_intent
    assert replayed.decisions[-1].evidence.selected_setup == on_snapshot
    assert [
        (
            f"future ATR {future_atr:.2f} is unavailable to this decision"
            if completed_future.evaluation_bar_end > decision_end
            and replayed.decisions[-1].evidence.selected_setup == on_snapshot
            and Decimal(on_snapshot.unit) == atr
            else "future ATR entered decision"
        ),
        (
            "ATR-on never falls back to 1.00"
            if unready.decisions[-1].reason_code == "NOT_READY_ATR"
            and Decimal(on_snapshot.unit) == atr
            else "ATR-on fallback"
        ),
        (
            f"ATR-off remains evaluable with unit {Decimal(off_snapshot.unit):.2f}"
            if off_result.decisions[-1].decision_type == "ARM"
            else "ATR-off not evaluable"
        ),
    ] == case["expected"]


def test_cooldown_and_daily_cap_are_fill_counted() -> None:
    case = _case("cooldown_and_daily_cap_are_fill_counted")
    given = case["given"]
    last_fill = given["last_entry_fill_execution_index"]
    cap = given["max_entries_per_trading_day"]
    config = _configured(sides="long", pivot_radius=50)
    assert config.strategy_version.definition.constraints.max_entries_per_trading_day == cap
    assert config.risk_policy.max_entries_per_trading_day == cap
    warm = run_engine(
        config,
        _bars(config, 0, (last_fill + 1) * 5, low="1999", high="2001", close="2000"),
    )
    assert warm.state.last_execution_slot_index == last_fill
    zone = _zone(kind="support", low="2000", high="2000.5").model_copy(
        update={"last_filled_execution_index": last_fill}
    )
    seeded = warm.state.model_copy(
        update={
            "zones": (zone,),
            "next_zone_sequence": 1,
            "risk": warm.state.risk.model_copy(
                update={"filled_entries_in_trading_day": given["filled_entries_before_candidate"]}
            ),
        }
    )
    blocked = run_engine(
        config,
        _bars(
            config, (last_fill + 1) * 5, (last_fill + 2) * 5, low="2004", high="2006", close="2005"
        ),
        initial_state=seeded,
    )
    assert blocked.decisions[-1].reason_code == "NO_ELIGIBLE_ZONE"
    assert blocked.state.risk.filled_entries_in_trading_day == cap - 1
    eligible = run_engine(
        config,
        _bars(
            config, (last_fill + 2) * 5, (last_fill + 3) * 5, low="2004", high="2006", close="2005"
        ),
        initial_state=blocked.state,
    )
    assert eligible.decisions[-1].reason_code == "ARM_LONG"
    snapshot = eligible.decisions[-1].evidence.selected_setup
    assert snapshot is not None and snapshot.arm_execution_index == last_fill + 2
    assert eligible.state.risk.filled_entries_in_trading_day == cap - 1
    retest = run_engine(
        config,
        _bars(
            config,
            (last_fill + 3) * 5,
            (last_fill + 4) * 5,
            low="2000.9",
            high="2004",
            close="2003",
        ),
        initial_state=eligible.state,
    )
    assert retest.decisions[-1].decision_type == "ENTRY"
    assert retest.state.pending_entry is not None
    assert retest.state.risk.filled_entries_in_trading_day == cap - 1
    waiting = step_engine(
        config,
        retest.state,
        make_bar(
            config, (last_fill + 4) * 5, open_price="2002", high="2004", low="2000.9", close="2002"
        ),
    )
    assert waiting.fills == ()
    filled = step_engine(
        config,
        waiting.state,
        make_bar(
            config,
            (last_fill + 4) * 5 + 1,
            open_price="2002",
            high="2004",
            low="2000.9",
            close="2002",
        ),
    )
    assert filled.fills and filled.state.risk.filled_entries_in_trading_day == cap
    flat = run_engine(
        config,
        _bars(
            config,
            (last_fill + 4) * 5 + 2,
            (last_fill + 6) * 5,
            low="2006",
            high="2008",
            close="2007",
        ),
        initial_state=filled.state,
    )
    assert flat.state.position is None
    after = run_engine(
        config,
        _bars(
            config, (last_fill + 6) * 5, (last_fill + 7) * 5, low="2004", high="2006", close="2005"
        ),
        initial_state=flat.state,
    )
    late_retest = run_engine(
        config,
        _bars(
            config,
            (last_fill + 7) * 5,
            (last_fill + 8) * 5,
            low="2000.9",
            high="2004",
            close="2003",
        ),
        initial_state=after.state,
    )
    assert late_retest.decisions[-1].reason_code == "DAILY_ENTRY_LIMIT"
    assert late_retest.state.pending_entry is None
    assert [
        (
            f"zone is blocked at index {last_fill + 1} and eligible at index {last_fill + 2}"
            if blocked.decisions[-1].reason_code == "NO_ELIGIBLE_ZONE"
            and eligible.decisions[-1].decision_type == "ARM"
            else "cooldown mismatch"
        ),
        (
            "an unfilled order does not alter cooldown or the daily counter"
            if retest.state.risk.filled_entries_in_trading_day == cap - 1
            and retest.state.zones[0].last_filled_execution_index == last_fill
            else "unfilled order counted"
        ),
        (
            "after the twenty-third atomic fill, further entries are blocked for that trading day"
            if filled.state.risk.filled_entries_in_trading_day == cap
            and late_retest.decisions[-1].reason_code == "DAILY_ENTRY_LIMIT"
            else "daily cap not enforced"
        ),
    ] == case["expected"]


def test_recent_peak_window_includes_signal_bar() -> None:
    case = _case("recent_peak_window_includes_signal_bar")
    given = case["given"]
    signal_index = given["signal_bar_index"]
    lows = tuple(Decimal(item) for item in given["completed_execution_lows_indices_90_to_92"])
    config = _configured(
        breakout=False,
        reversal=True,
        sides="long",
        recent_peak=True,
        use_atr=True,
        pivot_radius=50,
        reversal_stop_multiple=given["reversal_stop_multiple"],
    )
    warm = run_engine(
        config,
        _bars(config, 0, (signal_index - 2) * 5, low="1998", high="2002", close="2000"),
    )
    current = warm.state
    for offset, low in enumerate(lows[:2]):
        current = run_engine(
            config,
            _bars(
                config,
                (signal_index - 2 + offset) * 5,
                (signal_index - 1 + offset) * 5,
                low=str(low),
                high=str(low + 4),
                close="2000",
            ),
            initial_state=current,
        ).state
    zone = _zone(kind="support", low=given["support_zone_low"], high=given["support_zone_low"])
    signal = run_engine(
        config,
        _bars(
            config,
            signal_index * 5,
            (signal_index + 1) * 5,
            low=str(lows[2]),
            high=str(lows[2] + 4),
            close="2000",
        ),
        initial_state=current.model_copy(update={"zones": (zone,), "next_zone_sequence": 1}),
    )
    snapshot = signal.decisions[-1].evidence.selected_setup
    assert snapshot is not None and snapshot.signal_execution_index == signal_index
    assert Decimal(snapshot.unit) == Decimal(given["unit"])
    assert snapshot.stop == Decimal("1995.40")
    assert signal.state.pending_entry is not None
    frozen_order = signal.state.pending_entry.intent
    later = run_engine(
        config,
        _bars(
            config,
            (signal_index + 1) * 5,
            (signal_index + 2) * 5,
            low="1996",
            high="2002",
            close="2000",
        ),
        initial_state=signal.state,
    )
    assert frozen_order.stop_price == snapshot.stop
    assert later.state.position is not None
    assert later.state.position.protective_bracket.stop.trigger_price == snapshot.stop
    missing_config = _configured(
        breakout=False,
        reversal=True,
        sides="long",
        recent_peak=True,
        use_atr=False,
        pivot_radius=50,
        reversal_stop_multiple=given["reversal_stop_multiple"],
    )
    missing_warm = run_engine(
        missing_config,
        _bars(
            missing_config,
            0,
            (signal_index - 2) * 5,
            low="1998",
            high="2002",
            close="2000",
        ),
    )
    missing_90 = run_engine(
        missing_config,
        _bars(
            missing_config,
            (signal_index - 2) * 5,
            (signal_index - 1) * 5,
            low=str(lows[0]),
            high=str(lows[0] + 4),
            close="2000",
        ),
        initial_state=missing_warm.state,
    )
    missing_bar = make_bar(missing_config, (signal_index - 1) * 5)
    missing_period = run_engine(
        missing_config,
        (
            _quality(missing_config, missing_bar.selection.bar.start_at),
            *_bars(
                missing_config,
                (signal_index - 1) * 5 + 1,
                signal_index * 5,
                low=str(lows[1]),
                high=str(lows[1] + 4),
                close="2000",
            ),
        ),
        initial_state=missing_90.state,
    )
    insufficient = run_engine(
        missing_config,
        _bars(
            missing_config,
            signal_index * 5,
            (signal_index + 1) * 5,
            low=str(lows[2]),
            high=str(lows[2] + 4),
            close="2000",
        ),
        initial_state=missing_period.state.model_copy(
            update={
                "zones": (zone,),
                "next_zone_sequence": 1,
            }
        ),
    )
    assert insufficient.decisions[-1].reason_code == "NOT_READY_PEAK_WINDOW"
    assert [
        (
            f"the three-bar window includes signal bar {signal_index} and freezes "
            f"stop {snapshot.stop:.2f}"
            if snapshot.stop
            == min(lows) - Decimal(given["reversal_stop_multiple"]) * Decimal(given["unit"])
            else "window/stop mismatch"
        ),
        (
            "if any of indices 90, 91 or 92 is missing/invalid, reject "
            f"{insufficient.decisions[-1].reason_code}"
            if insufficient.decisions[-1].decision_type == "HOLD"
            else "missing window accepted"
        ),
        (
            "later lows cannot change the frozen order"
            if later.state.position.protective_bracket.stop.trigger_price == frozen_order.stop_price
            else "later low moved stop"
        ),
    ] == case["expected"]
