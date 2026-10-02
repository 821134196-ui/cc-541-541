"""模拟适配器：网络测速、短信通知。不访问真实网络，结果可复现。"""
import time

from . import config


class AdapterError(Exception):
    pass


class SpeedTestAdapter:
    """模拟网络测速仪。

    合格线：下行 >= 签约带宽 * 90%（上行按 30% 签约带宽折算）。
    force_fail=True 时模拟线路异常，结果必定低于阈值，用于演示与测试。
    """

    UP_RATIO = 0.30

    def run(self, plan_mbps, force_fail=False, jitter=0.0):
        if not plan_mbps or plan_mbps <= 0:
            raise AdapterError("签约带宽无效，无法测速")
        down_threshold = plan_mbps * config.SPEED_RATIO_REQUIRED
        up_threshold = plan_mbps * self.UP_RATIO * config.SPEED_RATIO_REQUIRED
        if force_fail:
            down = round(plan_mbps * 0.42 + jitter, 1)   # 远低于 90%
            up = round(plan_mbps * 0.10, 1)
        else:
            down = round(plan_mbps * (0.94 + jitter), 1)  # 略高于阈值
            up = round(plan_mbps * 0.32, 1)
        return {
            "down_mbps": down,
            "up_mbps": up,
            "down_threshold": round(down_threshold, 1),
            "up_threshold": round(up_threshold, 1),
            "plan_mbps": plan_mbps,
            "passed": down >= down_threshold and up >= up_threshold,
            "at": time.time(),
        }


class SmsAdapter:
    """模拟短信网关：仅落库 sms_log，不外发。"""

    def send(self, conn, phone, content):
        now = time.time()
        cur = conn.execute(
            "INSERT INTO sms_log(phone, content, created_at) VALUES (?,?,?)",
            (phone, content, now),
        )
        conn.commit()
        return {"id": cur.lastrowid, "phone": phone, "content": content, "at": now}


SPEED = SpeedTestAdapter()
SMS = SmsAdapter()

# 签收验证码：模拟短信下发，6 位。生产环境应由用户手机持有；
# 演示环境同时把验证码返回给页面，便于走通流程。
def make_accept_code(order_id):
    return f"{(order_id * 7919 + 20260101) % 1000000:06d}"
