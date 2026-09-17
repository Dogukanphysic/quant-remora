"""Conservative background trader for Binance Spot Testnet.

The worker is deliberately isolated from the paper engines.  It owns one
SQLite database, one process lock, and at most one BTCUSDT position acquired by
this worker.  Exchange/account BTC balances are never used to infer position
state because Spot Testnet accounts are prefunded.

Credentials are read by :class:`binance_execution.Client` from the inherited
process environment.  They are never accepted by this module's CLI, stored in
SQLite, or written to logs.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Iterator, Mapping, Sequence
import uuid

import binance_execution as execution


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DB_PATH = STATE_DIR / "binance-testnet-worker.sqlite3"
LOCK_PATH = STATE_DIR / "binance-testnet-worker.lock"
CONTROL_LOCK_PATH = STATE_DIR / "binance-testnet-worker-control.lock"

SYMBOL = "BTCUSDT"
BASE_ASSET = "BTC"
QUOTE_ASSET = "USDT"
POLICY = "btc_daily_momentum_30d_v1"
INTERVAL = "1d"
LOOKBACK_DAYS = 30
MOMENTUM_THRESHOLD = Decimal("0.20")
ENTRY_QUOTE_USDT = Decimal("10")
POLL_SECONDS = 5.0
API_POLL_SECONDS = 60.0
DAY_MS = 86_400_000
MAX_CANDLE_AGE_MS = 36 * 60 * 60 * 1000
STARTUP_WAIT_SECONDS = 3.0
STOP_WAIT_SECONDS = 20.0


class WorkerHalt(RuntimeError):
    """A fail-closed condition requiring inspection or manual intervention."""


class WorkerStopped(RuntimeError):
    """The operator disabled new decisions before an intent could be submitted."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decimal(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WorkerHalt(f"Invalid {field}: {value!r}") from exc
    if not number.is_finite():
        raise WorkerHalt(f"Invalid {field}: {value!r}")
    return number


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(db)
    return db


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS worker_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            desired_running INTEGER NOT NULL DEFAULT 0 CHECK (desired_running IN (0,1)),
            halted INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0,1)),
            halt_reason TEXT,
            position_qty TEXT NOT NULL DEFAULT '0',
            position_quote_cost TEXT NOT NULL DEFAULT '0',
            last_candle_close_ms INTEGER,
            pending_client_id TEXT,
            pending_side TEXT CHECK (pending_side IS NULL OR pending_side IN ('BUY','SELL')),
            pending_decision_ms INTEGER,
            pending_quote TEXT,
            pending_qty TEXT,
            last_error TEXT,
            realized_pnl_usdt TEXT NOT NULL DEFAULT '0',
            pnl_complete INTEGER NOT NULL DEFAULT 1 CHECK (pnl_complete IN (0,1)),
            pnl_incomplete_reason TEXT,
            completed_round_trips INTEGER NOT NULL DEFAULT 0,
            transient_failures INTEGER NOT NULL DEFAULT 0,
            last_transient_error_ms INTEGER,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_decisions (
            candle_close_ms INTEGER PRIMARY KEY,
            policy TEXT NOT NULL,
            close_latest TEXT NOT NULL,
            close_30d TEXT NOT NULL,
            momentum TEXT NOT NULL,
            target_long INTEGER NOT NULL CHECK (target_long IN (0,1)),
            action TEXT NOT NULL,
            client_id TEXT,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_order_intents (
            client_id TEXT PRIMARY KEY,
            policy TEXT NOT NULL,
            decision_ms INTEGER NOT NULL,
            candle_close_ms INTEGER NOT NULL,
            symbol TEXT NOT NULL CHECK (symbol = 'BTCUSDT'),
            side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
            requested_quote TEXT,
            requested_qty TEXT,
            state TEXT NOT NULL,
            exchange_order_id TEXT,
            executed_qty TEXT,
            net_base_qty TEXT,
            cumulative_quote_qty TEXT,
            realized_pnl_usdt TEXT,
            commission_by_asset TEXT,
            response_json TEXT,
            error TEXT,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL,
            FOREIGN KEY (candle_close_ms) REFERENCES worker_decisions(candle_close_ms)
        );

        CREATE INDEX IF NOT EXISTS worker_intents_state_idx
            ON worker_order_intents(state, updated_ms);

        CREATE TABLE IF NOT EXISTS worker_epochs (
            epoch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            archived_ms INTEGER NOT NULL,
            reason TEXT NOT NULL,
            state_json TEXT NOT NULL,
            decision_count INTEGER NOT NULL,
            order_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_epoch_decisions (
            epoch_id INTEGER NOT NULL,
            candle_close_ms INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY (epoch_id, candle_close_ms),
            FOREIGN KEY (epoch_id) REFERENCES worker_epochs(epoch_id)
        );

        CREATE TABLE IF NOT EXISTS worker_epoch_order_intents (
            epoch_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY (epoch_id, client_id),
            FOREIGN KEY (epoch_id) REFERENCES worker_epochs(epoch_id)
        );
        """
    )
    # Additive migration for databases created before realized-P&L accounting.
    state_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(worker_state)")
    }
    if "realized_pnl_usdt" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "realized_pnl_usdt TEXT NOT NULL DEFAULT '0'"
        )
    if "completed_round_trips" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "completed_round_trips INTEGER NOT NULL DEFAULT 0"
        )
    if "pnl_complete" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "pnl_complete INTEGER NOT NULL DEFAULT 1"
        )
    if "pnl_incomplete_reason" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN pnl_incomplete_reason TEXT"
        )
    if "transient_failures" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "transient_failures INTEGER NOT NULL DEFAULT 0"
        )
    if "last_transient_error_ms" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN last_transient_error_ms INTEGER"
        )
    intent_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(worker_order_intents)")
    }
    if "realized_pnl_usdt" not in intent_columns:
        db.execute(
            "ALTER TABLE worker_order_intents ADD COLUMN realized_pnl_usdt TEXT"
        )
    if "commission_by_asset" not in intent_columns:
        db.execute(
            "ALTER TABLE worker_order_intents ADD COLUMN commission_by_asset TEXT"
        )
    now = _now_ms()
    db.execute(
        """INSERT OR IGNORE INTO worker_state
           (singleton, created_ms, updated_ms) VALUES (1, ?, ?)""",
        (now, now),
    )


@contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def _execution_gate() -> None:
    if os.getenv("BINANCE_TESTNET_WORKER_ENABLED", "").lower() != "true":
        raise WorkerHalt(
            "Worker is fail-closed; set BINANCE_TESTNET_WORKER_ENABLED=true."
        )
    if os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") != "testnet":
        raise WorkerHalt(
            "Order execution is fail-closed; set BINANCE_ORDER_EXECUTION_ENABLED=testnet."
        )
    if not os.getenv("BINANCE_TESTNET_API_KEY") or not os.getenv(
        "BINANCE_TESTNET_SECRET_KEY"
    ):
        raise WorkerHalt("Binance Spot Testnet credentials are missing.")


def _reset_gate() -> None:
    if os.getenv("BINANCE_TESTNET_RESET_ENABLED") != "reset":
        raise WorkerHalt(
            "Testnet epoch reset is fail-closed; set "
            "BINANCE_TESTNET_RESET_ENABLED=reset."
        )
    if not os.getenv("BINANCE_TESTNET_API_KEY") or not os.getenv(
        "BINANCE_TESTNET_SECRET_KEY"
    ):
        raise WorkerHalt("Binance Spot Testnet credentials are missing.")


def _client_order_id(decision_ms: int, side: str) -> str:
    material = f"{POLICY}|{decision_ms}|{side.upper()}".encode("ascii")
    digest = hashlib.sha256(material).hexdigest()[:12]
    # Binance permits at most 36 characters for newClientOrderId.
    return f"qr-{side[0].lower()}-{decision_ms}-{digest}"[:36]


def _rule(rules: object, name: str) -> object:
    if isinstance(rules, Mapping):
        return rules[name]
    return getattr(rules, name)


def _closed_candles(
    klines: Sequence[Sequence[object]], *, now_ms: int | None = None
) -> list[tuple[int, Decimal]]:
    """Return sorted, unique ``(close_time_ms, close)`` pairs.

    ``Client.klines`` already excludes the in-progress UTC candle.  We still
    validate shape/order here so malformed data cannot create an order.
    """
    parsed: list[tuple[int, Decimal]] = []
    for candle in klines:
        if not isinstance(candle, Sequence) or isinstance(candle, (str, bytes)):
            raise WorkerHalt("Malformed Binance daily kline response.")
        if len(candle) < 7:
            raise WorkerHalt("Malformed Binance daily kline response.")
        open_time = int(candle[0])
        close_time = int(candle[6])
        if open_time % DAY_MS != 0 or close_time - open_time != DAY_MS - 1:
            raise WorkerHalt("Daily klines must be complete UTC calendar days.")
        close = _decimal(candle[4], "daily close")
        if close <= 0:
            raise WorkerHalt("Daily close must be positive.")
        if parsed and close_time <= parsed[-1][0]:
            raise WorkerHalt("Daily klines must be strictly chronological and unique.")
        parsed.append((close_time, close))
    if len(parsed) < LOOKBACK_DAYS + 1:
        raise WorkerHalt("At least 31 completed daily candles are required.")
    window = parsed[-(LOOKBACK_DAYS + 1) :]
    for previous, current in zip(window, window[1:]):
        if current[0] - previous[0] != DAY_MS:
            raise WorkerHalt("The 31 daily candles must be exactly contiguous.")
    current_ms = _now_ms() if now_ms is None else int(now_ms)
    age_ms = current_ms - window[-1][0]
    if age_ms < 0:
        raise WorkerHalt("Latest daily candle is not completed yet.")
    if age_ms > MAX_CANDLE_AGE_MS:
        raise WorkerHalt("Latest completed daily candle is stale (older than 36 hours).")
    return window


def _signal(market_data_client: object) -> dict[str, object]:
    window = _closed_candles(
        market_data_client.klines(
            interval=INTERVAL, limit=LOOKBACK_DAYS + 2, symbol=SYMBOL
        )
    )
    old_close = window[0][1]
    latest_close = window[-1][1]
    momentum = latest_close / old_close - Decimal("1")
    return {
        "candle_close_ms": window[-1][0],
        "close_latest": latest_close,
        "close_30d": old_close,
        "momentum": momentum,
        "target_long": momentum > MOMENTUM_THRESHOLD,
    }


def _free_balance(account: object, asset: str) -> Decimal:
    if not isinstance(account, Mapping):
        raise WorkerHalt("Binance account response is invalid.")
    balances = account.get("balances")
    if not isinstance(balances, list):
        raise WorkerHalt("Binance account balances are missing.")
    matches = [
        balance
        for balance in balances
        if isinstance(balance, Mapping) and balance.get("asset") == asset
    ]
    if len(matches) > 1:
        raise WorkerHalt(f"Binance returned duplicate {asset} balances.")
    if not matches:
        return Decimal("0")
    free = _decimal(matches[0].get("free"), f"free {asset} balance")
    if free < 0:
        raise WorkerHalt(f"Free {asset} balance cannot be negative.")
    return free


def _commissions_by_asset(order: Mapping[str, object]) -> dict[str, Decimal]:
    fills = order.get("fills")
    if not isinstance(fills, list) or not fills:
        raise WorkerHalt("A FULL Binance order response with fills is required.")
    commissions: dict[str, Decimal] = {}
    for fill in fills:
        if not isinstance(fill, Mapping):
            raise WorkerHalt("Binance fill response is invalid.")
        amount = _decimal(fill.get("commission", "0"), "fill commission")
        if amount < 0:
            raise WorkerHalt("Fill commission cannot be negative.")
        asset = fill.get("commissionAsset")
        if not isinstance(asset, str) or not asset:
            raise WorkerHalt("Fill commission asset is missing.")
        commissions[asset] = commissions.get(asset, Decimal("0")) + amount
    return commissions


def _state(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute("SELECT * FROM worker_state WHERE singleton=1").fetchone()
    if row is None:  # pragma: no cover - guarded by schema initialization
        raise WorkerHalt("Worker state is missing.")
    return row


def _halt(db: sqlite3.Connection, reason: str, *, client_id: str | None = None) -> None:
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_state SET halted=1, halt_reason=?, last_error=?,
               updated_ms=? WHERE singleton=1""",
            (reason, reason, now),
        )
        if client_id:
            db.execute(
                """UPDATE worker_order_intents SET state='halted', error=?, updated_ms=?
                   WHERE client_id=?""",
                (reason, now, client_id),
            )


def _transient_retry(db: sqlite3.Connection, reason: str) -> dict[str, object]:
    """Record a pre-intent read outage without converting it into a halt."""
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_state SET transient_failures=transient_failures+1,
               last_transient_error_ms=?, last_error=?, updated_ms=? WHERE singleton=1""",
            (now, reason, now),
        )
        failures = int(_state(db)["transient_failures"])
    backoff = min(900, int(API_POLL_SECONDS) * (2 ** min(failures - 1, 4)))
    return {"action": "retry", "reason": reason, "backoff_seconds": backoff}


def _clear_transient_error(db: sqlite3.Connection) -> None:
    state = _state(db)
    if not state["transient_failures"] and state["last_error"] is None:
        return
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_state SET transient_failures=0,
               last_transient_error_ms=NULL, last_error=NULL, updated_ms=?
               WHERE singleton=1""",
            (now,),
        )


