"""Verified Binance Vision derivatives metrics and causal offline challengers."""

from __future__ import annotations

from bisect import bisect_right
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO, StringIO
import json
import math
from pathlib import Path, PurePosixPath
import sqlite3
import statistics
from typing import Mapping, Sequence
from zipfile import BadZipFile, ZipFile

import binance_archive


BASE_URL = "https://data.binance.vision"
STEP_MS = 15 * 60 * 1000
FIVE_MINUTES_MS = 5 * 60 * 1000
ROOT = Path(__file__).resolve().parent
DEFAULT_METRICS_DATA = ROOT / "data/binance-um-btcusdt-metrics-5m.csv"
DEFAULT_FUNDING_DATA = ROOT / "data/binance-um-btcusdt-funding.csv"
DEFAULT_MODEL = ROOT / "state/remora-binance-derivatives-offline.json"
DEFAULT_REPORT = ROOT / "reports/remora-binance-derivatives-training.json"
DEFAULT_EXIT_REPORT = ROOT / "reports/remora-binance-exit-policy-search.json"
METRIC_FIELDS = [
    "ts", "symbol", "open_interest", "open_interest_value",
    "top_account_ratio", "top_position_ratio", "global_account_ratio",
    "taker_buy_sell_ratio",
]
FUNDING_FIELDS = ["ts", "symbol", "funding_interval_hours", "funding_rate"]
DERIVATIVE_FEATURE_NAMES = [
    "funding_rate", "funding_zscore_30",
    "oi_log_change_1h", "oi_value_log_change_1h",
    "top_account_log_ratio", "top_position_log_ratio",
    "global_account_log_ratio", "taker_log_ratio",
]
REGIME_FEATURE_NAMES = [
    "return_4h", "realized_vol_ratio_4h_24h", "volume_zscore_24h",
]


def _symbol(value: str) -> str:
    value = value.upper()
    if not value.isalnum() or len(value) > 30:
        raise ValueError("Geçersiz Binance sembolü.")
    return value


def _date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Tarih YYYY-MM-DD biçiminde olmalı.") from exc


def dates_between(start: str, end: str) -> list[str]:
    current, finish = _date(start), _date(end)
    if current > finish:
        raise ValueError("Başlangıç tarihi bitiş tarihinden sonra olamaz.")
    days = (finish - current).days + 1
    if days > 366:
        raise ValueError("Tek metrics indirmesinde en fazla 366 gün kullanılabilir.")
    return [(current + timedelta(days=index)).strftime("%Y-%m-%d")
            for index in range(days)]


def metrics_url(symbol: str, day: str) -> str:
    symbol = _symbol(symbol)
    _date(day)
    name = f"{symbol}-metrics-{day}.zip"
    return f"{BASE_URL}/data/futures/um/daily/metrics/{symbol}/{name}"


def funding_url(symbol: str, month: str) -> str:
    symbol = _symbol(symbol)
    binance_archive.months_between(month, month)
    name = f"{symbol}-fundingRate-{month}.zip"
    return f"{BASE_URL}/data/futures/um/monthly/fundingRate/{symbol}/{name}"


def _verified_csv(url: str) -> tuple[str, str]:
    filename = url.rsplit("/", 1)[-1]
    blob = binance_archive._download(url)
    checksum = binance_archive._download(url + ".CHECKSUM")
    expected = binance_archive._expected_checksum(checksum, filename)
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ValueError(f"Binance SHA-256 doğrulaması başarısız: {filename}")
    try:
        with ZipFile(BytesIO(blob)) as archive:
            members = [name for name in archive.namelist() if not name.endswith("/")]
            if len(members) != 1 or not members[0].lower().endswith(".csv"):
                raise ValueError("Binance ZIP tam olarak bir CSV içermeli.")
            member = PurePosixPath(members[0])
            if member.is_absolute() or ".." in member.parts:
                raise ValueError("Binance ZIP güvenli olmayan dosya yolu içeriyor.")
            content = archive.read(members[0]).decode("utf-8-sig")
    except (BadZipFile, UnicodeDecodeError) as exc:
        raise ValueError("Binance ZIP/CSV içeriği geçersiz.") from exc
    return content, actual


