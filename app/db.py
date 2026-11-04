"""SQLite-сховище: замовлення, журнал подій, оброблені вебхуки."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_conn = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn
    path = path or os.getenv("DB_PATH", "orders.db")
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            ttn TEXT,
            receipt_id TEXT,
            crm_synced INTEGER DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            step TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webhooks (
            event_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL
        );
        """
    )
    return _conn


def conn() -> sqlite3.Connection:
    return _conn or connect()


def remember_webhook(event_id: str) -> bool:
    """True, якщо подія нова. False — дубль, його треба проігнорувати."""
    with _lock:
        try:
            conn().execute(
                "INSERT INTO webhooks (event_id, received_at) VALUES (?, ?)",
                (event_id, now()),
            )
            conn().commit()
            return True
        except sqlite3.IntegrityError:
            return False


def create_order(order_id: str, payload: dict) -> bool:
    with _lock:
        try:
            conn().execute(
                "INSERT INTO orders (id, payload, status, created_at) VALUES (?, ?, 'new', ?)",
                (order_id, json.dumps(payload, ensure_ascii=False), now()),
            )
            conn().commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_order(order_id: str) -> dict | None:
    row = conn().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return None
    order = dict(row)
    order["payload"] = json.loads(order["payload"])
    return order


def update_order(order_id: str, **fields) -> None:
    if "payload" in fields:
        fields["payload"] = json.dumps(fields["payload"], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        conn().execute(f"UPDATE orders SET {cols} WHERE id = ?", (*fields.values(), order_id))
        conn().commit()


def log(order_id: str, step: str, message: str, level: str = "info") -> None:
    with _lock:
        conn().execute(
            "INSERT INTO events (order_id, ts, level, step, message) VALUES (?, ?, ?, ?, ?)",
            (order_id, now(), level, step, message),
        )
        conn().commit()


def list_orders(limit: int = 20) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for row in rows:
        order = dict(row)
        order["payload"] = json.loads(order["payload"])
        order["events"] = [
            dict(e)
            for e in conn().execute(
                "SELECT ts, level, step, message FROM events WHERE order_id = ? ORDER BY id",
                (order["id"],),
            ).fetchall()
        ]
        result.append(order)
    return result
