"""Verified Binance Vision kline archive ingestion and offline Remora training."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
from io import BytesIO, StringIO
import json
import math
from pathlib import Path, PurePosixPath
import sqlite3
import time
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from strategies import BAR_MS


BASE_URL = "https://data.binance.vision"
INTERVAL = "15m"
STEP_MS = 15 * 60 * 1000
MARKETS = {"um": "futures/um", "spot": "spot"}
FIELDS = ["ts", "open", "high", "low", "close", "volume", "exchange", "symbol"]
DEFAULT_DATA = Path(__file__).resolve().parent / "data/binance-um-btcusdt-15m.csv"
DEFAULT_MODEL = Path(__file__).resolve().parent / "state/remora-binance-um-offline.json"
DEFAULT_REPORT = Path(__file__).resolve().parent / "reports/remora-binance-um-training.json"
DEFAULT_SPOT_DATA = Path(__file__).resolve().parent / "data/binance-spot-btcusdt-15m.csv"
DEFAULT_SPOT_MODEL = Path(__file__).resolve().parent / "state/remora-binance-spot-offline.json"
DEFAULT_SPOT_REPORT = Path(__file__).resolve().parent / "reports/remora-binance-spot-training.json"
DEFAULT_BLEND_MODEL = Path(__file__).resolve().parent / "state/remora-binance-basis-offline.json"
DEFAULT_BLEND_REPORT = Path(__file__).resolve().parent / "reports/remora-binance-basis-training.json"
SPOT_REST_BASES = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api-gcp.binance.com",
)


def _month(value: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError("Ay YYYY-MM biçiminde olmalı.") from exc
    return parsed.year, parsed.month


def months_between(start: str, end: str) -> list[str]:
    year, month = _month(start)
    end_year, end_month = _month(end)
    if (year, month) > (end_year, end_month):
        raise ValueError("Başlangıç ayı bitiş ayından sonra olamaz.")
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    if len(result) > 120:
        raise ValueError("Tek indirmede en fazla 120 ay kullanılabilir.")
    return result


def archive_url(market: str, symbol: str, month: str) -> str:
    if market not in MARKETS:
        raise ValueError("Binance market yalnız 'um' veya 'spot' olabilir.")
    symbol = symbol.upper()
    if not symbol.isalnum() or len(symbol) > 30:
        raise ValueError("Geçersiz Binance sembolü.")
    _month(month)
    filename = f"{symbol}-{INTERVAL}-{month}.zip"
    return (
        f"{BASE_URL}/data/{MARKETS[market]}/monthly/klines/"
        f"{symbol}/{INTERVAL}/{filename}"
    )


def _download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "quant-remora-binance-archive/1.0"})
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except URLError as exc:
        if "WRONG_VERSION_NUMBER" in str(exc):
            raise ValueError(
                "Ağ, data.binance.vision TLS bağlantısını engelliyor. "
                "Binance Vision erişimine izin verilen bir ağda tekrar deneyin."
            ) from exc
        raise ValueError(f"Binance Vision indirilemedi: {exc}") from exc


def _date_ms(value: str) -> int:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Tarih YYYY-MM-DD biçiminde olmalı.") from exc
    return int(parsed.timestamp() * 1000)


def _rest_json(path: str, params: dict[str, object], bases: Sequence[str] = SPOT_REST_BASES):
    errors = []
    for base in bases:
        url = base + path + "?" + urlencode(params)
        request = Request(url, headers={"User-Agent": "quant-remora-binance-rest/1.0"})
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if isinstance(payload, dict) and "code" in payload:
                raise ValueError(f"Binance REST hatası: {payload}")
            return payload, base
        except HTTPError as exc:
            if exc.code in {418, 429}:
                retry = exc.headers.get("Retry-After", "belirtilmedi")
                raise ValueError(
                    f"Binance REST rate limit: HTTP {exc.code}; Retry-After={retry}."
                ) from exc
            errors.append(f"{base}: HTTP {exc.code}")
        except (URLError, OSError) as exc:
            errors.append(f"{base}: {exc}")
    if any("WRONG_VERSION_NUMBER" in error for error in errors):
        raise ValueError(
            "Ağ, Binance REST TLS bağlantılarını engelliyor. Binance erişimine "
            "izin verilen bir ağda tekrar deneyin."
        )
    raise ValueError("Binance REST uçlarının hiçbiri çalışmadı: " + " | ".join(errors))


def _rest_rows(payload: object, symbol: str) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        raise ValueError("Binance REST kline yanıtı liste değil.")
    result = []
    for index, values in enumerate(payload, start=1):
        if not isinstance(values, list) or len(values) < 7:
            raise ValueError(f"Binance REST kline {index} biçimi geçersiz.")
        row = {
            "ts": _timestamp_ms(str(values[0])),
            "open": float(values[1]), "high": float(values[2]),
            "low": float(values[3]), "close": float(values[4]),
            "volume": float(values[5]),
            "exchange": "binance_spot", "symbol": symbol,
        }
        close_ts = int(values[6])
        if close_ts != int(row["ts"]) + STEP_MS - 1:
            raise ValueError("Binance REST kline kapanış zamanı uyuşmuyor.")
        _validate_price_row(row, index)
        result.append(row)
    return result


def _expected_checksum(checksum: bytes, filename: str) -> str:
    try:
        words = checksum.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise ValueError("Binance CHECKSUM dosyası ASCII değil.") from exc
    if not words or len(words[0]) != 64 or any(c not in "0123456789abcdefABCDEF" for c in words[0]):
        raise ValueError("Binance CHECKSUM biçimi geçersiz.")
    if len(words) > 1 and words[-1].lstrip("*") != filename:
        raise ValueError("Binance CHECKSUM dosya adı uyuşmuyor.")
    return words[0].lower()


def _timestamp_ms(value: str) -> int:
    raw = int(value)
    # Official spot archives use microseconds from 2025 onward. Futures archives
    # currently use milliseconds; magnitude detection safely supports both.
    if raw >= 100_000_000_000_000:
        if raw % 1000:
            raise ValueError("Kline açılış mikrosaniyesi tam milisaniyeye hizalı değil.")
        raw //= 1000
    if raw <= 0 or raw % STEP_MS:
        raise ValueError("Binance kline zamanı 15 dakikaya hizalı değil.")
    return raw


def _parse_zip(blob: bytes, filename: str, market: str, symbol: str) -> list[dict[str, object]]:
    try:
        with ZipFile(BytesIO(blob)) as archive:
            members = [name for name in archive.namelist() if not name.endswith("/")]
            if len(members) != 1 or not members[0].lower().endswith(".csv"):
                raise ValueError("Binance ZIP tam olarak bir CSV içermeli.")
            member = PurePosixPath(members[0])
            if member.is_absolute() or ".." in member.parts:
                raise ValueError("Binance ZIP güvenli olmayan dosya yolu içeriyor.")
            raw_csv = archive.read(members[0]).decode("utf-8-sig")
    except (BadZipFile, UnicodeDecodeError) as exc:
        raise ValueError("Binance ZIP/CSV içeriği geçersiz.") from exc

    rows = []
    for line_number, values in enumerate(csv.reader(StringIO(raw_csv)), start=1):
        if not values or not values[0].strip():
            continue
        if not values[0].strip().lstrip("-").isdigit():
            if line_number == 1:
                continue
            raise ValueError(f"Binance CSV satır {line_number} zaman alanı geçersiz.")
        if len(values) < 6:
            raise ValueError(f"Binance CSV satır {line_number} eksik.")
        row = {
            "ts": _timestamp_ms(values[0].strip()),
            "open": float(values[1]), "high": float(values[2]),
            "low": float(values[3]), "close": float(values[4]),
            "volume": float(values[5]),
            "exchange": f"binance_{market}", "symbol": symbol,
        }
        _validate_price_row(row, line_number)
        rows.append(row)
    if not rows:
        raise ValueError(f"Binance arşivi boş: {filename}")
    return rows


def _validate_price_row(row: dict[str, object], line_number: int = 0) -> None:
    values = [float(row[key]) for key in ("open", "high", "low", "close", "volume")]
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"Binance satır {line_number} sonlu olmayan değer içeriyor.")
    open_, high, low, close, volume = values
    if min(open_, high, low, close) <= 0 or volume < 0:
        raise ValueError(f"Binance satır {line_number} fiyat/hacim değeri geçersiz.")
    if not low <= min(open_, close) <= max(open_, close) <= high:
        raise ValueError(f"Binance satır {line_number} OHLC sırası geçersiz.")


def validate_dataset(rows: Sequence[dict[str, object]], market: str, symbol: str) -> list[dict[str, object]]:
    if not rows:
        raise ValueError("Binance veri kümesi boş.")
    expected_exchange = f"binance_{market}"
    ordered = sorted(rows, key=lambda row: int(row["ts"]))
    for index, row in enumerate(ordered):
        if row.get("exchange") != expected_exchange or row.get("symbol") != symbol:
            raise ValueError("Binance veri kaynağı/sembolü uyuşmuyor.")
        _validate_price_row(row, index + 1)
        if int(row["ts"]) % STEP_MS:
            raise ValueError("Binance verisi 15 dakikaya hizalı değil.")
        if index and int(row["ts"]) - int(ordered[index - 1]["ts"]) != STEP_MS:
            raise ValueError("Binance verisinde eksik, tekrar veya sırasız 15m mum var.")
    return ordered


def download_monthly(
    market: str,
    symbol: str,
    start: str,
    end: str,
    out: Path = DEFAULT_DATA,
) -> dict[str, object]:
    symbol = symbol.upper()
    all_rows = []
    files = []
    for month in months_between(start, end):
        url = archive_url(market, symbol, month)
        filename = url.rsplit("/", 1)[-1]
        blob = _download(url)
        checksum_blob = _download(url + ".CHECKSUM")
        expected = _expected_checksum(checksum_blob, filename)
        actual = hashlib.sha256(blob).hexdigest()
        if actual != expected:
            raise ValueError(f"Binance SHA-256 doğrulaması başarısız: {filename}")
        rows = _parse_zip(blob, filename, market, symbol)
        all_rows.extend(rows)
        files.append({"file": filename, "sha256": actual, "candles": len(rows)})
    rows = validate_dataset(all_rows, market, symbol)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(out)
    manifest = {
        "source": BASE_URL, "market": market, "symbol": symbol,
        "interval": INTERVAL, "start_month": start, "end_month": end,
        "candles": len(rows), "first_ts": int(rows[0]["ts"]),
        "last_ts": int(rows[-1]["ts"]), "files": files,
        "dataset_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "output": str(out.resolve()),
    }
    manifest_path = out.with_suffix(out.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def download_spot_rest(
    symbol: str,
    start: str,
    end: str,
    out: Path = DEFAULT_DATA,
) -> dict[str, object]:
    """Download inclusive UTC dates from public, unsigned Spot REST klines."""
    symbol = symbol.upper()
    if not symbol.isalnum() or len(symbol) > 30:
        raise ValueError("Geçersiz Binance sembolü.")
    start_ms = _date_ms(start)
    end_exclusive = _date_ms(end) + 86_400_000
    if start_ms >= end_exclusive:
        raise ValueError("REST başlangıç tarihi bitiş tarihinden önce olmalı.")
    closed_cutoff = int(time.time() * 1000) // STEP_MS * STEP_MS
    end_exclusive = min(end_exclusive, closed_cutoff)
    if start_ms >= end_exclusive:
        raise ValueError("İstenen REST aralığında kapanmış mum yok.")

    cursor, rows, selected_base = start_ms, [], None
    while cursor < end_exclusive:
        payload, used_base = _rest_json(
            "/api/v3/klines",
            {"symbol": symbol, "interval": INTERVAL, "startTime": cursor,
             "endTime": end_exclusive - 1, "limit": 1000},
            (selected_base,) if selected_base else SPOT_REST_BASES,
        )
        selected_base = used_base
        page = _rest_rows(payload, symbol)
        page = [row for row in page if cursor <= int(row["ts"]) < end_exclusive]
        if not page:
            break
        rows.extend(page)
        next_cursor = int(page[-1]["ts"]) + STEP_MS
        if next_cursor <= cursor:
            raise ValueError("Binance REST sayfalama ilerlemiyor.")
        cursor = next_cursor
        if len(page) < 1000:
            break
        time.sleep(0.05)
    rows = validate_dataset(rows, "spot", symbol)
    if int(rows[0]["ts"]) != start_ms or int(rows[-1]["ts"]) != end_exclusive - STEP_MS:
        raise ValueError("Binance REST istenen kapalı mum aralığını eksik döndürdü.")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(out)
    manifest = {
        "source": selected_base, "endpoint": "/api/v3/klines",
        "security": "NONE", "market": "spot", "symbol": symbol,
        "interval": INTERVAL, "start_date": start, "end_date": end,
        "candles": len(rows), "first_ts": int(rows[0]["ts"]),
        "last_ts": int(rows[-1]["ts"]),
        "dataset_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "output": str(out.resolve()),
    }
    out.with_suffix(out.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def read_dataset(path: Path, market: str = "um", symbol: str = "BTCUSDT") -> list[dict[str, object]]:
    with Path(path).open(encoding="utf-8") as stream:
        raw = list(csv.DictReader(stream))
    rows = [{
        "ts": int(row["ts"]),
        **{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")},
        "exchange": row["exchange"], "symbol": row["symbol"],
    } for row in raw]
    return validate_dataset(rows, market, symbol.upper())


def train_offline(
    data_path: Path,
    model_path: Path = DEFAULT_MODEL,
    report_path: Path = DEFAULT_REPORT,
    samples: int = 400,
    market: str = "um",
    symbol: str = "BTCUSDT",
) -> dict[str, object]:
    """Train an isolated artifact; never modify the running paper database."""
    import learning
    import paper_v3

    rows = read_dataset(data_path, market, symbol)
    db = sqlite3.connect(":memory:")
    try:
        result = paper_v3.seed_historical_samples(db, rows, samples)
        state = learning.model_state(db, learning.REMORA_MODEL)
    finally:
        db.close()
    artifact = {
        "kind": "quant_remora_binance_offline_candidate",
        "market": market, "symbol": symbol.upper(), "interval": INTERVAL,
        "data_path": str(Path(data_path).resolve()),
        "dataset_sha256": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
        "candles": len(rows), "first_ts": int(rows[0]["ts"]),
        "last_ts": int(rows[-1]["ts"]), "training": result,
        "model_state": state, "deployed": False, "real_orders_enabled": False,
        "note": "Offline historical candidate; it cannot satisfy forward-paper promotion gates.",
    }
    model_path, report_path = Path(model_path), Path(report_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps({
        "kind": artifact["kind"], "market": market, "symbol": symbol.upper(),
        "dataset_sha256": artifact["dataset_sha256"],
        "model": state.get("model"), "model_version": state.get("version"),
        "deployed": False,
    }, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return artifact


def _basis_features(
    spot_rows: Sequence[dict[str, object]],
    futures_rows: Sequence[dict[str, object]],
) -> dict[int, tuple[float, float, float]]:
    if len(spot_rows) != len(futures_rows):
        raise ValueError("Spot ve USD-M veri uzunlukları uyuşmuyor.")
    basis = []
    timestamps = []
    for spot, future in zip(spot_rows, futures_rows):
        if int(spot["ts"]) != int(future["ts"]):
            raise ValueError("Spot ve USD-M mum zamanları uyuşmuyor.")
        timestamps.append(int(spot["ts"]))
        basis.append(float(future["close"]) / float(spot["close"]) - 1)
    result = {}
    rolling_sum = rolling_sq = 0.0
    window = 96
    for index, value in enumerate(basis):
        rolling_sum += value
        rolling_sq += value * value
        if index >= window:
            old = basis[index - window]
            rolling_sum -= old
            rolling_sq -= old * old
        count = min(index + 1, window)
        mean = rolling_sum / count
        variance = max(rolling_sq / count - mean * mean, 0.0)
        scale = math.sqrt(variance)
        zscore = (value - mean) / scale if scale > 1e-12 else 0.0
        change_1h = value - basis[index - 4] if index >= 4 else 0.0
        result[timestamps[index]] = (value, zscore, change_1h)
    return result


def train_blended_offline(
    spot_path: Path,
    futures_path: Path,
    model_path: Path = DEFAULT_BLEND_MODEL,
    report_path: Path = DEFAULT_BLEND_REPORT,
    samples: int = 400,
) -> dict[str, object]:
    """Train a futures H8 challenger augmented with causal spot/futures basis."""
    import learning
    import paper_v3

    spot_rows = read_dataset(spot_path, "spot", "BTCUSDT")
    futures_rows = read_dataset(futures_path, "um", "BTCUSDT")
    features_by_ts = _basis_features(spot_rows, futures_rows)
    db = sqlite3.connect(":memory:")
    try:
        paper_v3.seed_historical_samples(db, futures_rows, samples)
        raw = [json.loads(row[0]) for row in db.execute(
            "SELECT detail FROM learning_samples WHERE strategy=? ORDER BY exit_ts",
            (learning.REMORA_MODEL,),
        )]
    finally:
        db.close()
    augmented = []
    for sample in raw:
        decision_ts = int(sample.get("metadata", {}).get("decision_ts", -1))
        extra = features_by_ts.get(decision_ts)
        if extra is None:
            raise ValueError("Basis özelliği için eşleşen karar mumu bulunamadı.")
        augmented.append({**sample, "x": [*sample["x"], *extra]})
    feature_names = [
        *learning.REMORA_FEATURES,
        "futures_spot_basis", "basis_zscore_24h", "basis_change_1h",
    ]
    state = learning.train_candidate(augmented, feature_names=feature_names)
    artifact = {
        "kind": "quant_remora_binance_spot_um_basis_challenger",
        "spot_data": str(Path(spot_path).resolve()),
        "futures_data": str(Path(futures_path).resolve()),
        "spot_sha256": hashlib.sha256(Path(spot_path).read_bytes()).hexdigest(),
        "futures_sha256": hashlib.sha256(Path(futures_path).read_bytes()).hexdigest(),
        "candles": len(futures_rows), "samples": len(augmented),
        "features": feature_names, "model_state": state,
        "deployed": False, "real_orders_enabled": False,
        "join_policy": "exact 15m timestamp; all basis features use decision close or earlier",
    }
    model_path, report_path = Path(model_path), Path(report_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps({
        "kind": artifact["kind"], "features": feature_names,
        "model": state.get("model"), "model_version": state.get("version"),
        "deployed": False,
    }, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return artifact


__all__ = [
    "BASE_URL", "DEFAULT_DATA", "DEFAULT_MODEL", "DEFAULT_REPORT",
    "DEFAULT_SPOT_DATA", "DEFAULT_SPOT_MODEL", "DEFAULT_SPOT_REPORT",
    "DEFAULT_BLEND_MODEL", "DEFAULT_BLEND_REPORT",
    "archive_url", "months_between", "download_monthly", "read_dataset",
    "download_spot_rest", "train_offline", "train_blended_offline",
    "validate_dataset",
]
