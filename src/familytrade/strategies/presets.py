"""Reviewed provenance presets; returned models are immutable fresh values."""

from __future__ import annotations

from familytrade.strategies.definitions import StrategyDraftFromDefinitionInput
from familytrade.strategies.validation import _as_model_input


def _value(name: str, *, reversal: bool, funded: bool) -> dict[str, object]:
    return {
        "kind": "definition",
        "schema_version": "v1",
        "name": name,
        "definition_schema_version": "rule-strategy-v1",
        "catalogue_version": "feature-catalogue-v1",
        "execution_interval_seconds": 900,
        "fill_interval_seconds": 60,
        "definition": {
            "kind": "rule_strategy_v1",
            "name": name,
            "side_policy": "both",
            "features": [],
            "nodes": [],
            "entry_rules": {"long_root": None, "short_root": None},
            "exit_rules": {"long_root": None, "short_root": None},
            "entry_combination": "setups_only",
            "exit_policy": {
                "kind": "bracket_exit_v1",
                "stop": {"kind": "setup_price", "field": "stop"},
                "target": {"kind": "setup_price", "field": "target"},
            },
            "setup_modules": [
                {"kind": "one_position_v1"},
                {
                    "kind": "confirmed_pivot_zones_v1",
                    "zone_interval_seconds": 300,
                    "use_atr": True,
                    "atr_length": 10 if funded else 20,
                    "pivot_left": 3,
                    "pivot_right": 3,
                    "merge_multiple": "0.25",
                    "max_width_multiple": "0.5",
                    "minimum_touches": 1,
                    "max_zones": 29 if funded else 12,
                    "zone_max_age_bars": 500,
                    "cooldown_execution_bars": 1 if funded else 0,
                },
                {
                    "kind": "reversal_setup_v1",
                    "enabled": reversal,
                    "sides": "both",
                    "approach_multiple": "2.5" if funded else "1.5",
                    "require_directional_approach": False,
                    "stop_buffer_multiple": "1" if funded else "0.4",
                    "recent_peak_stop": True,
                    "peak_lookback": 12,
                    "target_mode": "measured_move",
                    "measured_move_multiple": "1",
                    "r_multiple": "3",
                    "filter_root": None,
                },
                {
                    "kind": "breakout_retest_v1",
                    "enabled": True,
                    "sides": "both",
                    "confirmation_mode": "beyond",
                    "break_multiple": "1",
                    "pullback_multiple": "0.5",
                    "setup_expiry_execution_bars": 18 if funded else 24,
                    "stop_buffer_multiple": "1" if funded else "0.8",
                    "use_vwap_stop": True,
                    "target_mode": "measured_move",
                    "measured_move_multiple": "1",
                    "r_multiple": "3",
                    "arm_filter_root": None,
                    "entry_filter_root": None,
                },
            ],
            "order_policy": {
                "entry_type": "market",
                "limit_price_source": None,
                "entry_ttl_execution_bars": 1,
                "both_hit_policy": "stop_first",
                "entry_bar_exit_policy": "conservative_stop_first",
            },
            "constraints": {
                "max_entries_per_trading_day": 200 if funded else 23,
                "entry_windows": [],
            },
        },
    }


def get_preset(preset_id: str) -> StrategyDraftFromDefinitionInput:
    values = {
        "reversal_breakout_mgc_original_v1": (True, False),
        "breakout_mgc_original_v1": (False, False),
        "reversal_breakout_funded_v2_reference_v1": (True, True),
    }
    try:
        reversal, funded = values[preset_id]
    except KeyError as error:
        raise KeyError("Unknown strategy preset.") from error
    return StrategyDraftFromDefinitionInput.model_validate(
        _as_model_input(_value(preset_id, reversal=reversal, funded=funded))
    )
