# -*- coding: utf-8 -*-
"""种子数据：两栋楼 + 各状态样例工单。

- 阳光花园1栋：6 层，每层 1 个分光器 × 8 端口，端口充足。
- 锦云大厦B座：3 层 × 4 端口，全部被存量用户占满 —— 端口不足样例。

用法：python3 seed.py            # 空库才播种
      python3 seed.py --reset    # 清库后重新播种
"""
import os
import struct
import sys
import time
import zlib

import core
from config import PHOTO_DIR
from db import get_conn, init_db

T = time.time()


def _png(path, rgb):
    """写一个 2×2 纯色 PNG 作为现场照片占位。"""
    def chunk(typ, data):
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * 2 for _ in range(2))
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(data)


def reset(conn):
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in (
        "events", "sms_log", "signoff_codes", "speed_tests", "device_bindings",
        "photos", "surveys", "orders", "ports", "buildings", "seq",
    ):
        conn.execute(f"DELETE FROM {t}")
    seq = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    if seq:
        conn.execute("DELETE FROM sqlite_sequence")
    conn.execute("PRAGMA foreign_keys=ON")


def add_building(conn, name, address, floors, ports_per_floor):
    cur = conn.execute(
        "INSERT INTO buildings(name,address) VALUES(?,?)", (name, address)
    )
    bid = cur.lastrowid
    for floor in range(1, floors + 1):
        for p in range(1, ports_per_floor + 1):
            conn.execute(
                "INSERT INTO ports(building_id,floor,splitter_no,port_no,status,updated_at) "
                "VALUES(?,?,?,?, 'free',?)",
                (bid, floor, f"F{floor}-SP1", p, T),
            )
    return bid


def occupy_all(conn, bid, floors, ports_per_floor):
    """用存量用户把整栋楼占满（端口不足样例）。"""
    n = 0
    for floor in range(1, floors + 1):
        for p in range(1, ports_per_floor + 1):
            n += 1
            o = conn.execute(
                "INSERT INTO orders(order_no,customer_name,customer_phone,address,"
                "building_id,room,plan,address_verified,status,created_at,updated_at,"
                "activated_at) VALUES(?,?,?,?,?,?,?,1,'activated',?,?,?)",
                (
                    f"BB-OLD-{n:03d}", f"存量用户{n:02d}", f"139{floor:02d}{p:02d}0000",
                    "锦云路12号锦云大厦B座", bid, f"{floor}{p:02d}", "300M",
                    T - 86400 * 30, T - 86400 * 30, T - 86400 * 30,
                ),
            ).lastrowid
            conn.execute(
                "UPDATE ports SET status='confirmed', order_id=?, updated_at=? "
                "WHERE building_id=? AND floor=? AND port_no=?",
                (o, T, bid, floor, p),
            )


def new_application(conn, name, phone, addr, room, plan, window):
    o = core.create_order(
        conn,
        {
            "customer_name": name,
            "customer_phone": phone,
            "address": addr,
            "room": room,
            "plan": plan,
            "expect_window": window,
        },
        actor="受理员",
        now=T,
    )
    return o["id"]


