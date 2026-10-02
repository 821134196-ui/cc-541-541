"""业务服务层：工单状态机、端口资源、勘线、设备、测速、签收。

关键业务约束：
* 一个端口同一时刻只能分配一户（free -> reserved -> confirmed，原子 UPDATE）。
* 预占超时自动释放；confirmed 端口永不自动释放，后到申请不可占用。
* 工程师不能替用户签收；测速不达标 / 光功率不达标不能开通。
"""
import os
import re
import time
import uuid

from . import config
from .adapters import SPEED, SMS, make_accept_code
from .db import ALLOC_LOCK


class OrderError(Exception):
    """业务规则违例（含可展示给页面的中文信息）。"""


# ---------------------------------------------------------------- 内部工具

def _now():
    return time.time()


def _log(conn, order_id, actor, action, detail=""):
    conn.execute(
        "INSERT INTO events(order_id, at, actor, action, detail) VALUES (?,?,?,?,?)",
        (order_id, _now(), actor, action, detail),
    )


def _set_status(conn, order_id, status):
    conn.execute(
        "UPDATE orders SET status=?, updated_at=? WHERE id=?",
        (status, _now(), order_id),
    )


def get_order(conn, order_id):
    row = conn.execute(
        """SELECT o.*, p.name AS plan_name, p.bandwidth_mbps, p.monthly_fee,
                  b.name AS building_name, s.name AS splitter_name
             FROM orders o
             LEFT JOIN plans p ON p.id = o.plan_id
             LEFT JOIN buildings b ON b.id = o.building_id
             LEFT JOIN splitters s ON s.id = o.splitter_id
            WHERE o.id=?""",
        (order_id,),
    ).fetchone()
    if not row:
        raise OrderError("工单不存在")
    order = dict(row)
    order["events"] = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE order_id=? ORDER BY id", (order_id,))]
    order["photos"] = [dict(r) for r in conn.execute(
        "SELECT * FROM photos WHERE order_id=? ORDER BY id", (order_id,))]
    order["optical_thresholds"] = {
        "rx_min_dbm": config.OPTICAL_RX_MIN_DBM, "rx_max_dbm": config.OPTICAL_RX_MAX_DBM}
    order["speed_threshold"] = {
        "down_mbps": round((order["bandwidth_mbps"] or 0) * config.SPEED_RATIO_REQUIRED, 1),
        "ratio": config.SPEED_RATIO_REQUIRED}
    if order["status"] == "surveyed":
        order["accept_code"] = make_accept_code(order_id)  # 演示环境：展示模拟短信验证码
    return order


def list_orders(conn, status=None, key=None):
    sql = """SELECT o.*, p.name AS plan_name, p.bandwidth_mbps, b.name AS building_name
               FROM orders o LEFT JOIN plans p ON p.id=o.plan_id
               LEFT JOIN buildings b ON b.id=o.building_id"""
    where, args = [], []
    if status:
        where.append("o.status=?")
        args.append(status)
    if key:
        where.append("(o.order_no LIKE ? OR o.customer_phone LIKE ? OR o.customer_name LIKE ?)")
        args += [f"%{key}%", f"%{key}%", f"%{key}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.id DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def resolve_building(conn, address):
    """按标准地址/楼栋名模糊匹配；匹配不到返回 None（地址写错）。"""
    if not address:
        return None
    kw = address.strip()
    rows = conn.execute("SELECT * FROM buildings").fetchall()
    for r in rows:
        if r["address"] == kw or r["name"] in kw or kw in r["address"] or kw in r["name"]:
            return dict(r)
    return None


def _gen_order_no(conn):
    ts = time.strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:4].upper()
    return f"BB{ts}{rand}"


# ---------------------------------------------------------------- 资源核查 / 预占

