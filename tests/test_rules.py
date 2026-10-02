# -*- coding: utf-8 -*-
"""业务规则测试：端口竞争、超时释放、设备换绑、签收权限等。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="bb-rules-")
os.environ["BB_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["BB_PHOTO_DIR"] = os.environ.get("BB_PHOTO_DIR", os.path.join(_TMP, "photos"))
os.environ["BB_RESERVE_TTL"] = "1800"
sys.path.insert(0, _ROOT)

import core  # noqa: E402
import seed as seed_mod  # noqa: E402
from config import RESERVE_TTL_SECONDS  # noqa: E402
from db import get_conn, init_db  # noqa: E402

ADDR_A = "阳光路88号阳光花园1栋"


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn()
        init_db(self.conn)
        seed_mod.reset(self.conn)
        seed_mod.seed(self.conn)
        self._seq = 0

    def tearDown(self):
        self.conn.close()

    # ---- 辅助 ----
    def apply(self, name="用户", room="302", plan="500M"):
        self._seq += 1
        o = core.create_order(
            self.conn,
            {
                "customer_name": name,
                "customer_phone": f"136{self._seq:08d}",
                "address": f"{ADDR_A}{room}",
                "room": room,
                "plan": plan,
                "expect_window": "周六 09:00",
            },
        )
        core.check_resources(self.conn, o["id"])
        return core.get_order(self.conn, o["id"])

    def to_surveyed(self, order, power=-19.0):
        d = core.order_detail(self.conn, order["id"])
        p = d["port"]
        return core.submit_survey(
            self.conn,
            order["id"],
            {"fiber_path": "竖井→客厅", "splitter_no": p["splitter_no"],
             "port_no": p["port_no"], "optical_power": power},
        )

    # ---- 1. 端口竞争：并发核查，一个端口同一时刻只能分配一户 ----
    def test_port_competition_concurrent(self):
        n = 10
        results = []
        errors = []
        lock = threading.Lock()

        def worker(i):
            conn = get_conn()
            try:
                o = core.create_order(
                    conn,
                    {"customer_name": f"并发{i}", "customer_phone": f"135{i:08d}",
                     "address": f"{ADDR_A}50{i}", "room": f"50{i}", "plan": "300M",
                     "expect_window": "周日"},
                )
                core.check_resources(conn, o["id"])
                with lock:
                    results.append(core.get_order(conn, o["id"])["port_id"])
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"并发核查不应报错：{errors}")
        self.assertEqual(len(results), n)
        self.assertEqual(len(set(results)), n, "同一端口被分配给了多户工单")
        dup = self.conn.execute(
            "SELECT port_id FROM orders WHERE port_id IS NOT NULL "
            "GROUP BY port_id HAVING COUNT(*) > 1 AND port_id IN "
            "(SELECT id FROM ports WHERE status='reserved')"
        ).fetchall()
        self.assertEqual(len(dup), 0)

    # ---- 2. 后到申请不得占用已确认端口 ----
    def test_confirmed_port_not_taken_by_later_order(self):
        first = self.apply(room="402")
        self.to_surveyed(first)
        d1 = core.order_detail(self.conn, first["id"])
        confirmed_id = first["port_id"]
        first_port_no = d1["port"]["port_no"]

        second = self.apply(room="403")
        self.assertNotEqual(second["port_id"], confirmed_id, "后到申请占用了已确认端口")

        port = self.conn.execute("SELECT * FROM ports WHERE id=?", (confirmed_id,)).fetchone()
        self.assertEqual(port["status"], "confirmed")
        self.assertEqual(port["order_id"], first["id"])

        # 后到工单即使在勘线时强行报该端口，也必须被拒绝
        d2 = core.order_detail(self.conn, second["id"])
        with self.assertRaises(core.ServiceError):
            core.submit_survey(
                self.conn,
                second["id"],
                {"fiber_path": "强占", "splitter_no": d2["port"]["splitter_no"],
                 "port_no": first_port_no, "optical_power": -20.0},
            )

    # ---- 3. 预占超时自动释放；释放后可被新单使用 ----
    def test_reservation_timeout_release(self):
        o = self.apply(room="404")
        pid = o["port_id"]
        base = time.time() + RESERVE_TTL_SECONDS + 60

        released = core.sweep_expired_reservations(self.conn, now=base)
        self.assertIn(o["id"], released)
        port = self.conn.execute("SELECT * FROM ports WHERE id=?", (pid,)).fetchone()
        self.assertEqual(port["status"], "free")
        self.assertIsNone(port["order_id"])
        self.assertEqual(core.get_order(self.conn, o["id"])["status"], "expired")

        # 超时释放的端口可分配给新申请（就近楼层策略会再次选中它）
        later = self.apply(room="405")
        self.assertEqual(later["port_id"], pid)

    # ---- 4. 已勘线确认的端口不参与超时清扫 ----
    def test_confirmed_port_never_swept(self):
        o = self.apply(room="406")
        self.to_surveyed(o)
        pid = o["port_id"]
        far_future = time.time() + RESERVE_TTL_SECONDS * 100

        released = core.sweep_expired_reservations(self.conn, now=far_future)
        self.assertNotIn(o["id"], released)
        port = self.conn.execute("SELECT * FROM ports WHERE id=?", (pid,)).fetchone()
        self.assertEqual(port["status"], "confirmed")
        self.assertEqual(core.get_order(self.conn, o["id"])["status"], "surveyed")

    # ---- 5. 用户不在立即释放端口，改期重新预占 ----
    def test_absent_releases_and_reschedule_reallocates(self):
        o = self.apply(room="407")
        pid = o["port_id"]
        core.mark_customer_absent(self.conn, o["id"])
        self.assertIsNone(core.get_order(self.conn, o["id"])["port_id"])
        self.assertEqual(
            self.conn.execute("SELECT status FROM ports WHERE id=?", (pid,)).fetchone()["status"],
            "free",
        )
        new = core.reschedule(self.conn, o["id"], "下周日 09:00")
        self.assertEqual(new["status"], "scheduled")
        self.assertEqual(new["port_id"], pid)

    # ---- 6. 设备绑定 / 换绑 ----
    def test_device_bind_replace_rebind(self):
        o1 = self.apply(room="303")
        self.to_surveyed(o1)
        core.bind_device(self.conn, o1["id"], "RT-SN-AAA")

        o2 = self.apply(room="304")
        self.to_surveyed(o2)
        # 同一路由器不能有效绑定两户
        with self.assertRaises(core.ServiceError):
            core.bind_device(self.conn, o2["id"], "RT-SN-AAA")
        # 未走“更换”不能重复绑
        with self.assertRaises(core.ServiceError):
            core.bind_device(self.conn, o1["id"], "RT-SN-BBB")

        # 换绑：旧 SN 解绑留痕，测速结果作废
        core.run_speed_test(self.conn, o1["id"], force="pass")
        self.assertEqual(core.get_order(self.conn, o1["id"])["status"], "speed_passed")
        core.bind_device(self.conn, o1["id"], "RT-SN-BBB", replace=True)
        self.assertEqual(core.get_order(self.conn, o1["id"])["status"], "device_bound")
        old = self.conn.execute(
            "SELECT * FROM device_bindings WHERE router_sn='RT-SN-AAA'"
        ).fetchone()
        self.assertEqual(old["status"], "replaced")

        # 旧设备释放后允许绑到另一户
        core.bind_device(self.conn, o2["id"], "RT-SN-AAA")
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM device_bindings WHERE status='active' AND router_sn='RT-SN-AAA'"
        ).fetchone()["c"]
        self.assertEqual(active, 1)

    # ---- 7. 签收权限：工程师禁签、未测速禁开、验证码校验 ----
    def test_signoff_permissions(self):
        o = self.apply(room="305")
        self.to_surveyed(o)
        core.bind_device(self.conn, o["id"], "RT-SIGN-01")

        # 未通过测速不能开通
        with self.assertRaisesRegex(core.ServiceError, "未通过测速"):
            core.signoff(self.conn, o["id"], "123456", role="customer")

        core.run_speed_test(self.conn, o["id"], force="fail")
        with self.assertRaisesRegex(core.ServiceError, "未通过测速"):
            core.signoff(self.conn, o["id"], "123456", role="customer")

        core.run_speed_test(self.conn, o["id"], force="pass")

        # 工程师不能代签
        with self.assertRaisesRegex(core.ServiceError, "工程师不能代替用户签收"):
            core.signoff(self.conn, o["id"], "123456", role="engineer")
        # 受理员同样不能代签
        with self.assertRaisesRegex(core.ServiceError, "工程师不能代替用户签收"):
            core.signoff(self.conn, o["id"], "123456", role="clerk")

        # 验证码错误
        with self.assertRaisesRegex(core.ServiceError, "验证码无效"):
            core.signoff(self.conn, o["id"], "000000", role="customer")

        # 正确验证码开通
        code = self.conn.execute(
            "SELECT code FROM signoff_codes WHERE order_id=? AND consumed=0", (o["id"],)
        ).fetchone()["code"]
        done = core.signoff(self.conn, o["id"], code, role="customer", actor="用户本人")
        self.assertEqual(done["status"], "activated")
        self.assertIsNotNone(done["activated_at"])

        # 验证码一次性
        with self.assertRaises(core.ServiceError):
            core.signoff(self.conn, o["id"], code, role="customer")

    def test_signoff_code_expired(self):
        o = self.apply(room="306")
        self.to_surveyed(o)
        core.bind_device(self.conn, o["id"], "RT-SIGN-02")
        core.run_speed_test(self.conn, o["id"], force="pass")
        code = self.conn.execute(
            "SELECT code FROM signoff_codes WHERE order_id=? AND consumed=0", (o["id"],)
        ).fetchone()["code"]
        with self.assertRaisesRegex(core.ServiceError, "验证码已过期"):
            core.signoff(self.conn, o["id"], code, role="customer", now=time.time() + 10 ** 6)

    # ---- 8. 地址写错 / 端口不足 ----
    def test_address_invalid_and_shortage(self):
        o = core.create_order(
            self.conn,
            {"customer_name": "错地址", "customer_phone": "13611112222",
             "address": "滨江路99号临江府", "room": "501", "plan": "300M",
             "expect_window": "周一"},
        )
        checked = core.check_resources(self.conn, o["id"])
        self.assertEqual(checked["status"], "address_invalid")
        fixed = core.fix_address(self.conn, o["id"], f"{ADDR_A}601", room="601")
        self.assertEqual(fixed["status"], "scheduled")

        full = core.create_order(
            self.conn,
            {"customer_name": "端口满", "customer_phone": "13633334444",
             "address": "锦云路12号锦云大厦B座101", "room": "101", "plan": "500M",
             "expect_window": "周二"},
        )
        checked = core.check_resources(self.conn, full["id"])
        self.assertEqual(checked["status"], "shortage")
        self.assertIsNone(checked["port_id"])

    # ---- 9. 光功率越界判定 ----
    def test_optical_power_threshold(self):
        o = self.apply(room="307")
        p = core.order_detail(self.conn, o["id"])["port"]
        bad = core.submit_survey(
            self.conn, o["id"],
            {"fiber_path": "衰耗过大", "splitter_no": p["splitter_no"],
             "port_no": p["port_no"], "optical_power": -31.2},
        )
        self.assertEqual(bad["status"], "power_failed")
        # 整改复测达标
        ok = core.submit_survey(
            self.conn, o["id"],
            {"fiber_path": "重新熔接", "splitter_no": p["splitter_no"],
             "port_no": p["port_no"], "optical_power": -22.0},
        )
        self.assertEqual(ok["status"], "surveyed")
        surveys = self.conn.execute(
            "SELECT power_passed FROM surveys WHERE order_id=?", (o["id"],)
        ).fetchall()
        self.assertEqual([r["power_passed"] for r in surveys], [0, 1])


if __name__ == "__main__":
    unittest.main()