def seed(conn):
    os.makedirs(PHOTO_DIR, exist_ok=True)

    bid_a = add_building(conn, "阳光花园1栋", "阳光路88号", floors=6, ports_per_floor=8)
    bid_b = add_building(conn, "锦云大厦B座", "锦云路12号", floors=3, ports_per_floor=4)
    occupy_all(conn, bid_b, floors=3, ports_per_floor=4)

    A = "阳光路88号阳光花园1栋"

    # 1) 完整开通流程样例
    oid = new_application(conn, "周航", "13800000001", f"{A}602", "602", "1000M", "周六 09:00-11:00")
    core.check_resources(conn, oid, now=T)
    core.submit_survey(
        conn, oid,
        {"fiber_path": "楼道竖井→客厅弱电箱→电视柜", "splitter_no": "F6-SP1",
         "port_no": 1, "optical_power": -18.6},
        now=T,
    )
    core.bind_device(conn, oid, "RT-AX88U-0001", now=T)
    _, _ = core.run_speed_test(conn, oid, force="pass", now=T)
    code = conn.execute(
        "SELECT code FROM signoff_codes WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)
    ).fetchone()["code"]
    core.signoff(conn, oid, code, role="customer", actor="周航", now=T)
    fn = f"order{oid}-fiber.png"
    _png(os.path.join(PHOTO_DIR, fn), (30, 80, 150))
    conn.execute("INSERT INTO photos(order_id,filename,created_at) VALUES(?,?,?)", (oid, fn, T))

    # 2) 测速通过·待用户签收（演示验证码：123456）
    oid = new_application(conn, "林晚", "13800000002", f"{A}501", "501", "500M", "周六 14:00-16:00")
    core.check_resources(conn, oid, now=T)
    core.submit_survey(
        conn, oid,
        {"fiber_path": "楼层分光箱→玄关→书房", "splitter_no": "F5-SP1",
         "port_no": 1, "optical_power": -20.1},
        now=T,
    )
    core.bind_device(conn, oid, "RT-AX56U-0002", now=T)
    core.run_speed_test(conn, oid, force="pass", now=T)
    conn.execute("UPDATE signoff_codes SET code='123456' WHERE order_id=?", (oid,))
    sms = conn.execute(
        "SELECT content FROM sms_log WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)
    ).fetchone()
    if sms:
        import re as _re
        conn.execute(
            "UPDATE sms_log SET content=? WHERE id=(SELECT id FROM sms_log WHERE order_id=? ORDER BY id DESC LIMIT 1)",
            (_re.sub(r"验证码为 \d{6}", "验证码为 123456", sms["content"]), oid),
        )

    # 3) 设备已绑定·待测速
    oid = new_application(conn, "高远", "13800000003", f"{A}401", "401", "300M", "周日 09:00-11:00")
    core.check_resources(conn, oid, now=T)
    core.submit_survey(
        conn, oid,
        {"fiber_path": "竖井横担→餐厅餐边柜", "splitter_no": "F4-SP1",
         "port_no": 1, "optical_power": -19.8},
        now=T,
    )
    core.bind_device(conn, oid, "RT-AX56U-0003", now=T)

    # 4) 勘线完成·待绑设备
    oid = new_application(conn, "陈默", "13800000004", f"{A}301", "301", "500M", "周日 14:00-16:00")
    core.check_resources(conn, oid, now=T)
    core.submit_survey(
        conn, oid,
        {"fiber_path": "弱电井→客厅墙角", "splitter_no": "F3-SP1",
         "port_no": 1, "optical_power": -21.4},
        now=T,
    )

    # 5) 光功率不达标·整改中（失败读数带阈值）
    oid = new_application(conn, "许晴", "13800000005", f"{A}201", "201", "300M", "周一 18:00-20:00")
    core.check_resources(conn, oid, now=T)
    core.submit_survey(
        conn, oid,
        {"fiber_path": "分光箱临时跳纤→门口（熔接点衰耗过大）", "splitter_no": "F2-SP1",
         "port_no": 1, "optical_power": -31.5},
        now=T,
    )

    # 6) 已预约·端口预占中
    oid = new_application(conn, "宋捷", "13800000006", f"{A}101", "101", "500M", "周二 10:00-12:00")
    core.check_resources(conn, oid, now=T)

    # 7) 用户不在·待改期（端口已释放）
    oid = new_application(conn, "唐婧", "13800000007", f"{A}202", "202", "300M", "周三 09:00-11:00")
    core.check_resources(conn, oid, now=T)
    core.mark_customer_absent(conn, oid, now=T)

    # 8) 新申请·待资源核查
    new_application(conn, "韩松", "13800000008", f"{A}102", "102", "1000M", "周四 14:00-16:00")

    # 9) 地址写错·地址异常
    oid = new_application(conn, "梁峥", "13800000009", "滨江路99号临江府3栋501", "501", "300M", "周五 09:00-11:00")
    core.check_resources(conn, oid, now=T)

    # 10) 端口不足样例：申请全满楼栋
    oid = new_application(conn, "邓琳", "13800000010", "锦云路12号锦云大厦B座201", "201", "500M", "周六 10:00-12:00")
    core.check_resources(conn, oid, now=T)


def main():
    do_reset = "--reset" in sys.argv
    conn = get_conn()
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM buildings").fetchone()["c"]:
        if not do_reset:
            print("数据库已有种子数据，跳过（用 --reset 重新播种）")
            return
        reset(conn)
    seed(conn)
    n_orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    n_ports = conn.execute("SELECT COUNT(*) c FROM ports").fetchone()["c"]
    print(f"播种完成：{n_ports} 个端口，{n_orders} 张工单（含存量用户）。")


if __name__ == "__main__":
    main()
