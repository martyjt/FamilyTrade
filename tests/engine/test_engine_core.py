from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, getcontext

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
    EngineError,
    EngineFailure,
    UnsupportedConfigurationDetails,
    ZoneState,
)
from familytrade.strategies.definitions import ConstantNode, FeatureInstance, FeatureNode, GroupNode
from familytrade.strategies.indicators import confirmed_pivot_index, quantize_feature, swing_regime
from familytrade.strategies.rules import evaluate_rule
from familytrade.strategies.setups import r_multiple_target, valid_bracket
from familytrade.strategies.validation import canonical_definition_sha256
from familytrade.strategies.zones import next_zone_target

from .conftest import make_bar


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


def test_long_target_gap_price_improvement() -> None:
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
    original = getcontext().prec
    getcontext().prec = 4
    try:
        assert commission(Decimal("1.005"), 2) == Decimal("2.01")
        assert quantize_feature(Decimal("2.333333333333333")) == Decimal("2.333333333333")
    finally:
        getcontext().prec = original


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
    highs = tuple(Decimal(item) for item in ("10", "15", "15", "14", "13", "12"))
    assert confirmed_pivot_index(highs[:4], candidate_index=2, left=2, right=2, high=True) is None
    assert confirmed_pivot_index(highs[:5], candidate_index=2, left=2, right=2, high=True) == 2
    assert (
        swing_regime(
            prior_high=Decimal(15),
            latest_high=Decimal(16),
            prior_low=Decimal(9),
            latest_low=Decimal(10),
        )
        == "bullish"
    )
    assert (
        swing_regime(
            prior_high=Decimal(15),
            latest_high=Decimal(15),
            prior_low=Decimal(9),
            latest_low=Decimal(10),
        )
        == "unknown"
    )


def test_pivot_confirmation_lag() -> None:
    highs = tuple(Decimal(item) for item in ("10", "15", "15", "14", "13"))
    for observed in range(3, 5):
        result = confirmed_pivot_index(
            highs[:observed], candidate_index=2, left=2, right=2, high=True
        )
        assert result is None
    assert confirmed_pivot_index(highs, candidate_index=2, left=2, right=2, high=True) == 2


def test_long_entry_then_stop_gap() -> None:
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
    zones = (
        _zone("018f4c00-0000-7000-8000-000000000101", "resistance", "2005", "2006", 10),
        _zone("018f4c00-0000-7000-8000-000000000102", "resistance", "2003", "2004", 8),
        _zone("018f4c00-0000-7000-8000-000000000103", "support", "2006", "2007", 10),
        _zone("018f4c00-0000-7000-8000-000000000104", "support", "2007.5", "2008", 8),
    )
    assert next_zone_target(zones, side="long", entry=Decimal(2000)) == Decimal(2003)
    assert next_zone_target(zones, side="short", entry=Decimal(2010)) == Decimal(2008)
    assert next_zone_target(zones, side="long", entry=Decimal(2020)) is None


def test_r_multiple_targets_long_short() -> None:
    assert r_multiple_target(
        side="long",
        entry=Decimal(2000),
        stop=Decimal("1997.3"),
        multiple=Decimal("2.5"),
        tick=Decimal("0.1"),
    ) == Decimal("2006.7")
    assert r_multiple_target(
        side="short",
        entry=Decimal(2010),
        stop=Decimal("2012.4"),
        multiple=Decimal(3),
        tick=Decimal("0.1"),
    ) == Decimal("2002.8")


def test_favorable_limit_gaps_revalidate_bracket_both_sides() -> None:
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
