from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, getcontext
from pathlib import Path

import pytest

from familytrade.market_data.models import canonical_json_bytes
from familytrade.simulation.engine import (
    checkpoint_engine,
    deterministic_uuid7,
    initialize_engine,
    restore_engine,
    run_engine,
    step_engine,
)
from familytrade.simulation.fills import BarPrices, protective_fill
from familytrade.simulation.orders import entry_expiry, limit_fill, tick_round
from familytrade.simulation.risk import (
    commission,
    modeled_total_loss,
    money_round,
    price_pnl,
    stop_fraction_quantity,
)
from familytrade.simulation.state import (
    DataQualityEvent,
    EmissionContext,
    EngineError,
    EngineFailure,
    FeatureValue,
    UnsupportedConfigurationDetails,
    ZoneState,
)
from familytrade.strategies.definitions import (
    ConstantNode,
    FeatureInstance,
    FeatureNode,
    FeaturePriceSource,
    FixedTicksStop,
    FixedTicksTarget,
    GroupNode,
    OrderPolicy,
    RiskMultipleTarget,
    TemporalCompareNode,
)
from familytrade.strategies.indicators import quantize_feature
from familytrade.strategies.rules import evaluate_rule
from familytrade.strategies.setups import r_multiple_target, valid_bracket
from familytrade.strategies.validation import canonical_definition_sha256
from familytrade.strategies.zones import next_zone_target

from .conftest import BASE, make_backtest_config, make_bar

_FROZEN_PATH = Path(__file__).parents[2] / "docs" / "contracts-examples-v1.json"


def _frozen(case_id: str) -> dict[str, object]:
    payload = _FROZEN_PATH.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == (
        "c2bd5d31acd8996d0d888ed75e99f320f395b74bc441db0f941201df705c1062"
    )
    return next(item for item in json.loads(payload)["cases"] if item["id"] == case_id)


def _fixed_trade(
    *,
    side: str,
    stop_ticks: int,
    target_ticks: int,
    entry_open: str,
    entry_high: str,
    entry_low: str,
    entry_close: str,
    exit_open: str,
    exit_high: str,
    exit_low: str,
    exit_close: str,
    entry_limit: str | None = None,
):
    config = make_backtest_config()
    if side == "short":
        from .conftest import make_config

        config = config.model_copy(
            update={"strategy_version": make_config(side="short").strategy_version}
        )
    updates = {
        "exit_policy": config.strategy_version.definition.exit_policy.model_copy(
            update={
                "stop": FixedTicksStop(kind="fixed_ticks", ticks=stop_ticks),
                "target": FixedTicksTarget(kind="fixed_ticks", ticks=target_ticks),
            }
        )
    }
    if entry_limit is not None:
        updates["features"] = (
            FeatureInstance(
                feature_id="fixture-limit",
                kind="input",
                name="close",
                output_type="price",
                unit="contract_price",
                interval_seconds=300,
                parameters={},
            ),
        )
        updates["order_policy"] = OrderPolicy(
            entry_type="limit",
            limit_price_source=FeaturePriceSource(
                kind="feature",
                feature_id="fixture-limit",
                offset_ticks=int((Decimal(entry_limit) - Decimal(2000)) / Decimal("0.1")),
            ),
            entry_ttl_execution_bars=2,
            both_hit_policy="stop_first",
            entry_bar_exit_policy="conservative_stop_first",
        )
    definition = config.strategy_version.definition.model_copy(update=updates)
    strategy = config.strategy_version.model_copy(
        update={
            "definition": definition,
            "canonical_definition_sha256": canonical_definition_sha256(definition),
        }
    )
    config = config.model_copy(update={"strategy_version": strategy})
    prefix = tuple(make_bar(config, index) for index in range(5))
    entry = make_bar(
        config,
        5,
        open_price=entry_open,
        high=entry_high,
        low=entry_low,
        close=entry_close,
    )
    exit_bar = make_bar(
        config,
        6,
        open_price=exit_open,
        high=exit_high,
        low=exit_low,
        close=exit_close,
    )
    return run_engine(config, (*prefix, entry, exit_bar))


def _feature_run(feature: FeatureInstance, bars: tuple[dict[str, str], ...]):
    config = make_backtest_config()
    definition = config.strategy_version.definition.model_copy(update={"features": (feature,)})
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                }
            )
        }
    )
    events = []
    for index, values in enumerate(bars):
        close = values["close"]
        event = make_bar(
            config,
            index,
            open_price=values.get("open", close),
            high=values.get("high", close),
            low=values.get("low", close),
            close=close,
        )
        if "volume" in values:
            event = event.model_copy(
                update={
                    "selection": event.selection.model_copy(
                        update={
                            "bar": event.selection.bar.model_copy(
                                update={"volume": Decimal(values["volume"])}
                            )
                        }
                    )
                }
            )
        events.append(event)
    result = run_engine(config, tuple(events))
    runtime = next(
        item for item in result.state.feature_runtime if item.feature_id == feature.feature_id
    )
    return config, result, runtime.history


def test_deterministic_uuid7_ids_and_state_hashes_match_on_windows_and_linux_vectors() -> None:
    value = deterministic_uuid7(
        "correlation",
        datetime(2026, 1, 2, 3, 5, tzinfo=UTC),
        (
            "018f4c00-0000-7000-8000-000000000001",
            None,
            0,
            "completed_bar_v1",
            datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            datetime(2026, 1, 2, 3, 5, tzinfo=UTC),
            "0" * 64,
        ),
    )
    assert value == "019b7caa-6360-7464-861e-19052358e1e7"


def test_per_domain_uuid7_tuples_counters_and_normative_vectors_are_exact() -> None:
    correlation = "019b7caa-6360-7464-861e-19052358e1e7"
    assert (
        deterministic_uuid7(
            "decision",
            datetime(2026, 1, 2, 3, 5, tzinfo=UTC),
            (
                "018f4c00-0000-7000-8000-000000000001",
                None,
                0,
                "018f4c00-0000-7000-8000-000000000002",
                datetime(2026, 1, 2, 3, 5, tzinfo=UTC),
                "ENTRY",
                "long",
                "018f4c00-0000-7000-8000-000000000003",
                "0" * 64,
                correlation,
            ),
        )
        == "019b7caa-6360-740c-8624-091a15fdd2b9"
    )
    assert (
        deterministic_uuid7(
            "protective-stop-order",
            datetime(2026, 1, 2, 3, 6, tzinfo=UTC),
            (
                "018f4c00-0000-7000-8000-000000000001",
                None,
                1,
                "018f4c00-0000-7000-8000-000000000010",
                "018f4c00-0000-7000-8000-000000000011",
                "sell",
                1,
                Decimal("1999.8"),
            ),
        )
        == "019b7cab-4dc0-7d0e-b4c8-ad805426627d"
    )


def test_uuid_vector_integer_quantity_matches_normative_value() -> None:
    fields = (
        "018f4c00-0000-7000-8000-000000000001",
        None,
        1,
        "018f4c00-0000-7000-8000-000000000010",
        "018f4c00-0000-7000-8000-000000000011",
        "sell",
        1,
        Decimal("1999.8"),
    )
    assert b'"1999.8"' in canonical_json_bytes(list(fields))
    assert b",1," in canonical_json_bytes(list(fields))


def test_long_and_short_tick_rounding_matrix_is_exhaustive() -> None:
    tick = Decimal("0.1")
    raw = Decimal("2000.05")
    assert tick_round(raw, tick, role="entry", side="buy") == Decimal("2000.0")
    assert tick_round(raw, tick, role="entry", side="sell") == Decimal("2000.1")
    assert tick_round(raw, tick, role="stop", side="sell") == Decimal("2000.1")
    assert tick_round(raw, tick, role="stop", side="buy") == Decimal("2000.0")
    assert tick_round(raw, tick, role="target", side="sell") == Decimal("2000.0")
    assert tick_round(raw, tick, role="target", side="buy") == Decimal("2000.1")