def _finite_positive(value: str, field: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"Binance {field} pozitif ve sonlu olmalı.")
    return parsed


def _parse_metrics(content: str, symbol: str) -> list[dict[str, object]]:
    symbol = _symbol(symbol)
    reader = csv.DictReader(StringIO(content))
    required = {
        "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
        "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
    }
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("Binance metrics sütunları eksik.")
    rows = []
    for index, raw in enumerate(reader, start=2):
        if raw["symbol"].upper() != symbol:
            raise ValueError(f"Binance metrics sembolü uyuşmuyor: satır {index}")
        try:
            ts = int(datetime.strptime(raw["create_time"], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Binance metrics zamanı geçersiz: satır {index}") from exc
        if ts % FIVE_MINUTES_MS:
            raise ValueError("Binance metrics zamanı 5 dakikaya hizalı değil.")
        rows.append({
            "ts": ts, "symbol": symbol,
            "open_interest": _finite_positive(raw["sum_open_interest"], "open interest"),
            "open_interest_value": _finite_positive(
                raw["sum_open_interest_value"], "open interest value"),
            "top_account_ratio": _finite_positive(
                raw["count_toptrader_long_short_ratio"], "top account ratio"),
            "top_position_ratio": _finite_positive(
                raw["sum_toptrader_long_short_ratio"], "top position ratio"),
            "global_account_ratio": _finite_positive(
                raw["count_long_short_ratio"], "global account ratio"),
            "taker_buy_sell_ratio": _finite_positive(
                raw["sum_taker_long_short_vol_ratio"], "taker ratio"),
        })
    return _validate_series(rows, FIVE_MINUTES_MS, "metrics")


def _parse_funding(content: str, symbol: str) -> list[dict[str, object]]:
    symbol = _symbol(symbol)
    reader = csv.DictReader(StringIO(content))
    required = {"calc_time", "funding_interval_hours", "last_funding_rate"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("Binance funding sütunları eksik.")
    rows = []
    for index, raw in enumerate(reader, start=2):
        try:
            ts = int(raw["calc_time"])
            interval = float(raw["funding_interval_hours"])
            rate = float(raw["last_funding_rate"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Binance funding satırı geçersiz: {index}") from exc
        if not all(math.isfinite(value) for value in (interval, rate)) or interval <= 0:
            raise ValueError(f"Binance funding değeri geçersiz: satır {index}")
        # Some archive settlements are recorded one millisecond after the boundary.
        rows.append({"ts": ts, "symbol": symbol,
                     "funding_interval_hours": interval, "funding_rate": rate})
    return _validate_series(rows, None, "funding")


def _validate_series(rows: Sequence[Mapping[str, object]], step_ms: int | None,
                     label: str) -> list[dict[str, object]]:
    if not rows:
        raise ValueError(f"Binance {label} verisi boş.")
    ordered = sorted((dict(row) for row in rows), key=lambda row: int(row["ts"]))
    for index, row in enumerate(ordered):
        ts = int(row["ts"])
        if ts <= 0 or (index and ts == int(ordered[index - 1]["ts"])):
            raise ValueError(f"Binance {label} zamanı geçersiz/tekrar.")
        if step_ms is not None and index:
            gap = ts - int(ordered[index - 1]["ts"])
            if gap != step_ms:
                raise ValueError(f"Binance {label} verisinde boşluk var.")
    return ordered


def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def download_metrics(symbol: str, start: str, end: str,
                     out: Path = DEFAULT_METRICS_DATA, workers: int = 6) -> dict[str, object]:
    symbol = _symbol(symbol)
    days = dates_between(start, end)
    if not 1 <= workers <= 12:
        raise ValueError("Metrics worker sayısı 1-12 arasında olmalı.")
    fetched: dict[str, tuple[list[dict[str, object]], str]] = {}

    def fetch(day: str):
        content, digest = _verified_csv(metrics_url(symbol, day))
        return _parse_metrics(content, symbol), digest

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, day): day for day in days}
        for future in as_completed(futures):
            day = futures[future]
            try:
                fetched[day] = future.result()
            except Exception as exc:
                raise ValueError(f"Binance metrics günü indirilemedi: {day}: {exc}") from exc
    rows = [row for day in days for row in fetched[day][0]]
    rows = _validate_series(rows, FIVE_MINUTES_MS, "metrics")
    _write_csv(out, METRIC_FIELDS, rows)
    manifest = {
        "source": BASE_URL, "dataset": "futures/um/daily/metrics",
        "symbol": symbol, "start_date": start, "end_date": end,
        "rows": len(rows), "first_ts": int(rows[0]["ts"]),
        "last_ts": int(rows[-1]["ts"]),
        "files": [{"day": day, "sha256": fetched[day][1],
                   "rows": len(fetched[day][0])} for day in days],
        "dataset_sha256": hashlib.sha256(Path(out).read_bytes()).hexdigest(),
    }
    Path(out).with_suffix(Path(out).suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def download_funding(symbol: str, start_month: str, end_month: str,
                     out: Path = DEFAULT_FUNDING_DATA) -> dict[str, object]:
    symbol = _symbol(symbol)
    months = binance_archive.months_between(start_month, end_month)
    rows, files = [], []
    for month in months:
        content, digest = _verified_csv(funding_url(symbol, month))
        parsed = _parse_funding(content, symbol)
        rows.extend(parsed)
        files.append({"month": month, "sha256": digest, "rows": len(parsed)})
    rows = _validate_series(rows, None, "funding")
    _write_csv(out, FUNDING_FIELDS, rows)
    manifest = {
        "source": BASE_URL, "dataset": "futures/um/monthly/fundingRate",
        "symbol": symbol, "start_month": start_month, "end_month": end_month,
        "rows": len(rows), "first_ts": int(rows[0]["ts"]),
        "last_ts": int(rows[-1]["ts"]), "files": files,
        "dataset_sha256": hashlib.sha256(Path(out).read_bytes()).hexdigest(),
    }
    Path(out).with_suffix(Path(out).suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _read_csv(path: Path, fields: Sequence[str], label: str) -> list[dict[str, object]]:
    with Path(path).open(encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not set(fields).issubset(reader.fieldnames):
            raise ValueError(f"Binance {label} CSV sütunları eksik.")
        rows = list(reader)
    numeric = [field for field in fields if field not in {"symbol"}]
    parsed = [{**row, **{field: float(row[field]) for field in numeric}}
              for row in rows]
    for row in parsed:
        row["ts"] = int(row["ts"])
        if row.get("symbol") != "BTCUSDT":
            raise ValueError(f"Binance {label} CSV sembolü BTCUSDT değil.")
        if not all(math.isfinite(float(row[field])) for field in numeric):
            raise ValueError(f"Binance {label} CSV sonlu olmayan değer içeriyor.")
        if label == "metrics" and any(float(row[field]) <= 0 for field in numeric[1:]):
            raise ValueError("Binance metrics CSV pozitif olmayan değer içeriyor.")
        if label == "funding" and float(row["funding_interval_hours"]) <= 0:
            raise ValueError("Binance funding aralığı pozitif değil.")
    return _validate_series(parsed, FIVE_MINUTES_MS if label == "metrics" else None, label)


def _asof(rows: Sequence[Mapping[str, object]], timestamps: Sequence[int], ts: int,
          max_age_ms: int | None = None) -> Mapping[str, object] | None:
    index = bisect_right(timestamps, ts) - 1
    if index < 0:
        return None
    row = rows[index]
    if max_age_ms is not None and ts - int(row["ts"]) > max_age_ms:
        return None
    return row


def derivative_features(metrics: Sequence[Mapping[str, object]],
                        funding: Sequence[Mapping[str, object]],
                        decision_timestamps: Sequence[int]) -> dict[int, tuple[float, ...]]:
    metric_ts = [int(row["ts"]) for row in metrics]
    funding_ts = [int(row["ts"]) for row in funding]
    result = {}
    for ts in decision_timestamps:
        current = _asof(metrics, metric_ts, ts, max_age_ms=FIVE_MINUTES_MS)
        previous = _asof(metrics, metric_ts, ts - 4 * STEP_MS, max_age_ms=FIVE_MINUTES_MS)
        latest_funding = _asof(funding, funding_ts, ts)
        if current is None or previous is None or latest_funding is None:
            continue
        funding_index = bisect_right(funding_ts, ts)
        history = [float(row["funding_rate"])
                   for row in funding[max(0, funding_index - 30):funding_index]]
        mean = statistics.mean(history)
        scale = statistics.pstdev(history)
        funding_rate = float(latest_funding["funding_rate"])
        funding_z = (funding_rate - mean) / scale if scale > 1e-12 else 0.0
        result[ts] = (
            funding_rate, funding_z,
            math.log(float(current["open_interest"]) /
                     float(previous["open_interest"])),
            math.log(float(current["open_interest_value"]) /
                     float(previous["open_interest_value"])),
            math.log(float(current["top_account_ratio"])),
            math.log(float(current["top_position_ratio"])),
            math.log(float(current["global_account_ratio"])),
            math.log(float(current["taker_buy_sell_ratio"])),
        )
    return result


def regime_features(rows: Sequence[Mapping[str, object]]) -> dict[int, tuple[float, ...]]:
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row["volume"]) for row in rows]
    returns = [0.0] + [math.log(closes[i] / closes[i - 1])
                       for i in range(1, len(closes))]
    result = {}
    for index in range(96, len(rows)):
        short = returns[index - 15:index + 1]
        long = returns[index - 95:index + 1]
        short_vol, long_vol = statistics.pstdev(short), statistics.pstdev(long)
        volume_window = volumes[index - 95:index + 1]
        volume_mean, volume_scale = statistics.mean(volume_window), statistics.pstdev(volume_window)
        result[int(rows[index]["ts"])] = (
            closes[index] / closes[index - 16] - 1,
            short_vol / long_vol if long_vol > 1e-12 else 1.0,
            (volumes[index] - volume_mean) / volume_scale if volume_scale > 1e-12 else 0.0,
        )
    return result


def _return_model_evaluation(samples: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Evaluate one fixed Ridge return model on the same purged 70/30 split."""
    import numpy as np
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    ordered = sorted(samples, key=lambda sample: (sample["exit_ts"], sample["entry_ts"]))
    split = int(len(ordered) * 0.7)
    train = ordered[:split]
    boundary = max(sample["exit_ts"] for sample in train)
    valid = [sample for sample in ordered[split:] if sample["entry_ts"] > boundary]
    if len(valid) < 30:
        raise ValueError("Ridge doğrulaması için yeterli purged örnek yok.")
    model = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    model.fit(np.asarray([sample["x"] for sample in train], dtype=float),
              np.asarray([sample["net_return"] for sample in train], dtype=float))
    predicted = model.predict(np.asarray([sample["x"] for sample in valid], dtype=float))
    threshold = 0.0015  # 0.10% extra stress + 0.05% required remaining edge.
    accepted_returns = [
        float(sample["net_return"]) for sample, estimate in zip(valid, predicted)
        if float(estimate) >= threshold
    ]
    stressed = [value - 0.001 for value in accepted_returns]
    half = len(valid) // 2
    halves = []
    for start, end in ((0, half), (half, len(valid))):
        halves.append(sum(
            float(valid[index]["net_return"]) - 0.001
            for index in range(start, end) if float(predicted[index]) >= threshold
        ))
    mean = statistics.mean(stressed) if stressed else 0.0
    buffer = statistics.pstdev(stressed) / math.sqrt(len(stressed)) if stressed else 0.0
    base = sum(float(sample["net_return"]) for sample in valid)
    passes = bool(
        len(accepted_returns) >= 10 and len(valid) - len(accepted_returns) >= 5
        and sum(stressed) > max(0.0, base) and mean > buffer
        and all(value > 0 for value in halves)
    )
    return {
        "family": "ridge_expected_net_return", "alpha": 10.0,
        "train_count": len(train), "validation_count": len(valid),
        "prediction_threshold": threshold, "accepted": len(accepted_returns),
        "filtered_sum_trade_returns": sum(accepted_returns),
        "stressed_sum_trade_returns": sum(stressed),
        "stressed_mean_trade_return": mean, "stability_buffer": buffer,
        "chronological_half_net_returns": halves,
        "baseline_sum_trade_returns": base,
        "prediction_min": float(min(predicted)), "prediction_max": float(max(predicted)),
        "quality_pass": passes,
        "note": "Research-only fixed Ridge; no threshold or alpha selection on validation.",
    }


def train_derivatives_offline(
    spot_path: Path,
    futures_path: Path,
    metrics_path: Path,
    funding_path: Path,
    model_path: Path = DEFAULT_MODEL,
    report_path: Path = DEFAULT_REPORT,
    samples: int = 400,
) -> dict[str, object]:
    """Run matched-sample ablations; never alter or deploy to the live paper DB."""
    import learning
    import paper_v3

    spot = binance_archive.read_dataset(spot_path, "spot", "BTCUSDT")
    futures = binance_archive.read_dataset(futures_path, "um", "BTCUSDT")
    metrics = _read_csv(metrics_path, METRIC_FIELDS, "metrics")
    funding = _read_csv(funding_path, FUNDING_FIELDS, "funding")
    start = max(int(spot[0]["ts"]), int(futures[0]["ts"]), int(metrics[0]["ts"]))
    end = min(int(spot[-1]["ts"]), int(futures[-1]["ts"]), int(metrics[-1]["ts"]))
    spot = [row for row in spot if start <= int(row["ts"]) <= end]
    futures = [row for row in futures if start <= int(row["ts"]) <= end]
    if [int(row["ts"]) for row in spot] != [int(row["ts"]) for row in futures]:
        raise ValueError("Türev eğitimi Spot/USD-M ortak 15m zamanlarını bulamadı.")
    basis = binance_archive._basis_features(spot, futures)
    derivative = derivative_features(metrics, funding, [int(row["ts"]) for row in futures])
    regimes = regime_features(futures)
    source_hashes = {
        "spot": hashlib.sha256(Path(spot_path).read_bytes()).hexdigest(),
        "futures": hashlib.sha256(Path(futures_path).read_bytes()).hexdigest(),
        "metrics": hashlib.sha256(Path(metrics_path).read_bytes()).hexdigest(),
        "funding": hashlib.sha256(Path(funding_path).read_bytes()).hexdigest(),
    }
    cache_path = Path(report_path).with_name(
        Path(report_path).stem + f"-samples-{samples}.json")
    cache_key = {"schema": 1, "samples": samples, "first_ts": start,
                 "last_ts": end, "data_sha256": source_hashes}
    matched = []
    cache_used = False
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if all(cached.get(key) == value for key, value in cache_key.items()):
            matched = [
                (item["sample"], tuple(item["basis"]),
                 tuple(item["derivative"]), tuple(item["regime"]))
                for item in cached.get("matched", [])
            ]
            cache_used = True
    if not matched:
        db = sqlite3.connect(":memory:")
        try:
            paper_v3.seed_historical_samples(db, futures, samples)
            raw = [json.loads(row[0]) for row in db.execute(
                "SELECT detail FROM learning_samples WHERE strategy=? ORDER BY exit_ts",
                (learning.REMORA_MODEL,),
            )]
        finally:
            db.close()
        for sample in raw:
            ts = int(sample.get("metadata", {}).get("decision_ts", -1))
            if ts in basis and ts in derivative and ts in regimes:
                matched.append((sample, basis[ts], derivative[ts], regimes[ts]))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            **cache_key,
            "matched": [
                {"sample": sample, "basis": basis_x,
                 "derivative": derivative_x, "regime": regime_x}
                for sample, basis_x, derivative_x, regime_x in matched
            ],
        }), encoding="utf-8")
    if len(matched) < 200:
        raise ValueError(f"Türev özellikleriyle yalnız {len(matched)} ortak örnek bulundu.")

    variants = {
        "control": ([], []),
        "basis": (["futures_spot_basis", "basis_zscore_24h", "basis_change_1h"], [1]),
        "funding": (DERIVATIVE_FEATURE_NAMES[:2], [2]),
        "metrics": (DERIVATIVE_FEATURE_NAMES[2:], [3]),
        "all_derivatives": (
            ["futures_spot_basis", "basis_zscore_24h", "basis_change_1h",
             *DERIVATIVE_FEATURE_NAMES], [1, 2, 3]),
        "all_plus_regime": (
            ["futures_spot_basis", "basis_zscore_24h", "basis_change_1h",
             *DERIVATIVE_FEATURE_NAMES, *REGIME_FEATURE_NAMES], [1, 2, 3, 4]),
    }
    evaluations = {}
    return_evaluations = {}
    for name, (extra_names, groups) in variants.items():
        augmented = []
        for sample, basis_x, derivative_x, regime_x in matched:
            values = []
            if 1 in groups:
                values.extend(basis_x)
            if 2 in groups:
                values.extend(derivative_x[:2])
            if 3 in groups:
                values.extend(derivative_x[2:])
            if 4 in groups:
                values.extend(regime_x)
            augmented.append({**sample, "x": [*sample["x"], *values]})
        names = [*learning.REMORA_FEATURES, *extra_names]
        state = learning.train_candidate(augmented, feature_names=names)
        evaluations[name] = {"features": names, "model_state": state}
        return_evaluations[name] = _return_model_evaluation(augmented)

    control_brier = evaluations["control"]["model_state"].get("validation", {}).get("brier")
    ranked = sorted(
        evaluations,
        key=lambda name: evaluations[name]["model_state"].get("validation", {}).get("brier", math.inf),
    )
    best_name = ranked[0]
    best = evaluations[best_name]["model_state"]
    best_validation = best.get("validation", {})
    research_quality_candidate = bool(
        best_name != "control" and best_validation.get("quality_pass")
        and best_validation.get("brier", math.inf) < (control_brier or math.inf)
    )
    # Offline model selection has seen this validation block and has no registered
    # forward executions. It must never enter the live shadow/paper promotion path.
    shadow_candidate = False
    best_return_name = max(
        return_evaluations,
        key=lambda name: return_evaluations[name]["stressed_sum_trade_returns"],
    )
    artifact = {
        "kind": "quant_remora_binance_derivatives_ablation",
        "period": {"first_ts": start, "last_ts": end},
        "candles": len(futures), "matched_samples": len(matched),
        "data_sha256": source_hashes,
        "sample_cache": str(cache_path.resolve()), "sample_cache_used": cache_used,
        "join_policy": "latest observation at or before decision timestamp; metrics max age 5m",
        "evaluations": evaluations, "best_brier_variant": best_name,
        "research_quality_candidate": research_quality_candidate,
        "return_evaluations": return_evaluations,
        "best_return_variant": best_return_name,
        "return_candidate_quality_pass": bool(
            return_evaluations[best_return_name]["quality_pass"]),
        "selected_for_forward_shadow": shadow_candidate,
        "deployed": False, "real_orders_enabled": False,
    }
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(model_path).write_text(json.dumps({
        "kind": artifact["kind"], "variant": best_name,
        "features": evaluations[best_name]["features"], "model": best.get("model"),
        "model_version": best.get("version"),
        "selected_for_forward_shadow": shadow_candidate, "deployed": False,
    }, indent=2), encoding="utf-8")
    Path(report_path).write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return artifact


def _policy_return(sample: Mapping[str, object], rows: Sequence[Mapping[str, object]],
                   index_by_ts: Mapping[int, int], stop_atr: float,
                   target_atr: float, horizon: int) -> float:
    import paper_v3

    decision_ts = int(sample["metadata"]["decision_ts"])
    side = str(sample["metadata"]["side"])
    index = index_by_ts[decision_ts]
    entry_index = index + 1
    if index + horizon >= len(rows):
        raise ValueError("Çıkış politikası için gelecek mum eksik.")
    entry = float(rows[entry_index]["open"])
    atr = float(sample["x"][1]) * float(rows[index]["close"])
    stop = entry - stop_atr * atr if side == "long" else entry + stop_atr * atr
    target = entry + target_atr * atr if side == "long" else entry - target_atr * atr
    exit_reference = float(rows[index + horizon]["close"])
    for cursor in range(entry_index, index + horizon + 1):
        row = rows[cursor]
        opened = float(row["open"])
        if side == "long" and float(row["low"]) <= stop:
            exit_reference = min(stop, opened)
            break
        if side == "long" and float(row["high"]) >= target:
            exit_reference = max(target, opened)
            break
        if side == "short" and float(row["high"]) >= stop:
            exit_reference = max(stop, opened)
            break
        if side == "short" and float(row["low"]) <= target:
            exit_reference = min(target, opened)
            break
    return paper_v3._shadow_net_return(side, entry, exit_reference)


def _policy_metrics(values: Sequence[float]) -> dict[str, object]:
    stressed = [float(value) - 0.001 for value in values]
    wins = sum(max(value, 0.0) for value in stressed)
    losses = -sum(min(value, 0.0) for value in stressed)
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in stressed:
        equity *= max(0.0, 1 + value)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
    return {
        "trades": len(values), "sum_net_return": sum(values),
        "stressed_sum_return": sum(stressed),
        "stressed_mean_return": statistics.mean(stressed) if stressed else 0.0,
        "win_rate": sum(value > 0 for value in stressed) / len(stressed) if stressed else None,
        "profit_factor": wins / losses if losses else None,
        "compounded_stressed_return": equity - 1, "max_drawdown": max_drawdown,
    }


def search_exit_policies(
    futures_path: Path,
    sample_cache_path: Path,
    report_path: Path = DEFAULT_EXIT_REPORT,
) -> dict[str, object]:
    """Search exits on development/selection, then open one untouched holdout."""
    rows = binance_archive.read_dataset(futures_path, "um", "BTCUSDT")
    cache = json.loads(Path(sample_cache_path).read_text(encoding="utf-8"))
    events = [item["sample"] for item in cache.get("matched", [])]
    if len(events) < 400:
        raise ValueError("Çıkış politikası araması için en az 400 önbellek olayı gerekir.")
    events.sort(key=lambda sample: int(sample["metadata"]["decision_ts"]))
    index_by_ts = {int(row["ts"]): index for index, row in enumerate(rows)}
    if any(int(sample["metadata"]["decision_ts"]) not in index_by_ts for sample in events):
        raise ValueError("Önbellek olayı futures veri kümesiyle uyuşmuyor.")

    development_end = len(events) // 2
    selection_end = len(events) * 3 // 4
    max_horizon = 32
    development = events[:development_end]
    development_boundary = int(development[-1]["metadata"]["decision_ts"])
    selection = [sample for sample in events[development_end:selection_end]
                 if int(sample["metadata"]["decision_ts"])
                 > development_boundary + max_horizon * STEP_MS]
    selection_boundary = int(events[selection_end - 1]["metadata"]["decision_ts"])
    holdout = [sample for sample in events[selection_end:]
               if int(sample["metadata"]["decision_ts"])
               > selection_boundary + max_horizon * STEP_MS]
    if min(len(development), len(selection), len(holdout)) < 80:
        raise ValueError("Embargo sonrası çıkış politikası segmentleri yetersiz.")

    candidates = []
    stops = (0.75, 1.0, 1.25, 1.5, 2.0)
    targets = (1.0, 1.5, 2.0, 2.5, 3.2, 4.0)
    horizons = (4, 8, 12, 16, 24, 32)
    for stop in stops:
        for target in targets:
            if target / stop < 1.5:
                continue
            for horizon in horizons:
                def evaluate(segment):
                    return _policy_metrics([
                        _policy_return(sample, rows, index_by_ts, stop, target, horizon)
                        for sample in segment
                    ])
                dev_metrics = evaluate(development)
                selection_metrics = evaluate(selection)
                ratios = [dev_metrics["profit_factor"], selection_metrics["profit_factor"]]
                passes = bool(
                    dev_metrics["stressed_sum_return"] > 0
                    and selection_metrics["stressed_sum_return"] > 0
                    and all(ratio is not None and ratio >= 1.2 for ratio in ratios)
                )
                candidates.append({
                    "stop_atr": stop, "target_atr": target, "horizon_bars": horizon,
                    "development": dev_metrics, "selection": selection_metrics,
                    "selection_gate_pass": passes,
                    "robust_score": min(dev_metrics["stressed_mean_return"],
                                        selection_metrics["stressed_mean_return"]),
                })
    eligible = [candidate for candidate in candidates if candidate["selection_gate_pass"]]
    selected = max(eligible, key=lambda candidate: candidate["robust_score"]) if eligible else None
    holdout_metrics = None
    holdout_pass = False
    if selected is not None:
        holdout_metrics = _policy_metrics([
            _policy_return(sample, rows, index_by_ts, selected["stop_atr"],
                           selected["target_atr"], selected["horizon_bars"])
            for sample in holdout
        ])
        holdout_pass = bool(
            holdout_metrics["stressed_sum_return"] > 0
            and holdout_metrics["profit_factor"] is not None
            and holdout_metrics["profit_factor"] >= 1.2
        )
    current = {
        "stop_atr": 1.5, "target_atr": 3.2, "horizon_bars": 8,
        "development": _policy_metrics([
            _policy_return(sample, rows, index_by_ts, 1.5, 3.2, 8)
            for sample in development]),
        "selection": _policy_metrics([
            _policy_return(sample, rows, index_by_ts, 1.5, 3.2, 8)
            for sample in selection]),
        "holdout": _policy_metrics([
            _policy_return(sample, rows, index_by_ts, 1.5, 3.2, 8)
            for sample in holdout]),
    }
    report = {
        "kind": "quant_remora_exit_policy_search",
        "events": len(events), "grid_candidates": len(candidates),
        "segments": {"development": len(development), "selection": len(selection),
                     "holdout": len(holdout), "embargo_bars": max_horizon},
        "cost_policy": "labels include fee/slippage; additional 0.10% stress",
        "selection_rules": "positive stressed return and PF>=1.2 in development and selection",
        "current_policy": current, "eligible_before_holdout": len(eligible),
        "selected_policy": selected, "holdout": holdout_metrics,
        "holdout_pass": holdout_pass,
        "deployable": False, "real_orders_enabled": False,
        "note": "Research grid; even a holdout pass still requires independent forward paper evidence.",
    }
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


__all__ = [
    "DEFAULT_METRICS_DATA", "DEFAULT_FUNDING_DATA", "DEFAULT_MODEL", "DEFAULT_REPORT",
    "DEFAULT_EXIT_REPORT",
    "DERIVATIVE_FEATURE_NAMES", "REGIME_FEATURE_NAMES", "dates_between", "metrics_url",
    "funding_url", "download_metrics", "download_funding", "derivative_features",
    "regime_features", "train_derivatives_offline", "search_exit_policies",
]
