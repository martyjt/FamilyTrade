import copy
import json
import os
from pathlib import Path
from uuid import uuid7

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from familytrade.access.credentials import EnvelopeCipher
from familytrade.access.models import AccessError, ErrorCode
from familytrade.access.repository import AccessRepository, access_metadata
from familytrade.access.service import AccessService
from familytrade.strategies.presets import get_preset
from familytrade.strategies.repository import StrategyRepository, strategy_metadata
from familytrade.strategies.validation import (
    canonical_definition_bytes,
    canonical_definition_sha256,
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


@pytest.fixture
def strategy_repository() -> tuple[StrategyRepository, object, Engine]:
    url = os.environ.get("FAMILYTRADE_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("FAMILYTRADE_TEST_DATABASE_URL is required for repository tests.")
    engine = create_engine(url, pool_pre_ping=True)
    access_metadata.create_all(engine, checkfirst=True)
    strategy_metadata.create_all(engine, checkfirst=True)
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


def test_create_draft_derives_owner_uuid_time_hash_and_warmup(
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


def test_presets_return_fresh_frozen_models() -> None:
    assert get_preset("reversal_breakout_mgc_original_v1") is not get_preset(
        "reversal_breakout_mgc_original_v1"
    )


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


def test_type_unit_operator_matrix_rejects_declared_wrong_arithmetic_result() -> None:
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


def test_feature_parameters_are_closed_typed_and_bounded() -> None:
    value = _fixture()
    features = value["features"]
    assert isinstance(features, list)
    features[0]["parameters"] = {"n": True, "input": "not-a-price", "extra": 1}
    result = _validate(value)
    assert {
        ("/features/0/parameters/n", "INVALID_TYPE"),
        ("/features/0/parameters/input", "INVALID_ENUM"),
        ("/features/0/parameters/extra", "UNSUPPORTED_PARAMETER"),
    } <= {(item.path, item.code.value) for item in result.errors}


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


def test_entry_window_requires_ordered_local_range_and_known_calendar() -> None:
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


def test_r_multiple_targets_long_short_fixture_uses_exact_existing_id() -> None:
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


def test_validate_transitions_same_id_and_replays_idempotently(
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
    value = {"kind": "definition", "schema_version": "v1", "name": definition["name"], "definition_schema_version": "rule-strategy-v1", "definition": definition, "catalogue_version": "feature-catalogue-v1", "execution_interval_seconds": 900, "fill_interval_seconds": 60}
    key = str(uuid7())
    repository.create_draft(context, value, idempotency_key=key)
    changed = copy.deepcopy(value)
    changed["name"] = "Different"
    with pytest.raises(AccessError) as error:
        repository.create_draft(context, changed, idempotency_key=key)
    assert error.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