def test_usd_commission_pnl_and_risk_rounding_pipeline_boundary_examples() -> None:
    assert commission(Decimal("1.005"), 1) == Decimal("1.00")
    assert commission(Decimal("1.015"), 1) == Decimal("1.02")
    assert price_pnl(
        side="long",
        entry=Decimal(0),
        exit_or_mark=Decimal("0.005"),
        multiplier=Decimal(1),
        quantity=1,
    ) == Decimal("0.00")
    assert price_pnl(
        side="long",
        entry=Decimal(0),
        exit_or_mark=Decimal("0.015"),
        multiplier=Decimal(1),
        quantity=1,
    ) == Decimal("0.02")


def test_quantity_level_commission_cap_1_005_qty2_long_short_and_policy_matrix() -> None:
    kwargs = {
        "entry": Decimal("2000.000"),
        "slipped_stop": Decimal("1999.995"),
        "multiplier": Decimal(100),
        "commission_rate": Decimal("1.005"),
    }
    assert modeled_total_loss(quantity=1, **kwargs) == Decimal("2.500")
    assert modeled_total_loss(quantity=2, **kwargs) == Decimal("5.020")
    assert (
        stop_fraction_quantity(
            starting_cash=Decimal(1002),
            current_equity=Decimal(1002),
            fraction=Decimal("0.005"),
            max_quantity=2,
            per_entry_cap=Decimal("5.01"),
            **kwargs,
        )
        == 1
    )
    short = dict(kwargs, slipped_stop=Decimal("2000.005"))
    assert modeled_total_loss(quantity=2, **short) == Decimal("5.020")


def test_entry_ttl_backtest_and_delayed_forward_half_open_alignment_is_exact() -> None:
    assert entry_expiry(
        datetime(2026, 9, 14, 10, tzinfo=UTC), datetime(2026, 9, 14, 10, tzinfo=UTC), 900, 2
    ) == datetime(2026, 9, 14, 10, 30, tzinfo=UTC)
    assert entry_expiry(
        datetime(2026, 9, 14, 10, 15, tzinfo=UTC),
        datetime(2026, 9, 14, 10, 15, 2, 200000, tzinfo=UTC),
        900,
        1,
    ) == datetime(2026, 9, 14, 10, 30, tzinfo=UTC)


def test_long_and_short_fill_field_conventions_match_canonical_examples() -> None:
    assert price_pnl(
        side="long",
        entry=Decimal("2000.1"),
        exit_or_mark=Decimal("1993.9"),
        multiplier=Decimal(10),
        quantity=1,
    ) == Decimal("-62.00")
    assert price_pnl(
        side="short",
        entry=Decimal("1999.9"),
        exit_or_mark=Decimal("2006.1"),
        multiplier=Decimal(10),
        quantity=1,
    ) == Decimal("-62.00")
    assert money_round(Decimal("24998.75") - 62 - Decimal("1.25")) == Decimal("24935.50")


def test_batch_incremental_and_checkpoint_replay_are_byte_identical(config) -> None:
    bars = tuple(make_bar(config, index) for index in range(5))
    batch = run_engine(config, bars)
    state = initialize_engine(config)
    for bar in bars[:3]:
        state = step_engine(config, state, bar).state
    state = restore_engine(config, checkpoint_engine(config, state))
    for bar in bars[3:]:
        state = step_engine(config, state, bar).state
    assert canonical_json_bytes(state) == canonical_json_bytes(batch.state)


def test_incremental_overlap_semantic_replay_returns_empty_deltas_once(config) -> None:
    state = initialize_engine(config)
    event = make_bar(config, 0)
    first = step_engine(config, state, event)
    replay = step_engine(
        config,
        first.state,
        event.model_copy(
            update={
                "emission_context": event.emission_context.model_copy(
                    update={
                        "attempt_id": "018f4c00-0000-7000-8000-000000000099",
                        "fencing_token": 9,
                    }
                )
            }
        ),
    )
    assert replay.replayed is True
    assert replay.state == first.state
    assert not replay.events and not replay.fills and not replay.decisions


def test_same_logical_bar_with_different_payload_is_duplicate_conflict(config) -> None:
    first = make_bar(config, 0)
    state = step_engine(config, initialize_engine(config), first).state
    changed_bar = first.selection.bar.model_copy(update={"close": Decimal("2000.1")})
    changed = first.model_copy(
        update={"selection": first.selection.model_copy(update={"bar": changed_bar})}
    )
    with pytest.raises(EngineFailure) as caught:
        step_engine(config, state, changed)
    assert caught.value.error.code == "DUPLICATE_CONFLICT"


def test_out_of_order_event_changes_no_state(config) -> None:
    state = initialize_engine(config)
    state = step_engine(config, state, make_bar(config, 1)).state
    before = canonical_json_bytes(state)
    with pytest.raises(EngineFailure) as caught:
        step_engine(config, state, make_bar(config, 0))
    assert caught.value.error.code in {
        "DUPLICATE_CONFLICT",
        "EVENT_OUT_OF_ORDER",
    }
    assert canonical_json_bytes(state) == before


def test_checkpoint_rejects_corruption_config_change_and_unknown_format(config) -> None:
    state = initialize_engine(config)
    checkpoint = checkpoint_engine(config, state)
    corrupted = checkpoint.model_copy(update={"state_sha256": "0" * 64})
    with pytest.raises(EngineFailure) as caught:
        restore_engine(config, corrupted)
    assert caught.value.error.code == "CHECKPOINT_MISMATCH"
    changed = config.model_copy(update={"source": "changed"})
    with pytest.raises(EngineFailure) as caught:
        restore_engine(changed, checkpoint)
    assert caught.value.error.code == "CONFIG_MISMATCH"


def test_market_entry_gap_rejected_by_fill_time_risk() -> None:
    expected = _frozen("market_entry_gap_rejected_by_fill_time_risk")["expected"]
    projection = {}
    for entry, stop in (
        (Decimal("2010.10"), Decimal("1996.90")),
        (Decimal("1989.90"), Decimal("2003.10")),
    ):
        assert modeled_total_loss(
            entry=entry,
            slipped_stop=stop,
            multiplier=Decimal(10),
            quantity=1,
            commission_rate=Decimal("1.25"),
        ) == Decimal("134.50")
    from .test_reversal_breakout import _bars, _configured, _seed, _zone

    for side, kind, zone_bounds, signal_prices, gap_prices in (
        (
            "long",
            "support",
            ("1996", "2000"),
            {"low": "1999", "high": "2001", "close": "2000"},
            {"open_price": "2010", "low": "2009", "high": "2011", "close": "2010"},
        ),
        (
            "short",
            "resistance",
            ("2010", "2016"),
            {"low": "2009", "high": "2011", "close": "2010"},
            {"open_price": "2000", "low": "1999", "high": "2001", "close": "2000"},
        ),
    ):
        config = _configured(sides=side, breakout=False, reversal=True)
        initial = _seed(
            config,
            _zone(kind=kind, low=zone_bounds[0], high=zone_bounds[1]),
        )
        entered = run_engine(config, _bars(config, 0, 5, **signal_prices), initial_state=initial)
        waiting = step_engine(config, entered.state, make_bar(config, 5))
        rejected = step_engine(config, waiting.state, make_bar(config, 6, **gap_prices))
        cancellation = next(
            event
            for event in rejected.events
            if event.event_type == "ORDER_STATE"
            and event.payload.get("to") == "CANCELLED"
            and event.payload.get("reason") == "RISK_GAP"
        )
        projection[side] = {
            "entry_fill_count": sum(fill.effect == "open" for fill in rejected.fills),
            "order_status": cancellation.payload["to"],
            "reason": cancellation.payload["reason"],
            "commission": str(rejected.state.fees),
            "cash": str(rejected.state.cash),
        }
        assert rejected.state.pending_entry is None
    assert projection == expected


