# 宽带新装受理演示平台

模拟运营商宽带新装全流程：用户申请 → 受理员核查端口资源 → 工程师上门勘线 →
设备绑定 → 测速 → 用户本人验收签收。零第三方依赖，Python 标准库 + 原生前端。

## 一条命令本地启动

```bash
python3 server.py
```

打开 http://localhost:8000 即可。首次启动自动建库并播种演示数据。
环境变量：`BB_PORT`（默认 8000）、`BB_DATA_DIR`、`BB_PHOTO_DIR`、`BB_RESERVE_TTL`（预占超时秒数，默认 1800）。

其他命令：

```bash
python3 seed.py --reset     # 重置并重新播种演示数据
python3 -m unittest tests.test_rules -v   # 规则测试
```

## 页面与角色

右上角可切换角色（请求头 `X-Role`），不同角色权限不同：

| 页面 | 说明 |
|---|---|
| 资源地图 | 两栋楼资源，楼层可展开；空闲/预占/已确认三色，显示占用工单 |
| 安装排程 | 新装申请、核查端口、修改地址、预约改期、登记“用户不在” |
| 现场勘线 | 工程师录入光纤路径、分光器端口、光功率；**端口号与光功率同一视区**，失败读数同时显示阈值 [-27, -8] dBm |
| 设备绑定 | 路由器 SN 绑定/更换，保留换绑记录；同一 SN 不能同时绑两户 |
| 测速与签收 | 模拟测速（可强制达标/不达标），达标后模拟短信下发签收码；**工程师不能代签** |
| 用户端 | 仅展示预约、测速结果与验收，不暴露端口资源；用户凭短信码本人签收 |

## 核心规则

1. **一个端口同一时刻只能分配一户**：端口分配在 `BEGIN IMMEDIATE` 写事务内完成，10 线程并发核查无重复分配（见测试）。
2. **端口状态**：`free`（空闲）→ `reserved`（预占）→ `confirmed`（工程师现场勘线确认）。
3. **预占超时自动释放**：后台线程每 10 秒清扫，预占超过 30 分钟的端口回到空闲、工单转“预占超时”；已确认端口**永不**被清扫。
4. **后到申请不得占用已确认端口**，工程师勘线时强占他人端口会被拒绝。
5. **用户不在**：登记后立即释放预占端口，改期时重新核查预占。
6. **光功率不达标**：收光超出 [-27, -8] dBm 转整改，可复测；读数旁始终展示阈值。
7. **设备更换**：旧 SN 置为 replaced 并留痕，原测速结果作废需重测；旧 SN 释放后可绑给其他工单。
8. **未通过测速的工单不能转已开通**；签收必须 `X-Role: customer` 且验证码有效（10 分钟、一次性）。
9. **模拟适配器**：测速（`adapters.MockSpeedAdapter`，可 force pass/fail）与短信（写入 `sms_log`，前端以“模拟短信”展示）。

## 端口不足样例

种子数据中 **锦云大厦B座**（锦云路12号）12 个端口全部被存量用户占用（confirmed）。
页面上在「安装排程 → 新装申请」地址填 `锦云大厦B座`，或直接用 curl：

```bash
curl -s -X POST localhost:8000/api/orders -H 'Content-Type: application/json' -d '{
  "customer_name":"邓琳","customer_phone":"13800000010",
  "address":"锦云路12号锦云大厦B座201","room":"201",
  "plan":"500M","expect_window":"周六 10:00-12:00"}'
# 返回新工单 id，假设为 22

curl -s -X POST localhost:8000/api/orders/22/check -d '{}'
# {"status":"shortage","fail_reason":"楼栋「锦云大厦B座」端口已满，等待扩容", ...}
```

资源地图选择该楼栋可看到“空闲 0”与端口已满提示。

## 快速体验完整流程（curl）

```bash
# 1. 申请并核查（阳光花园1栋端口充足）
OID=$(curl -s -X POST localhost:8000/api/orders -H 'Content-Type: application/json' -d \
 '{"customer_name":"体验","customer_phone":"13700000099","address":"阳光路88号阳光花园1栋601","room":"601","plan":"500M","expect_window":"周六 09:00"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/api/orders/$OID/check -d '{}'

# 2. 工程师勘线（从工单详情取预占端口号）
curl -s -X POST localhost:8000/api/orders/$OID/survey -H 'X-Role: engineer' -H 'Content-Type: application/json' \
 -d '{"fiber_path":"竖井→客厅弱电箱","splitter_no":"F6-SP1","port_no":1,"optical_power":-19.2}'

# 3. 绑定设备 → 测速达标（返回的模拟短信里有签收码）
curl -s -X POST localhost:8000/api/orders/$OID/device -H 'X-Role: engineer' -d '{"router_sn":"RT-DEMO-99"}'
curl -s -X POST localhost:8000/api/orders/$OID/speed-test -H 'X-Role: engineer' -d '{"force":"pass"}'

# 4. 工程师代签被拒；用户本人凭码签收
curl -s -X POST localhost:8000/api/orders/$OID/signoff -H 'X-Role: engineer' -d '{"code":"123456"}'   # 400 拒绝
curl -s -X POST localhost:8000/api/orders/$OID/resend-code -H 'X-Role: customer' -d '{}'             # 取演示码
curl -s -X POST localhost:8000/api/orders/$OID/signoff  -H 'X-Role: customer'  -d '{"code":"<上一步demo_code>"}'
```

## 数据与文件

- `data/broadband.db`：SQLite，工单、端口、勘线、设备、测速、验证码、短信、流转事件。
- `photos/`：现场照片（multipart 上传，png/jpg），经 `/photos/<文件>` 访问。

## 测试覆盖（tests/test_rules.py）

端口并发竞争、超时释放、已确认端口不被后单占用/不被清扫、用户不在与改期、
设备换绑与 SN 唯一、工程师禁签、未测速禁开通、验证码错误/过期/一次性、
地址异常与端口不足、光功率阈值判定。
