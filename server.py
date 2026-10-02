# -*- coding: utf-8 -*-
"""零依赖 HTTP 服务：REST API + 静态页面 + 照片 + 预占超时清扫。

启动：python3 server.py   （默认 0.0.0.0:8000，可用 BB_PORT 覆盖）
角色：请求头 X-Role: clerk | engineer | customer（默认 clerk）。
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import core
import seed as seed_mod
from config import (
    HOST,
    OPTICAL_RX_MAX,
    OPTICAL_RX_MIN,
    PHOTO_DIR,
    PLANS,
    PORT,
    PORT_STATUS,
    ROLE_CLERK,
    ROLE_CUSTOMER,
    ROLE_ENGINEER,
    STATUS,
    STATIC_DIR,
    SWEEP_INTERVAL_SECONDS,
)
from db import get_conn, init_db

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
}

_sweep_lock = threading.Lock()


def sweeper_loop():
    """后台线程：周期性自动释放预占超时端口。"""
    while True:
        time.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            with _sweep_lock:
                conn = get_conn()
                try:
                    released = core.sweep_expired_reservations(conn)
                    if released:
                        print(f"[sweep] 预占超时释放工单：{released}")
                finally:
                    conn.close()
        except Exception as exc:  # 清扫异常不能杀死线程
            print(f"[sweep] 错误：{exc}")


def building_view(conn, building_id):
    b = conn.execute("SELECT * FROM buildings WHERE id=?", (building_id,)).fetchone()
    if not b:
        return None
    floors = {}
    ports = conn.execute(
        "SELECT p.*, o.order_no, o.customer_name, o.status order_status "
        "FROM ports p LEFT JOIN orders o ON o.id=p.order_id "
        "WHERE p.building_id=? ORDER BY p.floor, p.splitter_no, p.port_no",
        (building_id,),
    ).fetchall()
    for p in ports:
        floors.setdefault(p["floor"], []).append(dict(p))
    return {
        "id": b["id"],
        "name": b["name"],
        "address": b["address"],
        "floors": [{"floor": f, "ports": floors[f]} for f in sorted(floors, reverse=True)],
        "stats": {
            s: sum(1 for p in ports if p["status"] == s) for s in PORT_STATUS
        },
        "total": len(ports),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "BroadbandDemo/1.0"

    # ---------- 工具 ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, code=400):
        self._send_json({"error": message}, code)

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise core.ServiceError("请求体不是合法 JSON")

    def _role(self):
        role = self.headers.get("X-Role", ROLE_CLERK)
        if role not in (ROLE_CLERK, ROLE_ENGINEER, ROLE_CUSTOMER):
            raise core.ServiceError("未知角色（X-Role 需为 clerk/engineer/customer）")
        return role

    def _actor(self, role):
        return {"clerk": "受理员", "engineer": "工程师", "customer": "用户"}[role]

    def _static(self, rel):
        rel = rel.lstrip("/") or "index.html"
        if rel == "photos":
            return None
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            return None
        return path

    def log_message(self, fmt, *args):
        print(f"[{self.command}] {self.path} {args[1] if len(args) > 1 else ''}")

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                return self._api_get(path)
            if path.startswith("/photos/"):
                return self._serve_photo(path[len("/photos/"):])
            static = self._static(path)
            if static:
                return self._serve_file(static)
            self._error("未找到", 404)
        except core.ServiceError as exc:
            self._error(str(exc))
        except Exception as exc:
            self._error(f"服务器错误：{exc}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                return self._api_post(path)
            self._error("未找到", 404)
        except core.ServiceError as exc:
            self._error(str(exc))
        except Exception as exc:
            self._error(f"服务器错误：{exc}", 500)

    # ---------- GET API ----------
    def _api_get(self, path):
        conn = get_conn()
        try:
            if path == "/api/meta":
                return self._send_json({
                    "plans": PLANS,
                    "status": STATUS,
                    "port_status": PORT_STATUS,
                    "optical": {"rx_min": OPTICAL_RX_MIN, "rx_max": OPTICAL_RX_MAX},
                    "roles": {"clerk": "受理员", "engineer": "工程师", "customer": "用户"},
                })
            if path == "/api/buildings":
                rows = conn.execute("SELECT * FROM buildings ORDER BY id").fetchall()
                return self._send_json([building_view(conn, b["id"]) for b in rows])
            m = re.fullmatch(r"/api/buildings/(\d+)", path)
            if m:
                view = building_view(conn, int(m.group(1)))
                return self._send_json(view) if view else self._error("楼栋不存在", 404)
            if path == "/api/orders":
                q = urlparse(self.path).query
                params = dict(pair.split("=", 1) for pair in q.split("&") if "=" in pair)
                sql = (
                    "SELECT o.*, b.name building_name FROM orders o "
                    "LEFT JOIN buildings b ON b.id=o.building_id"
                )
                where, args = [], []
                if params.get("status"):
                    where.append("o.status=?")
                    args.append(params["status"])
                if params.get("building"):
                    where.append("o.building_id=?")
                    args.append(params["building"])
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY o.id DESC"
                return self._send_json([dict(r) for r in conn.execute(sql, args).fetchall()])
            m = re.fullmatch(r"/api/orders/(\d+)", path)
            if m:
                return self._send_json(core.order_detail(conn, int(m.group(1))))
            m = re.fullmatch(r"/api/orders/(\d+)/events", path)
            if m:
                rows = conn.execute(
                    "SELECT * FROM events WHERE order_id=? ORDER BY id", (int(m.group(1)),)
                ).fetchall()
                return self._send_json([dict(r) for r in rows])
            self._error("接口不存在", 404)
        finally:
            conn.close()

    # ---------- POST API ----------
    def _api_post(self, path):
        role = self._role()
        actor = self._actor(role)
        conn = get_conn()
        try:
            data = None

            def payload():
                nonlocal data
                if data is None:
                    data = self._body_json()
                return data

            m = re.fullmatch(r"/api/orders", path)
            if m:
                order = core.create_order(conn, payload(), actor=actor)
                return self._send_json(dict(order), 201)

            m = re.fullmatch(r"/api/orders/(\d+)/(\w[\w-]*)", path)
            if not m:
                return self._error("接口不存在", 404)
            oid, action = int(m.group(1)), m.group(2)

            if action == "check":
                if role not in ("clerk", "engineer"):
                    raise core.ServiceError("该操作仅受理员可执行")
                return self._send_json(dict(core.check_resources(conn, oid, actor=actor)))
            if action == "address":
                d = payload()
                return self._send_json(dict(core.fix_address(
                    conn, oid, d["address"], d.get("room"), d.get("building_id"), actor=actor
                )))
            if action == "reschedule":
                return self._send_json(dict(core.reschedule(
                    conn, oid, payload()["window"], actor=actor
                )))
            if action == "absent":
                if role not in ("clerk", "engineer"):
                    raise core.ServiceError("该操作仅工作人员可执行")
                return self._send_json(dict(core.mark_customer_absent(conn, oid, actor=actor)))
            if action == "survey":
                if role != "engineer":
                    raise core.ServiceError("现场勘线仅工程师可提交")
                return self._send_json(dict(core.submit_survey(conn, oid, payload(), actor=actor)))
            if action == "photos":
                return self._upload_photo(conn, oid)
            if action == "device":
                if role != "engineer":
                    raise core.ServiceError("设备绑定仅工程师可操作")
                return self._send_json(dict(core.bind_device(
                    conn, oid, payload()["router_sn"], actor=actor
                )))
            if action == "device-replace":
                if role != "engineer":
                    raise core.ServiceError("设备更换仅工程师可操作")
                return self._send_json(dict(core.bind_device(
                    conn, oid, payload()["router_sn"], actor=actor, replace=True
                )))
            if action == "speed-test":
                if role != "engineer":
                    raise CoreRoleError()
                order, result = core.run_speed_test(
                    conn, oid, actor=actor, force=payload().get("force")
                )
                return self._send_json({"order": dict(order), "result": result})
            if action == "resend-code":
                code = core.resend_signoff_code(conn, oid, actor=actor)
                return self._send_json({"sent": True, "demo_code": code})
            if action == "signoff":
                # 角色由 core 强制：工程师代签直接拒绝；用户用短信验证码签收。
                order = core.signoff(conn, oid, payload().get("code"), role=role, actor=actor)
                return self._send_json(dict(order))
            self._error("接口不存在", 404)
        finally:
            conn.close()

    # ---------- 照片上传 ----------
    def _upload_photo(self, conn, oid):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            raise core.ServiceError("照片需以 multipart/form-data 上传（字段名 photo）")
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        filename, content = _parse_multipart(body, boundary)
        if not filename or not content:
            raise core.ServiceError("未解析到照片文件")
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg"):
            raise core.ServiceError("照片仅支持 png/jpg")
        core.get_order(conn, oid)
        os.makedirs(PHOTO_DIR, exist_ok=True)
        safe = f"order{oid}-{int(time.time() * 1000)}{ext}"
        with open(os.path.join(PHOTO_DIR, safe), "wb") as f:
            f.write(content)
        conn.execute(
            "INSERT INTO photos(order_id,filename,created_at) VALUES(?,?,?)",
            (oid, safe, time.time()),
        )
        core.log_event(conn, oid, self._actor(self._role()), "上传现场照片", safe)
        self._send_json({"filename": safe}, 201)

    # ---------- 静态/照片 ----------
    def _serve_file(self, path):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_photo(self, name):
        name = os.path.basename(name)
        path = os.path.join(PHOTO_DIR, name)
        if not os.path.isfile(path):
            return self._error("照片不存在", 404)
        self._serve_file(path)


class CoreRoleError(core.ServiceError):
    def __init__(self):
        super().__init__("测速操作仅工程师可执行")


def _parse_multipart(body, boundary):
    """极简 multipipart 解析：取第一个含 filename 的分片内容。"""
    delim = b"--" + boundary.encode()
    for part in body.split(delim):
        if b"filename=" not in part:
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        m = re.search(rb'filename="([^"]+)"', head)
        filename = m.group(1).decode("utf-8", "ignore") if m else ""
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return filename, content
    return "", b""


def ensure_seed():
    conn = get_conn()
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM buildings").fetchone()["c"] == 0:
        seed_mod.seed(conn)
        print("[init] 已自动播种演示数据")
    conn.close()


def main():
    ensure_seed()
    t = threading.Thread(target=sweeper_loop, daemon=True, name="reservation-sweeper")
    t.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"宽带新装受理演示已启动：http://localhost:{PORT}")
    print(f"  数据：{os.environ.get('BB_DB_PATH', 'data/broadband.db')}  照片：{PHOTO_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
