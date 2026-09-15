from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from familytrade.market_data.models import (
    BarSelection,
    CalendarVersion,
    CalendarWindow,
    CompletedBar,
    DatasetRevision,
    FuturesContract,
    OwnedSeriesKey,
    SourceWatermark,
)
from familytrade.simulation.state import (
    CompletedBarEvent,
    CostModel,
    EmissionContext,
    EngineConfig,
    FillModel,
    FixedContractsSizing,
    RiskPolicy,
)
from familytrade.strategies.definitions import (
    ConstantNode,
    EntryRules,
    ExitPolicy,
    ExitRules,
    FixedTicksStop,
    FixedTicksTarget,
    OrderPolicy,
    RuleDefinition,
    StrategyConstraints,
    StrategyVersion,
)
from familytrade.strategies.validation import canonical_definition_sha256

OWNER = "018f4c00-0000-7000-8000-000000000001"
RUN = "018f4c00-0000-7000-8000-000000000002"
LANE = "018f4c00-0000-7000-8000-000000000003"
STRATEGY = "018f4c00-0000-7000-8000-000000000004"
CONTRACT = "018f4c00-0000-7000-8000-000000000005"
CALENDAR = "018f4c00-0000-7000-8000-000000000006"
BASE = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)


def make_config(*, side: str = "long", fill_interval: int = 60) -> EngineConfig:
    long_root = "entry" if side == "long" else None
    short_root = "entry" if side == "short" else None
    definition = RuleDefinition(
        kind="rule_strategy_v1",
        name="engine-test",
        side_policy=side,
        features=(),
        nodes=(
            ConstantNode(
                kind="constant", node_id="entry", value_type="boolean", unit="boolean", value=True
            ),
        ),
        entry_rules=EntryRules(long_root=long_root, short_root=short_root),
        exit_rules=ExitRules(long_root=None, short_root=None),
        entry_combination="rules_only",
        exit_policy=ExitPolicy(
            kind="bracket_exit_v1",
            stop=FixedTicksStop(kind="fixed_ticks", ticks=20),
            target=FixedTicksTarget(kind="fixed_ticks", ticks=20),
        ),
        setup_modules=(),
        order_policy=OrderPolicy(
            entry_type="market",
            limit_price_source=None,
            entry_ttl_execution_bars=2,
            both_hit_policy="stop_first",
            entry_bar_exit_policy="conservative_stop_first",
        ),
        constraints=StrategyConstraints(max_entries_per_trading_day=23, entry_windows=()),
    )
    strategy = StrategyVersion(
        schema_version="v1",
        owner_user_id=OWNER,
        strategy_version_id=STRATEGY,
        name="engine-test",
        status="validated",
        definition_schema_version="rule-strategy-v1",
        definition=definition,
        canonical_definition_sha256=canonical_definition_sha256(definition),
        catalogue_version="feature-catalogue-v1",
        execution_interval_seconds=300,
        fill_interval_seconds=fill_interval,
        required_warmup_bars=0,
        created_from_version_id=None,
        created_at=BASE,
        record_version=1,
    )
    calendar = CalendarVersion(
        schema_version="v1",
        calendar_id=CALENDAR,
        owner_user_id=OWNER,
        calendar_version=1,
        exchange_timezone="UTC",
        coverage_start=BASE - timedelta(days=1),
        coverage_end=BASE + timedelta(days=3),
        windows=(
            CalendarWindow(
                kind="open",
                start_at=BASE - timedelta(days=1),
                end_at=BASE + timedelta(days=3),
                trading_day=date(2026, 1, 2),
                reason=None,
                ordinal=0,
            ),
        ),
        metadata_as_of=BASE,
        provenance_ref="synthetic:test",
        created_at=BASE,
        record_version=1,
    )
    contract = FuturesContract(
        schema_version="v1",
        contract_id=CONTRACT,
        owner_user_id=OWNER,
        provider="synthetic",
        provider_contract_id="MGC-test",
        root_symbol="MGC",
        exchange="COMEX",
        currency="USD",
        tick_size=Decimal("0.1"),
        multiplier=Decimal(10),
        expiry_label="2026-02",
        first_trade_at=BASE - timedelta(days=30),
        last_trade_at=BASE + timedelta(days=2),
        exchange_timezone="UTC",
        calendar_id=CALENDAR,
        calendar_version=1,
        entry_cutoff_at=BASE + timedelta(days=1, hours=22),
        liquidation_start_at=BASE + timedelta(days=1, hours=23),
        metadata_as_of=BASE,
        provenance_ref="synthetic:test",
        created_at=BASE,
        record_version=1,
    )
    return EngineConfig(
        schema_version="v1",
        engine_version="paper-engine-v1",
        run_id=RUN,
        lane_id=LANE,
        mode="forward_paper",
        owner_user_id=OWNER,
        strategy_version=strategy,
        contract=contract,
        calendar=calendar,
        dataset_revision=None,
        source="synthetic",
        price_basis="trades",
        entry_windows=(),
        start_at=BASE,
        end_at=None,
        force_close_at=None,
        starting_cash=Decimal(25000),
        base_currency="USD",
        cost_model=CostModel(
            commission_per_contract_per_side=Decimal("1.25"),
            currency="USD",
            market_slippage_ticks=1,
            stop_slippage_ticks=1,
            limit_slippage_ticks=0,
        ),
        fill_model=FillModel(
            fill_interval_seconds=fill_interval,
            partial_fill_policy="all_or_none",
            both_hit_policy="stop_first",
            entry_bar_exit_policy="conservative_stop_first",
        ),
        sizing_policy=FixedContractsSizing(kind="fixed_contracts", quantity=1),
        risk_policy=RiskPolicy(
            per_entry_loss_cap=Decimal(125),
            daily_loss_cap=Decimal(250),
            cumulative_drawdown_cap=Decimal(2500),
            max_positions=1,
            max_entries_per_trading_day=23,
        ),
        end_policy="mark_open",
        max_canonical_bars=500000,
        random_seed=None,
    )


