# -*- coding: utf-8 -*-
"""核心业务：工单状态机、端口分配/释放、勘线、设备、测速、签收。

状态流转：
  pending --核查--> scheduled(reserved) / address_invalid / shortage
  scheduled --超时--> expired          scheduled --用户不在--> reschedule
  expired/reschedule --改期重新核查--> scheduled / shortage
  scheduled --工程师勘线--> surveyed(端口 confirmed) / power_failed
  surveyed --绑设备--> device_bound --测速--> speed_passed / speed_failed
  speed_passed --用户本人验证码签收--> activated
"""
import re
import time

from adapters import MockSpeedAdapter, MockSmsAdapter
from config import (
    OPTICAL_RX_MAX,
    OPTICAL_RX_MIN,
    PLANS,
    PING_MAX_MS,
    RESERVE_TTL_SECONDS,
    ROLE_CUSTOMER,
    ROLE_ENGINEER,
    SIGNOFF_CODE_TTL_SECONDS,
    SPEED_PASS_RATIO,
)

speed_adapter = MockSpeedAdapter()
sms_adapter = MockSmsAdapter()


class ServiceError(Exception):
    """业务规则错误，message 可直接展示。"""


# ---------------- 基础工具 ----------------

def log_event(conn, order_id, actor, action, detail=""):
    conn.execute(
        "INSERT INTO events(order_id, actor, action, detail, created_at) "
        "VALUES(?,?,?,?,?)",
        (order_id, actor, action, detail, time.time()),
    )


def _now(now):
    return time.time() if now is None else now


def get_order(conn, order_id):
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        raise ServiceError("工单不存在")
    return row


def order_detail(conn, order_id):
    order = dict(get_order(conn, order_id))
    if order.get("port_id"):
        p = conn.execute("SELECT * FROM ports WHERE id=?", (order["port_id"],)).fetchone()
        order["port"] = dict(p) if p else None
    else:
        order["port"] = None
    for table, key in (
        ("surveys", "surveys"),
        ("photos", "photos"),
        ("device_bindings", "devices"),
        ("speed_tests", "speed_tests"),
    ):
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        order[key] = [dict(r) for r in rows]
    order["events"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM events WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
    ]
    order["sms"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM sms_log WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
    ]
    return order


def _gen_order_no(conn):
    """用全局自增序列取号（原子提交），并发下绝不撞号。"""
    cur = conn.execute("INSERT INTO seq DEFAULT VALUES")
    return time.strftime("BB%Y%m%d-") + f"{cur.lastrowid:04d}"


def _insert_order(conn, fields, now):
    cur = conn.execute(
        "INSERT INTO orders(order_no,customer_name,customer_phone,address,room,plan,"
        "expect_window,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_gen_order_no(conn), *fields, now, now),
    )
    return cur


def _room_floor(room):
    """从房号猜楼层：1202 -> 12，3-501 -> 5。"""
    if not room:
        return None
    m = re.search(r"(\d{1,2})\d{2}(?!\d)", room)
    return int(m.group(1)) if m else None


# ---------------- 受理 ----------------

def create_order(conn, data, actor="受理员", now=None):
    """用户申请：地址、楼栋房号、期望时段、套餐。"""
    now = _now(now)
    for field, label in (
        ("customer_name", "用户姓名"),
        ("customer_phone", "联系电话"),
        ("address", "安装地址"),
        ("plan", "套餐"),
    ):
        if not str(data.get(field, "")).strip():
            raise ServiceError(f"缺少必填项：{label}")
    if data["plan"] not in PLANS:
        raise ServiceError("套餐无效")
    phone = str(data["customer_phone"]).strip()
    if not re.fullmatch(r"1\d{10}", phone):
        raise ServiceError("联系电话格式不正确（需为 11 位手机号）")

    cur = _insert_order(
        conn,
        (
            str(data["customer_name"]).strip(),
            phone,
            str(data["address"]).strip(),
            str(data.get("room", "")).strip(),
            data["plan"],
            str(data.get("expect_window", "")).strip(),
            "pending",
        ),
        now,
    )
    order_id = cur.lastrowid
    log_event(conn, order_id, actor, "新建申请", f"套餐 {data['plan']}")
    return get_order(conn, order_id)


