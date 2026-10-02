"""幂等种子数据：楼栋/分光器/端口/套餐/设备 + 各环节演示工单。

    python3 -m app.seed            # 已存在数据则跳过
    python3 -m app.seed --reset    # 清空后重建（保留 photos 目录）

端口不足样例：「阳光花园2栋」唯一分光器 4 个端口全部 confirmed，
新申请该楼栋会被核查为 rejected；种子同时内置一条 rejected 工单。
"""
import sys
import time

from . import config
from .db import connect, init_db


def reset_db(conn):
    # 顺序需满足外键：先清引用方（events/photos/ports/devices/orders），
    # 再清被引用方（splitters/buildings/plans）
    conn.executescript(
        """DELETE FROM sms_log; DELETE FROM events; DELETE FROM photos;
           DELETE FROM ports; DELETE FROM devices; DELETE FROM orders;
           DELETE FROM splitters; DELETE FROM buildings; DELETE FROM plans;""")
    conn.commit()


def _mk_building(conn, name, address, floors):
    cur = conn.execute(
        "INSERT INTO buildings(name, address, floors) VALUES (?,?,?)",
        (name, address, floors))
    return cur.lastrowid


def _mk_splitter(conn, building_id, floor, name, n_ports):
    sid = conn.execute(
        "INSERT INTO splitters(building_id, floor, name) VALUES (?,?,?)",
        (building_id, floor, name)).lastrowid
    for p in range(1, n_ports + 1):
        conn.execute(
            "INSERT INTO ports(splitter_id, port_no, status) VALUES (?,?,'free')",
            (sid, p))
    return sid


def _mk_order(conn, order_no, name, phone, addr, bid, room, plan_id,
              status, **kw):
    now = time.time()
    fields = dict(order_no=order_no, customer_name=name, customer_phone=phone,
                  address=addr, building_id=bid, room_no=room, plan_id=plan_id,
                  status=status, created_at=now, updated_at=now)
    fields.update(kw)
    cols = ",".join(fields)
    ph = ",".join("?" * len(fields))
    oid = conn.execute(
        f"INSERT INTO orders({cols}) VALUES ({ph})", tuple(fields.values())).lastrowid
    conn.execute(
        "INSERT INTO events(order_id, at, actor, action, detail) VALUES (?,?,?,?,?)",
        (oid, now, "system", "种子工单", f"演示数据：{status}"))
    return oid