def _order_identity(order: Mapping[str, object], client_id: str, side: str) -> None:
    returned_id = order.get("clientOrderId") or order.get("origClientOrderId")
    if returned_id != client_id:
        raise WorkerHalt(
            f"Managed order client-id mismatch: expected {client_id!r}, got {returned_id!r}."
        )
    if order.get("symbol") != SYMBOL:
        raise WorkerHalt("Managed order symbol mismatch.")
    if str(order.get("side", "")).upper() != side:
        raise WorkerHalt("Managed order side mismatch.")


def _apply_filled(
    db: sqlite3.Connection,
    order: Mapping[str, object],
    client_id: str,
    side: str,
) -> dict[str, object]:
    _order_identity(order, client_id, side)
    if str(order.get("status", "")).upper() != "FILLED":
        raise WorkerHalt("Attempted to apply a non-filled managed order.")

    executed = _decimal(order.get("executedQty"), "executed quantity")
    quote = _decimal(order.get("cummulativeQuoteQty", "0"), "cumulative quote quantity")
    if executed <= 0:
        raise WorkerHalt("Filled managed order has no executed quantity.")

    state = _state(db)
    current = _decimal(state["position_qty"], "tracked position")
    current_cost = _decimal(state["position_quote_cost"], "position cost")
    cumulative_pnl = _decimal(state["realized_pnl_usdt"], "realized PnL")
    completed_round_trips = int(state["completed_round_trips"])
    commissions = _commissions_by_asset(order)
    quote_commission = commissions.get(QUOTE_ASSET, Decimal("0"))
    base_commission = commissions.get(BASE_ASSET, Decimal("0"))
    unsupported = {
        asset: amount
        for asset, amount in commissions.items()
        if asset not in {BASE_ASSET, QUOTE_ASSET} and amount != 0
    }
    incomplete_reason = state["pnl_incomplete_reason"]
    if unsupported:
        detail = ", ".join(
            f"{asset}={format(amount, 'f')}" for asset, amount in sorted(unsupported.items())
        )
        incomplete_reason = (
            "Exact USDT P&L is unavailable because fill commission used an "
            f"unsupported asset: {detail}."
        )
    pnl_complete = bool(state["pnl_complete"]) and not unsupported
    realized_pnl: Decimal | None = None
    if side == "BUY":
        if current != 0:
            raise WorkerHalt("A managed BUY cannot be applied over an existing position.")
        net = _decimal(
            execution.net_base_quantity(order, base_asset=BASE_ASSET),
            "net acquired quantity",
        )
        if net <= 0:
            raise WorkerHalt("Filled BUY has no net acquired BTC quantity.")
        new_qty = current + net
        new_cost = current_cost + quote + quote_commission
    else:
        net = Decimal("0")
        if current <= 0:
            raise WorkerHalt("A managed SELL cannot be applied without a tracked position.")
        base_depletion = executed + base_commission
        if base_depletion > current:
            raise WorkerHalt(
                "Filled SELL plus BTC commission exceeds the worker's tracked quantity."
            )
        new_qty = current - base_depletion
        if new_qty < 0:
            new_qty = Decimal("0")
        sold_cost = current_cost * base_depletion / current
        proceeds = quote - quote_commission
        if proceeds < 0:
            raise WorkerHalt("Quote-asset commission exceeds SELL proceeds.")
        calculated_pnl = proceeds - sold_cost
        if pnl_complete:
            realized_pnl = calculated_pnl
            cumulative_pnl += calculated_pnl
        new_cost = current_cost - sold_cost
        if new_qty == 0:
            new_cost = Decimal("0")
            completed_round_trips += 1

    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_order_intents SET state='filled', exchange_order_id=?,
               executed_qty=?, net_base_qty=?, cumulative_quote_qty=?, response_json=?,
               realized_pnl_usdt=?, commission_by_asset=?, error=NULL, updated_ms=?
               WHERE client_id=?""",
            (
                str(order.get("orderId", "")),
                format(executed, "f"),
                format(net, "f") if side == "BUY" else None,
                format(quote, "f"),
                _json(order),
                format(realized_pnl, "f") if realized_pnl is not None else None,
                _json({asset: format(amount, "f") for asset, amount in commissions.items()}),
                now,
                client_id,
            ),
        )
        db.execute(
            """UPDATE worker_state SET position_qty=?, position_quote_cost=?,
               pending_client_id=NULL, pending_side=NULL, pending_decision_ms=NULL,
               pending_quote=NULL, pending_qty=NULL, halted=?, halt_reason=?,
               last_error=NULL, realized_pnl_usdt=?, completed_round_trips=?,
               pnl_complete=?, pnl_incomplete_reason=?,
               updated_ms=? WHERE singleton=1""",
            (
                format(new_qty, "f"),
                format(new_cost, "f"),
                int(bool(unsupported)),
                incomplete_reason if unsupported else None,
                format(cumulative_pnl, "f"),
                completed_round_trips,
                int(pnl_complete),
                incomplete_reason,
                now,
            ),
        )
    execution_action = "bought" if side == "BUY" else "sold"
    return {
        "action": "halted" if unsupported else execution_action,
        "execution_action": execution_action,
        "client_id": client_id,
        "executed_qty": format(executed, "f"),
        "position_qty": format(new_qty, "f"),
        "realized_pnl_usdt": (
            format(realized_pnl, "f") if realized_pnl is not None else None
        ),
        "pnl_complete": pnl_complete,
        "pnl_incomplete_reason": incomplete_reason,
    }


def _terminal_without_fill(
    db: sqlite3.Connection, order: Mapping[str, object], client_id: str, side: str
) -> dict[str, object]:
    _order_identity(order, client_id, side)
    status = str(order.get("status", "")).upper()
    if status not in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        raise WorkerHalt(f"Unexpected managed order status: {status or 'missing'}.")
    executed = _decimal(order.get("executedQty", "0"), "terminal executed quantity")
    if executed != 0:
        raise WorkerHalt(
            f"Managed order ended as {status} after a partial execution; "
            "automatic position changes are blocked pending manual reconciliation."
        )
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_order_intents SET state='terminal', response_json=?,
               error=?, updated_ms=? WHERE client_id=?""",
            (_json(order), status, now, client_id),
        )
        db.execute(
            """UPDATE worker_state SET pending_client_id=NULL, pending_side=NULL,
               pending_decision_ms=NULL, pending_quote=NULL, pending_qty=NULL,
               halted=0, halt_reason=NULL, last_error=?, updated_ms=? WHERE singleton=1""",
            (f"Managed order ended as {status}; it will not be retried.", now),
        )
    return {"action": "terminal", "client_id": client_id, "status": status}


