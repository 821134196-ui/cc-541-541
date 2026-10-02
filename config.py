# -*- coding: utf-8 -*-
"""全局配置：阈值、套餐、路径、预占超时。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BB_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("BB_DB_PATH", os.path.join(DATA_DIR, "broadband.db"))
PHOTO_DIR = os.environ.get("BB_PHOTO_DIR", os.path.join(BASE_DIR, "photos"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

HOST = os.environ.get("BB_HOST", "0.0.0.0")
PORT = int(os.environ.get("BB_PORT", "8000"))

# 端口预占超时：工程师上门前的“预占”超过该时长自动释放。
# 现场已勘线（施工中）的端口不参与超时清扫。
RESERVE_TTL_SECONDS = int(os.environ.get("BB_RESERVE_TTL", "1800"))
SWEEP_INTERVAL_SECONDS = int(os.environ.get("BB_SWEEP_INTERVAL", "10"))
SIGNOFF_CODE_TTL_SECONDS = 600

# 光功率收光阈值（dBm），现场 ONU 收光需落在 [-27, -8]。
OPTICAL_RX_MIN = -27.0
OPTICAL_RX_MAX = -8.0

# 套餐 -> 标称带宽（Mbps）。测速达到标称的 90% 视为合格。
PLANS = {
    "300M": {"name": "300M 融合套餐", "down": 300, "up": 50},
    "500M": {"name": "500M 融合套餐", "down": 500, "up": 100},
    "1000M": {"name": "千兆尊享套餐", "down": 1000, "up": 200},
}
SPEED_PASS_RATIO = 0.9
PING_MAX_MS = 30

# 角色
ROLE_CLERK = "clerk"
ROLE_ENGINEER = "engineer"
ROLE_CUSTOMER = "customer"
ROLE_NAMES = {ROLE_CLERK: "受理员", ROLE_ENGINEER: "工程师", ROLE_CUSTOMER: "用户"}

# 工单状态
STATUS = {
    "pending": "待资源核查",
    "address_invalid": "地址异常待修正",
    "shortage": "端口不足待扩容",
    "scheduled": "已预约（端口预占中）",
    "reschedule": "用户不在·待改期",
    "expired": "预占超时·需重新核查",
    "power_failed": "光功率不达标·整改中",
    "surveyed": "现场勘线完成",
    "device_bound": "设备已绑定",
    "speed_failed": "测速不达标·需重测",
    "speed_passed": "测速通过·待用户签收",
    "activated": "已开通",
}

PORT_STATUS = {"free": "空闲", "reserved": "预占", "confirmed": "已确认"}