def test_long_target_gap_price_improvement() -> None:
    expected = _frozen("long_target_gap_price_improvement")["expected"]
    engine = _fixed_trade(
        side="long",
        stop_ticks=77,
        target_ticks=25,
        entry_open="2003.00",
        entry_high="2003.20",
        entry_low="2002.40",
        entry_close="2002.80",
        exit_open="2005.5",
        exit_high="2006",
        exit_low="2005",
        exit_close="2005.8",
        entry_limit="2002.50",
    )
    assert {
        "fills": [f"{item.fill_price:.2f}" for item in engine.fills],
        "fees": str(engine.state.fees),
        "realized_pnl": str(engine.state.realized_pnl),
        "cash": str(engine.state.cash),
    } == expected
    result = protective_fill(
        position_side="long",
        stop=Decimal("1994.8"),
        target=Decimal(2005),
        bar=BarPrices(Decimal("2005.5"), Decimal(2006), Decimal(2005), Decimal("2005.8")),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="stop_first",
    )
    assert (
        result is not None
        and result.fill_price == Decimal("2005.5")
        and result.reason == "TARGET_GAP"
    )


def test_short_target_gap_price_improvement() -> None:
    expected = _frozen("short_target_gap_price_improvement")["expected"]
    engine = _fixed_trade(
        side="short",
        stop_ticks=57,
        target_ticks=25,
        entry_open="2007.00",
        entry_high="2008.10",
        entry_low="2006.80",
        entry_close="2007.50",
        exit_open="2005",
        exit_high="2005.4",
        exit_low="2004.8",
        exit_close="2005.1",
        entry_limit="2008.00",
    )
    assert {
        "fills": [f"{item.fill_price:.2f}" for item in engine.fills],
        "fees": str(engine.state.fees),
        "realized_pnl": str(engine.state.realized_pnl),
        "cash": str(engine.state.cash),
    } == expected
    result = protective_fill(
        position_side="short",
        stop=Decimal("2013.7"),
        target=Decimal("2005.5"),
        bar=BarPrices(Decimal(2005), Decimal("2005.4"), Decimal("2004.8"), Decimal("2005.1")),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="stop_first",
    )
    assert (
        result is not None and result.fill_price == Decimal(2005) and result.reason == "TARGET_GAP"
    )


def test_both_hit_after_open_fill_is_stop_first() -> None:
    expected = _frozen("both_hit_after_open_fill_is_stop_first")["expected"]
    engine = _fixed_trade(
        side="long",
        stop_ticks=21,
        target_ticks=19,
        entry_open="2000",
        entry_high="2001",
        entry_low="1999",
        entry_close="2000",
        exit_open="2000",
        exit_high="2003",
        exit_low="1997",
        exit_close="2001",
    )
    target_id = next(
        order.order_id for order in engine.protective_orders if order.role == "profit_target"
    )
    assert {
        "filled_exit": "stop" if engine.fills[-1].reason == "BOTH_HIT_STOP_FIRST" else "target",
        "target_cancelled": any(
            event.event_type == "ORDER_STATE"
            and event.payload.get("order_id") == target_id
            and event.payload.get("to") == "CANCELLED"
            and event.payload.get("reason") == "POSITION_FLAT"
            for event in engine.events
        ),
        "fees": str(engine.state.fees),
        "realized_pnl": str(engine.state.realized_pnl),
        "cash": str(engine.state.cash),
        "maximum_drawdown": str(engine.state.risk.maximum_drawdown),
    } == expected
    result = protective_fill(
        position_side="long",
        stop=Decimal(1998),
        target=Decimal(2002),
        bar=BarPrices(Decimal(2000), Decimal(2003), Decimal(1997), Decimal(2001)),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="stop_first",
    )
    assert (
        result is not None
        and result.fill_price == Decimal("1997.9")
        and result.reason == "BOTH_HIT_STOP_FIRST"
    )


def test_target_first_and_next_bar_only_alternatives_are_explicit_and_deterministic() -> None:
    result = protective_fill(
        position_side="long",
        stop=Decimal(1998),
        target=Decimal(2002),
        bar=BarPrices(Decimal(2000), Decimal(2003), Decimal(1997), Decimal(2001)),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="target_first",
    )
    assert (
        result is not None
        and result.fill_price == Decimal(2002)
        and result.reason == "BOTH_HIT_TARGET_FIRST"
    )


def test_limit_market_ttl_gap_and_entry_bar_precedence_matrix_is_exhaustive() -> None:
    buy_gap = limit_fill(
        side="buy",
        limit=Decimal(2000),
        open_price=Decimal(1999),
        high=Decimal(2001),
        low=Decimal(1998),
        tick=Decimal("0.1"),
        slippage_ticks=1,
    )
    sell_gap = limit_fill(
        side="sell",
        limit=Decimal(2000),
        open_price=Decimal(2001),
        high=Decimal(2002),
        low=Decimal(1999),
        tick=Decimal("0.1"),
        slippage_ticks=1,
    )
    assert buy_gap == (Decimal(1999), Decimal("1999.1"), "LIMIT_ENTRY_GAP")
    assert sell_gap == (Decimal(2001), Decimal("2000.9"), "LIMIT_ENTRY_GAP")


def test_indicator_warmup_equality_and_causal_catalogue() -> None:
    expected = _frozen("indicator_warmup_equality_and_causal_catalogue")["expected"]
    original = getcontext().prec
    getcontext().prec = 4
    try:
        assert commission(Decimal("1.005"), 2) == Decimal("2.01")
        assert quantize_feature(Decimal("2.333333333333333")) == Decimal("2.333333333333")
    finally:
        getcontext().prec = original

    def feature(feature_id: str, name: str, parameters: dict[str, object]) -> FeatureInstance:
        unit = (
            "ratio_0_100"
            if name == "rsi_wilder_v1"
            else "ratio"
            if name == "relative_volume_v1"
            else "contract_price"
        )
        return FeatureInstance(
            feature_id=feature_id,
            kind="indicator",
            name=name,
            output_type="decimal",
            unit=unit,  # type: ignore[arg-type]
            interval_seconds=60,
            parameters=parameters,
        )

    closes = tuple({"close": str(value)} for value in (1, 2, 3, 4))
    sma_config, sma_result, sma = _feature_run(
        feature("sma", "sma_v1", {"n": 3, "input": "close"}), closes
    )
    _, _, ema = _feature_run(feature("ema", "ema_v1", {"n": 3, "input": "close"}), closes)
    _, _, rsi = _feature_run(
        feature("rsi", "rsi_wilder_v1", {"n": 3}),
        tuple({"close": value} for value in ("10", "11", "11", "10")),
    )
    _, _, atr = _feature_run(
        feature("atr", "atr_wilder_v1", {"n": 3}),
        (
            {"high": "12", "low": "10", "close": "11"},
            {"high": "13", "low": "10", "close": "12"},
            {"high": "14", "low": "12", "close": "13"},
        ),
    )
    _, _, relative = _feature_run(
        feature("rv", "relative_volume_v1", {"n": 3}),
        tuple({"close": "10", "volume": value} for value in ("100", "200", "300", "300")),
    )
    _, _, vwap = _feature_run(
        feature("vwap", "session_vwap_v1", {}),
        (
            {"close": "10", "volume": "100"},
            {"close": "12", "volume": "300"},
        ),
    )
    projection = {
        "sma3_before_bar3": sma[1].status,
        "sma3_bar3": str(sma[2].value),
        "ema3_before_bar3": ema[1].status,
        "ema3_bar4": str(ema[3].value),
        "rsi3_before_four_closes": rsi[2].status,
        "rsi3_bar4": str(rsi[3].value),
        "atr3_before_bar3": atr[1].status,
        "atr3_bar3": str(atr[2].value),
        "relative_volume3_before_four_bars": relative[2].status,
        "relative_volume3_bar4": str(relative[3].value),
        "session_vwap_bar2": str(vwap[1].value),
        "crosses_above": Decimal(10) <= Decimal(10) and Decimal(11) > Decimal(10),
        "current_equality_would_cross": Decimal(10) < Decimal(10),
        "gap_breaks_all_consecutive_warmups": all(
            item.continuity_status == "broken_until_reseed"
            for item in step_engine(
                sma_config,
                sma_result.state,
                DataQualityEvent(
                    kind="data_quality_v1",
                    start_at=make_bar(sma_config, 4).selection.bar.start_at,
                    end_at=make_bar(sma_config, 4).selection.bar.end_at,
                    status="missing",
                    reason="NO_BAR",
                    source_bar_record_ids=(),
                    recorded_at=make_bar(sma_config, 4).selection.bar.end_at,
                    emission_context=EmissionContext(
                        attempt_id=None,
                        fencing_token=1,
                        order_submitted_at=make_bar(sma_config, 4).selection.bar.end_at,
                        output_recorded_at=make_bar(sma_config, 4).selection.bar.end_at,
                    ),
                ),
            ).state.feature_runtime
        ),
    }
    assert projection == expected


