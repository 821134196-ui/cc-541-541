# -*- coding: utf-8 -*-
"""SQLite 数据层：连接与建表。"""
import os
import sqlite3

from config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    address TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ports (
    id           INTEGER PRIMARY KEY,
    building_id  INTEGER NOT NULL REFERENCES buildings(id),
    floor        INTEGER NOT NULL,
    splitter_no  TEXT NOT NULL,
    port_no      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'free',  -- free / reserved / confirmed
    order_id     INTEGER,
    reserved_at  REAL,
    updated_at   REAL,
    UNIQUE(building_id, splitter_no, port_no)
);
CREATE INDEX IF NOT EXISTS idx_ports_building ON ports(building_id, status);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY,
    order_no         TEXT NOT NULL UNIQUE,
    customer_name    TEXT NOT NULL,
    customer_phone   TEXT NOT NULL,
    address          TEXT NOT NULL,
    building_id      INTEGER REFERENCES buildings(id),
    room             TEXT,
    plan             TEXT NOT NULL,
    expect_window    TEXT,
    address_verified INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,
    port_id          INTEGER,
    scheduled_window TEXT,
    fail_reason      TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    activated_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS surveys (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(id),
    fiber_path    TEXT,
    splitter_no   TEXT NOT NULL,
    port_no       INTEGER NOT NULL,
    optical_power REAL NOT NULL,
    power_passed  INTEGER NOT NULL,
    engineer      TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    filename   TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS device_bindings (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    router_sn   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',  -- active / replaced
    bound_at    REAL NOT NULL,
    replaced_at REAL
);
-- 同一路由器同一时刻只能在一户工单上处于有效绑定。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_router
    ON device_bindings(router_sn) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS speed_tests (
    id             INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES orders(id),
    down_mbps      REAL NOT NULL,
    up_mbps        REAL NOT NULL,
    ping_ms        REAL NOT NULL,
    threshold_down REAL NOT NULL,
    threshold_up   REAL NOT NULL,
    passed         INTEGER NOT NULL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS signoff_codes (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    code       TEXT NOT NULL,
    consumed   INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_log (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    phone      TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at REAL NOT NULL
);

-- 工单号全局原子序列，杜绝并发下 COUNT+1 撞号。
CREATE TABLE IF NOT EXISTS seq (id INTEGER PRIMARY KEY AUTOINCREMENT);
"""


def get_conn(db_path=None):
    """打开一个连接。WAL 提高并发，外键开启，行工厂为 dict 风格 sqlite3.Row。"""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or get_conn()
    conn.executescript(SCHEMA)
    if own:
        conn.close()
