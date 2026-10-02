# -*- coding: utf-8 -*-
"""模拟适配器：网络测速与短信。

真实场景中二者应调用测速探针与短信网关；本地演示用可重复的随机结果，
短信只写库（sms_log），前端以“模拟短信”横幅展示内容。
"""
import random
import time

from config import PLANS, SPEED_PASS_RATIO, PING_MAX_MS


class MockSpeedAdapter:
    """模拟网络测速。

    force="pass" / "fail" 用于演示与测试；默认在标称带宽附近抖动。
    """

    def __init__(self, seed=None):
        self._rng = random.Random(seed)

    def run(self, plan, force=None):
        target = PLANS[plan]
        if force == "fail":
            down = target["down"] * self._rng.uniform(0.35, 0.75)
            up = target["up"] * self._rng.uniform(0.3, 0.7)
            ping = self._rng.uniform(45, 120)
        else:
            down = target["down"] * self._rng.uniform(0.94, 1.08)
            up = target["up"] * self._rng.uniform(0.92, 1.1)
            ping = self._rng.uniform(4, 24)
        return {
            "down_mbps": round(down, 1),
            "up_mbps": round(up, 1),
            "ping_ms": round(ping, 1),
        }


class MockSmsAdapter:
    """模拟短信：生成 6 位验证码并落 sms_log，返回短信内容。"""

    def __init__(self, seed=None):
        self._rng = random.Random(seed if seed is not None else time.time_ns())

    def _new_code(self):
        return f"{self._rng.randint(0, 999999):06d}"

    def send(self, conn, order_id, phone, content, code=None):
        code = code or self._new_code()
        content = content.format(code=code)
        conn.execute(
            "INSERT INTO sms_log(order_id, phone, content, created_at) VALUES(?,?,?,?)",
            (order_id, phone, content, time.time()),
        )
        return {"code": code, "content": content}