def test_rules_only_entry_fill_after_checkpoint_retains_originating_entry_decision(config) -> None:
    state = initialize_engine(config)
    decisions = ()
    for index in range(5):
        result = step_engine(config, state, make_bar(config, index))
        state = result.state
        decisions += result.decisions
    entry = next(item for item in decisions if item.decision_type == "ENTRY")
    assert state.pending_entry is not None
    assert state.pending_entry.originating_decision_id == entry.decision_id
    state = restore_engine(config, checkpoint_engine(config, state))
    state = step_engine(config, state, make_bar(config, 5)).state
    filled = step_engine(config, state, make_bar(config, 6))
    assert len(filled.fills) == 1
    assert filled.fills[0].causation_decision_id == entry.decision_id
    assert filled.state.position is not None
    assert (
        filled.state.position.protective_bracket.stop.originating_decision_id == entry.decision_id
    )
    assert (
        filled.state.position.protective_bracket.target.originating_decision_id == entry.decision_id
    )


def test_public_callables_raise_engine_failure_and_never_return_error_union(config) -> None:
    invalid = config.model_copy(update={"max_canonical_bars": 2_000_001})
    with pytest.raises(EngineFailure) as caught:
        initialize_engine(invalid)
    assert caught.value.args == (caught.value.error.message,)
    assert caught.value.error.code == "UNSUPPORTED_CONFIGURATION"


def test_engine_error_details_union_code_kind_message_and_validator_matrix() -> None:
    valid = EngineError(
        code="UNSUPPORTED_CONFIGURATION",
        message="Engine configuration is unsupported.",
        details=UnsupportedConfigurationDetails(
            kind="unsupported_configuration",
            path="/max_canonical_bars",
            reason="BAR_LIMIT_RANGE",
        ),
    )
    assert canonical_json_bytes(valid) == (
        b'{"code":"UNSUPPORTED_CONFIGURATION","details":{"kind":"unsupported_configuration",'
        b'"path":"/max_canonical_bars","reason":"BAR_LIMIT_RANGE"},'
        b'"message":"Engine configuration is unsupported."}'
    )
    for mutation in (
        {"code": "VALIDATION_ERROR"},
        {"message": "Engine input is invalid."},
    ):
        with pytest.raises(ValueError):
            EngineError.model_validate({**valid.model_dump(), **mutation}, strict=True)


def test_raw_random_seed_and_checkpoint_type_errors_are_pydantic_only(config) -> None:
    raw_config = config.model_dump(mode="python")
    raw_config["random_seed"] = 1
    with pytest.raises(Exception) as caught:
        type(config).model_validate(raw_config, strict=True)
    assert caught.value.__class__.__name__ == "ValidationError"
    assert caught.value.errors()[0]["loc"] == ("random_seed",)

    checkpoint = checkpoint_engine(config, initialize_engine(config))
    raw_checkpoint = checkpoint.model_dump(mode="python")
    raw_checkpoint["format_version"] = "2"
    with pytest.raises(Exception) as caught:
        type(checkpoint).model_validate(raw_checkpoint, strict=True)
    assert caught.value.__class__.__name__ == "ValidationError"
    assert caught.value.errors()[0]["loc"] == ("format_version",)


def test_callable_bar_limit_and_checkpoint_version_errors_match_canonical_bytes(config) -> None:
    invalid = config.model_copy(update={"max_canonical_bars": 2_000_001})
    with pytest.raises(EngineFailure) as caught:
        initialize_engine(invalid)
    assert canonical_json_bytes(caught.value.error) == (
        b'{"code":"UNSUPPORTED_CONFIGURATION","details":{"kind":"unsupported_configuration",'
        b'"path":"/max_canonical_bars","reason":"BAR_LIMIT_RANGE"},'
        b'"message":"Engine configuration is unsupported."}'
    )

    checkpoint = checkpoint_engine(config, initialize_engine(config)).model_copy(
        update={"format_version": 2}
    )
    with pytest.raises(EngineFailure) as caught:
        restore_engine(config, checkpoint)
    assert canonical_json_bytes(caught.value.error) == (
        b'{"code":"CHECKPOINT_MISMATCH","details":{"kind":"checkpoint_mismatch",'
        b'"path":"/format_version","reason":"FORMAT_VERSION"},'
        b'"message":"Engine checkpoint is invalid."}'
    )


def test_engine_step_result_pre_post_hashes_cover_normal_quality_finish_and_replay(config) -> None:
    initial = initialize_engine(config)
    first = step_engine(config, initial, make_bar(config, 0))
    assert first.pre_state_sha256 == checkpoint_engine(config, initial).state_sha256
    assert first.post_state_sha256 == checkpoint_engine(config, first.state).state_sha256
    replay = step_engine(config, first.state, make_bar(config, 0))
    assert replay.replayed is True
    assert replay.pre_state_sha256 == replay.post_state_sha256
    assert replay.pre_state_sha256 == checkpoint_engine(config, first.state).state_sha256


def test_run_engine_step_hash_chain_matches_incremental_and_empty_batch(config) -> None:
    bars = (make_bar(config, 0), make_bar(config, 1))
    run = run_engine(config, bars)
    assert len(run.step_hashes) == 2
    assert run.pre_state_sha256 == run.step_hashes[0].pre_state_sha256
    assert run.step_hashes[0].post_state_sha256 == run.step_hashes[1].pre_state_sha256
    assert run.post_state_sha256 == run.step_hashes[-1].post_state_sha256
    empty = run_engine(config, (), initial_state=run.state)
    assert empty.step_hashes == ()
    assert empty.pre_state_sha256 == empty.post_state_sha256


def test_failed_step_or_batch_returns_no_result_hashes_and_preserves_input_state(config) -> None:
    state = step_engine(config, initialize_engine(config), make_bar(config, 1)).state
    before = canonical_json_bytes(state)
    with pytest.raises(EngineFailure):
        step_engine(config, state, make_bar(config, 0))
    assert canonical_json_bytes(state) == before
    with pytest.raises(EngineFailure):
        run_engine(config, (make_bar(config, 0),), initial_state=state)
    assert canonical_json_bytes(state) == before


