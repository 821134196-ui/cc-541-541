"""全局配置：阈值、路径、状态常量。"""
import os
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 数据与文件 ----
DATA_DIR = os.environ.get("BB_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("BB_DB_PATH", os.path.join(DATA_DIR, "broadband.db"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")

# ---- 业务规则 ----
# 端口预占超时：受理核查时预占端口，超时未确认（完成勘线）则自动释放
PORT_RESERVE_TIMEOUT_SECONDS = int(os.environ.get("BB_RESERVE_TIMEOUT", "1800"))  # 30 分钟
# 光功率接收阈值（dBm）：PON 接收光功率合格区间 [-27, -8]，低于 -27 视为不达标
OPTICAL_RX_MIN_DBM = -27.0
OPTICAL_RX_MAX_DBM = -8.0
# 测速达标线：不低于签约带宽的 90%
SPEED_RATIO_REQUIRED = 0.9
# 预约允许的时段
TIME_SLOTS = ["09:00-11:00", "11:00-13:00", "14:00-16:00", "16:00-18:00"]

# ---- 工单状态机 ----
# draft         已受理（待核查端口）
# address_error 地址核查不通过（地址写错）
# reserved      端口已预占（待预约）
# scheduled     已排期（等待上门）
# user_absent   用户不在，需改期
# rescheduled   改期后重新排期
# surveyed      现场勘线完成（光功率合格、分光器端口已确认、设备已绑）
# optical_failed 光功率不达标（须整改后复测）
# speed_failed  测速不达标（不能开通，须整改复测）
# expired       端口预占超时被自动释放（须重新核查资源）
# rejected      端口已满，无法受理
# activated     测速通过且用户签收，正式开通
ORDER_STATUSES = [
    "draft", "address_error", "reserved", "scheduled", "user_absent",
    "rescheduled", "surveyed", "optical_failed", "speed_failed",
    "expired", "rejected", "activated",
]

# 可上门勘线的状态
VISITABLE_STATUSES = ("scheduled", "rescheduled")
# 可整改复测的状态
RECTIFIABLE_STATUSES = ("optical_failed", "speed_failed")

# 端口状态
PORT_FREE = "free"
PORT_RESERVED = "reserved"
PORT_CONFIRMED = "confirmed"


def ensure_dirs():
    os.makedirs(DATA_DIR, 0o755, exist_ok=True)
    os.makedirs(PHOTO_DIR, 0o755, exist_ok=True)