def _consume_order(
    db: sqlite3.Connection,
    order: Mapping[str, object],
    client_id: str,
    side: str,
    client: object | None = None,
) -> dict[str, object]:
    _order_identity(order, client_id, side)
    status = str(order.get("status", "")).upper()
    if status == "FILLED":
        filled_order = order
        if not isinstance(order.get("fills"), list) or not order.get("fills"):
            reconcile = getattr(client, "reconcile_filled_order", None)
            if not callable(reconcile):
                raise WorkerHalt(
                    "FILLED order lookup omitted fills and the client cannot reconstruct them."
                )
            filled_order = reconcile(client_id, symbol=SYMBOL)
            if not isinstance(filled_order, Mapping):
                raise WorkerHalt("Filled-order reconciliation returned an invalid response.")
            _order_identity(filled_order, client_id, side)
            if str(filled_order.get("status", "")).upper() != "FILLED":
                raise WorkerHalt("Filled-order reconciliation changed the order status.")
        return _apply_filled(db, filled_order, client_id, side)
    if status in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        return _terminal_without_fill(db, order, client_id, side)
    raise WorkerHalt(f"Managed market order has unsafe pending status {status or 'missing'}.")


def _reconcile_pending(db: sqlite3.Connection, client: object) -> dict[str, object] | None:
    state = _state(db)
    client_id = state["pending_client_id"]
    if not client_id:
        return None
    side = str(state["pending_side"])
    try:
        order = client.order_by_client_id(client_id, symbol=SYMBOL)
        if not isinstance(order, Mapping):
            raise WorkerHalt("Managed order lookup returned an invalid response.")
        return _consume_order(db, order, client_id, side, client)
    except WorkerHalt as exc:
        _halt(db, str(exc), client_id=client_id)
        return {"action": "halted", "reason": str(exc), "client_id": client_id}
    except Exception as exc:  # an absent or unreachable pending order is ambiguous
        reason = (
            f"Unable to reconcile managed pending order {client_id}; no order was retried: "
            f"{type(exc).__name__}: {exc}"
        )
        _halt(db, reason, client_id=client_id)
        return {"action": "halted", "reason": reason, "client_id": client_id}


