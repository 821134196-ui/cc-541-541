"""SQLite 连接管理与建表脚本。"""
import sqlite3
import threading

from . import config

# 进程级互斥：端口分配（UPDATE ... WHERE status='free'）本身是原子的，
# 但“查可用端口 + 预占 + 建工单”需要一个串行临界区，测试并发竞争时也依赖它。
ALLOC_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,             -- 小区/楼栋名
    address     TEXT NOT NULL,             -- 标准地址
    floors      INTEGER NOT NULL           -- 楼层数
);

CREATE TABLE IF NOT EXISTS splitters (
    id           INTEGER PRIMARY KEY,
    building_id  INTEGER NOT NULL REFERENCES buildings(id),
    floor        INTEGER NOT NULL,         -- 所在楼层
    name         TEXT NOT NULL,            -- 分光器编号/名称
    UNIQUE(building_id, name)
);

CREATE TABLE IF NOT EXISTS ports (
    id           INTEGER PRIMARY KEY,
    splitter_id  INTEGER NOT NULL REFERENCES splitters(id),
    port_no      INTEGER NOT NULL,         -- 分光器端口号
    status       TEXT NOT NULL DEFAULT 'free',  -- free / reserved / confirmed
    order_id     INTEGER REFERENCES orders(id), -- 当前占用工单
    reserved_at  REAL,                     -- 预占时刻（epoch 秒）
    UNIQUE(splitter_id, port_no)
);

CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    bandwidth_mbps  INTEGER NOT NULL,      -- 签约带宽
    monthly_fee     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    order_no        TEXT NOT NULL UNIQUE,
    customer_name   TEXT NOT NULL,
    customer_phone  TEXT NOT NULL,
    address         TEXT NOT NULL,         -- 用户填写地址
    building_id     INTEGER REFERENCES buildings(id),
    room_no         TEXT NOT NULL,         -- 楼栋房号
    plan_id         INTEGER REFERENCES plans(id),
    status          TEXT NOT NULL DEFAULT 'draft',
    slot            TEXT,                  -- 期望/预约时段
    visit_date      TEXT,                  -- 预约日期 YYYY-MM-DD
    engineer        TEXT,                  -- 上门工程师
    fiber_path      TEXT,                  -- 光纤入户路径
    splitter_id     INTEGER REFERENCES splitters(id),
    splitter_port   INTEGER,               -- 分光器端口号
    optical_rx_dbm  REAL,                  -- 实测接收光功率
    router_sn       TEXT,                  -- 路由器编号
    speed_down_mbps REAL,                  -- 实测下行
    speed_up_mbps   REAL,                  -- 实测上行
    confirmed_at    REAL,                  -- 用户签收时刻
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    at          REAL NOT NULL,
    actor       TEXT NOT NULL,             -- dispatcher/engineer/customer/system
    action      TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    router_sn   TEXT NOT NULL UNIQUE,
    model       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'stock',  -- stock / bound
    order_id    INTEGER REFERENCES orders(id),
    bound_at    REAL
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    filename    TEXT NOT NULL,
    caption     TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_log (
    id          INTEGER PRIMARY KEY,
    phone       TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ports_status ON ports(status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


def connect(db_path=None):
    # isolation_level=None：autocommit，需要原子性的临界区显式
    # BEGIN IMMEDIATE / commit / rollback（见 services._allocate_port 等）
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=10,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()
