"""Хранилище заказов (SQLite). На Railway файл лежит на подключённом томе (DATA_DIR)."""
import json
import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ploika.db"

STATUSES = ["new", "confirmed", "delivering", "done", "cancelled"]

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
# SQLite сравнивает кириллицу с учётом регистра — ищем через Python lower()
_conn.create_function("pylower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True)
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS orders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  INTEGER NOT NULL,
        source      TEXT NOT NULL,          -- telegram | site
        tg_user_id  INTEGER,
        tg_username TEXT,
        name        TEXT NOT NULL,
        phone       TEXT NOT NULL,          -- только цифры, 998...
        area        TEXT,
        address     TEXT,
        pay         TEXT,
        note        TEXT,
        items_json  TEXT NOT NULL,          -- [{id,name,category,price,qty}]
        subtotal    INTEGER NOT NULL,
        delivery    INTEGER NOT NULL,
        total       INTEGER NOT NULL,
        status      TEXT NOT NULL DEFAULT 'new',
        updated_at  INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
    CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(phone);
    """
)
# Номера заказов начинаются с 1001
if _conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
    _conn.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES ('orders', 1000)")
_conn.commit()


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["items"] = json.loads(d.pop("items_json"))
    return d


def add_order(*, source, name, phone, area, address, pay, note, items, subtotal, delivery,
              tg_user_id=None, tg_username=None) -> dict:
    now = int(time.time())
    cur = _conn.execute(
        """INSERT INTO orders (created_at, source, tg_user_id, tg_username, name, phone, area, address,
                               pay, note, items_json, subtotal, delivery, total, status, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', ?)""",
        (now, source, tg_user_id, tg_username, name, phone, area, address, pay, note,
         json.dumps(items, ensure_ascii=False), subtotal, delivery, subtotal + delivery, now),
    )
    _conn.commit()
    return get_order(cur.lastrowid)


def get_order(order_id: int) -> dict | None:
    r = _conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return _row(r) if r else None


def set_status(order_id: int, status: str) -> dict | None:
    if status not in STATUSES:
        raise ValueError("bad status")
    _conn.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, int(time.time()), order_id))
    _conn.commit()
    return get_order(order_id)


def list_orders(*, since: int = 0, until: int | None = None, status: str = "", q: str = "",
                phone: str = "", limit: int = 500) -> list[dict]:
    sql, args = "SELECT * FROM orders WHERE created_at >= ?", [since]
    if until:
        sql += " AND created_at < ?"; args.append(until)
    if status:
        sql += " AND status = ?"; args.append(status)
    if phone:
        sql += " AND phone = ?"; args.append(phone)
    if q:
        like = f"%{q.lower()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        sql += (" AND (pylower(name) LIKE ? OR pylower(address) LIKE ? OR pylower(tg_username) LIKE ? OR CAST(id AS TEXT) = ?"
                + (" OR phone LIKE ?" if digits else "") + ")")
        args.append(like)
        args += [like, like, q.lstrip("№#")] + ([f"%{digits}%"] if digits else [])
    sql += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    return [_row(r) for r in _conn.execute(sql, args)]


def customers() -> list[dict]:
    rows = _conn.execute(
        """SELECT phone,
                  (SELECT name FROM orders o2 WHERE o2.phone=o.phone ORDER BY created_at DESC LIMIT 1) AS name,
                  (SELECT area FROM orders o2 WHERE o2.phone=o.phone ORDER BY created_at DESC LIMIT 1) AS area,
                  MAX(tg_username) AS tg_username,
                  COUNT(*) AS orders,
                  SUM(CASE WHEN status!='cancelled' THEN total ELSE 0 END) AS spent,
                  MIN(created_at) AS first_order,
                  MAX(created_at) AS last_order
           FROM orders o GROUP BY phone ORDER BY last_order DESC"""
    )
    return [dict(r) for r in rows]
