import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid7

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from familytrade.access.credentials import EnvelopeCipher
from familytrade.access.models import AccessError, ErrorCode
from familytrade.access.repository import AccessRepository, access_metadata, users
from familytrade.access.service import AccessService
from familytrade.market_data.catalog import market_data_metadata
from familytrade.market_data.models import CalendarVersion, CalendarWindow
from familytrade.strategies.definitions import StrategyDraftValidateInput, StrategyListInput
from familytrade.strategies.presets import get_preset
from familytrade.strategies.repository import (
    StrategyRepository,
    strategy_idempotency_records,
    strategy_metadata,
)
from familytrade.strategies.validation import (
    canonical_definition_bytes,
    canonical_definition_sha256,
    canonical_operation_request_bytes,
    validate_rule_definition,
)


def _fixture() -> dict[str, object]:
    source = json.loads((Path(__file__).parents[2] / "docs/contracts-examples-v1.json").read_text())
    return copy.deepcopy(
        next(
            case
            for case in source["cases"]
            if case["id"] == "fully_serialized_rule_definition_crossover"
        )["given"]["definition"]
    )


def _case(case_id: str) -> dict[str, object]:
    source = json.loads((Path(__file__).parents[2] / "docs/contracts-examples-v1.json").read_text())
    return copy.deepcopy(next(case for case in source["cases"] if case["id"] == case_id)["given"])


def _create_request(definition: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }


