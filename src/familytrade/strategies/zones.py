"""Deterministic zone selection and target helpers."""

from __future__ import annotations

from decimal import Decimal

from familytrade.simulation.state import ZoneState


def nearest_zone(zones: tuple[ZoneState, ...], *, price: Decimal, kind: str) -> ZoneState | None:
    eligible = tuple(zone for zone in zones if zone.kind == kind)
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda zone: (
            min(abs(price - zone.low), abs(price - zone.high)),
            -zone.creation_sequence,
            zone.zone_id,
        ),
    )


def next_zone_target(zones: tuple[ZoneState, ...], *, side: str, entry: Decimal) -> Decimal | None:
    if side == "long":
        candidates = tuple(zone for zone in zones if zone.kind == "resistance" and zone.low > entry)
        if not candidates:
            return None
        return min(candidates, key=lambda z: (z.low - entry, -z.creation_sequence, z.zone_id)).low
    candidates = tuple(zone for zone in zones if zone.kind == "support" and zone.high < entry)
    if not candidates:
        return None
    return min(candidates, key=lambda z: (entry - z.high, -z.creation_sequence, z.zone_id)).high


def merge_candidate(
    zones: tuple[ZoneState, ...],
    candidate: ZoneState,
    *,
    merge_distance: Decimal,
    max_width: Decimal,
    max_zones: int,
) -> tuple[ZoneState, ...]:
    """Merge the first creation-ordered eligible same-kind zone, else append."""
    ordered = sorted(zones, key=lambda zone: (zone.creation_sequence, zone.zone_id))
    result: list[ZoneState] = []
    merged = False
    for zone in ordered:
        if not merged and zone.kind == candidate.kind:
            distance = max(
                Decimal(0), max(zone.low, candidate.low) - min(zone.high, candidate.high)
            )
            low, high = min(zone.low, candidate.low), max(zone.high, candidate.high)
            if distance <= merge_distance and high - low <= max_width:
                zone = zone.model_copy(
                    update={
                        "low": low,
                        "high": high,
                        "touch_count": zone.touch_count + 1,
                        "last_touch_zone_index": candidate.last_touch_zone_index,
                        "known_at": candidate.known_at,
                        "source_bar_record_ids": tuple(
                            dict.fromkeys(
                                (*zone.source_bar_record_ids, *candidate.source_bar_record_ids)
                            )
                        ),
                        "source_dataset_provenance": tuple(
                            dict.fromkeys(
                                (
                                    *zone.source_dataset_provenance,
                                    *candidate.source_dataset_provenance,
                                )
                            )
                        ),
                    }
                )
                merged = True
        result.append(zone)
    if not merged:
        result.append(candidate)
    return tuple(sorted(result, key=lambda z: (z.creation_sequence, z.zone_id))[-max_zones:])