def sweep_expired(conn, now=None):
    """自动释放预占超时的端口（confirmed 永不释放；已排期端口由预约保留）。"""
    now = now if now is not None else _now()
    deadline = now - config.PORT_RESERVE_TIMEOUT_SECONDS
    released = []
    with ALLOC_LOCK:
        rows = conn.execute(
            """SELECT p.id AS port_id, p.order_id FROM ports p
               JOIN orders o ON o.id = p.order_id
              WHERE p.status='reserved' AND p.reserved_at IS NOT NULL
                AND p.reserved_at < ? AND o.status='reserved'""",
            (deadline,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE ports SET status='free', order_id=NULL, reserved_at=NULL WHERE id=?",
                (r["port_id"],))
            _set_status(conn, r["order_id"], "expired")
            _log(conn, r["order_id"], "system", "预占超时释放",
                 f"端口 {r['port_id']} 预占超过 "
                 f"{config.PORT_RESERVE_TIMEOUT_SECONDS // 60} 分钟，已自动释放")
            released.append(dict(r))
        if released:
            conn.commit()
    return released


def _allocate_port(conn, building_id, order_id, now):
    """在指定楼栋内原子抢占一个 free 端口；成功返回 port 行，无端口返回 None。

    优先选择与房号楼层接近的分光器；抢占用
    UPDATE ... WHERE id=? AND status='free' 保证并发下只有一户成功。
    """
    with ALLOC_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            port = conn.execute(
                """SELECT p.* FROM ports p
                   JOIN splitters s ON s.id = p.splitter_id
                  WHERE s.building_id=? AND p.status='free'
                  ORDER BY s.floor, p.port_no LIMIT 1""",
                (building_id,),
            ).fetchone()
            if not port:
                conn.commit()
                return None
            cur = conn.execute(
                "UPDATE ports SET status='reserved', order_id=?, reserved_at=? "
                "WHERE id=? AND status='free'",
                (order_id, now, port["id"]))
            if cur.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return dict(port)
        except Exception:
            conn.rollback()
            raise


def accept_order(conn, customer_name, customer_phone, address, room_no,
                 plan_id, slot=None):
    """受理新装申请：建工单 -> 核查地址 -> 核查端口并预占。"""
    if not all([customer_name, customer_phone, address, room_no, plan_id]):
        raise OrderError("姓名、电话、安装地址、楼栋房号、套餐均为必填")
    now = _now()
    sweep_expired(conn, now)
    building = resolve_building(conn, address)

    cur = conn.execute(
        """INSERT INTO orders(order_no, customer_name, customer_phone, address,
              building_id, room_no, plan_id, status, slot, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (_gen_order_no(conn), customer_name, customer_phone, address,
         building["id"] if building else None, room_no, plan_id,
         "draft", slot, now, now))
    oid = cur.lastrowid
    _log(conn, oid, "dispatcher", "受理新装申请",
         f"地址：{address} {room_no}")

    if not building:
        _set_status(conn, oid, "address_error")
        _log(conn, oid, "dispatcher", "地址核查不通过",
             "标准地址库未匹配到该楼栋，请核对地址")
        conn.commit()
        return get_order(conn, oid)

    port = _allocate_port(conn, building["id"], oid, now)
    if port is None:
        _set_status(conn, oid, "rejected")
        _log(conn, oid, "dispatcher", "端口资源核查", "该楼栋分光器端口已满")
        conn.commit()
        return get_order(conn, oid)

    _set_status(conn, oid, "reserved")
    _log(conn, oid, "dispatcher", "端口预占",
         f"端口 {port['id']} 已预占，"
         f"{config.PORT_RESERVE_TIMEOUT_SECONDS // 60} 分钟内完成预约")
    conn.commit()
    return get_order(conn, oid)


def fix_address(conn, order_id, new_address):
    """受理员更正写错的地址并重新核查资源。"""
    o = get_order(conn, order_id)
    if o["status"] != "address_error":
        raise OrderError("仅地址核查不通过的工单可以更正地址")
    building = resolve_building(conn, new_address)
    now = _now()
    conn.execute("UPDATE orders SET address=?, building_id=?, updated_at=? WHERE id=?",
                 (new_address, building["id"] if building else None, now, order_id))
    _log(conn, order_id, "dispatcher", "更正地址", new_address)
    if not building:
        conn.commit()
        raise OrderError("更正后的地址仍无法匹配标准地址库")
    port = _allocate_port(conn, building["id"], order_id, now)
    if port is None:
        _set_status(conn, order_id, "rejected")
        _log(conn, order_id, "dispatcher", "端口资源核查", "该楼栋分光器端口已满")
    else:
        _set_status(conn, order_id, "reserved")
        _log(conn, order_id, "dispatcher", "端口预占",
             f"端口 {port['id']} 已预占")
    conn.commit()
    return get_order(conn, order_id)


def recheck_resource(conn, order_id):
    """预占超时/资源已满工单在有端口释放后重新核查。"""
    o = get_order(conn, order_id)
    if o["status"] not in ("expired", "rejected"):
        raise OrderError("当前状态不支持重新核查资源")
    if not o["building_id"]:
        raise OrderError("缺少楼栋信息，请先更正地址")
    now = _now()
    sweep_expired(conn, now)
    port = _allocate_port(conn, o["building_id"], order_id, now)
    if port is None:
        _log(conn, order_id, "dispatcher", "重新核查资源", "仍无可用端口")
        conn.commit()
        raise OrderError("该楼栋仍无可用端口")
    _set_status(conn, order_id, "reserved")
    _log(conn, order_id, "dispatcher", "端口重新预占", f"端口 {port['id']}")
    conn.commit()
    return get_order(conn, order_id)


# ---------------------------------------------------------------- 排程 / 改期

def schedule(conn, order_id, visit_date, slot, engineer):
    o = get_order(conn, order_id)
    if o["status"] != "reserved":
        raise OrderError("仅已预占端口的工单可以预约排期")
    _validate_slot(visit_date, slot, engineer)
    conn.execute(
        "UPDATE orders SET visit_date=?, slot=?, engineer=?, updated_at=? WHERE id=?",
        (visit_date, slot, engineer, _now(), order_id))
    # 排期成功后预占计时从预约时刻重新起算
    conn.execute("UPDATE ports SET reserved_at=? WHERE order_id=? AND status='reserved'",
                 (_now(), order_id))
    _set_status(conn, order_id, "scheduled")
    _log(conn, order_id, "dispatcher", "预约排期",
         f"{visit_date} {slot}，工程师 {engineer}")
    conn.commit()
    SMS.send(conn, o["customer_phone"],
             f"【宽带新装】您的工单 {o['order_no']} 已预约 {visit_date} {slot}，"
             f"工程师 {engineer} 将上门安装，请留人在场。")
    return get_order(conn, order_id)


def mark_user_absent(conn, order_id):
    """上门时用户不在：登记异常，等待改期（端口继续保留）。"""
    o = get_order(conn, order_id)
    if o["status"] not in config.VISITABLE_STATUSES:
        raise OrderError("仅已排期工单可以登记用户不在")
    _set_status(conn, order_id, "user_absent")
    _log(conn, order_id, "engineer", "用户不在", "上门无人应答，已联系待改期")
    conn.commit()
    SMS.send(conn, o["customer_phone"],
             f"【宽带新装】工单 {o['order_no']} 上门时您不在场，请联系受理台改期。")
    return get_order(conn, order_id)


def reschedule(conn, order_id, visit_date, slot):
    o = get_order(conn, order_id)
    if o["status"] not in ("scheduled", "user_absent", "rescheduled"):
        raise OrderError("当前状态不能改期")
    _validate_slot(visit_date, slot, o["engineer"] or "待派工")
    conn.execute(
        "UPDATE orders SET visit_date=?, slot=?, updated_at=? WHERE id=?",
        (visit_date, slot, _now(), order_id))
    conn.execute("UPDATE ports SET reserved_at=? WHERE order_id=? AND status='reserved'",
                 (_now(), order_id))
    _set_status(conn, order_id, "rescheduled")
    _log(conn, order_id, "dispatcher", "预约改期", f"{visit_date} {slot}")
    conn.commit()
    SMS.send(conn, o["customer_phone"],
             f"【宽带新装】工单 {o['order_no']} 已改期至 {visit_date} {slot}。")
    return get_order(conn, order_id)


def _validate_slot(visit_date, slot, engineer):
    if not visit_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", visit_date):
        raise OrderError("预约日期格式应为 YYYY-MM-DD")
    if slot not in config.TIME_SLOTS:
        raise OrderError(f"时段必须是 {config.TIME_SLOTS} 之一")
    if not engineer:
        raise OrderError("必须指派工程师")


# ---------------------------------------------------------------- 现场勘线

def save_photo(conn, order_id, filename, data, caption=""):
    get_order(conn, order_id)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        raise OrderError("照片仅支持 jpg/png/webp/gif")
    folder = os.path.join(config.PHOTO_DIR, f"order_{order_id}")
    os.makedirs(folder, exist_ok=True)
    safe = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    with open(os.path.join(folder, safe), "wb") as f:
        f.write(data)
    conn.execute(
        "INSERT INTO photos(order_id, filename, caption, created_at) VALUES (?,?,?,?)",
        (order_id, safe, caption, _now()))
    conn.commit()
    return safe


def optical_ok(rx_dbm):
    return rx_dbm is not None and config.OPTICAL_RX_MIN_DBM <= rx_dbm <= config.OPTICAL_RX_MAX_DBM


def survey(conn, order_id, fiber_path, splitter_id, splitter_port,
           optical_rx, photos=None):
    """工程师现场勘线：记录入户路径、分光器端口与光功率。

    光功率合格 -> reserved 端口原子确认为 confirmed，工单进入 surveyed；
    不达标 -> optical_failed，端口保持预占，整改后可再次勘线。
    """
    o = get_order(conn, order_id)
    if o["status"] not in config.VISITABLE_STATUSES + config.RECTIFIABLE_STATUSES:
        raise OrderError("当前状态不允许现场勘线（请先完成排期）")
    if not fiber_path or not splitter_id or splitter_port is None:
        raise OrderError("入户路径、分光器与端口号必须填写")
    try:
        optical_rx = float(optical_rx)
    except (TypeError, ValueError):
        raise OrderError("光功率读数无效")

    sp = conn.execute(
        "SELECT * FROM splitters WHERE id=?", (splitter_id,)).fetchone()
    if not sp:
        raise OrderError("分光器不存在")
    port = conn.execute(
        "SELECT * FROM ports WHERE splitter_id=? AND port_no=?",
        (splitter_id, splitter_port)).fetchone()
    if not port:
        raise OrderError(f"分光器 {sp['name']} 上不存在端口 {splitter_port}")
    # 只能确认本工单预占的端口：他人已确认端口、空闲端口一律不允许
    if port["order_id"] != order_id or port["status"] not in ("reserved", "confirmed"):
        raise OrderError(
            f"{sp['name']} 端口 {splitter_port} 不属于本工单"
            f"（当前状态 {port['status']}），一户一端口，不能占用")

    def _save_photos(caption_default):
        for item in photos or []:
            save_photo(conn, order_id, item["filename"], item["data"],
                       item.get("caption", caption_default))

    conn.execute(
        """UPDATE orders SET fiber_path=?, splitter_id=?, splitter_port=?,
              optical_rx_dbm=?, updated_at=? WHERE id=?""",
        (fiber_path, splitter_id, splitter_port, optical_rx, _now(), order_id))

    if not optical_ok(optical_rx):
        _set_status(conn, order_id, "optical_failed")
        _log(conn, order_id, "engineer", "光功率不达标",
             f"实测 {optical_rx} dBm，合格区间 "
             f"[{config.OPTICAL_RX_MIN_DBM}, {config.OPTICAL_RX_MAX_DBM}] dBm")
        conn.commit()
        _save_photos("光功率不达标现场照")
        raise OrderError(
            f"光功率 {optical_rx} dBm 不达标（阈值 "
            f"{config.OPTICAL_RX_MIN_DBM}~{config.OPTICAL_RX_MAX_DBM} dBm），整改后复测")

    # 光功率合格：原子确认属于本工单的预占端口（已被超时释放则失败）
    with ALLOC_LOCK:
        cur = conn.execute(
            "UPDATE ports SET status='confirmed' "
            "WHERE id=? AND order_id=? AND status='reserved'",
            (port["id"], order_id))
        if cur.rowcount != 1 and port["status"] != "confirmed":
            conn.rollback()
            raise OrderError("端口预占已失效（可能超时释放），请重新核查资源后再上门")
        _set_status(conn, order_id, "surveyed")
        _log(conn, order_id, "engineer", "现场勘线完成",
             f"路径：{fiber_path}；{sp['name']} 端口 {splitter_port}；"
             f"光功率 {optical_rx} dBm；端口已确认占用")
        conn.commit()
    _save_photos("勘线现场照")
    return get_order(conn, order_id)


# ---------------------------------------------------------------- 设备绑定 / 换绑

def bind_device(conn, order_id, router_sn):
    o = get_order(conn, order_id)
    if o["status"] != "surveyed":
        raise OrderError("勘线合格后才能绑定设备")
    return _do_bind(conn, o, router_sn, action="设备绑定")


def rebind_device(conn, order_id, new_sn):
    """设备更换换绑：旧机回库，新机绑定，原子完成。"""
    o = get_order(conn, order_id)
    if o["status"] not in ("surveyed", "speed_failed"):
        raise OrderError("仅安装中的工单可以更换设备")
    if not o["router_sn"]:
        raise OrderError("该工单尚未绑定设备，请直接绑定")
    old_sn = o["router_sn"]
    with ALLOC_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            new = conn.execute(
                "SELECT * FROM devices WHERE router_sn=? AND status='stock'",
                (new_sn,)).fetchone()
            if not new:
                conn.rollback()
                raise OrderError(f"新设备 {new_sn} 不在库存或已被占用")
            conn.execute(
                "UPDATE devices SET status='stock', order_id=NULL, bound_at=NULL "
                "WHERE router_sn=?", (old_sn,))
            cur = conn.execute(
                "UPDATE devices SET status='bound', order_id=?, bound_at=? "
                "WHERE router_sn=? AND status='stock'",
                (order_id, _now(), new_sn))
            if cur.rowcount != 1:
                conn.rollback()
                raise OrderError(f"新设备 {new_sn} 绑定失败")
            conn.execute("UPDATE orders SET router_sn=?, updated_at=? WHERE id=?",
                         (new_sn, _now(), order_id))
            _log(conn, order_id, "engineer", "设备换绑",
                 f"{old_sn} -> {new_sn}（旧机已回库）")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return get_order(conn, order_id)


def _do_bind(conn, o, router_sn, action):
    if not router_sn:
        raise OrderError("路由器编号必填")
    with ALLOC_LOCK:
        cur = conn.execute(
            "UPDATE devices SET status='bound', order_id=?, bound_at=? "
            "WHERE router_sn=? AND status='stock'",
            (o["id"], _now(), router_sn))
        if cur.rowcount != 1:
            raise OrderError(f"设备 {router_sn} 不存在或已被其他工单绑定")
        conn.execute("UPDATE orders SET router_sn=?, updated_at=? WHERE id=?",
                     (router_sn, _now(), o["id"]))
        _log(conn, o["id"], "engineer", action, f"路由器 SN：{router_sn}")
        conn.commit()
    return get_order(conn, o["id"])


# ---------------------------------------------------------------- 测速 / 签收

def run_speed_test(conn, order_id, force_fail=False):
    o = get_order(conn, order_id)
    if o["status"] not in ("surveyed", "speed_failed"):
        raise OrderError("勘线合格并绑定设备后才能测速")
    if not o["router_sn"]:
        raise OrderError("请先绑定路由器再测速")
    result = SPEED.run(o["bandwidth_mbps"], force_fail=force_fail)
    conn.execute(
        "UPDATE orders SET speed_down_mbps=?, speed_up_mbps=?, updated_at=? WHERE id=?",
        (result["down_mbps"], result["up_mbps"], _now(), order_id))
    if result["passed"]:
        _set_status(conn, order_id, "surveyed")
        _log(conn, order_id, "engineer", "测速通过",
             f"下行 {result['down_mbps']} / 上行 {result['up_mbps']} Mbps，"
             f"阈值下行 {result['down_threshold']} Mbps")
        conn.commit()
        SMS.send(conn, o["customer_phone"],
                 f"【宽带新装】工单 {o['order_no']} 测速达标（"
                 f"{result['down_mbps']}Mbps），验证码 {make_accept_code(order_id)}，"
                 f"请本人在验收页确认签收。")
    else:
        _set_status(conn, order_id, "speed_failed")
        _log(conn, order_id, "engineer", "测速不达标",
             f"下行 {result['down_mbps']} < 阈值 {result['down_threshold']} Mbps")
        conn.commit()
        SMS.send(conn, o["customer_phone"],
                 f"【宽带新装】工单 {o['order_no']} 测速未达标，工程师正在整改，请等待复测。")
    out = get_order(conn, order_id)
    out["speed_result"] = result
    return out


def customer_accept(conn, order_id, actor, code):
    """用户本人验收签收。工程师代签一律拒绝；未通过测速一律拒绝。"""
    if actor != "customer":
        raise OrderError("工程师不能替用户签收，必须用户本人确认")
    o = get_order(conn, order_id)
    if not code or str(code) != make_accept_code(order_id):
        raise OrderError("签收验证码不正确（模拟短信中的 6 位验证码）")
    if o["status"] != "surveyed":
        raise OrderError(f"当前状态（{o['status']}）不能签收")
    threshold = round(o["bandwidth_mbps"] * config.SPEED_RATIO_REQUIRED, 1)
    if o["speed_down_mbps"] is None or o["speed_down_mbps"] < threshold:
        raise OrderError("测速未达标，不能开通")
    if not o["router_sn"]:
        raise OrderError("设备未绑定，不能开通")
    if not optical_ok(o["optical_rx_dbm"]):
        raise OrderError("光功率不达标，不能开通")
    port = conn.execute(
        "SELECT p.status FROM ports p JOIN orders o ON o.id=p.order_id WHERE o.id=?",
        (order_id,)).fetchone()
    if not port or port["status"] != "confirmed":
        raise OrderError("端口未确认占用，不能开通")

    _set_status(conn, order_id, "activated")
    conn.execute("UPDATE orders SET confirmed_at=? WHERE id=?", (_now(), order_id))
    _log(conn, order_id, "customer", "用户签收",
         f"本人确认测速 {o['speed_down_mbps']}Mbps，验收通过，正式开通")
    conn.commit()
    SMS.send(conn, o["customer_phone"],
             f"【宽带新装】工单 {o['order_no']} 已验收开通，欢迎使用。")
    return get_order(conn, order_id)


# ---------------------------------------------------------------- 资源地图

def resource_map(conn):
    """返回楼栋-楼层-分光器-端口树（先做一次超时释放）。"""
    sweep_expired(conn)
    buildings = []
    for b in conn.execute("SELECT * FROM buildings ORDER BY id"):
        bd = dict(b)
        bd["floors_data"] = []
        total = free = reserved = confirmed = 0
        for f in range(bd["floors"], 0, -1):
            splitters = []
            for s in conn.execute(
                    "SELECT * FROM splitters WHERE building_id=? AND floor=? ORDER BY id",
                    (b["id"], f)):
                ports = [dict(r) for r in conn.execute(
                    "SELECT * FROM ports WHERE splitter_id=? ORDER BY port_no",
                    (s["id"],))]
                st = {"splitters": [], "ports": ports}
                st_counts = {"free": 0, "reserved": 0, "confirmed": 0}
                for p in ports:
                    st_counts[p["status"]] = st_counts.get(p["status"], 0) + 1
                sd = dict(s)
                sd["ports"] = ports
                sd["counts"] = st_counts
                splitters.append(sd)
                total += len(ports)
                free += st_counts["free"]
                reserved += st_counts["reserved"]
                confirmed += st_counts["confirmed"]
            if splitters:
                bd["floors_data"].append({"floor": f, "splitters": splitters})
        bd["counts"] = {"total": total, "free": free,
                        "reserved": reserved, "confirmed": confirmed}
        buildings.append(bd)
    return buildings