def seed(conn):
    now = time.time()
    # ---- 套餐 ----
    plans = {}
    for nm, bw, fee in [("300M 家庭版", 300, 79.0),
                        ("500M 家庭版", 500, 99.0),
                        ("1000M 旗舰版", 1000, 149.0)]:
        plans[bw] = conn.execute(
            "INSERT INTO plans(name, bandwidth_mbps, monthly_fee) VALUES (?,?,?)",
            (nm, bw, fee)).lastrowid

    # ---- 设备库存 ----
    for i in range(1, 21):
        conn.execute(
            "INSERT INTO devices(router_sn, model, status) VALUES (?,?,'stock')",
            (f"RT-{1000 + i}", "AX3000 双频"))

    # ---- 楼栋 1：阳光花园1栋，6 层，3 个 8 口分光器 ----
    b1 = _mk_building(conn, "阳光花园1栋", "阳光花园1栋", 6)
    s11 = _mk_splitter(conn, b1, 1, "SP-1-01", 8)
    s13 = _mk_splitter(conn, b1, 3, "SP-1-02", 8)
    s15 = _mk_splitter(conn, b1, 5, "SP-1-03", 8)

    # ---- 楼栋 2：阳光花园2栋，6 层，仅 1 个 4 口分光器且全部占满（端口不足样例）----
    b2 = _mk_building(conn, "阳光花园2栋", "阳光花园2栋", 6)
    s21 = _mk_splitter(conn, b2, 1, "SP-2-01", 4)

    # ---- 楼栋 3：滨江大厦，18 层，每 3 层一个 16 口分光器 ----
    b3 = _mk_building(conn, "滨江大厦", "滨江大厦", 18)
    for f in (3, 6, 9, 12, 15, 18):
        _mk_splitter(conn, b3, f, f"SP-B-{f:02d}", 16)

    # ---- 演示工单 ----
    # 2 栋 4 户全部 confirmed + activated（分光器占满）
    for i, room in enumerate(["101", "102", "201", "202"], start=1):
        oid = _mk_order(
            conn, f"BB-SEED-FULL{i}", f"老用户{i}", "13800000000",
            "阳光花园2栋", b2, room, plans[300], "activated",
            visit_date="2026-09-20", slot="09:00-11:00", engineer="周工",
            fiber_path="楼道竖井→客厅弱电箱", splitter_id=s21, splitter_port=i,
            optical_rx_dbm=-18.5, router_sn=None,
            speed_down_mbps=295.0, speed_up_mbps=96.0, confirmed_at=now - 86400)
        conn.execute("UPDATE ports SET status='confirmed', order_id=?, reserved_at=? "
                     "WHERE splitter_id=? AND port_no=?", (oid, now - 90000, s21, i))

    # 端口不足样例工单（rejected），页面可直接查看
    _mk_order(conn, "BB-SEED-PORTFULL", "钱六", "13900000006",
              "阳光花园2栋", b2, "301", plans[500], "rejected")

    # 1 栋：一条已排期待上门工单（占用 SP-1-01 端口1，预占中）
    o_sched = _mk_order(
        conn, "BB-SEED-SCHED", "张三", "13900000001",
        "阳光花园1栋", b1, "302", plans[500], "scheduled",
        visit_date="2026-10-03", slot="14:00-16:00", engineer="李工")
    conn.execute("UPDATE ports SET status='reserved', order_id=?, reserved_at=? "
                 "WHERE splitter_id=? AND port_no=1", (o_sched, now, s11))

    # 1 栋：一条勘线+测速通过、待用户签收工单（用户端演示），占用 SP-1-02 端口1
    o_wait = _mk_order(
        conn, "BB-SEED-WAIT", "李四", "13900000002",
        "阳光花园1栋", b1, "301", plans[300], "surveyed",
        visit_date="2026-10-02", slot="09:00-11:00", engineer="李工",
        fiber_path="3层弱电井→入户门上方→客厅电视柜",
        splitter_id=s13, splitter_port=1, optical_rx_dbm=-20.3,
        router_sn="RT-1001", speed_down_mbps=286.4, speed_up_mbps=95.2)
    conn.execute("UPDATE ports SET status='confirmed', order_id=?, reserved_at=? "
                 "WHERE splitter_id=? AND port_no=1", (o_wait, now - 3600, s13))
    conn.execute("UPDATE devices SET status='bound', order_id=?, bound_at=? "
                 "WHERE router_sn='RT-1001'", (o_wait, now - 3600))

    # 1 栋：一条完整已开通工单（SP-1-03 端口1）
    o_done = _mk_order(
        conn, "BB-SEED-DONE", "王五", "13900000003",
        "阳光花园1栋", b1, "501", plans[1000], "activated",
        visit_date="2026-09-28", slot="11:00-13:00", engineer="周工",
        fiber_path="5层弱电井→客厅", splitter_id=s15, splitter_port=1,
        optical_rx_dbm=-16.9, router_sn="RT-1002",
        speed_down_mbps=968.0, speed_up_mbps=310.0, confirmed_at=now - 200000)
    conn.execute("UPDATE ports SET status='confirmed', order_id=?, reserved_at=? "
                 "WHERE splitter_id=? AND port_no=1", (o_done, now - 300000, s15))
    conn.execute("UPDATE devices SET status='bound', order_id=?, bound_at=? "
                 "WHERE router_sn='RT-1002'", (o_done, now - 300000))

    # 滨江大厦：一个长期预占（reserved_at 很久以前）——演示超时自动释放
    o_stale = _mk_order(
        conn, "BB-SEED-STALE", "赵七", "13900000004",
        "滨江大厦", b3, "901", plans[1000], "reserved",
        slot="09:00-11:00")
    sp_b9 = conn.execute("SELECT id FROM splitters WHERE building_id=? AND floor=9",
                         (b3,)).fetchone()[0]
    conn.execute("UPDATE ports SET status='reserved', order_id=?, reserved_at=? "
                 "WHERE splitter_id=? AND port_no=1",
                 (o_stale, now - config.PORT_RESERVE_TIMEOUT_SECONDS - 60, sp_b9))

    conn.commit()


def main():
    config.ensure_dirs()
    reset = "--reset" in sys.argv
    conn = connect()
    init_db(conn)
    has = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    if has and not reset:
        print("数据库已有种子数据，跳过（--reset 可重建）。")
        return
    if reset:
        reset_db(conn)
    seed(conn)
    print("种子数据已写入：", config.DB_PATH)
    print("演示工单：BB-SEED-SCHED(已排期) / BB-SEED-WAIT(待签收) / "
          "BB-SEED-PORTFULL(端口不足样例) / BB-SEED-STALE(预占超时)")


if __name__ == "__main__":
    main()
