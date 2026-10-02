"""端到端业务规则测试（标准库 unittest，无第三方依赖）。

运行：python3 -m unittest discover -s tests -v
覆盖：端口并发竞争、预占超时自动释放、已确认端口不可抢占、
      设备换绑、工程师签收被拒、测速/光功率不达标阻断开通、
      地址错误更正、用户不在与改期。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="bb-test-")
os.environ["BB_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, services  # noqa: E402
from app.adapters import make_accept_code  # noqa: E402
from app.db import connect, init_db  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        # 每个用例独立数据库文件
        self.db_path = os.path.join(_TMP, f"case_{self._testMethodName}.db")
        os.environ["BB_DB_PATH"] = self.db_path
        config.DB_PATH = self.db_path
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.plan = self._plan(500, 99.0)
        self.devices = [self._device(f"RT-{i}") for i in range(1, 11)]
        self.seq = 0

    def tearDown(self):
        self.conn.close()

    # ---- 测试夹具 ----
    def _plan(self, mbps=500, fee=99.0):
        return self.conn.execute(
            "INSERT INTO plans(name, bandwidth_mbps, monthly_fee) VALUES (?,?,?)",
            (f"{mbps}M", mbps, fee)).lastrowid

    def _device(self, sn, model="AX3000"):
        return self.conn.execute(
            "INSERT INTO devices(router_sn, model, status) VALUES (?,?,'stock')",
            (sn, model)).lastrowid

    def _building(self, name, floors=3, ports_per_splitter=8, splitters=(1,)):
        bid = self.conn.execute(
            "INSERT INTO buildings(name, address, floors) VALUES (?,?,?)",
            (name, name, floors)).lastrowid
        ids = []
        for f in splitters:
            sid = self.conn.execute(
                "INSERT INTO splitters(building_id, floor, name) VALUES (?,?,?)",
                (bid, f, f"SP-{name}-{f}")).lastrowid
            ids.append(sid)
            for p in range(1, ports_per_splitter + 1):
                self.conn.execute(
                    "INSERT INTO ports(splitter_id, port_no, status) VALUES (?,?,'free')",
                    (sid, p))
        self.conn.commit()
        return bid, ids

    def _accept(self, address, room=None, plan=None, phone=None):
        self.seq += 1
        return services.accept_order(
            self.conn, f"用户{self.seq}", phone or f"139{self.seq:08d}",
            address, room or f"{self.seq}01", plan or self.plan, "09:00-11:00")

    def _to_scheduled(self, o):
        return services.schedule(self.conn, o["id"], "2026-10-05",
                                 "14:00-16:00", "李工")

    def _to_surveyed(self, o, rx=-20.0):
        """受理→（必要时排期）→勘线合格的最短路径，返回刷新后的工单。"""
        if o["status"] == "reserved":
            o = self._to_scheduled(o)
        sp = self.conn.execute(
            "SELECT id FROM splitters WHERE building_id=?", (o["building_id"],)
        ).fetchone()
        return services.survey(self.conn, o["id"], "弱电井→客厅",
                               sp["id"], self._port_no_of(o), rx)

    def _port_no_of(self, o):
        row = self.conn.execute(
            "SELECT port_no FROM ports WHERE order_id=?", (o["id"],)).fetchone()
        return row["port_no"] if row else None

    def _port_status(self, order_id):
        r = self.conn.execute(
            "SELECT status FROM ports WHERE order_id=?", (order_id,)).fetchone()
        return r["status"] if r else None


class TestPortCompetition(BaseCase):
    def test_concurrent_accept_only_one_wins(self):
        """20 个线程同时申请仅有 1 个空闲端口的楼栋：只有 1 户预占成功。"""
        self._building("竞争楼", floors=1, ports_per_splitter=1, splitters=(1,))
        results, errors = [], []
        barrier = threading.Barrier(20)

        def worker():
            conn = connect(self.db_path)
            try:
                barrier.wait()
                o = services.accept_order(
                    conn, threading.current_thread().name, "13800000000",
                    "竞争楼", "101", self.plan)
                results.append(o["status"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(results.count("reserved"), 1, f"结果：{results}")
        self.assertEqual(results.count("rejected"), 19)
        # 数据库中该楼栋端口：1 confirmed? 不，应只有 1 reserved，其余 free=0
        row = self.conn.execute(
            """SELECT p.status, COUNT(*) n FROM ports p
               JOIN splitters s ON s.id=p.splitter_id
               JOIN buildings b ON b.id=s.building_id
              WHERE b.name='竞争楼' GROUP BY p.status""").fetchall()
        counts = {r["status"]: r["n"] for r in row}
        self.assertEqual(counts.get("reserved"), 1)
        self.assertNotIn("free", counts)

    def test_later_order_cannot_take_confirmed_port(self):
        """后到申请不得占用已确认端口：先到工单勘线 confirmed 后，新单只能 rejected。"""
        self._building("确认楼", floors=1, ports_per_splitter=1, splitters=(1,))
        first = self._accept("确认楼")
        self.assertEqual(first["status"], "reserved")
        first = self._to_surveyed(first)
        self.assertEqual(first["status"], "surveyed")
        self.assertEqual(self._port_status(first["id"]), "confirmed")

        later = self._accept("确认楼")
        self.assertEqual(later["status"], "rejected")

        # 即使后来者强行在勘线里填该端口，也必须被拒绝
        bid, sids = self._building("双口楼", floors=1, ports_per_splitter=2, splitters=(1,))
        a = self._to_surveyed(self._accept("双口楼"))
        b = self._to_scheduled(self._accept("双口楼"))
        sp_b = self.conn.execute(
            "SELECT id FROM splitters WHERE building_id=?", (bid,)).fetchone()
        with self.assertRaises(services.OrderError) as cm:
            services.survey(self.conn, b["id"], "路径", sp_b["id"],
                            self._port_no_of(a), -18.0)
        self.assertIn("不属于本工单", str(cm.exception))
        # b 的端口仍是预占，a 的端口仍 confirmed
        self.assertEqual(self._port_status(a["id"]), "confirmed")
        self.assertEqual(self._port_status(b["id"]), "reserved")


class TestReserveTimeout(BaseCase):
    def test_reserved_port_auto_released_after_timeout(self):
        """预占超时自动释放：端口回 free、工单 expired、新申请可顶上。"""
        self._building("超时楼", floors=1, ports_per_splitter=1, splitters=(1,))
        stale = self._accept("超时楼")
        self.assertEqual(stale["status"], "reserved")
        # 模拟预占时刻已超过超时窗口
        self.conn.execute("UPDATE ports SET reserved_at=? WHERE order_id=?",
                          (time.time() - config.PORT_RESERVE_TIMEOUT_SECONDS - 5,
                           stale["id"]))
        self.conn.commit()

        released = services.sweep_expired(self.conn)
        self.assertEqual(len(released), 1)
        self.assertIsNone(self._port_status(stale["id"]))  # 端口解绑
        row = self.conn.execute(
            "SELECT status FROM ports WHERE id=?", (released[0]["port_id"],)).fetchone()
        self.assertEqual(row["status"], "free")
        self.assertEqual(services.get_order(self.conn, stale["id"])["status"], "expired")

        # 新申请立刻可以拿到刚释放的端口
        fresh = self._accept("超时楼")
        self.assertEqual(fresh["status"], "reserved")

        # 原工单重新核查资源时端口已被新单占用 → 报无可用端口
        with self.assertRaises(services.OrderError):
            services.recheck_resource(self.conn, stale["id"])

    def test_scheduled_port_not_released(self):
        """已排期/改期/用户不在的端口不因预占时间而被释放。"""
        self._building("排期楼", floors=1, ports_per_splitter=1, splitters=(1,))
        o = self._to_scheduled(self._accept("排期楼"))
        self.conn.execute("UPDATE ports SET reserved_at=? WHERE order_id=?",
                          (time.time() - 99999, o["id"]))
        self.conn.commit()
        self.assertEqual(services.sweep_expired(self.conn), [])
        self.assertEqual(self._port_status(o["id"]), "reserved")

        services.mark_user_absent(self.conn, o["id"])
        self.assertEqual(services.sweep_expired(self.conn), [])
        self.assertEqual(self._port_status(o["id"]), "reserved")

    def test_recheck_after_new_frees_port(self):
        """端口满被拒的工单，在有端口释放后可重新核查预占。"""
        self._building("满员楼", floors=1, ports_per_splitter=1, splitters=(1,))
        a = self._to_surveyed(self._accept("满员楼"))
        rejected = self._accept("满员楼")
        self.assertEqual(rejected["status"], "rejected")

        # 已 confirmed 的端口不能通过取消受理释放；模拟扩容：新开通一个端口
        sid = self.conn.execute(
            "SELECT id FROM splitters WHERE building_id=(SELECT id FROM buildings WHERE name='满员楼')"
        ).fetchone()["id"]
        self.conn.execute(
            "INSERT INTO ports(splitter_id, port_no, status) VALUES (?,2,'free')", (sid,))
        self.conn.commit()
        redone = services.recheck_resource(self.conn, rejected["id"])
        self.assertEqual(redone["status"], "reserved")


class TestDeviceRebind(BaseCase):
    def _ready_order(self, addr="设备楼"):
        self._building(addr, floors=1, ports_per_splitter=4, splitters=(1,))
        return self._to_surveyed(self._accept(addr))

    def test_bind_and_rebind(self):
        o = self._ready_order()
        services.bind_device(self.conn, o["id"], "RT-1")
        o = services.get_order(self.conn, o["id"])
        self.assertEqual(o["router_sn"], "RT-1")
        self.assertEqual(self._device_status("RT-1"), "bound")

        # 换绑：旧机回库、新机占用
        services.rebind_device(self.conn, o["id"], "RT-2")
        o = services.get_order(self.conn, o["id"])
        self.assertEqual(o["router_sn"], "RT-2")
        self.assertEqual(self._device_status("RT-1"), "stock")
        self.assertEqual(self._device_status("RT-2"), "bound")

        detail = [e["action"] for e in o["events"]]
        self.assertIn("设备换绑", detail)

    def test_cannot_bind_already_bound_device(self):
        o1 = self._ready_order("甲楼")
        self._building("乙楼", floors=1, ports_per_splitter=4, splitters=(1,))
        o2 = self._to_surveyed(self._accept("乙楼"))
        services.bind_device(self.conn, o1["id"], "RT-3")
        with self.assertRaises(services.OrderError):
            services.bind_device(self.conn, o2["id"], "RT-3")

    def test_concurrent_rebind_same_device_single_winner(self):
        """两单同时换绑同一台库存设备，只有一单成功。"""
        o1 = self._ready_order("换绑甲")
        self._building("换绑乙", floors=1, ports_per_splitter=4, splitters=(1,))
        o2 = self._to_surveyed(self._accept("换绑乙"))
        services.bind_device(self.conn, o1["id"], "RT-1")
        services.bind_device(self.conn, o2["id"], "RT-2")
        outcomes = []
        barrier = threading.Barrier(2)

        def worker(oid):
            conn = connect(self.db_path)
            try:
                barrier.wait()
                services.rebind_device(conn, oid, "RT-9")
                outcomes.append(oid)
            except services.OrderError:
                pass
            finally:
                conn.close()

        ts = [threading.Thread(target=worker, args=(o["id"],))
              for o in (o1, o2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(self._device_status("RT-9"), "bound")

    def _device_status(self, sn):
        return self.conn.execute(
            "SELECT status FROM devices WHERE router_sn=?", (sn,)).fetchone()["status"]


class TestAcceptanceRules(BaseCase):
    def _full_ready(self, addr="签收楼", force_fail=False, rx=-19.5):
        self._building(addr, floors=1, ports_per_splitter=4, splitters=(1,))
        o = self._to_surveyed(self._accept(addr), rx=rx)
        services.bind_device(self.conn, o["id"], "RT-1")
        services.run_speed_test(self.conn, o["id"], force_fail=force_fail)
        return services.get_order(self.conn, o["id"])

    def test_engineer_cannot_sign_for_customer(self):
        o = self._full_ready()
        code = make_accept_code(o["id"])
        with self.assertRaises(services.OrderError) as cm:
            services.customer_accept(self.conn, o["id"], "engineer", code)
        self.assertIn("工程师不能替用户签收", str(cm.exception))
        self.assertEqual(services.get_order(self.conn, o["id"])["status"], "surveyed")

    def test_wrong_code_rejected(self):
        o = self._full_ready()
        with self.assertRaises(services.OrderError):
            services.customer_accept(self.conn, o["id"], "customer", "000000")

    def test_speed_failed_cannot_activate(self):
        """未通过测速的工单不能转成已开通：签收被拒，状态停留在 speed_failed。"""
        o = self._full_ready(force_fail=True)
        self.assertEqual(o["status"], "speed_failed")
        self.assertLess(o["speed_down_mbps"], o["speed_threshold"]["down_mbps"])
        with self.assertRaises(services.OrderError) as cm:
            services.customer_accept(self.conn, o["id"], "customer",
                                     make_accept_code(o["id"]))
        self.assertIn("不能签收", str(cm.exception))
        # 整改（换设备）后复测达标，用户签收成功开通
        services.rebind_device(self.conn, o["id"], "RT-2")
        services.run_speed_test(self.conn, o["id"], force_fail=False)
        done = services.customer_accept(
            self.conn, o["id"], "customer", make_accept_code(o["id"]))
        self.assertEqual(done["status"], "activated")
        self.assertIsNotNone(done["confirmed_at"])

    def test_optical_failed_blocks_and_retest_ok(self):
        self._building("光功率楼", floors=1, ports_per_splitter=4, splitters=(1,))
        o = self._to_scheduled(self._accept("光功率楼"))
        sp = self.conn.execute(
            "SELECT id FROM splitters WHERE building_id=?", (o["building_id"],)
        ).fetchone()
        port_no = self._port_no_of(o)
        with self.assertRaises(services.OrderError) as cm:
            services.survey(self.conn, o["id"], "路径", sp["id"], port_no, -32.0)
        self.assertIn("光功率", str(cm.exception))
        o = services.get_order(self.conn, o["id"])
        self.assertEqual(o["status"], "optical_failed")
        # 不达标期间端口保持预占，未被确认
        self.assertEqual(self._port_status(o["id"]), "reserved")
        # 整改后复测合格
        o = services.survey(self.conn, o["id"], "路径", sp["id"], port_no, -20.1)
        self.assertEqual(o["status"], "surveyed")
        self.assertEqual(self._port_status(o["id"]), "confirmed")


class TestAddressAndSchedule(BaseCase):
    def test_unknown_address_then_fix(self):
        self._building("真实花园1栋", floors=2, ports_per_splitter=4, splitters=(1,))
        o = self._accept("写错的花园99栋")
        self.assertEqual(o["status"], "address_error")
        o = services.fix_address(self.conn, o["id"], "真实花园1栋")
        self.assertEqual(o["status"], "reserved")

    def test_fix_to_full_building_rejected(self):
        self._building("满满楼", floors=1, ports_per_splitter=1, splitters=(1,))
        self._to_surveyed(self._accept("满满楼"))
        o = self._accept("不存在的地方")
        self.assertEqual(o["status"], "address_error")
        o = services.fix_address(self.conn, o["id"], "满满楼")
        self.assertEqual(o["status"], "rejected")

    def test_user_absent_and_reschedule(self):
        self._building("改期楼", floors=1, ports_per_splitter=4, splitters=(1,))
        o = self._to_scheduled(self._accept("改期楼"))
        services.mark_user_absent(self.conn, o["id"])
        o = services.get_order(self.conn, o["id"])
        self.assertEqual(o["status"], "user_absent")
        # 端口仍在
        self.assertEqual(self._port_status(o["id"]), "reserved")
        o = services.reschedule(self.conn, o["id"], "2026-10-08", "09:00-11:00")
        self.assertEqual(o["status"], "rescheduled")
        # 改期后可以正常勘线开通
        sp = self.conn.execute(
            "SELECT id FROM splitters WHERE building_id=?", (o["building_id"],)
        ).fetchone()
        o = services.survey(self.conn, o["id"], "路径", sp["id"],
                            self._port_no_of(o), -17.0)
        self.assertEqual(o["status"], "surveyed")

    def test_sms_dispatched_on_schedule_and_activation(self):
        self._building("短信楼", floors=1, ports_per_splitter=4, splitters=(1,))
        o = self._to_scheduled(self._accept("短信楼", phone="13700000001"))
        msgs = self.conn.execute(
            "SELECT * FROM sms_log WHERE phone='13700000001'").fetchall()
        self.assertTrue(any("已预约" in m["content"] for m in msgs))
        o = self._to_surveyed(o)
        services.bind_device(self.conn, o["id"], "RT-1")
        services.run_speed_test(self.conn, o["id"])
        services.customer_accept(self.conn, o["id"], "customer",
                                 make_accept_code(o["id"]))
        msgs = self.conn.execute(
            "SELECT * FROM sms_log WHERE phone='13700000001'").fetchall()
        self.assertTrue(any("验收开通" in m["content"] for m in msgs))


class TestPhotoStorage(BaseCase):
    def test_photo_saved_to_local_dir(self):
        self._building("照片楼", floors=1, ports_per_splitter=4, splitters=(1,))
        o = self._to_scheduled(self._accept("照片楼"))
        sp = self.conn.execute(
            "SELECT id FROM splitters WHERE building_id=?", (o["building_id"],)
        ).fetchone()
        services.survey(
            self.conn, o["id"], "路径", sp["id"], self._port_no_of(o), -18.0,
            photos=[{"filename": "proof.png",
                     "data": b"\x89PNG-fake-bytes", "caption": "成端标签"}])
        o = services.get_order(self.conn, o["id"])
        self.assertEqual(len(o["photos"]), 1)
        saved = os.path.join(config.PHOTO_DIR, f"order_{o['id']}", o["photos"][0]["filename"])
        self.assertTrue(os.path.exists(saved))


if __name__ == "__main__":
    unittest.main(verbosity=2)