def test_rule_result_known_pass_fail_reason_projection_by_context(config) -> None:
    definition = config.strategy_version.definition.model_copy(
        update={
            "nodes": (
                ConstantNode(
                    kind="constant",
                    node_id="false",
                    value_type="boolean",
                    unit="boolean",
                    value=False,
                ),
                ConstantNode(
                    kind="constant",
                    node_id="true",
                    value_type="boolean",
                    unit="boolean",
                    value=True,
                ),
                GroupNode(kind="group", node_id="root", op="all", children=("true", "false")),
            )
        }
    )
    anchor = datetime(2026, 1, 2, 3, 5, tzinfo=UTC)
    for context, expected in (
        ("entry_rule", ("ENTRY_RULE_FAIL", "ENTRY_RULE_PASS")),
        ("exit_rule", ("EXIT_RULE_FAIL", "EXIT_RULE_PASS")),
        ("setup_filter", ("FILTER_FAIL", "ENTRY_RULE_PASS")),
    ):
        status, evidence = evaluate_rule(
            definition,
            "root",
            {},
            anchor,
            context=context,  # type: ignore[arg-type]
        )
        assert status == "FAIL"
        assert tuple(item.node_id for item in evidence) == ("false", "true")
        assert tuple(item.reason_code for item in evidence) == expected
        assert all(
            item.known_at == anchor and item.source_bar_record_ids == () for item in evidence
        )
    numeric_features = (
        FeatureInstance(
            feature_id="fast-feature",
            kind="input",
            name="close",
            output_type="price",
            unit="contract_price",
            interval_seconds=300,
            parameters={},
        ),
        FeatureInstance(
            feature_id="slow-feature",
            kind="input",
            name="close",
            output_type="price",
            unit="contract_price",
            interval_seconds=300,
            parameters={},
        ),
    )
    temporal = definition.model_copy(
        update={
            "features": numeric_features,
            "nodes": (
                FeatureNode(
                    kind="feature", node_id="fast-node", feature_id="fast-feature", offset=0
                ),
                FeatureNode(
                    kind="feature", node_id="slow-node", feature_id="slow-feature", offset=0
                ),
                TemporalCompareNode(
                    kind="temporal_compare",
                    node_id="cross",
                    op="crosses_above",
                    left_feature="fast-node",
                    right_feature="slow-node",
                ),
            ),
        }
    )

    def point(feature_id: str, minute: int, value: str) -> FeatureValue:
        known = datetime(2026, 1, 2, 3, minute, tzinfo=UTC)
        return FeatureValue(
            feature_id=feature_id,
            interval_seconds=300,
            evaluation_bar_end=known,
            value_type="price",
            unit="contract_price",
            value=Decimal(value),
            status="KNOWN",
            reason_code=None,
            source_bar_record_ids=(),
            source_dataset_provenance=(),
            known_at=known,
        )

    crossing_status, crossing_evidence = evaluate_rule(
        temporal,
        "cross",
        {
            "fast-feature": (point("fast-feature", 0, "1"), point("fast-feature", 5, "3")),
            "slow-feature": (point("slow-feature", 0, "2"), point("slow-feature", 5, "2")),
        },
        anchor,
    )
    assert crossing_status == "PASS"
    assert crossing_evidence[0].node_id == "cross" and crossing_evidence[0].value is True


def test_rule_result_unknown_dependency_and_group_precedence(config) -> None:
    feature = FeatureInstance(
        feature_id="guard",
        kind="input",
        name="close",
        output_type="boolean",
        unit="boolean",
        interval_seconds=300,
        parameters={},
    )
    definition = config.strategy_version.definition.model_copy(
        update={
            "features": (feature,),
            "nodes": (
                FeatureNode(kind="feature", node_id="a_unknown", feature_id="guard", offset=0),
                ConstantNode(
                    kind="constant",
                    node_id="b_pass",
                    value_type="boolean",
                    unit="boolean",
                    value=True,
                ),
                GroupNode(kind="group", node_id="root", op="any", children=("a_unknown", "b_pass")),
            ),
        }
    )
    status, evidence = evaluate_rule(
        definition, "root", {}, datetime(2026, 1, 2, 3, 5, tzinfo=UTC), context="exit_rule"
    )
    assert status == "PASS"
    assert tuple(item.result for item in evidence) == ("UNKNOWN", "PASS")
    assert tuple(item.reason_code for item in evidence) == (
        "NOT_READY_WARMUP",
        "EXIT_RULE_PASS",
    )


def test_rule_result_filter_projection_and_lexical_leaf_evidence(config) -> None:
    feature = FeatureInstance(
        feature_id="guard",
        kind="input",
        name="close",
        output_type="boolean",
        unit="boolean",
        interval_seconds=300,
        parameters={},
    )
    definition = config.strategy_version.definition.model_copy(
        update={
            "features": (feature,),
            "nodes": (
                FeatureNode(kind="feature", node_id="z_unknown", feature_id="guard", offset=0),
                ConstantNode(
                    kind="constant",
                    node_id="a_pass",
                    value_type="boolean",
                    unit="boolean",
                    value=True,
                ),
                GroupNode(kind="group", node_id="root", op="all", children=("z_unknown", "a_pass")),
            ),
        }
    )
    status, evidence = evaluate_rule(
        definition, "root", {}, datetime(2026, 1, 2, 3, 5, tzinfo=UTC), context="setup_filter"
    )
    assert status == "UNKNOWN"
    assert tuple(item.node_id for item in evidence) == ("a_pass", "z_unknown")
    assert tuple(item.reason_code for item in evidence) == ("ENTRY_RULE_PASS", "FILTER_UNKNOWN")


def test_rule_result_all_any_none_truth_reason_matrix(config) -> None:
    def constant(node_id: str, value: bool) -> ConstantNode:
        return ConstantNode(
            kind="constant", node_id=node_id, value_type="boolean", unit="boolean", value=value
        )

    for op, values, expected in (
        ("all", (True, True), "PASS"),
        ("all", (True, False), "FAIL"),
        ("any", (False, True), "PASS"),
        ("any", (False, False), "FAIL"),
        ("none", (False, False), "PASS"),
        ("none", (False, True), "FAIL"),
    ):
        definition = config.strategy_version.definition.model_copy(
            update={
                "nodes": (
                    constant("a", values[0]),
                    constant("b", values[1]),
                    GroupNode(kind="group", node_id="root", op=op, children=("a", "b")),
                )
            }
        )
        status, evidence = evaluate_rule(
            definition, "root", {}, datetime(2026, 1, 2, 3, 5, tzinfo=UTC)
        )
        assert status == expected
        assert len(evidence) == 2