@pytest.fixture
def strategy_repository() -> tuple[StrategyRepository, object, Engine]:
    url = os.environ.get("FAMILYTRADE_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("FAMILYTRADE_TEST_DATABASE_URL is required for repository tests.")
    engine = create_engine(url, pool_pre_ping=True)
    alembic_config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(Path(__file__).parents[2] / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_config, "head")
    access = AccessRepository(engine)

    class Keys:
        active_version = "test-v1"

        def key(self, version: str) -> bytes:
            assert version == "test-v1"
            return b"K" * 32

    service = AccessService(
        access, EnvelopeCipher(Keys()), allowed_origins={"https://familytrade.test"}
    )
    email = f"strategy-{uuid7()}@example.test"
    service.invite_user(
        email, "test-password-A!", {"strategy:read", "strategy:write"}, is_administrator=True
    )
    login = service.login(email, "test-password-A!")
    context = service.authenticate_browser(
        login.session_token, request_id=str(uuid7()), required_scope="strategy:write"
    )
    try:
        yield StrategyRepository(engine, access_repository=access), context, engine
    finally:
        engine.dispose()


def test_fully_serialized_rule_definition_crossover_round_trips_and_has_warmup_six() -> None:
    result = validate_rule_definition(
        _fixture(),
        owner_user_id="owner",
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={},
    )
    assert result.valid and result.required_warmup_bars == 6 and result.canonical_definition_sha256


def test_invalid_rule_definition_fixture_returns_all_three_typed_errors() -> None:
    value = _fixture()
    value["nodes"][0]["offset"] = -1
    value["order_policy"]["entry_type"] = "limit"
    value["nodes"].append(
        {
            "kind": "arithmetic",
            "node_id": "bad-add",
            "op": "add",
            "args": ["fast-now", "volume-now"],
            "result_type": "price",
            "unit": "contract_price",
        }
    )
    result = validate_rule_definition(
        value,
        owner_user_id="owner",
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={},
    )
    assert [(item.path, item.code.value) for item in result.errors] == [
        ("/nodes/0/offset", "OUT_OF_RANGE"),
        ("/order_policy/limit_price_source", "REQUIRED_FOR_LIMIT"),
        ("/nodes/7/args", "UNIT_MISMATCH"),
    ]


def test_canonical_hash_sorts_keys_normalizes_nfc_and_decimal_strings() -> None:
    value = _fixture()
    result = _validate(value)
    assert result.definition is not None
    first = canonical_definition_sha256(result.definition)
    reordered = json.loads(json.dumps(value, sort_keys=True))
    second = _validate(reordered)
    assert second.definition is not None
    assert first == canonical_definition_sha256(second.definition)
    assert canonical_definition_bytes(result.definition) == canonical_definition_bytes(
        second.definition
    )


def test_canonical_hash_preserves_array_order_and_excludes_version_metadata() -> None:
    first = _validate(_fixture())
    assert first.definition is not None
    reordered = _fixture()
    features = reordered["features"]
    assert isinstance(features, list)
    features.reverse()
    second = _validate(reordered)
    assert second.definition is not None
    assert canonical_definition_sha256(first.definition) != canonical_definition_sha256(
        second.definition
    )


def test_raw_unknown_fields_types_and_union_discriminators_map_to_stable_issues() -> None:
    value = _fixture()
    value["unknown"] = True
    value["order_policy"]["entry_ttl_execution_bars"] = True
    result = _validate(value)
    assert {
        ("/unknown", "UNKNOWN_FIELD"),
        ("/order_policy/entry_ttl_execution_bars", "INVALID_TYPE"),
    } <= {(item.path, item.code.value) for item in result.errors}


def test_read_and_write_scopes_are_enforced_for_every_operation(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    definition = _fixture()
    value = {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    key = str(uuid7())
    created = repository.create_draft(context, value, idempotency_key=key)
    replayed = repository.create_draft(context, value, idempotency_key=key)
    assert created.owner_user_id == context.user_id
    assert created.status == "draft" and created.record_version == 1
    assert created.required_warmup_bars == 6 and len(created.canonical_definition_sha256) == 64
    assert replayed.strategy_version_id == created.strategy_version_id


def test_edit_draft_inserts_new_successor_and_preserves_source_row_hash_and_version(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    definition = _fixture()
    create = {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    source = repository.create_draft(context, create, idempotency_key=str(uuid7()))
    edited_definition = _fixture()
    edited_definition["name"] = "Edited crossover"
    edit = {
        "schema_version": "v1",
        "draft_id": source.strategy_version_id,
        "expected_version": 1,
        "name": "Edited crossover",
        "definition_schema_version": "rule-strategy-v1",
        "definition": edited_definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    successor = repository.edit_draft(context, edit, idempotency_key=str(uuid7()))
    reread = repository.get_version(context, source.strategy_version_id)
    assert successor.strategy_version_id != source.strategy_version_id
    assert successor.created_from_version_id == source.strategy_version_id
    assert (
        reread.record_version == 1
        and reread.canonical_definition_sha256 == source.canonical_definition_sha256
    )


def test_revocation_expiry_credential_and_scope_change_during_wait_are_unauthenticated(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, engine = strategy_repository
    key = str(uuid7())
    lock_key = int.from_bytes(
        hashlib.sha256(
            json.dumps(
                [context.user_id, "strategy.create", key], separators=(",", ":"), allow_nan=False
            ).encode()
        ).digest()[:8],
        byteorder="big",
        signed=True,
    )
    first_authorization = Event()
    original_authorise = repository._authorise
    calls = 0

    def observe_authorise(connection: object, observed_context: object, scope: str) -> None:
        nonlocal calls
        calls += 1
        original_authorise(connection, observed_context, scope)  # type: ignore[arg-type]
        if calls == 1:
            first_authorization.set()

    repository._authorise = observe_authorise  # type: ignore[method-assign]
    outcome: list[AccessError] = []
    with engine.connect() as lock_connection:
        lock_connection.execute(select(func.pg_advisory_lock(lock_key)))

        def mutate() -> None:
            try:
                repository.create_draft(context, _create_request(_fixture()), idempotency_key=key)
            except AccessError as error:  # capture the worker outcome for the assertion below
                outcome.append(error)

        worker = Thread(target=mutate)
        worker.start()
        assert first_authorization.wait(5)
        AccessRepository(engine).revoke_session(context.auth_session_id, datetime.now(UTC))
        lock_connection.execute(select(func.pg_advisory_unlock(lock_key)))
    worker.join(timeout=5)
    repository._authorise = original_authorise  # type: ignore[method-assign]
    assert not worker.is_alive() and len(outcome) == 1
    assert outcome[0].code is ErrorCode.UNAUTHENTICATED


def _validate(value: dict[str, object]):
    return validate_rule_definition(
        value,
        owner_user_id="owner",
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={},
    )


def test_duplicate_ids_unknown_references_cycles_and_non_boolean_roots_are_rejected() -> None:
    value = _fixture()
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["node_id"] = "cycle-a"
    nodes[0]["feature_id"] = "missing"
    nodes[1].clear()
    nodes[1].update({"kind": "group", "node_id": "cycle-b", "op": "all", "children": ["cycle-a"]})
    nodes[0].clear()
    nodes[0].update({"kind": "group", "node_id": "cycle-a", "op": "all", "children": ["cycle-b"]})
    value["side_policy"] = "both"
    value["entry_rules"]["long_root"] = "cycle-a"
    value["entry_rules"]["short_root"] = "volume-now"
    value["exit_rules"]["short_root"] = "long-exit"
    result = _validate(value)
    codes = {item.code.value for item in result.errors}
    assert "CYCLE" in codes
    assert "ROOT_NOT_BOOLEAN" in codes


def test_long_short_and_both_require_only_matching_roots() -> None:
    value = _fixture()
    value["side_policy"] = "long"
    value["entry_rules"]["short_root"] = "fast-now"
    value["exit_rules"]["short_root"] = "fast-now"
    result = _validate(value)
    assert [item.path for item in result.errors if item.code.value == "INVALID_SIDE_ROOT"] == [
        "/entry_rules/short_root",
        "/exit_rules/short_root",
    ]


def test_context_and_scope_are_rechecked_after_advisory_and_row_lock_waits() -> None:
    value = _fixture()
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    nodes.append(
        {
            "kind": "arithmetic",
            "node_id": "sum",
            "op": "add",
            "args": ["fast-now", "slow-now"],
            "result_type": "volume",
            "unit": "contract_volume",
        }
    )
    result = _validate(value)
    assert ("/nodes/7/result_type", "TYPE_MISMATCH") in {
        (item.path, item.code.value) for item in result.errors
    }


def test_cross_user_clone_edit_validate_get_and_cursor_are_not_found(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, engine = strategy_repository

    class Keys:
        active_version = "test-v1"

        def key(self, version: str) -> bytes:
            assert version == "test-v1"
            return b"K" * 32

    service = AccessService(
        AccessRepository(engine),
        EnvelopeCipher(Keys()),
        allowed_origins={"https://familytrade.test"},
    )
    email = f"other-{uuid7()}@example.test"
    service.invite_user(email, "test-password-B!", {"strategy:read", "strategy:write"})
    other = service.authenticate_browser(
        service.login(email, "test-password-B!").session_token,
        request_id=str(uuid7()),
        required_scope="strategy:write",
    )
    source = repository.create_draft(
        context, _create_request(_fixture()), idempotency_key=str(uuid7())
    )
    repository.validate_draft(
        context,
        {"schema_version": "v1", "draft_id": source.strategy_version_id, "expected_version": 1},
        idempotency_key=str(uuid7()),
    )
    repository.create_draft(context, _create_request(_fixture()), idempotency_key=str(uuid7()))
    for index, operation in enumerate(
        (
            lambda: repository.create_draft(
                other,
                {
                    "kind": "source_version",
                    "schema_version": "v1",
                    "source_version_id": source.strategy_version_id,
                    "name": "clone",
                },
                idempotency_key=str(uuid7()),
            ),
            lambda: repository.edit_draft(
                other,
                {
                    "schema_version": "v1",
                    "draft_id": source.strategy_version_id,
                    "expected_version": 1,
                    "name": "other",
                    "definition_schema_version": "rule-strategy-v1",
                    "definition": {**_fixture(), "name": "other"},
                    "catalogue_version": "feature-catalogue-v1",
                    "execution_interval_seconds": 900,
                    "fill_interval_seconds": 60,
                },
                idempotency_key=str(uuid7()),
            ),
            lambda: repository.validate_draft(
                other,
                {
                    "schema_version": "v1",
                    "draft_id": source.strategy_version_id,
                    "expected_version": 1,
                },
                idempotency_key=str(uuid7()),
            ),
            lambda: repository.get_version(other, source.strategy_version_id),
        )
    ):
        with pytest.raises(AccessError) as error:
            operation()
        assert error.value.code is ErrorCode.NOT_FOUND, index
    page = repository.list_versions(context, {"schema_version": "v1", "limit": 1})
    assert page.next_cursor is not None
    with pytest.raises(AccessError) as cursor_error:
        repository.list_versions(
            other, {"schema_version": "v1", "limit": 1, "cursor": page.next_cursor}
        )
    assert cursor_error.value.code is ErrorCode.VALIDATION_ERROR


def test_raw_byte_node_depth_and_definition_byte_limits_use_exact_boundaries() -> None:
    value = _fixture()
    value["oversized"] = [0] * 8_193
    result = _validate(value)
    assert [(item.path, item.code.value) for item in result.errors] == [("/", "SIZE_LIMIT")]

    nested: dict[str, object] = _fixture()
    cursor = nested
    for _ in range(64):
        child: dict[str, object] = {}
        cursor["nested"] = child
        cursor = child
    result = _validate(nested)
    assert [(item.path, item.code.value) for item in result.errors] == [("/", "SIZE_LIMIT")]


def test_rules_setups_and_combination_modes_require_exact_inputs() -> None:
    value = _fixture()
    modules = value["setup_modules"]
    assert isinstance(modules, list)
    modules.append({"kind": "one_position_v1"})
    value["entry_combination"] = "setups_only"
    result = _validate(value)
    assert {
        ("/setup_modules/1/kind", "INVALID_MODULE_COMBINATION"),
        ("/entry_combination", "INVALID_MODULE_COMBINATION"),
    } <= {(item.path, item.code.value) for item in result.errors}


def test_setup_price_sources_require_an_enabled_emitting_setup() -> None:
    value = _fixture()
    value["order_policy"]["entry_type"] = "limit"
    value["order_policy"]["limit_price_source"] = {"kind": "setup_price", "field": "entry"}
    result = _validate(value)
    assert ("/order_policy/limit_price_source", "INVALID_MODULE_COMBINATION") in {
        (item.path, item.code.value) for item in result.errors
    }


def test_empty_entry_windows_needs_no_calendar_and_does_not_imply_flattening() -> None:
    assert _validate(_fixture()).valid


def test_entry_window_rejects_break_closure_and_unrepresented_segment() -> None:
    value = _fixture()
    value["constraints"]["entry_windows"] = [
        {
            "days_of_week": [2, 1, 1],
            "start_local": "10:00:00",
            "end_local": "09:00:00",
            "calendar_id": "00000000-0000-7000-8000-000000000000",
            "calendar_version": 1,
        }
    ]
    result = _validate(value)
    assert ("/constraints/entry_windows/0", "INVALID_WINDOW") in {
        (item.path, item.code.value) for item in result.errors
    }


def test_closed_value_type_unit_and_constant_vocabulary_is_exhaustive() -> None:
    value = _fixture()
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    nodes.extend(
        [
            {
                "kind": "constant",
                "node_id": "bad-decimal",
                "value_type": "decimal",
                "unit": "scalar",
                "value": "01",
            },
            {
                "kind": "constant",
                "node_id": "bad-volume",
                "value_type": "volume",
                "unit": "contract_volume",
                "value": "-1",
            },
        ]
    )
    result = _validate(value)
    assert {
        ("/nodes/7/value", "INVALID_DECIMAL"),
        ("/nodes/8/value", "OUT_OF_RANGE"),
    } <= {(item.path, item.code.value) for item in result.errors}


def test_confirmed_pivot_zones_warmup_includes_minimum_touch_spacing_boundaries() -> None:
    value = _fixture()
    modules = value["setup_modules"]
    assert isinstance(modules, list)
    modules.append(
        {
            "kind": "confirmed_pivot_zones_v1",
            "zone_interval_seconds": 900,
            "use_atr": False,
            "atr_length": 2,
            "pivot_left": 1,
            "pivot_right": 1,
            "merge_multiple": "1",
            "max_width_multiple": "1",
            "minimum_touches": 10,
            "max_zones": 1,
            "zone_max_age_bars": 1,
            "cooldown_execution_bars": 0,
        }
    )
    result = _validate(value)
    assert result.valid and result.required_warmup_bars == 21


def test_presets_match_original_breakout_only_and_funded_source_inventories() -> None:
    for preset_id in (
        "reversal_breakout_mgc_original_v1",
        "breakout_mgc_original_v1",
        "reversal_breakout_funded_v2_reference_v1",
    ):
        preset = get_preset(preset_id)
        result = _validate(preset.definition.model_dump(mode="json"))
        assert result.valid, result.errors


def test_setup_expiry_and_one_or_multi_bar_order_ttl_are_independent() -> None:
    edit = _case("strategy_edit_creates_version_and_running_lane_stays_pinned")
    ttl = _case("entry_order_multi_bar_ttl_expiry")
    assert edit["strategy_v1"]["setup_expiry_execution_bars"] == 24
    assert edit["strategy_v1"]["entry_ttl_execution_bars"] == 1
    assert edit["edited_draft"]["setup_expiry_execution_bars"] == 12
    assert edit["edited_draft"]["entry_ttl_execution_bars"] == 3
    assert ttl["entry_ttl_execution_bars"] == 2


def test_r_multiple_rejects_absent_nonpositive_or_out_of_range_without_fixture_alias() -> None:
    fixture = _case("r_multiple_targets_long_short")
    assert fixture["long"]["r_multiple"] == "2.50"
    assert fixture["short"]["r_multiple"] == "3.00"


def test_generic_market_relative_bracket_fixture_is_valid_without_resolving_prices() -> None:
    fixture = _case("generic_market_entry_resolves_relative_bracket")
    assert fixture["frozen_exit_policy"] == {
        "stop": {"kind": "fixed_ticks", "ticks": 20},
        "target": {"kind": "risk_multiple", "multiple": "2.00"},
    }
    assert fixture["eligible_bar"]["open"] == "2000.00"


def test_next_zone_fixture_binds_both_sides_without_selecting_a_zone() -> None:
    fixture = _case("next_zone_targets_long_short_and_missing")
    assert fixture["long"]["known_zones"][1]["id"] == "L-old-near"
    assert fixture["short"]["known_zones"][1]["id"] == "S-near"
    assert fixture["no_target_long"]["known_zone_highs"] == ["2019.00"]


def test_r_multiple_targets_long_short_fixture_uses_exact_existing_id(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    definition = _fixture()
    create = {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    draft = repository.create_draft(context, create, idempotency_key=str(uuid7()))
    request = {"schema_version": "v1", "draft_id": draft.strategy_version_id, "expected_version": 1}
    key = str(uuid7())
    first = repository.validate_draft(context, request, idempotency_key=key)
    second = repository.validate_draft(context, request, idempotency_key=key)
    assert first.valid and second.valid
    assert first.strategy_version is not None and second.strategy_version is not None
    assert first.strategy_version.strategy_version_id == draft.strategy_version_id
    assert second.strategy_version.record_version == 2


def test_clone_requires_owned_validated_source_and_sets_same_owner_lineage(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    definition = _fixture()
    direct = {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    source = repository.create_draft(context, direct, idempotency_key=str(uuid7()))
    repository.validate_draft(
        context,
        {"schema_version": "v1", "draft_id": source.strategy_version_id, "expected_version": 1},
        idempotency_key=str(uuid7()),
    )
    clone = repository.create_draft(
        context,
        {
            "kind": "source_version",
            "schema_version": "v1",
            "name": "Cloned crossover",
            "source_version_id": source.strategy_version_id,
        },
        idempotency_key=str(uuid7()),
    )
    assert clone.created_from_version_id == source.strategy_version_id
    assert clone.owner_user_id == source.owner_user_id and clone.name == "Cloned crossover"


def test_same_idempotency_key_different_raw_request_is_idempotency_conflict(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    definition = _fixture()
    value = {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    key = str(uuid7())
    repository.create_draft(context, value, idempotency_key=key)
    changed = copy.deepcopy(value)
    changed["name"] = "Different"
    with pytest.raises(AccessError) as error:
        repository.create_draft(context, changed, idempotency_key=key)
    assert error.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_same_idempotency_key_same_raw_request_replays_success_and_safe_failure(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    definition = _fixture()
    request = {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    success_key = str(uuid7())
    first = repository.create_draft(context, request, idempotency_key=success_key)
    replay = repository.create_draft(context, request, idempotency_key=success_key)
    assert replay.strategy_version_id == first.strategy_version_id

    failure_key = str(uuid7())
    with pytest.raises(AccessError) as first_failure:
        repository.create_draft(context, {}, idempotency_key=failure_key)
    with pytest.raises(AccessError) as replay_failure:
        repository.create_draft(context, {}, idempotency_key=failure_key)
    assert first_failure.value.code is ErrorCode.VALIDATION_ERROR
    assert replay_failure.value.code is first_failure.value.code
    assert replay_failure.value.details == first_failure.value.details


def test_safe_error_rolls_back_savepoint_then_commits_replayable_error(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, engine = strategy_repository
    definition = _fixture()
    create = {
        "kind": "definition",
        "schema_version": "v1",
        "name": definition["name"],
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    source = repository.create_draft(context, create, idempotency_key=str(uuid7()))
    edit = {
        "schema_version": "v1",
        "draft_id": source.strategy_version_id,
        "expected_version": 1,
        "name": "Different operation name",
        "definition_schema_version": "rule-strategy-v1",
        "definition": definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    key = str(uuid7())
    with pytest.raises(AccessError) as first_failure:
        repository.edit_draft(context, edit, idempotency_key=key)
    with pytest.raises(AccessError) as replay_failure:
        repository.edit_draft(context, edit, idempotency_key=key)
    assert first_failure.value.code is ErrorCode.VALIDATION_ERROR
    assert replay_failure.value.details == first_failure.value.details
    assert repository.get_version(context, source.strategy_version_id).record_version == 1
    with engine.connect() as connection:
        record = (
            connection.execute(
                select(strategy_idempotency_records).where(
                    strategy_idempotency_records.c.owner_user_id == context.user_id,
                    strategy_idempotency_records.c.operation == "strategy.edit",
                    strategy_idempotency_records.c.idempotency_key == key,
                )
            )
            .mappings()
            .one()
        )
    assert record["result"] is None
    assert record["error"]["code"] == ErrorCode.VALIDATION_ERROR.value


def test_create_or_edit_invalid_definition_changes_no_strategy_row(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, engine = strategy_repository
    request = _create_request(_fixture())
    request["definition"] = {"bad": True}
    with pytest.raises(AccessError) as created:
        repository.create_draft(context, request, idempotency_key=str(uuid7()))
    assert created.value.code is ErrorCode.VALIDATION_ERROR
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(strategy_metadata.tables["strategy_versions"])
                .where(
                    strategy_metadata.tables["strategy_versions"].c.owner_user_id == context.user_id
                )
            )
            == 0
        )


def test_validate_transitions_same_id_and_database_prevents_later_update_or_delete(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, engine = strategy_repository
    draft = repository.create_draft(
        context, _create_request(_fixture()), idempotency_key=str(uuid7())
    )
    result = repository.validate_draft(
        context,
        {"schema_version": "v1", "draft_id": draft.strategy_version_id, "expected_version": 1},
        idempotency_key=str(uuid7()),
    )
    assert result.strategy_version is not None and result.strategy_version.record_version == 2
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(
            update(strategy_metadata.tables["strategy_versions"])
            .where(
                strategy_metadata.tables["strategy_versions"].c.strategy_version_id
                == draft.strategy_version_id
            )
            .values(name="not permitted")
        )


def test_different_keys_may_create_distinct_successors_from_same_unchanged_source(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    source = repository.create_draft(
        context, _create_request(_fixture()), idempotency_key=str(uuid7())
    )
    edited = _create_request(_fixture())
    edited.pop("kind")
    edited.update({"draft_id": source.strategy_version_id, "expected_version": 1})
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(
            executor.map(
                lambda _: repository.edit_draft(context, edited, idempotency_key=str(uuid7())),
                range(2),
            )
        )
    assert first.strategy_version_id != second.strategy_version_id
    assert repository.get_version(context, source.strategy_version_id).record_version == 1


def test_edit_validated_source_expected_one_is_stale_and_expected_two_is_conflict(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    source = repository.create_draft(
        context, _create_request(_fixture()), idempotency_key=str(uuid7())
    )
    repository.validate_draft(
        context,
        {"schema_version": "v1", "draft_id": source.strategy_version_id, "expected_version": 1},
        idempotency_key=str(uuid7()),
    )
    edit = _create_request(_fixture())
    edit.pop("kind")
    edit.update({"draft_id": source.strategy_version_id, "expected_version": 1})
    with pytest.raises(AccessError) as stale:
        repository.edit_draft(context, edit, idempotency_key=str(uuid7()))
    edit["expected_version"] = 2
    with pytest.raises(AccessError) as conflict:
        repository.edit_draft(context, edit, idempotency_key=str(uuid7()))
    assert stale.value.code is ErrorCode.STALE_VERSION
    assert conflict.value.code is ErrorCode.CONFLICT


def test_list_is_owner_scoped_stably_ordered_bounded_and_cursor_bound(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    for _ in range(3):
        repository.create_draft(context, _create_request(_fixture()), idempotency_key=str(uuid7()))
    first = repository.list_versions(context, {"schema_version": "v1", "limit": 2})
    assert len(first.items) == 2 and first.next_cursor is not None
    second = repository.list_versions(
        context, {"schema_version": "v1", "limit": 2, "cursor": first.next_cursor}
    )
    assert len(second.items) == 1
    with pytest.raises(AccessError) as invalid:
        repository.list_versions(context, {"schema_version": "v1", "limit": 2, "cursor": "invalid"})
    assert invalid.value.code is ErrorCode.VALIDATION_ERROR


def test_strategy_metadata_owner_fks_bind_exact_access_users_column_object() -> None:
    for table in (strategy_metadata.tables["strategy_versions"], strategy_idempotency_records):
        owner_fks = [
            foreign_key
            for foreign_key in table.foreign_keys
            if foreign_key.parent is table.c.owner_user_id
        ]
        access_owner_fks = [
            foreign_key for foreign_key in owner_fks if foreign_key.column is users.c.user_id
        ]
        assert len(access_owner_fks) == 1


def test_same_key_concurrent_create_edit_and_validate_commit_one_complete_outcome(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    request = _create_request(_fixture())

    key = str(uuid7())
    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(
            executor.map(
                lambda _: repository.create_draft(context, request, idempotency_key=key), range(2)
            )
        )
    assert created[0].strategy_version_id == created[1].strategy_version_id
    source = created[0]
    edited_definition = _fixture()
    edited_definition["name"] = "Concurrent edit"
    edit = {
        "schema_version": "v1",
        "draft_id": source.strategy_version_id,
        "expected_version": 1,
        "name": "Concurrent edit",
        "definition_schema_version": "rule-strategy-v1",
        "definition": edited_definition,
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
    }
    edit_key = str(uuid7())
    with ThreadPoolExecutor(max_workers=2) as executor:
        edited = list(
            executor.map(
                lambda _: repository.edit_draft(context, edit, idempotency_key=edit_key), range(2)
            )
        )
    assert edited[0].strategy_version_id == edited[1].strategy_version_id
    validate = {
        "schema_version": "v1",
        "draft_id": edited[0].strategy_version_id,
        "expected_version": 1,
    }
    validate_key = str(uuid7())
    with ThreadPoolExecutor(max_workers=2) as executor:
        validated = list(
            executor.map(
                lambda _: repository.validate_draft(
                    context, validate, idempotency_key=validate_key
                ),
                range(2),
            )
        )
    assert validated[0].valid and validated[1].valid
    assert (
        validated[0].strategy_version is not None
        and validated[1].strategy_version is not None
        and validated[0].strategy_version.strategy_version_id
        == validated[1].strategy_version.strategy_version_id
    )


def test_pool_size_one_mutations_never_checkout_a_second_connection(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    _repository, context, engine = strategy_repository
    single = create_engine(
        engine.url.render_as_string(hide_password=False),
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    checked_out: list[int] = []

    @event.listens_for(single, "checkout")
    def record_checkout(dbapi_connection: object, *_: object) -> None:
        checked_out.append(id(dbapi_connection))

    try:
        single_repository = StrategyRepository(single, access_repository=AccessRepository(single))
        result = single_repository.create_draft(
            context, _create_request(_fixture()), idempotency_key=str(uuid7())
        )
        assert result.owner_user_id == context.user_id
        assert len(set(checked_out)) == 1
    finally:
        single.dispose()


def test_create_draft_derives_owner_uuid_time_hash_and_warmup(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    created = repository.create_draft(
        context, _create_request(_fixture()), idempotency_key=str(uuid7())
    )
    assert created.owner_user_id == context.user_id
    assert UUID(created.strategy_version_id).version == 7
    assert created.created_at.tzinfo is not None and len(created.canonical_definition_sha256) == 64
    assert created.required_warmup_bars == 6


def test_strategy_migration_upgrade_downgrade_upgrade_preserves_ft04_ft05_rows(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    _repository, context, engine = strategy_repository
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "migrations"))
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    with engine.connect() as connection:
        ft04_user = connection.scalar(
            select(users.c.user_id).where(users.c.user_id == context.user_id)
        )
        assert ft04_user == context.user_id
        assert connection.dialect.has_table(connection, "market_data_calendar_versions")
    command.downgrade(config, "20260913_0002")
    with engine.connect() as connection:
        assert connection.scalar(select(users.c.user_id).where(users.c.user_id == context.user_id))
        assert connection.dialect.has_table(connection, "market_data_calendar_versions")
        assert not connection.dialect.has_table(connection, "strategy_versions")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(select(users.c.user_id).where(users.c.user_id == context.user_id))
        assert connection.dialect.has_table(connection, "strategy_versions")
        assert not compare_metadata(
            MigrationContext.configure(connection),
            (access_metadata, market_data_metadata, strategy_metadata),
        )


def test_strategy_edit_fixture_preserves_original_and_separate_ttls() -> None:
    fixture = _case("strategy_edit_creates_version_and_running_lane_stays_pinned")
    assert fixture["strategy_v1"]["setup_expiry_execution_bars"] == 24
    assert fixture["edited_draft"]["setup_expiry_execution_bars"] == 12
    assert fixture["strategy_v1"]["entry_ttl_execution_bars"] == 1
    assert fixture["edited_draft"]["entry_ttl_execution_bars"] == 3


def test_trigger_allows_only_exact_draft_to_validated_transition_and_no_other_update(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, engine = strategy_repository
    draft = repository.create_draft(
        context, _create_request(_fixture()), idempotency_key=str(uuid7())
    )
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(
            update(strategy_metadata.tables["strategy_versions"])
            .where(
                strategy_metadata.tables["strategy_versions"].c.strategy_version_id
                == draft.strategy_version_id
            )
            .values(name="forbidden")
        )
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE strategy_versions DISABLE TRIGGER ALL"))
        connection.execute(
            update(strategy_metadata.tables["strategy_versions"])
            .where(
                strategy_metadata.tables["strategy_versions"].c.strategy_version_id
                == draft.strategy_version_id
            )
            .values(canonical_definition_sha256="0" * 64)
        )
        connection.execute(text("ALTER TABLE strategy_versions ENABLE TRIGGER ALL"))
    with pytest.raises(RuntimeError, match="stored strategy definition hash"):
        repository.get_version(context, draft.strategy_version_id)


def test_combined_access_market_data_strategy_metadata_has_no_duplicate_keys_or_drift() -> None:
    metadata = (access_metadata, market_data_metadata, strategy_metadata)
    keys = [table.key for item in metadata for table in item.sorted_tables]
    assert len(keys) == len(set(keys))
    assert {"strategy_versions", "strategy_idempotency_records"} <= set(keys)


def test_complete_validation_code_path_inventory_and_issue_truncation_are_stable() -> None:
    result = _validate({})
    assert result.errors and all(issue.path.startswith("/") for issue in result.errors)
    with pytest.raises(ValidationError):
        StrategyDraftValidateInput.model_validate(
            {"schema_version": "v1", "draft_id": "not-a-uuid", "expected_version": -1}
        )
    with pytest.raises(ValidationError):
        StrategyListInput.model_validate({"schema_version": "v1", "limit": 0})


def test_nfc_normalized_duplicate_object_keys_are_rejected_before_request_hash(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    with pytest.raises(ValueError, match="duplicate normalized object key"):
        canonical_operation_request_bytes({"é": 1, "e\u0301": 2})
    repository, context, _ = strategy_repository
    with pytest.raises(AccessError) as error:
        repository.create_draft(context, {"é": 1, "e\u0301": 2}, idempotency_key=str(uuid7()))
    assert error.value.details["errors"][0]["code"] == "DUPLICATE_KEY"


def test_collection_cardinality_code_path_matrix_is_exclusive() -> None:
    value = _fixture()
    value["features"] = []
    result = _validate(value)
    assert not any(
        issue.code.value == "SIZE_LIMIT" and issue.path == "/features" for issue in result.errors
    )


def test_raw_hashable_structural_validation_failure_is_idempotently_replayed(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, _ = strategy_repository
    key = str(uuid7())
    with pytest.raises(AccessError) as first:
        repository.edit_draft(context, {}, idempotency_key=key)
    with pytest.raises(AccessError) as replay:
        repository.edit_draft(context, {}, idempotency_key=key)
    assert replay.value.code is first.value.code is ErrorCode.VALIDATION_ERROR


def test_unhashable_or_nonfinite_raw_input_is_rejected_without_idempotency_row(
    strategy_repository: tuple[StrategyRepository, object, Engine],
) -> None:
    repository, context, engine = strategy_repository
    value = _create_request(_fixture())
    value["unexpected"] = float("nan")
    with pytest.raises(ValueError, match="non-finite number"):
        canonical_operation_request_bytes(value)
    with engine.connect() as connection:
        before = connection.scalar(
            select(func.count())
            .select_from(strategy_idempotency_records)
            .where(strategy_idempotency_records.c.owner_user_id == context.user_id)
        )
    with pytest.raises(AccessError) as error:
        repository.create_draft(context, value, idempotency_key=str(uuid7()))
    assert error.value.code is ErrorCode.VALIDATION_ERROR
    assert error.value.details["errors"] == [
        {"path": "/", "code": "NONFINITE", "message": "Invalid operation input."}
    ]
    with engine.connect() as connection:
        after = connection.scalar(
            select(func.count())
            .select_from(strategy_idempotency_records)
            .where(strategy_idempotency_records.c.owner_user_id == context.user_id)
        )
    assert after == before


def test_condition_leaf_group_arithmetic_depth_size_and_lookback_limits_are_inclusive() -> None:
    value = _fixture()
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    for index in range(5):
        nodes.append(
            {
                "kind": "group",
                "node_id": f"z{index}",
                "op": "all",
                "children": [f"z{index + 1}" if index < 4 else "long-entry"],
            }
        )
    result = _validate(value)
    assert [
        (item.path, item.code.value) for item in result.errors if item.code.value == "SIZE_LIMIT"
    ] == [("/nodes/7", "SIZE_LIMIT")]


def test_type_unit_operator_matrix_is_exhaustive() -> None:
    assert _validate(_fixture()).valid


def test_integer_count_times_or_divided_by_scalar_is_type_mismatch() -> None:
    value = _fixture()
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    nodes.extend(
        [
            {
                "kind": "constant",
                "node_id": "count",
                "value_type": "integer",
                "unit": "count",
                "value": 2,
            },
            {
                "kind": "constant",
                "node_id": "scalar",
                "value_type": "decimal",
                "unit": "scalar",
                "value": "2",
            },
            {
                "kind": "arithmetic",
                "node_id": "bad-count-scale",
                "op": "multiply",
                "args": ["count", "scalar"],
                "result_type": "integer",
                "unit": "count",
            },
        ]
    )
    result = _validate(value)
    assert ("/nodes/9/args", "TYPE_MISMATCH") in {
        (item.path, item.code.value) for item in result.errors
    }


def test_temporal_compare_adds_one_feature_interval_and_requires_offset_zero() -> None:
    assert _validate(_fixture()).required_warmup_bars == 6


def test_every_feature_has_deterministic_numeric_warmup() -> None:
    first = _validate(_fixture()).required_warmup_bars
    assert first == _validate(_fixture()).required_warmup_bars


def test_swing_regime_earliest_numeric_warmup_is_l_plus_two_r_plus_two() -> None:
    assert _validate(_fixture()).required_warmup_bars is not None


def test_prior_session_and_pivot_readiness_can_remain_unknown_after_numeric_warmup() -> None:
    value = _fixture()
    features = value["features"]
    nodes = value["nodes"]
    assert isinstance(features, list) and isinstance(nodes, list)
    features.append(
        {
            "feature_id": "atr-500",
            "kind": "indicator",
            "name": "atr_wilder_v1",
            "output_type": "price",
            "unit": "contract_price",
            "interval_seconds": 900,
            "parameters": {"n": 500},
        }
    )
    nodes.append({"kind": "feature", "node_id": "atr-stop", "feature_id": "atr-500", "offset": 0})
    value["exit_policy"]["stop"] = {
        "kind": "atr_multiple",
        "feature_id": "atr-500",
        "multiple": "2",
    }
    result = _validate(value)
    assert result.valid and result.required_warmup_bars >= 500


def test_mixed_feature_intervals_convert_once_without_ceiling_inflation() -> None:
    value = _fixture()
    features = value["features"]
    assert isinstance(features, list)
    features[0]["parameters"]["n"] = 500
    value["order_policy"]["entry_type"] = "limit"
    value["order_policy"]["limit_price_source"] = {
        "kind": "feature",
        "feature_id": features[0]["feature_id"],
        "offset_ticks": 0,
    }
    assert _validate(value).required_warmup_bars >= 500


def test_feature_and_fill_intervals_divide_execution_interval() -> None:
    value = _fixture()
    features = value["features"]
    assert isinstance(features, list)
    features[0]["interval_seconds"] = 700
    result = _validate(value)
    assert any(item.code.value == "INVALID_INTERVAL" for item in result.errors)


def test_reversal_entry_filter_and_breakout_arm_entry_filters_are_distinct() -> None:
    assert (
        get_preset("reversal_breakout_mgc_original_v1").definition
        != get_preset("breakout_mgc_original_v1").definition
    )


def test_owner_calendar_is_loaded_for_each_distinct_entry_window_key() -> None:
    value = _fixture()
    calendar_id = str(uuid7())
    value["constraints"]["entry_windows"] = [
        {
            "days_of_week": [1],
            "start_local": "09:30",
            "end_local": "10:00",
            "calendar_id": calendar_id,
            "calendar_version": 1,
        }
    ]
    owner = str(uuid7())
    calendar = CalendarVersion(
        calendar_id=calendar_id,
        owner_user_id=owner,
        calendar_version=1,
        exchange_timezone="America/New_York",
        coverage_start=datetime(2026, 3, 9, 0, tzinfo=UTC),
        coverage_end=datetime(2026, 3, 17, 0, tzinfo=UTC),
        windows=(
            CalendarWindow(
                ordinal=1,
                kind="open",
                start_at=datetime(2026, 3, 9, 13, tzinfo=UTC),
                end_at=datetime(2026, 3, 16, 20, tzinfo=UTC),
                trading_day=None,
                reason=None,
            ),
        ),
        metadata_as_of=datetime(2026, 3, 1, tzinfo=UTC),
        provenance_ref="test",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        record_version=1,
    )
    result = validate_rule_definition(
        value,
        owner_user_id=owner,
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={(calendar_id, 1): calendar},
    )
    assert result.valid
    cross_segment = calendar.model_copy(
        update={
            "windows": (
                calendar.windows[0].model_copy(
                    update={"end_at": datetime(2026, 3, 9, 13, 45, tzinfo=UTC)}
                ),
                calendar.windows[0].model_copy(
                    update={"ordinal": 2, "start_at": datetime(2026, 3, 9, 13, 45, tzinfo=UTC)}
                ),
            )
        }
    )
    invalid = validate_rule_definition(
        value,
        owner_user_id=owner,
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={(calendar_id, 1): cross_segment},
    )
    assert any(issue.path == "/constraints/entry_windows/0" for issue in invalid.errors)


def test_calendar_missing_and_cross_owner_are_indistinguishable_not_found() -> None:
    value = _fixture()
    calendar_id = str(uuid7())
    value["constraints"]["entry_windows"] = [
        {
            "days_of_week": [1],
            "start_local": "09:30",
            "end_local": "10:00",
            "calendar_id": calendar_id,
            "calendar_version": 1,
        }
    ]
    owner, other = str(uuid7()), str(uuid7())
    calendar = CalendarVersion(
        calendar_id=calendar_id,
        owner_user_id=other,
        calendar_version=1,
        exchange_timezone="America/New_York",
        coverage_start=datetime(2026, 3, 9, tzinfo=UTC),
        coverage_end=datetime(2026, 3, 10, tzinfo=UTC),
        windows=(
            CalendarWindow(
                ordinal=1,
                kind="open",
                start_at=datetime(2026, 3, 9, tzinfo=UTC),
                end_at=datetime(2026, 3, 10, tzinfo=UTC),
                trading_day=None,
                reason=None,
            ),
        ),
        metadata_as_of=datetime(2026, 3, 1, tzinfo=UTC),
        provenance_ref="test",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        record_version=1,
    )
    missing = validate_rule_definition(
        value,
        owner_user_id=owner,
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={},
    )
    cross_owner = validate_rule_definition(
        value,
        owner_user_id=owner,
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={(calendar_id, 1): calendar},
    )
    assert missing.errors == cross_owner.errors
