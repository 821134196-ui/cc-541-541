"""零依赖 HTTP 服务：REST JSON API + 静态页面 + 本地照片目录。

启动：python3 -m app.server [--host 0.0.0.0] [--port 8000]
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, services
from .db import connect, init_db
from .seed import seed

WEB_DIR = os.path.join(config.BASE_DIR, "web")

STATIC = {
    "/": ("installer.html", "text/html; charset=utf-8"),
    "/installer": ("installer.html", "text/html; charset=utf-8"),
    "/user": ("user.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


class Api:
    """把 services 的函数封装成 (payload, files) -> result 的处理器。"""

    @staticmethod
    def GET(path, qs, conn):
        if path == "/api/meta":
            return {"time_slots": config.TIME_SLOTS,
                    "optical": {"rx_min_dbm": config.OPTICAL_RX_MIN_DBM,
                                "rx_max_dbm": config.OPTICAL_RX_MAX_DBM},
                    "speed_ratio": config.SPEED_RATIO_REQUIRED,
                    "reserve_timeout_seconds": config.PORT_RESERVE_TIMEOUT_SECONDS}
        if path == "/api/buildings":
            return services.resource_map(conn)
        if path == "/api/plans":
            return [dict(r) for r in conn.execute("SELECT * FROM plans ORDER BY id")]
        if path == "/api/devices":
            return [dict(r) for r in conn.execute(
                "SELECT * FROM devices ORDER BY router_sn")]
        if path == "/api/orders":
            return services.list_orders(conn, qs.get("status"), qs.get("key"))
        m = _match(path, r"^/api/orders/(\d+)$")
        if m:
            return services.get_order(conn, int(m.group(1)))
        m = _match(path, r"^/api/orders/(\d+)/sms$")
        if m:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM sms_log WHERE phone="
                "(SELECT customer_phone FROM orders WHERE id=?) ORDER BY id",
                (int(m.group(1)),))]
        return None

    @staticmethod
    def POST(path, body, files, conn):
        def b(*keys, default=None):
            return tuple(body.get(k, default) for k in keys)

        if path == "/api/orders":
            return services.accept_order(
                conn, body.get("customer_name"), body.get("customer_phone"),
                body.get("address"), body.get("room_no"),
                _int(body.get("plan_id")), body.get("slot"))

        m = _match(path, r"^/api/orders/(\d+)/fix-address$")
        if m:
            return services.fix_address(conn, int(m.group(1)), body.get("address"))
        m = _match(path, r"^/api/orders/(\d+)/recheck$")
        if m:
            return services.recheck_resource(conn, int(m.group(1)))
        m = _match(path, r"^/api/orders/(\d+)/schedule$")
        if m:
            return services.schedule(conn, int(m.group(1)),
                                     body.get("visit_date"), body.get("slot"),
                                     body.get("engineer"))
        m = _match(path, r"^/api/orders/(\d+)/absent$")
        if m:
            return services.mark_user_absent(conn, int(m.group(1)))
        m = _match(path, r"^/api/orders/(\d+)/reschedule$")
        if m:
            return services.reschedule(conn, int(m.group(1)),
                                       body.get("visit_date"), body.get("slot"))
        m = _match(path, r"^/api/orders/(\d+)/survey$")
        if m:
            return services.survey(
                conn, int(m.group(1)), body.get("fiber_path"),
                _int(body.get("splitter_id")), _int(body.get("splitter_port")),
                _float(body.get("optical_rx")), photos=files or None)
        m = _match(path, r"^/api/orders/(\d+)/bind-device$")
        if m:
            return services.bind_device(conn, int(m.group(1)),
                                        body.get("router_sn"))
        m = _match(path, r"^/api/orders/(\d+)/rebind-device$")
        if m:
            return services.rebind_device(conn, int(m.group(1)),
                                          body.get("router_sn"))
        m = _match(path, r"^/api/orders/(\d+)/speed-test$")
        if m:
            return services.run_speed_test(conn, int(m.group(1)),
                                           force_fail=bool(body.get("force_fail")))
        m = _match(path, r"^/api/orders/(\d+)/accept$")
        if m:
            return services.customer_accept(conn, int(m.group(1)),
                                            body.get("actor", "customer"),
                                            str(body.get("code", "")))
        m = _match(path, r"^/api/orders/(\d+)/sweep$")
        if m:  # 手动触发超时清理（正常情况下每次查询自动执行）
            return {"released": services.sweep_expired(conn)}
        return None


def _match(path, pattern):
    import re
    return re.match(pattern, path)


def _int(v):
    return None if v in (None, "") else int(v)


def _float(v):
    return None if v in (None, "") else float(v)


def _parse_multipart(raw, content_type):
    """极简 multipart/form-data 解析：提取普通字段与文件字段（photos）。"""
    boundary = content_type.split("boundary=", 1)[1].encode()
    delim = b"--" + boundary
    parts = raw.split(delim)
    fields, files = {}, []
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header, data = part.split(b"\r\n\r\n", 1)
        header = header.decode("utf-8", "replace")
        if 'name="' not in header:
            continue
        name = header.split('name="', 1)[1].split('"', 1)[0]
        filename = None
        if 'filename="' in header:
            filename = header.split('filename="', 1)[1].split('"', 1)[0]
        data = data.rstrip(b"\r\n")
        if filename is not None:
            if data:
                files.append({"field": name, "filename": filename, "data": data})
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    server_version = "BroadbandOrder/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默；需要时可打开

    # ---- 响应工具 ----
    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._json({"error": message, "status": status}, status)

    def _serve_file(self, abs_path, ctype):
        try:
            with open(abs_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self._error(404, "文件不存在")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs_raw = parsed.path, urllib.parse.parse_qs(parsed.query)
        qs = {k: v[0] for k, v in qs_raw.items()}

        if path.startswith("/api/"):
            with connect() as conn:
                try:
                    result = Api.GET(path, qs, conn)
                    if result is None:
                        self._error(404, "未知接口")
                    else:
                        self._json({"ok": True, "data": result})
                except services.OrderError as e:
                    self._error(400, str(e))
                except Exception as e:  # noqa: BLE001
                    self._error(500, f"服务器错误：{e}")
            return

        if path.startswith("/photos/"):
            # /photos/order_3/xxx.jpg
            rel = urllib.parse.unquote(path[len("/photos/"):])
            safe = os.path.normpath(rel).lstrip(os.sep)
            if safe.startswith(".."):
                self._error(403, "非法路径")
                return
            ext = os.path.splitext(safe)[1].lower()
            self._serve_file(os.path.join(config.PHOTO_DIR, safe),
                             CONTENT_TYPES.get(ext, "application/octet-stream"))
            return

        if path in STATIC:
            fname, ctype = STATIC[path]
            self._serve_file(os.path.join(WEB_DIR, fname), ctype)
        else:
            ext = os.path.splitext(path)[1].lower()
            if ext in CONTENT_TYPES:
                self._serve_file(os.path.join(WEB_DIR, os.path.basename(path)),
                                 CONTENT_TYPES[ext])
            else:
                self._error(404, "页面不存在")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            self._error(404, "未知接口")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                body, files = _parse_multipart(raw, ctype)
            else:
                body = json.loads(raw.decode("utf-8") or "{}")
                files = []
        except (ValueError, UnicodeDecodeError) as e:
            self._error(400, f"请求解析失败：{e}")
            return

        with connect() as conn:
            try:
                result = Api.POST(path, body, files, conn)
                if result is None:
                    self._error(404, "未知接口")
                else:
                    self._json({"ok": True, "data": result})
            except services.OrderError as e:
                self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"服务器错误：{e}"}, 500)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="宽带新装工单系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-seed", action="store_true", help="不自动初始化种子数据")
    args = parser.parse_args()

    config.ensure_dirs()
    conn = connect()
    init_db(conn)
    if not args.no_seed and conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
        seed(conn)
    conn.close()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"宽带新装工单系统已启动：")
    print(f"  安装端（受理/工程师）: http://{args.host}:{args.port}/installer")
    print(f"  用户端（预约/验收）  : http://{args.host}:{args.port}/user")
    print(f"  数据库: {config.DB_PATH}    照片目录: {config.PHOTO_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