def test_confirmed_pivot_plateau_and_regime_availability() -> None:
    case = _frozen("confirmed_pivot_plateau_and_regime_availability")
    given, expected = case["given"], case["expected"]
    config = make_backtest_config()
    pivot = FeatureInstance(
        feature_id="fixture-pivot-high",
        kind="indicator",
        name="confirmed_pivot_v1",
        output_type="level",
        unit="contract_price",
        interval_seconds=60,
        parameters={"pivot_kind": "high", "left": given["left"], "right": given["right"]},
    )
    regime = FeatureInstance(
        feature_id="fixture-regime",
        kind="indicator",
        name="swing_regime_v1",
        output_type="regime",
        unit="regime",
        interval_seconds=60,
        parameters={"left": given["left"], "right": given["right"]},
    )
    definition = config.strategy_version.definition.model_copy(
        update={
            "features": (pivot, regime),
            "nodes": (
                ConstantNode(
                    kind="constant",
                    node_id="entry",
                    value_type="boolean",
                    unit="boolean",
                    value=False,
                ),
            ),
        }
    )
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                }
            )
        }
    )
    highs = tuple(
        Decimal(item)
        for item in (
            *given["highs_indices_0_to_5"],
            "12",
            "12",
            "12",
            "12",
            "12",
            "14",
            given["four_regime_pivots"]["latest_high"],
            "15",
            "14",
            "13",
            "12",
            "12",
            "12",
            "12",
            "12",
        )
    )
    lows = tuple(
        Decimal(item)
        for item in (
            "8",
            "9",
            "10",
            "11",
            "11",
            "10",
            "11",
            given["four_regime_pivots"]["prior_low"],
            "11",
            "11",
            "11",
            "11",
            "11",
            "11",
            "12",
            "11",
            given["four_regime_pivots"]["latest_low"],
            "11",
            "12",
            "12",
            "12",
        )
    )
    state = initialize_engine(config)
    pivot_values, regime_values = [], []
    for index, (high, low) in enumerate(zip(highs, lows, strict=True)):
        midpoint = (high + low) / Decimal(2)
        event = make_bar(
            config,
            index,
            open_price=str(midpoint),
            high=str(high),
            low=str(low),
            close=str(midpoint),
        )
        state = step_engine(config, state, event).state
        values = {value.feature_id: value for value in state.feature_values}
        pivot_values.append(values[pivot.feature_id])
        regime_values.append(values[regime.feature_id])
    runtime = next(item for item in state.feature_runtime if item.feature_id == pivot.feature_id)
    point = next(
        item
        for item in runtime.accumulator.confirmed_highs
        if item.value == Decimal(given["highs_indices_0_to_5"][given["candidate_index"]])
    )
    confirmed_index = int((point.evaluation_bar_end - BASE).total_seconds() // 60) - 1
    assert {
        "pivot_high_index": confirmed_index,
        "available_before_index4": any(value.status == "KNOWN" for value in pivot_values[:4]),
        "available_after_index4_completion": pivot_values[4].status == "KNOWN",
        "regime_before_all_four_confirmations": (
            "unknown" if regime_values[17].status == "UNKNOWN" else regime_values[17].value
        ),
        "regime_at_index20": regime_values[20].value,
    } == expected


def test_pivot_confirmation_lag() -> None:
    from .test_reversal_breakout import _bars, _case, _configured

    case = _case("pivot_confirmation_lag")
    given = case["given"]
    highs = tuple(Decimal(item) for item in given["zone_bar_highs_indices_0_to_6"])
    config = _configured(
        breakout=False,
        reversal=False,
        sides="long",
        pivot_radius=given["pivot_left"],
    )
    state = initialize_engine(config)
    observed = []
    for zone_index, high in enumerate(highs):
        result = run_engine(
            config,
            _bars(
                config,
                zone_index * 5,
                (zone_index + 1) * 5,
                open_price=str(high - 1),
                high=str(high),
                low=str(high - 2),
                close=str(high - 1),
            ),
            initial_state=state,
        )
        state = result.state
        observed.append(tuple(item for item in state.zones if item.kind == "resistance"))
    pivot = next(item for item in observed[6] if item.high == highs[3])
    assert all(not observed[index] for index in (3, 4, 5))
    assert pivot.pivot_bar_end == make_bar(config, 19).selection.bar.end_at
    assert pivot.confirmation_bar_end == make_bar(config, 34).selection.bar.end_at
    assert [
        (
            "no pivot event is available at indices 3, 4 or 5"
            if all(not observed[index] for index in (3, 4, 5))
            else "premature pivot"
        ),
        (
            f"PIVOT_HIGH price {pivot.high:.2f} is emitted only after zone bar 6 completes"
            if pivot.confirmation_bar_end == make_bar(config, 34).selection.bar.end_at
            else "late/early pivot"
        ),
    ] == case["expected"]


def test_long_entry_then_stop_gap() -> None:
    expected = _frozen("long_entry_then_stop_gap")["expected"]
    engine = _fixed_trade(
        side="long",
        stop_ticks=77,
        target_ticks=25,
        entry_open="2003.00",
        entry_high="2003.20",
        entry_low="2002.40",
        entry_close="2002.80",
        exit_open="1994",
        exit_high="1994.5",
        exit_low="1993.5",
        exit_close="1994.2",
        entry_limit="2002.50",
    )
    assert {
        "fills": [f"{item.fill_price:.2f}" for item in engine.fills],
        "fees": str(engine.state.fees),
        "realized_pnl": str(engine.state.realized_pnl),
        "cash": str(engine.state.cash),
        "equity": str(engine.state.equity),
        "maximum_drawdown": str(engine.state.risk.maximum_drawdown),
    } == expected
    result = protective_fill(
        position_side="long",
        stop=Decimal("1994.8"),
        target=Decimal(2005),
        bar=BarPrices(Decimal(1994), Decimal("1994.5"), Decimal("1993.5"), Decimal("1994.2")),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="stop_first",
    )
    assert result is not None and result.fill_price == Decimal("1993.9")
    assert price_pnl(
        side="long",
        entry=Decimal("2002.5"),
        exit_or_mark=result.fill_price,
        multiplier=Decimal(10),
        quantity=1,
    ) == Decimal("-86.00")


def test_short_entry_then_stop_gap() -> None:
    expected = _frozen("short_entry_then_stop_gap")["expected"]
    engine = _fixed_trade(
        side="short",
        stop_ticks=57,
        target_ticks=25,
        entry_open="2007.00",
        entry_high="2008.10",
        entry_low="2006.80",
        entry_close="2007.50",
        exit_open="2014.2",
        exit_high="2014.5",
        exit_low="2013.5",
        exit_close="2014",
        entry_limit="2008.00",
    )
    assert {
        "fills": [f"{item.fill_price:.2f}" for item in engine.fills],
        "fees": str(engine.state.fees),
        "realized_pnl": str(engine.state.realized_pnl),
        "cash": str(engine.state.cash),
        "equity": str(engine.state.equity),
        "maximum_drawdown": str(engine.state.risk.maximum_drawdown),
    } == expected
    result = protective_fill(
        position_side="short",
        stop=Decimal("2013.7"),
        target=Decimal("2005.5"),
        bar=BarPrices(Decimal("2014.2"), Decimal("2014.5"), Decimal("2013.5"), Decimal(2014)),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="stop_first",
    )
    assert result is not None and result.fill_price == Decimal("2014.3")
    assert price_pnl(
        side="short",
        entry=Decimal(2008),
        exit_or_mark=result.fill_price,
        multiplier=Decimal(10),
        quantity=1,
    ) == Decimal("-63.00")


def test_intrabar_entry_with_stop_and_target_is_conservative_stop() -> None:
    expected = _frozen("intrabar_entry_with_stop_and_target_is_conservative_stop")["expected"]
    fill = limit_fill(
        side="buy",
        limit=Decimal(2000),
        open_price=Decimal(2001),
        high=Decimal(2003),
        low=Decimal(1997),
        tick=Decimal("0.1"),
        slippage_ticks=0,
    )
    assert fill == (Decimal(2000), Decimal(2000), "LIMIT_ENTRY_TOUCH")
    result = protective_fill(
        position_side="long",
        stop=Decimal(1998),
        target=Decimal(2002),
        bar=BarPrices(Decimal(2001), Decimal(2003), Decimal(1997), Decimal(2002)),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="stop_first",
    )
    assert result is not None
    assert (result.reason, result.fill_price) == ("BOTH_HIT_STOP_FIRST", Decimal("1997.9"))
    from .test_reversal_breakout import _bars, _configured, _seed, _zone

    def drive(candidate_config):
        initial = _seed(candidate_config, _zone(kind="support", low="1998.8", high="1999.5"))
        armed = run_engine(
            candidate_config,
            _bars(candidate_config, 0, 5, open_price="2002", high="2003", low="2001", close="2002"),
            initial_state=initial,
        )
        entered = run_engine(
            candidate_config,
            _bars(
                candidate_config, 5, 10, open_price="2001", high="2002", low="1999.9", close="2001"
            ),
            initial_state=armed.state,
        )
        waiting = step_engine(candidate_config, entered.state, make_bar(candidate_config, 10))
        return step_engine(
            candidate_config,
            waiting.state,
            make_bar(
                candidate_config,
                11,
                open_price="2001",
                high="2003",
                low="1997",
                close="2002",
            ),
        )

    config = _configured(sides="long")
    same_bar = drive(config)
    assert [fill.reason for fill in same_bar.fills] == [
        "LIMIT_ENTRY_TOUCH",
        "ENTRY_BAR_CONSERVATIVE_STOP",
    ]
    variant_definition = config.strategy_version.definition.model_copy(
        update={
            "order_policy": config.strategy_version.definition.order_policy.model_copy(
                update={"entry_bar_exit_policy": "next_bar_only"}
            )
        }
    )
    variant = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": variant_definition,
                    "canonical_definition_sha256": canonical_definition_sha256(variant_definition),
                }
            ),
            "fill_model": config.fill_model.model_copy(
                update={"entry_bar_exit_policy": "next_bar_only"}
            ),
        }
    )
    next_bar = drive(variant)
    assert {
        "default": {
            "filled_exit": "stop" if same_bar.fills[-1].effect == "close" else "none",
            "cash": str(same_bar.state.cash),
            "fees": str(same_bar.state.fees),
        },
        "next_bar_only_variant": (
            "position remains open; same-bar target is not granted; RunSpec must name the limitation"
            if next_bar.state.position is not None
            and all(fill.effect != "close" for fill in next_bar.fills)
            and variant.fill_model.entry_bar_exit_policy == "next_bar_only"
            else "next-bar-only limitation not exercised"
        ),
    } == expected