def _verify_position_origin(
    db: sqlite3.Connection, client: object, tracked_qty: Decimal
) -> dict[str, object] | None:
    """Prove that local BTC came from a still-present worker BUY order."""
    origin = db.execute(
        """SELECT client_id, executed_qty, net_base_qty, cumulative_quote_qty
           FROM worker_order_intents
           WHERE side='BUY' AND state='filled'
           ORDER BY decision_ms DESC LIMIT 1"""
    ).fetchone()
    if origin is None:
        reason = "Tracked BTC has no filled worker BUY provenance; worker halted."
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    client_id = str(origin["client_id"])
    try:
        remote = client.order_by_client_id(client_id, symbol=SYMBOL)
    except execution.BinanceTransportError as exc:
        return _transient_retry(db, f"Position-origin read outage: {exc}")
    except Exception as exc:
        reason = (
            f"Worker BUY origin {client_id} is absent from Binance Spot Testnet; "
            f"a Testnet reset is suspected ({type(exc).__name__}: {exc})."
        )
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    if not isinstance(remote, Mapping):
        reason = "Worker BUY origin lookup returned an invalid response."
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    try:
        _order_identity(remote, client_id, "BUY")
        if str(remote.get("status", "")).upper() != "FILLED":
            raise WorkerHalt("Worker BUY origin is no longer FILLED.")
        remote_executed = _decimal(remote.get("executedQty"), "origin executed quantity")
        stored_executed = _decimal(origin["executed_qty"], "stored origin quantity")
        remote_quote = _decimal(
            remote.get("cummulativeQuoteQty"), "origin cumulative quote"
        )
        stored_quote = _decimal(
            origin["cumulative_quote_qty"], "stored origin cumulative quote"
        )
        origin_net = _decimal(origin["net_base_qty"], "stored origin net quantity")
        if remote_executed != stored_executed or remote_quote != stored_quote:
            raise WorkerHalt("Worker BUY origin execution no longer matches local state.")
        if tracked_qty <= 0 or tracked_qty > origin_net:
            raise WorkerHalt("Tracked BTC quantity exceeds its worker BUY provenance.")
    except WorkerHalt as exc:
        reason = f"Worker BUY provenance mismatch: {exc}"
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    return None