def _match_building(conn, order):
    """受理员核查地址：地址文本需能匹配到资源楼栋。"""
    b = order["building_id"] and conn.execute(
        "SELECT * FROM buildings WHERE id=?", (order["building_id"],)
    ).fetchone()
    if b:
        return b
    text = (order["address"] or "").replace(" ", "")
    for row in conn.execute("SELECT * FROM buildings").fetchall():
        for token in (row["name"], row["address"]):
            if token and token.replace(" ", "") in text:
                return row
    return None


def _pick_free_port(conn, building_id, floor):
    """选一个空闲端口：优先同楼层，再就近楼层。调用方须已开启写事务。"""
    rows = conn.execute(
        "SELECT * FROM ports WHERE building_id=? AND status='free'", (building_id,)
    ).fetchall()
    if not rows:
        return None
    if floor is None:
        return rows[0]
    return min(rows, key=lambda r: (abs(r["floor"] - floor), r["floor"], r["port_no"]))


def check_resources(conn, order_id, actor="受理员", now=None):
    """受理员核查端口资源并预占。"""
    now = _now(now)
    order = get_order(conn, order_id)
    if order["status"] not in ("pending", "address_invalid", "shortage", "expired"):
        raise ServiceError(f"当前状态（{order['status']}）不能核查端口")

    building = _match_building(conn, order)
    if not building:
        conn.execute(
            "UPDATE orders SET status='address_invalid', address_verified=0, "
            "fail_reason=?, updated_at=? WHERE id=?",
            ("资源系统中查无此地址，请核对地址或联系资源建档", now, order_id),
        )
        log_event(conn, order_id, actor, "资源核查", "地址无法匹配楼栋")
        return get_order(conn, order_id)

    # BEGIN IMMEDIATE：同一时刻全库只有一个写事务，从根上杜绝两个工单拿到同端口。
    conn.execute("BEGIN IMMEDIATE")
    try:
        port = _pick_free_port(conn, building["id"], _room_floor(order["room"]))
        if port is None:
            conn.execute(
                "UPDATE orders SET building_id=?, address_verified=1, "
                "status='shortage', fail_reason=?, updated_at=? WHERE id=?",
                (building["id"], f"楼栋「{building['name']}」端口已满，等待扩容", now, order_id),
            )
            log_event(conn, order_id, actor, "资源核查", "端口已满，进入扩容队列")
            conn.execute("COMMIT")
            return get_order(conn, order_id)

        # 若工单原来占着端口（重复核查/改期再分配），先释放旧端口。
        if order["port_id"]:
            conn.execute(
                "UPDATE ports SET status='free', order_id=NULL, reserved_at=NULL, "
                "updated_at=? WHERE id=? AND status='reserved'",
                (now, order["port_id"]),
            )
        conn.execute(
            "UPDATE ports SET status='reserved', order_id=?, reserved_at=?, "
            "updated_at=? WHERE id=?",
            (order_id, now, now, port["id"]),
        )
        conn.execute(
            "UPDATE orders SET building_id=?, address_verified=1, status='scheduled', "
            "scheduled_window=COALESCE(scheduled_window, expect_window), "
            "fail_reason=NULL, port_id=?, updated_at=? WHERE id=?",
            (building["id"], port["id"], now, order_id),
        )
        log_event(
            conn,
            order_id,
            actor,
            "资源核查通过·端口预占",
            f"{building['name']} {port['splitter_no']}-P{port['port_no']}，"
            f"预占 {RESERVE_TTL_SECONDS // 60} 分钟",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    o = get_order(conn, order_id)
    conn.execute(
        "INSERT INTO sms_log(order_id,phone,content,created_at) VALUES(?,?,?,?)",
        (
            order_id,
            o["customer_phone"],
            f"【宽带受理】工单 {o['order_no']} 已核查通过"
            + (f"，预约时段：{o['scheduled_window']}" if o["scheduled_window"] else "")
            + "，端口已预占，请保持电话畅通。",
            now,
        ),
    )
    return o


def fix_address(conn, order_id, address, room=None, building_id=None, actor="受理员", now=None):
    """地址写错：修正地址/房号/楼栋后回到待核查。"""
    now = _now(now)
    order = get_order(conn, order_id)
    if order["status"] not in ("pending", "address_invalid"):
        raise ServiceError("仅待核查或地址异常的工单可以修改地址")
    conn.execute(
        "UPDATE orders SET address=?, room=COALESCE(?,room), building_id=?, "
        "address_verified=0, status='pending', updated_at=? WHERE id=?",
        (str(address).strip(), room, building_id, now, order_id),
    )
    log_event(conn, order_id, actor, "修正地址", str(address).strip())
    return check_resources(conn, order_id, actor=actor, now=now)


def reschedule(conn, order_id, window, actor="受理员", now=None):
    """预约改期 / 用户不在 / 预占超时后的重新预约：重新核查并预占端口。"""
    now = _now(now)
    order = get_order(conn, order_id)
    if not str(window).strip():
        raise ServiceError("请选择新的预约时段")
    if order["status"] not in ("reschedule", "expired", "shortage", "scheduled"):
        raise ServiceError("当前状态不支持改期")
    conn.execute(
        "UPDATE orders SET scheduled_window=?, status='pending', updated_at=? WHERE id=?",
        (str(window).strip(), now, order_id),
    )
    log_event(conn, order_id, actor, "预约改期", str(window).strip())
    return check_resources(conn, order_id, actor=actor, now=now)


def mark_customer_absent(conn, order_id, actor="工程师", now=None):
    """上门用户不在：记录并立即释放预占端口，等待改期。"""
    now = _now(now)
    order = get_order(conn, order_id)
    if order["status"] != "scheduled":
        raise ServiceError("仅已预约工单可以登记“用户不在”")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if order["port_id"]:
            conn.execute(
                "UPDATE ports SET status='free', order_id=NULL, reserved_at=NULL, "
                "updated_at=? WHERE id=? AND status='reserved'",
                (now, order["port_id"]),
            )
        conn.execute(
            "UPDATE orders SET status='reschedule', port_id=NULL, "
            "fail_reason='上门时用户不在，端口已释放，待改期', updated_at=? WHERE id=?",
            (now, order_id),
        )
        log_event(conn, order_id, actor, "上门无人", "释放预占端口，转待改期")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_order(conn, order_id)


def sweep_expired_reservations(conn, now=None):
    """预占超时自动释放。仅清扫 reserved；confirmed（已勘线确认）永不清扫。

    返回被释放的工单 id 列表。
    """
    now = _now(now)
    expired_orders = conn.execute(
        "SELECT o.id id FROM orders o JOIN ports p ON p.id=o.port_id "
        "WHERE o.status='scheduled' AND p.status='reserved' "
        "AND p.reserved_at IS NOT NULL AND ? - p.reserved_at > ?",
        (now, RESERVE_TTL_SECONDS),
    ).fetchall()
    released = []
    for row in expired_orders:
        oid = row["id"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            p = conn.execute(
                "SELECT * FROM ports WHERE id=(SELECT port_id FROM orders WHERE id=?)",
                (oid,),
            ).fetchone()
            # 双重检查：期间工程师可能已完成勘线（confirmed），此时不得释放。
            if p and p["status"] == "reserved":
                conn.execute(
                    "UPDATE ports SET status='free', order_id=NULL, reserved_at=NULL, "
                    "updated_at=? WHERE id=?",
                    (now, p["id"]),
                )
                conn.execute(
                    "UPDATE orders SET status='expired', port_id=NULL, "
                    "fail_reason='端口预占超时已自动释放，需重新核查', updated_at=? WHERE id=?",
                    (now, oid),
                )
                log_event(conn, oid, "系统", "预占超时", "自动释放端口")
                released.append(oid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return released


# ---------------- 现场勘线 ----------------

def submit_survey(conn, order_id, data, actor="工程师", now=None):
    """工程师上门：记录光纤入户路径、分光器端口、光功率。

    端口号与光功率一起提交、在前端同视区展示。功率越界即 power_failed；
    所报端口若是别的空闲端口，则从原预占端口切换到该端口。
    """
    now = _now(now)
    order = get_order(conn, order_id)
    if order["status"] not in ("scheduled", "power_failed", "surveyed"):
        raise ServiceError("当前状态不能提交勘线记录")
    fiber_path = str(data.get("fiber_path", "")).strip()
    splitter_no = str(data.get("splitter_no", "")).strip()
    if not fiber_path:
        raise ServiceError("请记录光纤入户路径")
    if not splitter_no:
        raise ServiceError("请填写分光器编号")
    try:
        port_no = int(data.get("port_no"))
        power = float(data.get("optical_power"))
    except (TypeError, ValueError):
        raise ServiceError("端口号与光功率格式不正确")
    if not (OPTICAL_RX_MIN <= power <= OPTICAL_RX_MAX):
        power_passed = False
    else:
        power_passed = True

    conn.execute("BEGIN IMMEDIATE")
    try:
        target = conn.execute(
            "SELECT * FROM ports WHERE building_id=? AND splitter_no=? AND port_no=?",
            (order["building_id"], splitter_no, port_no),
        ).fetchone()
        if not target:
            raise ServiceError(f"本楼栋不存在分光器 {splitter_no} 的端口 {port_no}")
        if target["order_id"] not in (None, order_id):
            raise ServiceError(
                f"分光器 {splitter_no} 端口 {port_no} 已分配给工单 "
                f"{_order_no_of(conn, target['order_id'])}，不能占用"
            )

        # 端口切换：原预占端口释放，新端口（工程师现场实际跳纤的端口）确认占用。
        if target["id"] != order["port_id"]:
            if target["status"] != "free":
                raise ServiceError("目标端口当前不可用")
            if order["port_id"]:
                conn.execute(
                    "UPDATE ports SET status='free', order_id=NULL, reserved_at=NULL, "
                    "updated_at=? WHERE id=? AND order_id=?",
                    (now, order["port_id"], order_id),
                )
        # 一经现场勘线，端口即从“预占”升级为“已确认”，超时清扫不再触碰。
        conn.execute(
            "UPDATE ports SET status='confirmed', order_id=?, reserved_at=NULL, "
            "updated_at=? WHERE id=?",
            (order_id, now, target["id"]),
        )
        conn.execute(
            "INSERT INTO surveys(order_id,fiber_path,splitter_no,port_no,optical_power,"
            "power_passed,engineer,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (order_id, fiber_path, splitter_no, port_no, power, int(power_passed), actor, now),
        )
        conn.execute(
            "UPDATE orders SET port_id=?, status=?, fail_reason=?, updated_at=? WHERE id=?",
            (
                target["id"],
                "surveyed" if power_passed else "power_failed",
                None
                if power_passed
                else f"收光 {power} dBm 超出阈值 [{OPTICAL_RX_MIN}, {OPTICAL_RX_MAX}] dBm，需整改",
                now,
                order_id,
            ),
        )
        log_event(
            conn,
            order_id,
            actor,
            "现场勘线" + ("·光功率达标" if power_passed else "·光功率不达标"),
            f"{splitter_no}-P{port_no}，收光 {power} dBm，路径：{fiber_path}",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_order(conn, order_id)


def _order_no_of(conn, order_id):
    r = conn.execute("SELECT order_no FROM orders WHERE id=?", (order_id,)).fetchone()
    return r["order_no"] if r else "?"


# ---------------- 设备绑定 ----------------

def bind_device(conn, order_id, router_sn, actor="工程师", now=None, replace=False):
    """绑定路由器编号；replace=True 表示设备更换（旧设备解绑、新设备绑定）。"""
    now = _now(now)
    sn = str(router_sn or "").strip().upper()
    if not sn:
        raise ServiceError("请录入路由器编号（SN）")
    order = get_order(conn, order_id)
    if order["status"] not in ("surveyed", "device_bound", "speed_failed", "speed_passed"):
        raise ServiceError("需先完成现场勘线才能绑定设备")

    conn.execute("BEGIN IMMEDIATE")
    try:
        active = conn.execute(
            "SELECT * FROM device_bindings WHERE order_id=? AND status='active'",
            (order_id,),
        ).fetchone()
        if active and not replace:
            raise ServiceError("该工单已绑定设备，如需更换请走“设备更换”")
        other = conn.execute(
            "SELECT o.order_no no FROM device_bindings d JOIN orders o ON o.id=d.order_id "
            "WHERE d.router_sn=? AND d.status='active'",
            (sn,),
        ).fetchone()
        if other:
            raise ServiceError(f"路由器 {sn} 已在工单 {other['no']} 上有效绑定，不能重复绑定")
        if replace:
            if not active:
                raise ServiceError("该工单尚无有效设备可更换")
            if active["router_sn"] == sn:
                raise ServiceError("新设备编号与当前设备相同")
            conn.execute(
                "UPDATE device_bindings SET status='replaced', replaced_at=? WHERE id=?",
                (now, active["id"]),
            )
        conn.execute(
            "INSERT INTO device_bindings(order_id,router_sn,status,bound_at) "
            "VALUES(?,?, 'active',?)",
            (order_id, sn, now),
        )
        # 更换设备后旧测速结果作废，回到待测速（若测速已通过也需重新测速）。
        new_status = "device_bound" if replace else "device_bound"
        conn.execute(
            "UPDATE orders SET status=?, fail_reason=NULL, updated_at=? WHERE id=?",
            (new_status, now, order_id),
        )
        log_event(
            conn, order_id, actor,
            "设备更换" if replace else "设备绑定",
            (f"{active['router_sn']} → {sn}" if replace else sn),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_order(conn, order_id)


# ---------------- 测速 ----------------

def run_speed_test(conn, order_id, actor="工程师", force=None, now=None):
    """模拟测速；达标阈值 = 套餐标称带宽 × 90%。通过后向用户发送签收验证码短信。"""
    now = _now(now)
    order = get_order(conn, order_id)
    if order["status"] not in ("device_bound", "speed_failed", "speed_passed"):
        raise ServiceError("需先绑定设备才能测速")
    plan = PLANS[order["plan"]]
    result = speed_adapter.run(order["plan"], force=force)
    th_down = round(plan["down"] * SPEED_PASS_RATIO, 1)
    th_up = round(plan["up"] * SPEED_PASS_RATIO, 1)
    passed = (
        result["down_mbps"] >= th_down
        and result["up_mbps"] >= th_up
        and result["ping_ms"] <= PING_MAX_MS
    )
    conn.execute(
        "INSERT INTO speed_tests(order_id,down_mbps,up_mbps,ping_ms,threshold_down,"
        "threshold_up,passed,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            order_id,
            result["down_mbps"],
            result["up_mbps"],
            result["ping_ms"],
            th_down,
            th_up,
            int(passed),
            now,
        ),
    )
    if passed:
        # 旧验证码作废，重新下发。
        conn.execute("UPDATE signoff_codes SET consumed=1 WHERE order_id=? AND consumed=0", (order_id,))
        code = f"{sms_adapter._rng.randint(0, 999999):06d}"
        conn.execute(
            "INSERT INTO signoff_codes(order_id,code,expires_at,created_at) VALUES(?,?,?,?)",
            (order_id, code, now + SIGNOFF_CODE_TTL_SECONDS, now),
        )
        conn.execute(
            "INSERT INTO sms_log(order_id,phone,content,created_at) VALUES(?,?,?,?)",
            (
                order_id,
                order["customer_phone"],
                f"【宽带开通】工单 {order['order_no']} 测速已通过，"
                f"您的签收验证码为 {code}，{SIGNOFF_CODE_TTL_SECONDS // 60} 分钟内有效。"
                "签收须由您本人完成，工程师无权代签。",
                now,
            ),
        )
        conn.execute(
            "UPDATE orders SET status='speed_passed', fail_reason=NULL, updated_at=? WHERE id=?",
            (now, order_id),
        )
        log_event(conn, order_id, actor, "测速通过", f"下行 {result['down_mbps']} Mbps，验证码已短信发送用户")
    else:
        conn.execute(
            "UPDATE orders SET status='speed_failed', "
            "fail_reason=?, updated_at=? WHERE id=?",
            (
                f"测速未达阈值（下行≥{th_down} / 上行≥{th_up} / 时延≤{PING_MAX_MS}ms）",
                now,
                order_id,
            ),
        )
        log_event(conn, order_id, actor, "测速不达标", f"下行 {result['down_mbps']}，未通过，不能开通")
    return get_order(conn, order_id), dict(result, passed=passed, threshold_down=th_down, threshold_up=th_up)


def resend_signoff_code(conn, order_id, actor="用户", now=None):
    now = _now(now)
    order = get_order(conn, order_id)
    if order["status"] != "speed_passed":
        raise ServiceError("仅测速通过待签收的工单可以索取验证码")
    conn.execute("UPDATE signoff_codes SET consumed=1 WHERE order_id=? AND consumed=0", (order_id,))
    code = f"{sms_adapter._rng.randint(0, 999999):06d}"
    conn.execute(
        "INSERT INTO signoff_codes(order_id,code,expires_at,created_at) VALUES(?,?,?,?)",
        (order_id, code, now + SIGNOFF_CODE_TTL_SECONDS, now),
    )
    conn.execute(
        "INSERT INTO sms_log(order_id,phone,content,created_at) VALUES(?,?,?,?)",
        (
            order_id,
            order["customer_phone"],
            f"【宽带开通】您的签收验证码为 {code}，{SIGNOFF_CODE_TTL_SECONDS // 60} 分钟内有效，"
            "请由本人确认签收。",
            now,
        ),
    )
    log_event(conn, order_id, actor, "重发签收码", "")
    return code


# ---------------- 签收 ----------------

def signoff(conn, order_id, code, role, actor="用户", now=None):
    """用户现场确认测速结果并签收开通。工程师角色一律拒绝。"""
    now = _now(now)
    if role != ROLE_CUSTOMER:
        raise ServiceError("工程师不能代替用户签收，签收必须由用户本人完成")
    order = get_order(conn, order_id)
    if order["status"] != "speed_passed":
        raise ServiceError("未通过测速的工单不能签收开通")
    code = str(code or "").strip()
    row = conn.execute(
        "SELECT * FROM signoff_codes WHERE order_id=? AND code=? AND consumed=0",
        (order_id, code),
    ).fetchone()
    if not row:
        raise ServiceError("验证码无效")
    if row["expires_at"] < now:
        raise ServiceError("验证码已过期，请重新获取")
    conn.execute("UPDATE signoff_codes SET consumed=1 WHERE id=?", (row["id"],))
    conn.execute(
        "UPDATE orders SET status='activated', activated_at=?, updated_at=? WHERE id=?",
        (now, now, order_id),
    )
    log_event(conn, order_id, actor, "用户签收·开通", f"用户本人验证码确认，工单 {order['order_no']} 正式开通")
    return get_order(conn, order_id)
