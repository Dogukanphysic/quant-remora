"""Build immutable historical development seeds for the Testnet learner.

The seed is deliberately limited to development labels.  It can accelerate an
initial threshold fit, but its samples never claim out-of-sample, true-forward,
or execution evidence.  Input is a complete, contiguous Binance Spot CSV at a
15-minute or one-day interval.  All price arithmetic uses :class:`Decimal`.

This module has no network, database, order, or active-configuration access.
"""

from __future__ import annotations

import copy
import csv
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import hmac
from io import StringIO
import json
from pathlib import Path
import re
import time
from typing import Mapping, Sequence

import testnet_online_learner as learner


SCHEMA = 1
KIND = "binance_spot_daily_momentum_historical_development_seed"
SYMBOL = "BTCUSDT"
MARKET = "spot"
SUPPORTED_INTERVALS = {"15m": 15 * 60 * 1000, "1d": learner.DAY_MS}
MOMENTUM_LOOKBACK_DAYS = 30
EVIDENCE_ROLE = "development_only_not_forward_or_execution_evidence"
EXPECTED_FIELDS = ("ts", "open", "high", "low", "close", "volume", "exchange", "symbol")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DECIMAL_CONTEXT = Context(
    prec=80,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
)
SOURCE_CLAIM_FIELDS = (
    "source",
    "endpoint",
    "security",
    "market",
    "symbol",
    "interval",
    "start_date",
    "end_date",
    "start_month",
    "end_month",
    "candles",
    "first_ts",
    "last_ts",
    "dataset_sha256",
    "files",
)
_TOP_LEVEL_FIELDS = {
    "schema",
    "kind",
    "symbol",
    "market",
    "source_interval",
    "daily_horizon_ms",
    "momentum_lookback_days",
    "evidence_role",
    "minimum_completed_at_ms",
    "raw_candle_count",
    "daily_candle_count",
    "first_candle_close_ms",
    "last_candle_close_ms",
    "last_close",
    "raw_dataset_sha256",
    "provenance",
    "sample_count",
    "first_sample_id",
    "last_sample_id",
    "first_decision_ts",
    "last_decision_ts",
    "last_label_available_ts",
    "ordered_sample_sha256",
    "samples",
    "immutable_sha256",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _canonical_decimal(value: object, field: str) -> tuple[Decimal, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty decimal string.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a finite decimal string.") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite decimal string.")
    if number == 0:
        return number, "0"
    rendered = format(number, "f")
    return number, rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _strict_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer.")
    return value


def _timestamp(value: object, field: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValueError(f"{field} must be an unsigned millisecond timestamp.")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive millisecond timestamp.")
    return parsed


def _read_source_manifest(
    data_path: Path,
    source_manifest_path: Path | None,
) -> tuple[Mapping[str, object] | None, str | None]:
    manifest_path = source_manifest_path
    if manifest_path is None:
        companion = data_path.with_suffix(data_path.suffix + ".manifest.json")
        manifest_path = companion if companion.exists() else None
    if manifest_path is None:
        return None, None
    raw = Path(manifest_path).read_bytes()
    try:
        decoded = raw.decode("utf-8")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Source manifest must be valid UTF-8 JSON.") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("Source manifest must contain a JSON object.")
    return parsed, hashlib.sha256(raw).hexdigest()


def _source_claims(
    source: Mapping[str, object] | None,
    *,
    raw_digest: str,
    interval: str | None,
    symbol: str,
) -> tuple[str, dict[str, object] | None]:
    if source is None:
        if interval is None:
            raise ValueError("interval is required when no source manifest is available.")
        selected_interval = interval
        claims = None
    else:
        claimed_digest = source.get("dataset_sha256")
        if claimed_digest != raw_digest:
            raise ValueError("Source manifest dataset_sha256 does not match the raw CSV.")
        if source.get("market") != MARKET:
            raise ValueError("Source manifest must describe Binance Spot data.")
        if source.get("symbol") != symbol:
            raise ValueError("Source manifest symbol does not match the requested symbol.")
        claimed_interval = source.get("interval")
        if claimed_interval not in SUPPORTED_INTERVALS:
            raise ValueError("Source manifest interval must be 15m or 1d.")
        if interval is not None and interval != claimed_interval:
            raise ValueError("Requested interval does not match the source manifest.")
        selected_interval = str(claimed_interval)
        claims = {
            key: copy.deepcopy(source[key])
            for key in SOURCE_CLAIM_FIELDS
            if key in source
        }
    if selected_interval not in SUPPORTED_INTERVALS:
        raise ValueError("interval must be exactly 15m or 1d.")
    return selected_interval, claims


def _parse_rows(raw: bytes, *, symbol: str) -> list[dict[str, object]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Historical candle CSV must be UTF-8.") from exc
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None or tuple(reader.fieldnames) != EXPECTED_FIELDS:
        raise ValueError(
            "Historical candle CSV header must be exactly: "
            + ",".join(EXPECTED_FIELDS)
        )
    result: list[dict[str, object]] = []
    for line_number, raw_row in enumerate(reader, start=2):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise ValueError(f"Historical candle CSV row {line_number} is malformed.")
        if raw_row["exchange"] != "binance_spot":
            raise ValueError(f"Historical candle row {line_number} is not Binance Spot.")
        if raw_row["symbol"] != symbol:
            raise ValueError(f"Historical candle row {line_number} has the wrong symbol.")
        ts = _timestamp(raw_row["ts"], f"row {line_number} ts")
        values: dict[str, Decimal] = {}
        texts: dict[str, str] = {}
        for field in ("open", "high", "low", "close", "volume"):
            values[field], texts[field] = _canonical_decimal(
                raw_row[field], f"row {line_number} {field}"
            )
        if min(values[key] for key in ("open", "high", "low", "close")) <= 0:
            raise ValueError(f"Historical candle row {line_number} has a non-positive price.")
        if values["volume"] < 0:
            raise ValueError(f"Historical candle row {line_number} has negative volume.")
        if not (
            values["low"] <= min(values["open"], values["close"])
            <= max(values["open"], values["close"]) <= values["high"]
        ):
            raise ValueError(f"Historical candle row {line_number} has invalid OHLC ordering.")
        result.append({"ts": ts, **values, "close_text": texts["close"]})
    if not result:
        raise ValueError("Historical candle CSV is empty.")
    return result


def _validate_timeline(
    rows: Sequence[Mapping[str, object]],
    *,
    interval: str,
    now_ms: int,
) -> None:
    step = SUPPORTED_INTERVALS[interval]
    previous: int | None = None
    for index, row in enumerate(rows):
        ts = int(row["ts"])
        if ts % step:
            raise ValueError(f"Historical candle {index + 1} is not aligned to {interval}.")
        if previous is not None and ts != previous + step:
            raise ValueError(
                "Historical candles contain a gap, duplicate, or out-of-order timestamp."
            )
        previous = ts
    if interval == "15m":
        bars_per_day = learner.DAY_MS // step
        if (
            int(rows[0]["ts"]) % learner.DAY_MS != 0
            or len(rows) % bars_per_day != 0
            or int(rows[-1]["ts"]) % learner.DAY_MS != learner.DAY_MS - step
        ):
            raise ValueError("15m input must contain complete UTC days (96 candles each).")
    elif int(rows[0]["ts"]) % learner.DAY_MS:
        raise ValueError("1d input must start at a UTC day boundary.")
    last_close_ms = int(rows[-1]["ts"]) + step - 1
    if last_close_ms >= now_ms:
        raise ValueError("Historical input contains an open candle or incomplete day.")


def _daily_closes(
    rows: Sequence[Mapping[str, object]], interval: str
) -> list[dict[str, object]]:
    if interval == "1d":
        return [
            {
                "open_ts": int(row["ts"]),
                "close_ts": int(row["ts"]) + learner.DAY_MS - 1,
                "close": row["close"],
                "close_text": row["close_text"],
            }
            for row in rows
        ]
    bars_per_day = learner.DAY_MS // SUPPORTED_INTERVALS["15m"]
    result = []
    for offset in range(0, len(rows), bars_per_day):
        chunk = rows[offset:offset + bars_per_day]
        first, last = chunk[0], chunk[-1]
        result.append(
            {
                "open_ts": int(first["ts"]),
                "close_ts": int(last["ts"]) + SUPPORTED_INTERVALS["15m"] - 1,
                "close": last["close"],
                "close_text": last["close_text"],
            }
        )
    return result


def _development_samples(
    daily: Sequence[Mapping[str, object]], raw_digest: str
) -> list[dict[str, object]]:
    if len(daily) < MOMENTUM_LOOKBACK_DAYS + 2:
        raise ValueError(
            "At least 32 complete UTC days are required for one 30-day momentum label."
        )
    result = []
    with localcontext(DECIMAL_CONTEXT):
        for index in range(MOMENTUM_LOOKBACK_DAYS, len(daily) - 1):
            decision = daily[index]
            earlier = daily[index - MOMENTUM_LOOKBACK_DAYS]
            following = daily[index + 1]
            decision_close = Decimal(decision["close"])
            momentum = decision_close / Decimal(earlier["close"]) - Decimal("1")
            forward_return = Decimal(following["close"]) / decision_close - Decimal("1")
            decision_ts = int(decision["close_ts"])
            result.append(
                learner.seal_sample(
                    {
                        "sample_id": f"hist:{raw_digest}:{decision_ts}",
                        "decision_ts": decision_ts,
                        "label_available_ts": int(following["close_ts"]),
                        "momentum": learner._format_decimal(momentum),
                        "forward_return": learner._format_decimal(forward_return),
                        "closed": True,
                        "out_of_sample": False,
                        "true_forward_after_freeze": False,
                    }
                )
            )
    return result


def build_seed_manifest(
    data_path: Path,
    source_manifest_path: Path | None = None,
    *,
    symbol: str = SYMBOL,
    interval: str | None = None,
    sample_limit: int | None = None,
    now_ms: int | None = None,
) -> dict[str, object]:
    """Create a sealed historical-development manifest from verified candles.

    When ``sample_limit`` is supplied, the newest exactly ``N`` contiguous
    labels are retained.  Consequently the final label always ends at the
    final daily close in the source dataset, which lets the store verify its
    boundary against the first live decision.
    """

    path = Path(data_path)
    raw = path.read_bytes()
    raw_digest = hashlib.sha256(raw).hexdigest()
    source, source_manifest_digest = _read_source_manifest(path, source_manifest_path)
    if symbol != SYMBOL:
        raise ValueError(f"Historical Testnet seeds only support {SYMBOL}.")
    selected_interval, claims = _source_claims(
        source, raw_digest=raw_digest, interval=interval, symbol=symbol
    )
    rows = _parse_rows(raw, symbol=symbol)
    observed_ms = int(time.time() * 1000) if now_ms is None else now_ms
    _strict_positive_int(observed_ms, "now_ms")
    _validate_timeline(rows, interval=selected_interval, now_ms=observed_ms)

    if source is not None:
        if source.get("candles") != len(rows):
            raise ValueError("Source manifest candle count does not match the CSV.")
        if source.get("first_ts") != int(rows[0]["ts"]):
            raise ValueError("Source manifest first_ts does not match the CSV.")
        if source.get("last_ts") != int(rows[-1]["ts"]):
            raise ValueError("Source manifest last_ts does not match the CSV.")

    daily = _daily_closes(rows, selected_interval)
    samples = _development_samples(daily, raw_digest)
    if sample_limit is not None:
        requested = _strict_positive_int(sample_limit, "sample_limit")
        if len(samples) < requested:
            raise ValueError(
                f"Requested {requested} samples but only {len(samples)} are available."
            )
        samples = samples[-requested:]

    step = SUPPORTED_INTERVALS[selected_interval]
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "kind": KIND,
        "symbol": SYMBOL,
        "market": MARKET,
        "source_interval": selected_interval,
        "daily_horizon_ms": learner.DAY_MS,
        "momentum_lookback_days": MOMENTUM_LOOKBACK_DAYS,
        "evidence_role": EVIDENCE_ROLE,
        # Deterministic lower bound: wall-clock ``now_ms`` is used to reject
        # open data above, but is intentionally excluded from the seal so an
        # exact retry produces the same immutable bundle.
        "minimum_completed_at_ms": int(rows[-1]["ts"]) + step,
        "raw_candle_count": len(rows),
        "daily_candle_count": len(daily),
        "first_candle_close_ms": int(rows[0]["ts"]) + step - 1,
        "last_candle_close_ms": int(rows[-1]["ts"]) + step - 1,
        "last_close": str(rows[-1]["close_text"]),
        "raw_dataset_sha256": raw_digest,
        "provenance": {
            "source_manifest_sha256": source_manifest_digest,
            "source_manifest_claims": claims,
        },
        "sample_count": len(samples),
        "first_sample_id": samples[0]["sample_id"],
        "last_sample_id": samples[-1]["sample_id"],
        "first_decision_ts": samples[0]["decision_ts"],
        "last_decision_ts": samples[-1]["decision_ts"],
        "last_label_available_ts": samples[-1]["label_available_ts"],
        "ordered_sample_sha256": _sha256(samples),
        "samples": samples,
    }
    return dict(payload, immutable_sha256=_sha256(payload))


def verify_seed_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    """Verify a seed and return an isolated canonical copy.

    This verifies the sealed bundle and its ordered sample chain.  The raw CSV
    is intentionally verified when the bundle is built; its SHA-256 remains in
    the manifest so callers can rebind it to the original bytes when desired.
    """

    if not isinstance(manifest, Mapping):
        raise ValueError("Historical seed manifest must be a mapping.")
    if set(manifest) != _TOP_LEVEL_FIELDS:
        raise ValueError("Historical seed manifest fields do not match the schema.")
    supplied = manifest.get("immutable_sha256")
    if not isinstance(supplied, str) or not HASH_PATTERN.fullmatch(supplied):
        raise ValueError("Historical seed immutable_sha256 is invalid.")
    payload = {key: copy.deepcopy(value) for key, value in manifest.items()
               if key != "immutable_sha256"}
    expected = _sha256(payload)
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("Historical seed immutable_sha256 does not match its payload.")

    fixed = {
        "schema": SCHEMA,
        "kind": KIND,
        "symbol": SYMBOL,
        "market": MARKET,
        "daily_horizon_ms": learner.DAY_MS,
        "momentum_lookback_days": MOMENTUM_LOOKBACK_DAYS,
        "evidence_role": EVIDENCE_ROLE,
    }
    for field, required in fixed.items():
        if payload.get(field) != required:
            raise ValueError(f"Historical seed {field} is invalid.")
    interval = payload.get("source_interval")
    if not isinstance(interval, str) or interval not in SUPPORTED_INTERVALS:
        raise ValueError("Historical seed source_interval is invalid.")
    for field in (
        "minimum_completed_at_ms",
        "raw_candle_count",
        "daily_candle_count",
        "first_candle_close_ms",
        "last_candle_close_ms",
        "sample_count",
        "first_decision_ts",
        "last_decision_ts",
        "last_label_available_ts",
    ):
        _strict_positive_int(payload.get(field), field)
    if int(payload["minimum_completed_at_ms"]) != int(
        payload["last_candle_close_ms"]
    ) + 1:
        raise ValueError("Historical seed completion boundary is invalid.")
    if int(payload["first_candle_close_ms"]) > int(payload["last_candle_close_ms"]):
        raise ValueError("Historical seed candle range is invalid.")
    _, canonical_last_close = _canonical_decimal(payload.get("last_close"), "last_close")
    if payload.get("last_close") != canonical_last_close:
        raise ValueError("Historical seed last_close is not canonical.")
    for field in ("raw_dataset_sha256", "ordered_sample_sha256"):
        if not isinstance(payload.get(field), str) or not HASH_PATTERN.fullmatch(
            str(payload.get(field))
        ):
            raise ValueError(f"Historical seed {field} is invalid.")
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "source_manifest_sha256",
        "source_manifest_claims",
    }:
        raise ValueError("Historical seed provenance is invalid.")
    source_hash = provenance.get("source_manifest_sha256")
    source_claims = provenance.get("source_manifest_claims")
    if (source_hash is None) != (source_claims is None):
        raise ValueError("Historical seed source provenance is incomplete.")
    if source_hash is not None and (
        not isinstance(source_hash, str) or not HASH_PATTERN.fullmatch(source_hash)
    ):
        raise ValueError("Historical seed source_manifest_sha256 is invalid.")
    if source_claims is not None and not isinstance(source_claims, Mapping):
        raise ValueError("Historical seed source_manifest_claims is invalid.")

    step = SUPPORTED_INTERVALS[str(interval)]
    raw_count = int(payload["raw_candle_count"])
    daily_count = int(payload["daily_candle_count"])
    first_close = int(payload["first_candle_close_ms"])
    last_close_ms = int(payload["last_candle_close_ms"])
    expected_raw_count = daily_count if interval == "1d" else daily_count * 96
    if raw_count != expected_raw_count or daily_count < MOMENTUM_LOOKBACK_DAYS + 2:
        raise ValueError("Historical seed candle counts are inconsistent.")
    if last_close_ms - first_close != (raw_count - 1) * step:
        raise ValueError("Historical seed candle range does not match its count.")
    if first_close % step != step - 1 or last_close_ms % step != step - 1:
        raise ValueError("Historical seed candle close timestamps are not aligned.")
    if interval == "15m" and (
        (first_close - step + 1) % learner.DAY_MS != 0
        or last_close_ms % learner.DAY_MS != learner.DAY_MS - 1
    ):
        raise ValueError("Historical seed 15m range does not contain complete UTC days.")

    if source_claims is not None:
        required_claims = {
            "market": MARKET,
            "symbol": SYMBOL,
            "interval": interval,
            "candles": raw_count,
            "first_ts": first_close - step + 1,
            "last_ts": last_close_ms - step + 1,
            "dataset_sha256": payload["raw_dataset_sha256"],
        }
        if not set(source_claims).issubset(SOURCE_CLAIM_FIELDS):
            raise ValueError("Historical seed source manifest claims are not canonical.")
        for field, required in required_claims.items():
            if source_claims.get(field) != required:
                raise ValueError(
                    f"Historical seed source manifest claim {field} is inconsistent."
                )

    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("Historical seed samples must be a non-empty sequence.")
    if payload.get("sample_count") != len(samples):
        raise ValueError("Historical seed sample_count does not match samples.")
    previous_label: int | None = None
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    canonical_samples: list[dict[str, object]] = []
    for raw_sample in samples:
        if not isinstance(raw_sample, Mapping):
            raise ValueError("Historical seed contains a non-mapping sample.")
        digest = raw_sample.get("immutable_sha256")
        if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
            raise ValueError("Historical seed sample hash is invalid.")
        sample_payload = {
            key: copy.deepcopy(value)
            for key, value in raw_sample.items()
            if key != "immutable_sha256"
        }
        resealed = learner.seal_sample(sample_payload)
        if dict(raw_sample) != resealed:
            raise ValueError("Historical seed sample is not canonical or its seal is invalid.")
        if (
            resealed["closed"] is not True
            or resealed["out_of_sample"] is not False
            or resealed["true_forward_after_freeze"] is not False
            or "freeze_id" in resealed
        ):
            raise ValueError("Historical seed samples must remain development-only.")
        sample_id = str(resealed["sample_id"])
        if not sample_id.startswith(f"hist:{payload['raw_dataset_sha256']}:"):
            raise ValueError("Historical seed sample_id is not bound to its raw dataset.")
        if sample_id in seen_ids or digest in seen_hashes:
            raise ValueError("Historical seed contains a duplicate sample.")
        seen_ids.add(sample_id)
        seen_hashes.add(digest)
        if previous_label is not None and resealed["decision_ts"] != previous_label:
            raise ValueError("Historical seed samples are not exactly contiguous.")
        previous_label = int(resealed["label_available_ts"])
        canonical_samples.append(resealed)
    if payload.get("ordered_sample_sha256") != _sha256(canonical_samples):
        raise ValueError("Historical seed ordered_sample_sha256 does not match samples.")
    first, last = canonical_samples[0], canonical_samples[-1]
    boundary_fields = {
        "first_sample_id": first["sample_id"],
        "last_sample_id": last["sample_id"],
        "first_decision_ts": first["decision_ts"],
        "last_decision_ts": last["decision_ts"],
        "last_label_available_ts": last["label_available_ts"],
    }
    for field, required in boundary_fields.items():
        if payload.get(field) != required:
            raise ValueError(f"Historical seed {field} does not match samples.")
    final_daily_close = int(payload["last_candle_close_ms"])
    if interval == "15m":
        final_daily_close = (
            final_daily_close // learner.DAY_MS * learner.DAY_MS
            + learner.DAY_MS - 1
        )
    if last["label_available_ts"] != final_daily_close:
        raise ValueError("Historical seed sample tail does not match the final daily close.")
    if len(canonical_samples) > daily_count - MOMENTUM_LOOKBACK_DAYS - 1:
        raise ValueError("Historical seed contains more samples than its daily candles allow.")
    payload["samples"] = canonical_samples
    payload["provenance"] = {
        "source_manifest_sha256": source_hash,
        "source_manifest_claims": (
            copy.deepcopy(dict(source_claims)) if source_claims is not None else None
        ),
    }
    return dict(payload, immutable_sha256=supplied)