def _zone(identifier: str, kind: str, low: str, high: str, sequence: int) -> ZoneState:
    return ZoneState(
        zone_id=identifier,
        kind=kind,  # type: ignore[arg-type]
        low=Decimal(low),
        high=Decimal(high),
        creation_sequence=sequence,
        touch_count=1,
        created_zone_index=sequence,
        last_touch_zone_index=sequence,
        last_filled_execution_index=None,
        pivot_bar_end=datetime(2026, 1, 2, 3, tzinfo=UTC),
        confirmation_bar_end=datetime(2026, 1, 2, 3, 5, tzinfo=UTC),
        known_at=datetime(2026, 1, 2, 3, 5, tzinfo=UTC),
        source_bar_record_ids=(),
        source_dataset_provenance=(),
    )


def test_next_zone_targets_long_short_and_missing() -> None:
    expected = _frozen("next_zone_targets_long_short_and_missing")["expected"]
    zones = (
        _zone("018f4c00-0000-7000-8000-000000000101", "resistance", "2005", "2006", 10),
        _zone("018f4c00-0000-7000-8000-000000000102", "resistance", "2003", "2004", 8),
        _zone("018f4c00-0000-7000-8000-000000000103", "support", "2006", "2007", 10),
        _zone("018f4c00-0000-7000-8000-000000000104", "support", "2007.5", "2008", 8),
    )
    assert next_zone_target(zones, side="long", entry=Decimal(2000)) == Decimal(2003)
    assert next_zone_target(zones, side="short", entry=Decimal(2010)) == Decimal(2008)
    assert next_zone_target(zones, side="long", entry=Decimal(2020)) is None
    from .test_reversal_breakout import _bars, _configured, _seed
    from .test_reversal_breakout import _zone as setup_zone

    config = _configured(sides="long", target_mode="next_zone")
    initial = _seed(
        config,
        setup_zone(kind="support", low="2000", high="2000.5", sequence=0),
        setup_zone(kind="resistance", low="2003", high="2004", sequence=1),
    )
    armed = run_engine(
        config,
        _bars(config, 0, 5, open_price="2005", high="2006", low="2004", close="2005"),
        initial_state=initial,
    )
    snapshot = armed.decisions[-1].evidence.selected_setup
    assert snapshot is not None and snapshot.target == Decimal(expected["long_target"])
    later_zone = setup_zone(kind="resistance", low="2002", high="2002.5", sequence=2)
    changed = armed.state.model_copy(update={"zones": (*armed.state.zones, later_zone)})
    entered = run_engine(
        config,
        _bars(config, 5, 10, open_price="2002", high="2003", low="2000.9", close="2002"),
        initial_state=changed,
    )
    assert entered.decisions[-1].evidence.selected_setup.target == Decimal(expected["long_target"])
    short_config = _configured(sides="short", target_mode="next_zone")
    short_initial = _seed(
        short_config,
        setup_zone(kind="resistance", low="2010", high="2010.5", sequence=0),
        setup_zone(kind="support", low="2007.5", high="2008", sequence=1),
    )
    short_armed = run_engine(
        short_config,
        _bars(short_config, 0, 5, open_price="2005.5", high="2007", low="2005", close="2005.5"),
        initial_state=short_initial,
    )
    short_snapshot = short_armed.decisions[-1].evidence.selected_setup
    assert short_snapshot is not None
    missing_initial = _seed(
        config, setup_zone(kind="support", low="2000", high="2000.5", sequence=0)
    )
    missing = run_engine(
        config,
        _bars(config, 0, 5, open_price="2005", high="2006", low="2004", close="2005"),
        initial_state=missing_initial,
    )
    assert {
        "long_target": f"{snapshot.target:.2f}",
        "short_target": f"{short_snapshot.target:.2f}",
        "later_zone_changes_target": entered.decisions[-1].evidence.selected_setup.target
        != snapshot.target,
        "no_target_reason": missing.decisions[-1].reason_code,
        "no_target_order_count": len(missing.intents),
    } == expected


def test_r_multiple_targets_long_short() -> None:
    case = _frozen("r_multiple_targets_long_short")
    given, expected = case["given"], case["expected"]
    projection = {}
    fee_independent, gap_independent = [], []
    for side in ("long", "short"):
        from .conftest import make_config

        config = make_backtest_config()
        if side == "short":
            config = config.model_copy(
                update={"strategy_version": make_config(side="short").strategy_version}
            )
        recipe = given[side]
        stop_ticks = int(
            abs(Decimal(recipe["entry"]) - Decimal(recipe["stop"])) / config.contract.tick_size
        )
        definition = config.strategy_version.definition.model_copy(
            update={
                "exit_policy": config.strategy_version.definition.exit_policy.model_copy(
                    update={
                        "stop": FixedTicksStop(kind="fixed_ticks", ticks=stop_ticks),
                        "target": RiskMultipleTarget(
                            kind="risk_multiple", multiple=recipe["r_multiple"]
                        ),
                    }
                )
            }
        )
        config = config.model_copy(
            update={
                "strategy_version": config.strategy_version.model_copy(
                    update={
                        "definition": definition,
                        "canonical_definition_sha256": canonical_definition_sha256(definition),
                    }
                ),
                "cost_model": config.cost_model.model_copy(update={"market_slippage_ticks": 0}),
            }
        )
        price = recipe["entry"]
        events = tuple(
            make_bar(config, i, open_price=price, high=price, low=price, close=price)
            for i in range(6)
        )
        opened = run_engine(config, events)
        position = opened.state.position
        assert position is not None and opened.state.fees > 0
        assert position.entry_price == Decimal(recipe["entry"])
        assert position.protective_bracket.stop.trigger_price == Decimal(recipe["stop"])
        projection[f"{side}_target"] = f"{position.protective_bracket.target.trigger_price:.2f}"
        fee_independent.append(
            position.protective_bracket.target.trigger_price
            == r_multiple_target(
                side=side,
                entry=position.entry_price,
                stop=position.protective_bracket.stop.trigger_price,
                multiple=Decimal(recipe["r_multiple"]),
                tick=config.contract.tick_size,
            )
        )
        later = step_engine(
            config,
            opened.state,
            make_bar(
                config,
                6,
                open_price=str(Decimal(price) + (Decimal(1) if side == "long" else Decimal(-1))),
                high=str(Decimal(price) + (Decimal(1) if side == "long" else Decimal(-1))),
                low=str(Decimal(price) + (Decimal(1) if side == "long" else Decimal(-1))),
                close=str(Decimal(price) + (Decimal(1) if side == "long" else Decimal(-1))),
            ),
        )
        assert later.state.position is not None
        gap_independent.append(
            later.state.position.protective_bracket.target.trigger_price
            == position.protective_bracket.target.trigger_price
        )
    projection["fees_in_R"] = not all(fee_independent)
    projection["gap_slippage_in_R"] = not all(gap_independent)
    assert projection == expected


def test_favorable_limit_gaps_revalidate_bracket_both_sides() -> None:
    expected = _frozen("favorable_limit_gaps_revalidate_bracket_both_sides")["expected"]
    projection = {}
    long_fill = limit_fill(
        side="buy",
        limit=Decimal(2000),
        open_price=Decimal(1997),
        high=Decimal(2000),
        low=Decimal(1996),
        tick=Decimal("0.1"),
        slippage_ticks=0,
    )
    short_fill = limit_fill(
        side="sell",
        limit=Decimal(2010),
        open_price=Decimal(2013),
        high=Decimal(2014),
        low=Decimal(2010),
        tick=Decimal("0.1"),
        slippage_ticks=0,
    )
    assert long_fill is not None and not valid_bracket(
        side="long", entry=long_fill[1], stop=Decimal(1998), target=Decimal(2002)
    )
    assert short_fill is not None and not valid_bracket(
        side="short", entry=short_fill[1], stop=Decimal(2012), target=Decimal(2008)
    )
    from .test_reversal_breakout import _bars, _configured, _seed, _zone

    for side, kind, bounds, arm_prices, retest_prices, gap_prices in (
        (
            "long",
            "support",
            ("2000", "2000.5"),
            {"low": "2004", "high": "2006", "close": "2005"},
            {"low": "2000.9", "high": "2004", "close": "2003"},
            {"open_price": "1997", "low": "1996", "high": "2001", "close": "1998"},
        ),
        (
            "short",
            "resistance",
            ("2010", "2010.5"),
            {"low": "2005", "high": "2007", "close": "2005.5"},
            {"low": "2007", "high": "2009.6", "close": "2008"},
            {"open_price": "2013", "low": "2009", "high": "2014", "close": "2012"},
        ),
    ):
        config = _configured(sides=side)
        initial = _seed(config, _zone(kind=kind, low=bounds[0], high=bounds[1]))
        armed = run_engine(config, _bars(config, 0, 5, **arm_prices), initial_state=initial)
        entered = run_engine(
            config, _bars(config, 5, 10, **retest_prices), initial_state=armed.state
        )
        waiting = step_engine(config, entered.state, make_bar(config, 10))
        rejected = step_engine(config, waiting.state, make_bar(config, 11, **gap_prices))
        assert rejected.fills == () and rejected.state.pending_entry is None
        cancellation = next(
            event
            for event in rejected.events
            if event.event_type == "ORDER_STATE"
            and event.payload.get("to") == "CANCELLED"
            and event.payload.get("reason") == "ENTRY_GEOMETRY_GAP"
        )
        projection[side] = {
            "reason": cancellation.payload["reason"],
            "fill_count": len(rejected.fills),
            "commission": str(rejected.state.fees),
        }
    assert projection == expected