def make_bar(
    config: EngineConfig,
    index: int,
    *,
    open_price: str = "2000",
    high: str = "2001",
    low: str = "1999",
    close: str = "2000",
) -> CompletedBarEvent:
    start = BASE + timedelta(minutes=index)
    end = start + timedelta(minutes=1)
    bar_id = f"018f4c00-0000-7{index:03x}-8000-{index:012x}"
    bar = CompletedBar(
        schema_version="v1",
        bar_record_id=bar_id,
        owner_user_id=OWNER,
        source=config.source,
        price_basis="trades",
        contract_id=CONTRACT,
        interval_seconds=60,
        start_at=start,
        end_at=end,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(10),
        source_revision=1,
        received_at=end,
        completed_at=end,
        quality="valid",
        supersedes_bar_record_id=None,
        payload_hash=f"{index:064x}",
        created_at=end,
        record_version=1,
    )
    recorded = end if config.mode == "backtest" else end + timedelta(milliseconds=100)
    submitted = recorded if config.mode == "backtest" else recorded + timedelta(milliseconds=100)
    output = submitted if config.mode == "backtest" else submitted + timedelta(milliseconds=100)
    return CompletedBarEvent(
        kind="completed_bar_v1",
        selection=BarSelection(
            bar=bar,
            origin="archive" if config.mode == "backtest" else "active",
            availability_at=end if config.mode == "backtest" else recorded,
            correction_observations=(),
        ),
        published_base_revision_id=(
            config.dataset_revision.dataset_revision_id
            if config.dataset_revision is not None
            else None
        ),
        recorded_at=recorded,
        emission_context=EmissionContext(
            attempt_id=None,
            fencing_token=1,
            order_submitted_at=submitted,
            output_recorded_at=output,
        ),
    )


def make_backtest_config(
    *, end_minutes: int = 30, end_policy: Literal["mark_open", "force_close"] = "mark_open"
) -> EngineConfig:
    config = make_config()
    revision_id = "018f4c00-0000-7000-8000-000000000007"
    revision = DatasetRevision(
        dataset_revision_id=revision_id,
        owner_user_id=OWNER,
        series_key=OwnedSeriesKey(
            owner_user_id=OWNER,
            source=config.source,
            price_basis="trades",
            contract_id=CONTRACT,
            interval_seconds=60,
        ),
        contract_version=1,
        parent_revision_id=None,
        manifest_uri=f"ft-archive://manifest/{revision_id}",
        manifest_sha256="0" * 64,
        manifest_byte_length=1,
        partition_refs=(),
        calendar_id=CALENDAR,
        calendar_version=1,
        coverage_start=BASE - timedelta(days=1),
        coverage_end=config.contract.last_trade_at + timedelta(minutes=1),
        source_watermark=SourceWatermark(
            max_received_at=BASE,
            max_bar_record_id="018f4c00-0000-7000-8000-000000000008",
        ),
        correction_refs=(),
        status="published",
        created_at=BASE,
        published_at=BASE,
        parent_depth=0,
        restore_closure_revision_count=1,
        restore_closure_row_count=0,
        restore_closure_bytes=0,
        record_version=1,
    )
    end = BASE + timedelta(minutes=end_minutes)
    force_close = end - timedelta(minutes=1) if end_policy == "force_close" else None
    return config.model_copy(
        update={
            "lane_id": None,
            "mode": "backtest",
            "dataset_revision": revision,
            "end_at": end,
            "force_close_at": force_close,
            "end_policy": end_policy,
        }
    )


@pytest.fixture
def config() -> EngineConfig:
    return make_config()