def _persist_decision(
    db: sqlite3.Connection,
    signal: Mapping[str, object],
    action: str,
    client_id: str | None,
    side: str | None = None,
    requested_quote: Decimal | None = None,
    requested_qty: Decimal | None = None,
    require_desired: bool = False,
) -> bool:
    """Atomically claim a candle and, when needed, prepare an order intent."""
    candle_ms = int(signal["candle_close_ms"])
    now = _now_ms()
    with _transaction(db):
        state = _state(db)
        if require_desired and not state["desired_running"]:
            raise WorkerStopped("Worker stop was requested before decision persistence.")
        previous = state["last_candle_close_ms"]
        if previous is not None and int(previous) >= candle_ms:
            return False
        db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                target_long, action, client_id, created_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candle_ms,
                POLICY,
                format(signal["close_latest"], "f"),
                format(signal["close_30d"], "f"),
                format(signal["momentum"], "f"),
                int(bool(signal["target_long"])),
                action,
                client_id,
                now,
            ),
        )
        if client_id and side:
            db.execute(
                """INSERT INTO worker_order_intents
                   (client_id, policy, decision_ms, candle_close_ms, symbol, side,
                    requested_quote, requested_qty, state, created_ms, updated_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                (
                    client_id,
                    POLICY,
                    candle_ms,
                    candle_ms,
                    SYMBOL,
                    side,
                    format(requested_quote, "f") if requested_quote is not None else None,
                    format(requested_qty, "f") if requested_qty is not None else None,
                    now,
                    now,
                ),
            )
            db.execute(
                """UPDATE worker_state SET last_candle_close_ms=?, pending_client_id=?,
                   pending_side=?, pending_decision_ms=?, pending_quote=?, pending_qty=?,
                   updated_ms=? WHERE singleton=1""",
                (
                    candle_ms,
                    client_id,
                    side,
                    candle_ms,
                    format(requested_quote, "f") if requested_quote is not None else None,
                    format(requested_qty, "f") if requested_qty is not None else None,
                    now,
                ),
            )
        else:
            db.execute(
                """UPDATE worker_state SET last_candle_close_ms=?, updated_ms=?
                   WHERE singleton=1""",
                (candle_ms, now),
            )
    return True


def _submit_prepared(
    db: sqlite3.Connection,
    client: object,
    client_id: str,
    side: str,
    *,
    quote: Decimal | None = None,
    quantity: Decimal | None = None,
    require_desired: bool = False,
) -> dict[str, object]:
    """Submit exactly once, then reconcile; POST is never automatically retried."""
    if require_desired:
        with _transaction(db):
            state = _state(db)
            if not state["desired_running"]:
                intent = db.execute(
                    "SELECT state FROM worker_order_intents WHERE client_id=?",
                    (client_id,),
                ).fetchone()
                if intent is None or intent["state"] != "prepared":
                    raise WorkerHalt("Stopped worker has an inconsistent prepared intent.")
                now = _now_ms()
                db.execute(
                    """UPDATE worker_order_intents SET state='aborted_before_post',
                       error='operator stop before POST', updated_ms=? WHERE client_id=?""",
                    (now, client_id),
                )
                db.execute(
                    """UPDATE worker_state SET pending_client_id=NULL, pending_side=NULL,
                       pending_decision_ms=NULL, pending_quote=NULL, pending_qty=NULL,
                       updated_ms=? WHERE singleton=1""",
                    (now,),
                )
                return {"action": "stopped", "client_id": client_id}
    try:
        if side == "BUY":
            response = client.place_market_buy(
                quote, symbol=SYMBOL, client_order_id=client_id
            )
        else:
            response = client.place_market_sell(
                quantity, symbol=SYMBOL, client_order_id=client_id
            )
        if not isinstance(response, Mapping):
            raise WorkerHalt("Managed order submission returned an invalid response.")
        now = _now_ms()
        with _transaction(db):
            db.execute(
                """UPDATE worker_order_intents SET state='submitted', response_json=?,
                   updated_ms=? WHERE client_id=?""",
                (_json(response), now, client_id),
            )
        try:
            return _consume_order(db, response, client_id, side, client)
        except WorkerHalt as exc:
            _halt(db, str(exc), client_id=client_id)
            return {"action": "halted", "reason": str(exc), "client_id": client_id}
    except WorkerHalt as exc:
        _halt(db, str(exc), client_id=client_id)
        return {"action": "halted", "reason": str(exc), "client_id": client_id}
    except Exception as submit_exc:
        # A timeout can occur after Binance accepted the POST.  A single GET is
        # safe; issuing a second POST is forbidden even when GET says not found.
        now = _now_ms()
        with _transaction(db):
            db.execute(
                """UPDATE worker_order_intents SET state='unknown', error=?, updated_ms=?
                   WHERE client_id=?""",
                (f"{type(submit_exc).__name__}: {submit_exc}", now, client_id),
            )
        try:
            response = client.order_by_client_id(client_id, symbol=SYMBOL)
            if not isinstance(response, Mapping):
                raise WorkerHalt("Managed order lookup returned an invalid response.")
            return _consume_order(db, response, client_id, side, client)
        except Exception as lookup_exc:
            reason = (
                f"Ambiguous submission for {client_id}; POST was not retried and lookup "
                f"did not prove a terminal result ({type(lookup_exc).__name__}: {lookup_exc})."
            )
            _halt(db, reason, client_id=client_id)
            return {"action": "halted", "reason": reason, "client_id": client_id}


def run_once(
    *,
    db_path: Path | str = DB_PATH,
    client: object | None = None,
    market_data_client: object | None = None,
    enforce_desired: bool = False,
) -> dict[str, object]:
    """Reconcile an existing intent or evaluate one completed daily candle."""
    _execution_gate()
    api = client if client is not None else execution.Client()
    market = (
        market_data_client
        if market_data_client is not None
        else execution.PublicMarketDataClient()
    )
    with closing(_connect(db_path)) as db:
        # Pending durable intent always takes precedence, including after a
        # prior halt.  This permits a restart to observe a delayed fill without
        # ever posting the order again.
        reconciled = _reconcile_pending(db, api)
        if reconciled is not None:
            return reconciled

        state = _state(db)
        if state["halted"]:
            return {"action": "halted", "reason": state["halt_reason"]}
        if enforce_desired and not state["desired_running"]:
            return {"action": "stopped"}
        tracked_qty = _decimal(state["position_qty"], "tracked position")
        if tracked_qty > 0:
            origin_result = _verify_position_origin(db, api, tracked_qty)
            if origin_result is not None:
                return origin_result

        try:
            open_orders = api.open_orders(symbol=SYMBOL)
            if not isinstance(open_orders, list):
                raise WorkerHalt("Binance open-order response is invalid.")
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Open-order read outage: {exc}")
        except Exception as exc:
            reason = f"Open-order safety check failed: {type(exc).__name__}: {exc}"
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        if open_orders:
            identifiers = [
                str(order.get("clientOrderId", order.get("orderId", "unknown")))
                if isinstance(order, Mapping)
                else "invalid"
                for order in open_orders[:5]
            ]
            reason = (
                "Untracked BTCUSDT open order(s) detected; worker halted: "
                + ", ".join(identifiers)
            )
            _halt(db, reason)
            return {"action": "halted", "reason": reason}

        try:
            signal = _signal(market)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Daily-kline read outage: {exc}")
        last = state["last_candle_close_ms"]
        if last is not None and int(last) >= int(signal["candle_close_ms"]):
            _clear_transient_error(db)
            return {
                "action": "waiting",
                "candle_close_ms": int(signal["candle_close_ms"]),
            }

        target_long = bool(signal["target_long"])
        if tracked_qty == 0 and not target_long:
            _clear_transient_error(db)
            try:
                _persist_decision(
                    db, signal, "hold_cash", None,
                    require_desired=enforce_desired,
                )
            except WorkerStopped:
                return {"action": "stopped"}
            return {
                "action": "hold_cash",
                "momentum": format(signal["momentum"], "f"),
                "candle_close_ms": int(signal["candle_close_ms"]),
            }
        if tracked_qty > 0 and target_long:
            _clear_transient_error(db)
            try:
                _persist_decision(
                    db, signal, "hold_long", None,
                    require_desired=enforce_desired,
                )
            except WorkerStopped:
                return {"action": "stopped"}
            return {
                "action": "hold_long",
                "momentum": format(signal["momentum"], "f"),
                "position_qty": format(tracked_qty, "f"),
                "candle_close_ms": int(signal["candle_close_ms"]),
            }

        try:
            rules = api.symbol_rules(symbol=SYMBOL)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Symbol-rules read outage: {exc}")
        min_notional = _decimal(_rule(rules, "min_notional"), "minimum notional")
        step_size = _decimal(_rule(rules, "step_size"), "step size")
        min_qty = _decimal(_rule(rules, "min_qty"), "minimum quantity")

        candle_ms = int(signal["candle_close_ms"])
        if tracked_qty == 0:
            if ENTRY_QUOTE_USDT < min_notional:
                reason = (
                    f"Fixed {ENTRY_QUOTE_USDT} USDT entry is below Binance minimum "
                    f"notional {min_notional}."
                )
                _halt(db, reason)
                return {"action": "halted", "reason": reason}
            try:
                free_quote = _free_balance(api.account(), QUOTE_ASSET)
            except execution.BinanceTransportError as exc:
                return _transient_retry(db, f"Account-balance read outage: {exc}")
            except Exception as exc:
                reason = f"USDT balance safety check failed: {type(exc).__name__}: {exc}"
                _halt(db, reason)
                return {"action": "halted", "reason": reason}
            if free_quote < ENTRY_QUOTE_USDT:
                reason = (
                    f"Insufficient free Testnet USDT: need {ENTRY_QUOTE_USDT}, "
                    f"available {free_quote}."
                )
                _halt(db, reason)
                return {"action": "halted", "reason": reason}
            _clear_transient_error(db)
            side = "BUY"
            client_id = _client_order_id(candle_ms, side)
            try:
                claimed = _persist_decision(
                    db,
                    signal,
                    "buy",
                    client_id,
                    side,
                    requested_quote=ENTRY_QUOTE_USDT,
                    require_desired=enforce_desired,
                )
            except WorkerStopped:
                return {"action": "stopped"}
            if not claimed:
                return {"action": "waiting", "candle_close_ms": candle_ms}
            return _submit_prepared(
                db, api, client_id, side, quote=ENTRY_QUOTE_USDT,
                require_desired=enforce_desired,
            )

        side = "SELL"
        origin_result = _verify_position_origin(db, api, tracked_qty)
        if origin_result is not None:
            return origin_result
        quantity = execution.floor_step(tracked_qty, step_size)
        try:
            ticker = api.book_ticker(symbol=SYMBOL)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Book-ticker read outage: {exc}")
        if not isinstance(ticker, Mapping):
            reason = "Binance book ticker returned an invalid response."
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        bid = _decimal(ticker.get("bidPrice"), "best bid")
        if quantity < min_qty or quantity * bid < min_notional:
            reason = (
                f"Tracked worker position is dust: sellable={quantity} BTC, "
                f"notional={quantity * bid} USDT. Manual Testnet cleanup is required."
            )
            _halt(db, reason)
            return {"action": "dust", "reason": reason, "position_qty": str(tracked_qty)}
        try:
            free_base = _free_balance(api.account(), BASE_ASSET)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Account-balance read outage: {exc}")
        except Exception as exc:
            reason = f"BTC balance safety check failed: {type(exc).__name__}: {exc}"
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        _clear_transient_error(db)
        if free_base < quantity:
            reason = (
                f"Insufficient free Testnet BTC for tracked SELL: need {quantity}, "
                f"available {free_base}."
            )
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        client_id = _client_order_id(candle_ms, side)
        try:
            claimed = _persist_decision(
                db, signal, "sell", client_id, side, requested_qty=quantity,
                require_desired=enforce_desired,
            )
        except WorkerStopped:
            return {"action": "stopped"}
        if not claimed:
            return {"action": "waiting", "candle_close_ms": candle_ms}
        return _submit_prepared(
            db, api, client_id, side, quantity=quantity,
            require_desired=enforce_desired,
        )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_lock(path: Path | str = LOCK_PATH) -> dict[str, object] | None:
    lock_path = Path(path)
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _lock_active(path: Path | str = LOCK_PATH) -> bool:
    info = _read_lock(path)
    return bool(info and _pid_alive(int(info.get("pid", 0))))


@contextmanager
def _process_lock(path: Path | str = LOCK_PATH) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists() and not _lock_active(lock_path):
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
    token = uuid.uuid4().hex
    payload = _json({"pid": os.getpid(), "token": token, "started_ms": _now_ms()})
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkerHalt("Binance Testnet worker is already running.") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        info = _read_lock(lock_path)
        if info and info.get("token") == token:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def _control_lock_path(worker_lock_path: Path | str) -> Path:
    path = Path(worker_lock_path)
    return path.with_name(f"{path.stem}-control{path.suffix or '.lock'}")


@contextmanager
def _control_mutex(worker_lock_path: Path | str = LOCK_PATH) -> Iterator[None]:
    """Serialize start/stop/reset so only one control transition can win."""
    path = _control_lock_path(worker_lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    token = uuid.uuid4().hex
    payload = _json({"pid": os.getpid(), "token": token, "started_ms": _now_ms()})
    while True:
        if path.exists() and not _lock_active(path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise WorkerHalt("Another Binance Testnet control action is in progress.")
            time.sleep(0.05)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        info = _read_lock(path)
        if info and info.get("token") == token:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _set_desired(db_path: Path | str, desired: bool) -> None:
    with closing(_connect(db_path)) as db, _transaction(db):
        db.execute(
            "UPDATE worker_state SET desired_running=?, updated_ms=? WHERE singleton=1",
            (int(desired), _now_ms()),
        )


def _archive_epoch_and_reset(db_path: Path | str) -> dict[str, int]:
    """Archive the active ledger in-place and atomically initialize a new epoch."""
    with closing(_connect(db_path)) as db, _transaction(db):
        state = dict(_state(db))
        if state["desired_running"]:
            raise WorkerHalt("Testnet epoch reset requires desired_running=false.")
        if state["pending_client_id"]:
            raise WorkerHalt("Testnet epoch reset refuses a pending managed intent.")
        decisions = [dict(row) for row in db.execute(
            "SELECT * FROM worker_decisions ORDER BY candle_close_ms"
        )]
        intents = [dict(row) for row in db.execute(
            "SELECT * FROM worker_order_intents ORDER BY decision_ms, client_id"
        )]
        now = _now_ms()
        cursor = db.execute(
            """INSERT INTO worker_epochs
               (archived_ms, reason, state_json, decision_count, order_count)
               VALUES (?, 'binance_spot_testnet_epoch_reset', ?, ?, ?)""",
            (now, _json(state), len(decisions), len(intents)),
        )
        epoch_id = int(cursor.lastrowid)
        db.executemany(
            """INSERT INTO worker_epoch_decisions
               (epoch_id, candle_close_ms, record_json) VALUES (?, ?, ?)""",
            [
                (epoch_id, int(row["candle_close_ms"]), _json(row))
                for row in decisions
            ],
        )
        db.executemany(
            """INSERT INTO worker_epoch_order_intents
               (epoch_id, client_id, record_json) VALUES (?, ?, ?)""",
            [(epoch_id, str(row["client_id"]), _json(row)) for row in intents],
        )
        db.execute("DELETE FROM worker_order_intents")
        db.execute("DELETE FROM worker_decisions")
        db.execute(
            """UPDATE worker_state SET desired_running=0, halted=0,
               halt_reason=NULL, position_qty='0', position_quote_cost='0',
               last_candle_close_ms=NULL, pending_client_id=NULL, pending_side=NULL,
               pending_decision_ms=NULL, pending_quote=NULL, pending_qty=NULL,
               last_error=NULL, realized_pnl_usdt='0', pnl_complete=1,
               pnl_incomplete_reason=NULL, completed_round_trips=0,
               transient_failures=0, last_transient_error_ms=NULL,
               created_ms=?, updated_ms=? WHERE singleton=1""",
            (now, now),
        )
    return {
        "reset_epoch_id": epoch_id,
        "archived_decisions": len(decisions),
        "archived_orders": len(intents),
    }


def status_snapshot(
    db_path: Path | str = DB_PATH, lock_path: Path | str = LOCK_PATH
) -> dict[str, object]:
    with closing(_connect(db_path)) as db:
        state = dict(_state(db))
        counts = db.execute(
            """SELECT COUNT(*) AS intents,
               COALESCE(SUM(state='filled'),0) AS filled
               FROM worker_order_intents"""
        ).fetchone()
        latest = db.execute(
            """SELECT candle_close_ms, momentum, action, client_id
               FROM worker_decisions ORDER BY candle_close_ms DESC LIMIT 1"""
        ).fetchone()
        archived_epochs = int(
            db.execute("SELECT COUNT(*) FROM worker_epochs").fetchone()[0]
        )
    qty = _decimal(state["position_qty"], "tracked position")
    return {
        "policy": POLICY,
        "symbol": SYMBOL,
        "market_data_source": "binance_public_spot",
        "execution_environment": "binance_spot_testnet",
        "running": _lock_active(lock_path),
        "desired_running": bool(state["desired_running"]),
        "halted": bool(state["halted"]),
        "halt_reason": state["halt_reason"],
        "position": "cash" if qty == 0 else "long",
        "tracked_position_qty": format(qty, "f"),
        "tracked_position_cost_usdt": state["position_quote_cost"],
        "realized_pnl_usdt": state["realized_pnl_usdt"],
        "pnl_complete": bool(state["pnl_complete"]),
        "pnl_incomplete_reason": state["pnl_incomplete_reason"],
        "completed_round_trips": int(state["completed_round_trips"]),
        "archived_epochs": archived_epochs,
        "transient_failures": int(state["transient_failures"]),
        "last_error": state["last_error"],
        "pending_client_id": state["pending_client_id"],
        "last_candle_close_ms": state["last_candle_close_ms"],
        "intents": int(counts["intents"]),
        "filled_orders": int(counts["filled"]),
        "latest_decision": dict(latest) if latest else None,
        "caller_env_credentials_present": bool(
            os.getenv("BINANCE_TESTNET_API_KEY")
            and os.getenv("BINANCE_TESTNET_SECRET_KEY")
        ),
        "caller_env_worker_enabled": (
            os.getenv("BINANCE_TESTNET_WORKER_ENABLED", "").lower() == "true"
        ),
        "caller_env_execution_enabled": (
            os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") == "testnet"
        ),
    }


def run_forever(
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
    poll_seconds: float = POLL_SECONDS,
    api_poll_seconds: float = API_POLL_SECONDS,
    arm: bool = False,
    client: object | None = None,
    market_data_client: object | None = None,
) -> dict[str, object]:
    try:
        _execution_gate()
    except Exception:
        try:
            _set_desired(db_path, False)
        except Exception:
            pass
        raise
    if arm:
        _set_desired(db_path, True)
    api = client if client is not None else execution.Client()
    market = (
        market_data_client
        if market_data_client is not None
        else execution.PublicMarketDataClient()
    )
    stop_event = threading.Event()
    control_poll = max(0.1, min(float(poll_seconds), 5.0))
    api_poll = max(API_POLL_SECONDS, float(api_poll_seconds))
    next_api_poll = 0.0
    with _process_lock(lock_path):
        try:
            while True:
                with closing(_connect(db_path)) as db:
                    state = _state(db)
                    pending = bool(state["pending_client_id"])
                    if not state["desired_running"] and not pending:
                        break
                    if not state["desired_running"] and pending:
                        # Stop still permits only reconciliation of an intent
                        # that may already have reached Binance.
                        next_api_poll = 0.0
                    # A halted pending intent gets exactly one reconciliation pass
                    # after an explicit restart. Other halted states are terminal.
                    if state["halted"] and not pending:
                        break
                now_mono = time.monotonic()
                if now_mono < next_api_poll:
                    stop_event.wait(min(control_poll, next_api_poll - now_mono))
                    continue
                result = run_once(
                    db_path=db_path,
                    client=api,
                    market_data_client=market,
                    enforce_desired=True,
                )
                delay = max(api_poll, float(result.get("backoff_seconds", 0)))
                next_api_poll = time.monotonic() + delay
                if result.get("action") == "halted":
                    break
                # desired_running remains responsive without repeating API calls.
                stop_event.wait(control_poll)
        except Exception as exc:
            try:
                with closing(_connect(db_path)) as db:
                    _halt(
                        db,
                        f"Worker loop failed closed: {type(exc).__name__}: {exc}",
                    )
            except Exception:
                try:
                    _set_desired(db_path, False)
                except Exception:
                    pass
            raise
    return status_snapshot(db_path, lock_path)


def _control_locked(
    action: str,
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
    client: object | None = None,
) -> dict[str, object]:
    """Start, stop, or inspect the detached Testnet worker."""
    command = action.lower().strip()
    if command == "status":
        return status_snapshot(db_path, lock_path)
    if command == "stop":
        _set_desired(db_path, False)
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        while _lock_active(lock_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _lock_active(lock_path):
            raise WorkerHalt(
                "Stop was requested, but the worker has not released its lock; "
                "no stopped state is being reported yet."
            )
        return status_snapshot(db_path, lock_path)
    if command == "reset":
        _reset_gate()
        if _lock_active(lock_path):
            raise WorkerHalt("Testnet epoch reset requires the worker to be stopped.")
        with closing(_connect(db_path)) as db:
            state = _state(db)
            if state["desired_running"]:
                raise WorkerHalt("Testnet epoch reset requires desired_running=false.")
            if state["pending_client_id"]:
                raise WorkerHalt("Testnet epoch reset refuses a pending managed intent.")
        api = client if client is not None else execution.Client()
        try:
            open_orders = api.open_orders(symbol=SYMBOL)
        except Exception as exc:
            raise WorkerHalt(f"Testnet reset open-order check failed: {exc}") from exc
        if not isinstance(open_orders, list):
            raise WorkerHalt("Testnet reset open-order response is invalid.")
        if open_orders:
            raise WorkerHalt("Testnet epoch reset refuses existing BTCUSDT open orders.")
        archive = _archive_epoch_and_reset(db_path)
        return {**status_snapshot(db_path, lock_path), **archive}
    if command != "start":
        raise ValueError("Worker control action must be start, stop, status, or reset.")

    _execution_gate()
    if _lock_active(lock_path):
        raise WorkerHalt(
            "Binance Testnet worker is already running; a new caller environment "
            "cannot replace the detached worker's inherited credentials."
        )
    with closing(_connect(db_path)) as db:
        state = _state(db)
        if state["halted"] and not state["pending_client_id"]:
            raise WorkerHalt(
                "Worker is halted; inspect status and reconcile/repair state before restart."
            )
    _set_desired(db_path, True)
    args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--db",
        str(Path(db_path).resolve()),
        "--lock",
        str(Path(lock_path).resolve()),
    ]
    kwargs: dict[str, object] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        child = subprocess.Popen(args, **kwargs)
    except Exception as exc:
        _set_desired(db_path, False)
        raise WorkerHalt(f"Unable to start Binance Testnet worker: {exc}") from exc

    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    lock_observed = False
    while time.monotonic() < deadline:
        if _lock_active(lock_path):
            if child.poll() is not None:
                _set_desired(db_path, False)
                snapshot = status_snapshot(db_path, lock_path)
                detail = snapshot.get("halt_reason") or "child exited during startup"
                raise WorkerHalt(
                    f"Binance Testnet worker did not remain running: {detail}"
                )
            if lock_observed:
                return status_snapshot(db_path, lock_path)
            lock_observed = True
            time.sleep(0.05)
            continue
        lock_observed = False
        return_code = child.poll()
        if return_code is not None:
            _set_desired(db_path, False)
            snapshot = status_snapshot(db_path, lock_path)
            detail = snapshot.get("halt_reason") or f"child exit code {return_code}"
            raise WorkerHalt(f"Binance Testnet worker did not remain running: {detail}")
        time.sleep(0.05)

    _set_desired(db_path, False)
    try:
        child.terminate()
    except (AttributeError, OSError, subprocess.SubprocessError):
        pass
    raise WorkerHalt("Binance Testnet worker did not acquire its startup lock in time.")


def control(
    action: str,
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
    client: object | None = None,
) -> dict[str, object]:
    command = action.lower().strip()
    if command == "status":
        return status_snapshot(db_path, lock_path)
    if command not in {"start", "stop", "reset"}:
        raise ValueError("Worker control action must be start, stop, status, or reset.")
    with _control_mutex(lock_path):
        return _control_locked(
            command, db_path=db_path, lock_path=lock_path, client=client
        )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "run", "reset"))
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--lock", default=str(LOCK_PATH))
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            result = run_forever(db_path=args.db, lock_path=args.lock)
        else:
            result = control(args.action, db_path=args.db, lock_path=args.lock)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "DB_PATH",
    "LOCK_PATH",
    "POLICY",
    "SYMBOL",
    "WorkerHalt",
    "control",
    "run_forever",
    "run_once",
    "status_snapshot",
]
