"""HTTP API 层冒烟测试：真实起服务（随机端口）走 JSON 接口。"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

_TMP = tempfile.mkdtemp(prefix="bb-api-")
os.environ.setdefault("BB_DATA_DIR", _TMP)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.seed import seed  # noqa: E402
from app.server import Handler  # noqa: E402


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(_TMP, "api.db")
        cls.photo_dir = os.path.join(_TMP, "photos")
        config.DB_PATH = cls.db_path
        config.PHOTO_DIR = cls.photo_dir
        os.makedirs(cls.photo_dir, exist_ok=True)
        conn = connect(cls.db_path)
        init_db(conn)
        seed(conn)
        conn.close()

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())["data"]
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_01_meta_and_static(self):
        status, data = self.call("GET", "/api/meta")
        self.assertEqual(status, 200)
        self.assertEqual(data["optical"]["rx_min_dbm"], -27.0)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/installer") as r:
            self.assertIn("安装端", r.read().decode())

    def test_02_port_full_sample(self):
        # 种子里阳光花园2栋已满
        status, data = self.call("POST", "/api/orders", {
            "customer_name": "接口测试", "customer_phone": "13955550000",
            "address": "阳光花园2栋", "room_no": "401", "plan_id": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "rejected")

    def test_03_full_flow_and_engineer_block(self):
        # 受理（滨江大厦端口充足）
        _, o = self.call("POST", "/api/orders", {
            "customer_name": "接口流程", "customer_phone": "13955550001",
            "address": "滨江大厦", "room_no": "1201", "plan_id": 1})
        self.assertEqual(o["status"], "reserved")
        oid = o["id"]
        self.assertEqual(self.call("POST", f"/api/orders/{oid}/schedule",
            {"visit_date": "2026-10-06", "slot": "09:00-11:00",
             "engineer": "周工"})[1]["status"], "scheduled")

        # 找到预占端口
        _, buildings = self.call("GET", "/api/buildings")
        b = [x for x in buildings if x["name"] == "滨江大厦"][0]
        mine = None
        for fl in b["floors_data"]:
            for sp in fl["splitters"]:
                for p in sp["ports"]:
                    if p["order_id"] == oid:
                        mine = (sp["id"], p["port_no"])
        self.assertIsNotNone(mine)

        _, o = self.call("POST", f"/api/orders/{oid}/survey", {
            "fiber_path": "弱电井→客厅", "splitter_id": mine[0],
            "splitter_port": mine[1], "optical_rx": -19.2})
        self.assertEqual(o["status"], "surveyed")

        _, o = self.call("POST", f"/api/orders/{oid}/bind-device",
                         {"router_sn": "RT-1005"})
        self.assertEqual(o["router_sn"], "RT-1005")
        _, o = self.call("POST", f"/api/orders/{oid}/rebind-device",
                         {"router_sn": "RT-1006"})
        self.assertEqual(o["router_sn"], "RT-1006")

        _, o = self.call("POST", f"/api/orders/{oid}/speed-test", {})
        self.assertEqual(o["status"], "surveyed")
        code = o["accept_code"]

        # 工程师代签：HTTP 400
        status, body = self.call("POST", f"/api/orders/{oid}/accept",
                                 {"actor": "engineer", "code": code})
        self.assertEqual(status, 400)
        self.assertIn("工程师不能替用户签收", body["error"])

        # 用户本人签收
        _, o = self.call("POST", f"/api/orders/{oid}/accept",
                         {"actor": "customer", "code": code})
        self.assertEqual(o["status"], "activated")

    def test_04_speed_fail_blocks_accept(self):
        _, o = self.call("POST", "/api/orders", {
            "customer_name": "测速失败", "customer_phone": "13955550002",
            "address": "滨江大厦", "room_no": "1202", "plan_id": 1})
        oid = o["id"]
        self.call("POST", f"/api/orders/{oid}/schedule",
                  {"visit_date": "2026-10-06", "slot": "11:00-13:00",
                   "engineer": "周工"})
        _, buildings = self.call("GET", "/api/buildings")
        b = [x for x in buildings if x["name"] == "滨江大厦"][0]
        mine = next((sp["id"], p["port_no"])
                    for fl in b["floors_data"] for sp in fl["splitters"]
                    for p in sp["ports"] if p["order_id"] == oid)
        self.call("POST", f"/api/orders/{oid}/survey",
                  {"fiber_path": "x", "splitter_id": mine[0],
                   "splitter_port": mine[1], "optical_rx": -15.0})
        self.call("POST", f"/api/orders/{oid}/bind-device",
                  {"router_sn": "RT-1007"})
        status, o = self.call("POST", f"/api/orders/{oid}/speed-test",
                              {"force_fail": True})
        self.assertEqual(status, 200)  # 测量动作成功，结果不达标体现在状态
        self.assertEqual(o["status"], "speed_failed")
        self.assertLess(o["speed_down_mbps"], o["speed_threshold"]["down_mbps"])
        _, o = self.call("GET", f"/api/orders/{oid}")
        self.assertEqual(o["status"], "speed_failed")
        from app.adapters import make_accept_code
        status, body = self.call("POST", f"/api/orders/{oid}/accept",
            {"actor": "customer", "code": make_accept_code(oid)})
        self.assertEqual(status, 400)
        self.assertIn("不能签收", body["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