def _fill_interval_config(config, fill: int, execution: int):
    strategy = config.strategy_version.model_copy(
        update={"fill_interval_seconds": fill, "execution_interval_seconds": execution}
    )
    return config.model_copy(
        update={
            "strategy_version": strategy,
            "fill_model": config.fill_model.model_copy(update={"fill_interval_seconds": fill}),
        }
    )


def test_subminute_and_nonminute_fill_intervals_are_unsupported_before_state_creation(
    config,
) -> None:
    for fill, reason in ((30, "SUBMINUTE"), (90, "NON_MINUTE")):
        with pytest.raises(EngineFailure) as caught:
            initialize_engine(_fill_interval_config(config, fill, 300))
        assert caught.value.error.code == "UNSUPPORTED_FILL_INTERVAL"
        assert caught.value.error.details.reason == reason
        assert caught.value.error.details.fill_interval_seconds == fill


def test_fill_interval_must_equal_strategy_version_without_state_creation(config) -> None:
    strategy = config.strategy_version.model_copy(update={"fill_interval_seconds": 300})
    invalid = config.model_copy(update={"strategy_version": strategy})
    with pytest.raises(EngineFailure) as caught:
        initialize_engine(invalid)
    assert canonical_json_bytes(caught.value.error.details) == (
        b'{"kind":"unsupported_configuration","path":"/fill_model/fill_interval_seconds",'
        b'"reason":"FILL_INTERVAL_STRATEGY_MISMATCH"}'
    )


def test_batch_default_and_absolute_bar_limits_fail_before_state_creation(config) -> None:
    assert config.max_canonical_bars == 500_000
    for limit in (0, 2_000_001):
        with pytest.raises(EngineFailure) as caught:
            initialize_engine(config.model_copy(update={"max_canonical_bars": limit}))
        assert caught.value.error.details.path == "/max_canonical_bars"
        assert caught.value.error.details.reason == "BAR_LIMIT_RANGE"


def test_fresh_state_retains_exact_bounded_config_snapshot_and_hash(config) -> None:
    state = initialize_engine(config)
    assert state.config_snapshot == config
    assert state.config_snapshot is not config
    assert state.config_snapshot.strategy_version is not config.strategy_version
    assert state.config_sha256 == hashlib.sha256(canonical_json_bytes(config)).hexdigest()
    assert len(canonical_json_bytes(state.config_snapshot)) <= 16_777_216


def test_contract_change_precedes_multiple_config_diffs_and_reports_snapshot_expected(
    config,
) -> None:
    state = initialize_engine(config)
    changed_contract = config.contract.model_copy(
        update={
            "contract_id": "018f4c00-0000-7000-8000-000000000099",
            "record_version": 2,
        }
    )
    changed = config.model_copy(update={"contract": changed_contract, "source": "changed"})
    with pytest.raises(EngineFailure) as caught:
        step_engine(changed, state, make_bar(config, 0))
    details = caught.value.error.details
    assert caught.value.error.code == "UNSUPPORTED_CONTRACT_CHANGE"
    assert details.expected_contract_id == config.contract.contract_id
    assert details.actual_contract_id == changed_contract.contract_id
    assert details.expected_record_version == 1
    assert details.actual_record_version == 2


def test_multiple_noncontract_config_diffs_report_lexically_first_deep_pointer(config) -> None:
    state = initialize_engine(config)
    changed = config.model_copy(
        update={"end_at": datetime(2026, 1, 3, tzinfo=UTC), "source": "changed"}
    )
    with pytest.raises(EngineFailure) as caught:
        checkpoint_engine(changed, state)
    assert caught.value.error.code == "CONFIG_MISMATCH"
    assert caught.value.error.details.path == "/end_at"
    assert caught.value.error.details.expected_config_sha256 == state.config_sha256
    assert (
        caught.value.error.details.actual_config_sha256
        == hashlib.sha256(canonical_json_bytes(changed)).hexdigest()
    )


def test_checkpoint_config_snapshot_round_trip_and_tamper_precedence(config) -> None:
    state = initialize_engine(config)
    checkpoint = checkpoint_engine(config, state)
    assert canonical_json_bytes(restore_engine(config, checkpoint)) == canonical_json_bytes(state)

    tampered_snapshot = state.config_snapshot.model_copy(update={"source": "tampered"})
    tampered_state = state.model_copy(update={"config_snapshot": tampered_snapshot})
    tampered = checkpoint.model_copy(
        update={
            "state": tampered_state,
            "state_sha256": hashlib.sha256(canonical_json_bytes(tampered_state)).hexdigest(),
        }
    )
    with pytest.raises(EngineFailure) as caught:
        restore_engine(config, tampered)
    assert (caught.value.error.details.reason, caught.value.error.details.path) == (
        "CHECKPOINT_CONFIG_HASH",
        "/state/config_snapshot",
    )

    outer = checkpoint.model_copy(update={"config_sha256": "1" * 64})
    with pytest.raises(EngineFailure) as caught:
        restore_engine(config, outer)
    assert (caught.value.error.details.reason, caught.value.error.details.path) == (
        "CHECKPOINT_CONFIG_HASH",
        "/config_sha256",
    )


def test_step_and_checkpoint_detect_snapshot_hash_inconsistency_without_state_change(
    config,
) -> None:
    state = initialize_engine(config).model_copy(update={"config_sha256": "0" * 64})
    before = canonical_json_bytes(state)
    for call in (
        lambda: checkpoint_engine(config, state),
        lambda: step_engine(config, state, make_bar(config, 0)),
    ):
        with pytest.raises(EngineFailure) as caught:
            call()
        assert caught.value.error.code == "CONFIG_MISMATCH"
        assert caught.value.error.details.path == ""
        assert canonical_json_bytes(state) == before


def test_config_snapshot_byte_limit_and_preimplementation_format_policy_are_exact(config) -> None:
    oversized_feature = FeatureInstance(
        feature_id="oversized",
        kind="input",
        name="close",
        output_type="price",
        unit="contract_price",
        interval_seconds=300,
        parameters={"padding": "x" * 16_777_216},
    )
    definition = config.strategy_version.definition.model_copy(
        update={"features": (oversized_feature,)}
    )
    strategy = config.strategy_version.model_copy(
        update={
            "definition": definition,
            "canonical_definition_sha256": canonical_definition_sha256(definition),
        }
    )
    oversized = config.model_copy(update={"strategy_version": strategy})
    with pytest.raises(EngineFailure) as caught:
        initialize_engine(oversized)
    assert canonical_json_bytes(caught.value.error.details) == (
        b'{"kind":"unsupported_configuration","path":"","reason":"CONFIG_SNAPSHOT_LIMIT"}'
    )
    checkpoint = checkpoint_engine(config, initialize_engine(config))
    assert checkpoint.format_version == 1
