/* 宽带新装工单系统 —— 安装端 / 用户端 共享脚本（原生 JS，无构建依赖） */

const STATUS_CN = {
  draft: "待核查", address_error: "地址有误", reserved: "端口预占中",
  scheduled: "已排期", user_absent: "用户不在", rescheduled: "已改期",
  surveyed: "待验收", optical_failed: "光功率不达标", speed_failed: "测速不达标",
  expired: "预占已超时", rejected: "端口已满", activated: "已开通",
};
const PORT_CN = { free: "空闲", reserved: "预占", confirmed: "已确认" };

let META = {};

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j.data;
}

async function apiForm(path, form) {
  const r = await fetch(path, { method: "POST", body: new FormData(form) });
  const j = await r.json();
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j.data;
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v === false || v === null || v === undefined) continue;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(c.nodeType ? c : document.createTextNode(c));
  }
  return el;
}

function pill(status) {
  return h("span", { class: `pill ${status}` }, STATUS_CN[status] || status);
}

function toast(msg, ok = true) {
  let box = document.querySelector(".toast");
  if (!box) { box = h("div", { class: "toast" }); document.body.appendChild(box); }
  const m = h("div", { class: `msg ${ok ? "ok" : "err"}` }, msg);
  box.appendChild(m);
  setTimeout(() => m.remove(), 4200);
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function readingCard(label, value, unit, passed, thresholdText) {
  const card = h("div", { class: `reading ${passed === null ? "" : passed ? "ok" : "bad"}` });
  card.appendChild(h("div", { class: "label" }, label));
  card.appendChild(h("div", { class: "value" },
    value === null || value === undefined ? "—" : String(value)));
  card.appendChild(h("div", { class: "label" }, unit));
  if (thresholdText)
    card.appendChild(h("div", { class: "threshold" }, thresholdText));
  return card;
}

function modal(title, contentNode, footerNode) {
  const mask = h("div", { class: "modal-mask" });
  const box = h("div", { class: "modal" });
  const close = () => mask.remove();
  mask.addEventListener("click", (e) => { if (e.target === mask) close(); });
  const head = h("div", { class: "row", style: "justify-content:space-between;margin-bottom:8px" },
    h("h3", { style: "margin:0" }, title),
    h("button", { class: "ghost sm", onclick: close }, "✕ 关闭"));
  box.appendChild(head);
  box.appendChild(contentNode);
  if (footerNode) {
    const f = h("div", { class: "row", style: "margin-top:16px;justify-content:flex-end" }, footerNode);
    box.appendChild(f);
  }
  mask.appendChild(box);
  document.body.appendChild(mask);
  return close;
}

// ================================================================ 安装端

async function initInstaller() {
  META = await api("GET", "/api/meta");
  document.querySelectorAll("nav a[data-tab]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      showTab(a.getAttribute("data-tab"));
    });
  });
  await Promise.all([renderAcceptForm(), renderMap(), renderSchedule(), renderOrders()]);
  // 每 20 秒刷新资源与工单（预占超时等后台变化）
  setInterval(() => { renderMap(); renderOrders(); }, 20000);
}

function showTab(id) {
  document.querySelectorAll("nav a[data-tab]").forEach((a) =>
    a.classList.toggle("active", a.getAttribute("data-tab") === id));
  document.querySelectorAll("section.tab").forEach((s) =>
    s.classList.toggle("hidden", s.id !== `tab-${id}`));
  if (id === "map") renderMap();
  if (id === "schedule") renderSchedule();
  if (id === "orders") renderOrders();
}

// ---- 受理新装 ----
async function renderAcceptForm() {
  const plans = await api("GET", "/api/plans");
  const buildings = await api("GET", "/api/buildings");
  const box = document.getElementById("accept-form");
  box.innerHTML = "";
  const grid = h("div", { class: "grid cols-3" });
  const f = {};
  const mk = (label, input, req = true) =>
    h("label", { class: "field" }, label + (req ? " *" : ""), input);
  f.name = h("input", { placeholder: "用户姓名" });
  f.phone = h("input", { placeholder: "手机号", value: "139" });
  f.address = h("input", { list: "addr-list", placeholder: "如：阳光花园1栋" });
  const dl = h("datalist", { id: "addr-list" },
    ...buildings.map((b) => h("option", { value: b.address })));
  f.room = h("input", { placeholder: "楼栋房号，如 302" });
  f.plan = h("select", {}, h("option", { value: "" }, "请选择套餐"),
    ...plans.map((p) => h("option", { value: p.id },
      `${p.name}（${p.bandwidth_mbps}Mbps / ¥${p.monthly_fee}）`)));
  f.slot = h("select", {}, h("option", { value: "" }, "期望时段（可选）"),
    ...META.time_slots.map((s) => h("option", { value: s }, s)));
  [["用户姓名", f.name], ["手机号", f.phone], ["安装地址", f.address],
   ["楼栋房号", f.room], ["套餐", f.plan], ["期望时段", f.slot, null, false]]
    .forEach(([label, input, extra, req = true]) => {
      grid.appendChild(mk(label, input, req));
      if (extra) grid.appendChild(extra);
    });
  box.appendChild(dl);
  box.appendChild(grid);
  box.appendChild(h("div", { class: "row", style: "margin-top:12px" },
    h("button", {
      onclick: async () => {
        try {
          const o = await api("POST", "/api/orders", {
            customer_name: f.name.value.trim(), customer_phone: f.phone.value.trim(),
            address: f.address.value.trim(), room_no: f.room.value.trim(),
            plan_id: Number(f.plan.value), slot: f.slot.value || null,
          });
          let note = `工单 ${o.order_no} 已受理：`;
          if (o.status === "rejected") note += "该楼栋端口已满，已转资源不足待扩容";
          else if (o.status === "address_error") note += "地址无法匹配，请更正";
          else if (o.status === "reserved") note += "端口已预占，请尽快排期";
          toast(note, o.status !== "rejected");
          if (o.status === "rejected") {
            showTab("orders");
            openOrder(o.id);
          }
          renderOrders(); renderMap();
        } catch (e) { toast(e.message, false); }
      }
    }, "受理并核查端口"),
    h("span", { class: "muted" },
      `端口预占 ${META.reserve_timeout_seconds / 60} 分钟内未排期将自动释放`)));
}

// ---- 资源地图（按楼层展开）----
async function renderMap() {
  const box = document.getElementById("resource-map");
  if (box.closest(".hidden")) return;
  const buildings = await api("GET", "/api/buildings");
  box.innerHTML = "";
  if (!buildings.length) { box.appendChild(h("p", { class: "muted" }, "暂无楼栋数据")); return; }
  for (const b of buildings) {
    const c = b.counts;
    const full = c.free === 0;
    const card = h("div", { class: "card" });
    card.appendChild(h("div", { class: "row", style: "justify-content:space-between" },
      h("div", {}, h("strong", { style: "color:var(--blue-900);font-size:16px" }, b.name),
        h("div", { class: "muted" }, `标准地址：${b.address} · ${b.floors} 层`)),
      h("div", { class: "stat-row", style: "min-width:360px" },
        stat(c.total, "端口总数", ""),
        stat(c.free, "空闲", "free"),
        stat(c.reserved, "预占中", ""),
        stat(c.confirmed, "已确认", ""),
        stat(full ? 0 : 1, full ? "端口已满" : "可受理", full ? "full" : "free"))));
    if (full) card.appendChild(h("div", { class: "banner warn", style: "margin-top:12px" },
      "⚠ 端口不足样例：该楼栋分光器端口全部占满，新申请将被核查为“端口已满”。"));
    for (const fl of b.floors_data) {
      const block = h("div", { class: "floor-block" });
      const usedHere = fl.splitters.reduce(
        (n, s) => n + s.ports.filter((p) => p.status !== "free").length, 0);
      const head = h("div", { class: "floor-head", onclick: () => block.classList.toggle("open") },
        h("span", { class: "caret" }, "▶"),
        h("strong", {}, `${fl.floor} 层`),
        h("span", { class: "muted" },
          fl.splitters.map((s) =>
            `${s.name}（${s.ports.length} 口 · 占用 ${s.counts.reserved + s.counts.confirmed}）`).join("，")));
      block.appendChild(head);
      const body = h("div", { class: "floor-body" });
      for (const s of fl.splitters) {
        body.appendChild(h("div", { class: "muted", style: "margin:6px 0 2px" },
          `分光器 ${s.name}`));
        body.appendChild(h("div", {},
          ...s.ports.map((p) => h("span", {
            class: `port-chip ${p.status}`,
            title: `端口 ${p.port_no}：${PORT_CN[p.status]}${p.order_id ? `，工单 #${p.order_id}` : ""}`
          }, String(p.port_no)))));
      }
      block.appendChild(body);
      card.appendChild(block);
      if (usedHere) block.classList.add("open");
    }
    box.appendChild(card);
  }
}

function stat(n, label, cls = "") {
  return h("div", { class: `stat ${cls}` },
    h("div", { class: "n" }, String(n)), h("div", { class: "l" }, label));
}

// ---- 排程看板 ----
function actBtn(text, fn, cls = "") {
  return h("button", { class: `sm ${cls}`, onclick: () => fn() }, text);
}

async function renderSchedule() {
  const orders = (await api("GET", "/api/orders"))
    .filter((o) => ["reserved", "scheduled", "user_absent", "rescheduled", "expired"].includes(o.status));
  const box = document.getElementById("schedule-board");
  box.innerHTML = "";
  if (!orders.length) { box.appendChild(h("p", { class: "muted" }, "暂无待排期/待上门工单")); return; }
  const tbl = h("table", {},
    h("thead", {}, h("tr", {},
      ...["工单号", "用户/电话", "地址房号", "套餐", "状态", "预约", "操作"]
        .map((t) => h("th", {}, t)))));
  const tb = h("tbody");
  for (const o of orders) {
    const acts = h("div", { class: "row" });
    if (o.status === "reserved")
      acts.appendChild(actBtn("预约排期", () => scheduleModal(o)));
    if (["scheduled", "rescheduled"].includes(o.status)) {
      acts.appendChild(actBtn("改期", () => rescheduleModal(o), "ghost"));
      acts.appendChild(actBtn("用户不在", () => absent(o), "ghost"));
    }
    if (o.status === "user_absent")
      acts.appendChild(actBtn("改期", () => rescheduleModal(o)));
    if (o.status === "expired")
      acts.appendChild(actBtn("重新核查端口", () => recheck(o)));
    acts.appendChild(h("button", { class: "sm ghost", onclick: () => openOrder(o.id) }, "详情"));
    tb.appendChild(h("tr", {},
      h("td", {}, o.order_no),
      h("td", {}, o.customer_name, h("br"), h("span", { class: "muted" }, o.customer_phone)),
      h("td", {}, `${o.address} ${o.room_no}`),
      h("td", {}, o.plan_name),
      h("td", {}, pill(o.status)),
      h("td", {}, o.visit_date ? [
        `${o.visit_date} ${o.slot || ""}`, h("br"),
        h("span", { class: "muted" }, o.engineer || "")] : "—"),
      h("td", {}, acts)));
  }
  tbl.appendChild(tb);
  box.appendChild(tbl);
}
// ---- 排期 / 改期弹窗 ----
async function scheduleModal(o) {
  const close = scheduleLikeModal(o, "预约排期", async (date, slot, engineer) => {
    await api("POST", `/api/orders/${o.id}/schedule`,
      { visit_date: date, slot, engineer });
  });
  return close;
}
async function rescheduleModal(o) {
  return scheduleLikeModal(o, "预约改期", async (date, slot) => {
    await api("POST", `/api/orders/${o.id}/reschedule`, { visit_date: date, slot });
  }, true);
}

function scheduleLikeModal(o, title, submit, isReschedule = false) {
  const date = h("input", { type: "date", value: o.visit_date || "2026-10-04" });
  const slot = h("select", {}, ...META.time_slots.map((s) =>
    h("option", { value: s, selected: s === o.slot }, s)));
  const engineer = h("input", { placeholder: "工程师姓名", value: o.engineer || "李工" });
  const content = h("div", {},
    h("div", { class: "kv", style: "margin-bottom:12px" },
      h("dt", {}, "工单"), h("dd", {}, o.order_no),
      h("dt", {}, "地址"), h("dd", {}, `${o.address} ${o.room_no}`)),
    h("label", { class: "field" }, "上门日期 *", date),
    h("label", { class: "field" }, "时段 *", slot),
    isReschedule ? null : h("label", { class: "field" }, "指派工程师 *", engineer),
    h("div", { class: "banner info" }, "提交后向用户手机发送模拟短信通知（见工单详情短信记录）。"));
  return modal(title, content, h("button", {
    onclick: async () => {
      try {
        await submit(date.value, slot.value, engineer.value);
        toast(`${title}成功`); closeModal(); refreshAll();
      } catch (e) { toast(e.message, false); }
    }
  }, "确认提交"));
}
function closeModal() { document.querySelector(".modal-mask")?.remove(); }

async function absent(o) {
  try {
    await api("POST", `/api/orders/${o.id}/absent`, {});
    toast("已登记用户不在，等待改期"); refreshAll();
  } catch (e) { toast(e.message, false); }
}
async function recheck(o) {
  try {
    await api("POST", `/api/orders/${o.id}/recheck`, {});
    toast("端口重新预占成功"); refreshAll();
  } catch (e) { toast(e.message, false); }
}

// ---- 工单总表 ----
async function renderOrders() {
  const orders = await api("GET", "/api/orders");
  const box = document.getElementById("orders-table");
  box.innerHTML = "";
  const filter = document.getElementById("order-filter");
  const want = filter ? filter.value : "all";
  const list = want === "all" ? orders : orders.filter((o) => o.status === want);
  const tbl = h("table", {}, h("thead", {}, h("tr", {},
    ...["工单号", "用户", "地址 / 房号", "套餐", "状态", "光功率", "测速", "操作"].map((t) => h("th", {}, t)))));
  const tb = h("tbody");
  for (const o of list) {
    const opBtn = h("button", {
      class: "sm", onclick: () => openOrder(o.id)
    }, mainActionText(o.status));
    tb.appendChild(h("tr", {},
      h("td", {}, o.order_no),
      h("td", {}, o.customer_name, h("br"),
        h("span", { class: "muted" }, o.customer_phone)),
      h("td", {}, o.address || "—", h("br"),
        h("span", { class: "muted" }, o.room_no || "")),
      h("td", {}, o.plan_name || "—"),
      h("td", {}, pill(o.status)),
      h("td", {}, o.optical_rx_dbm == null ? "—" : `${o.optical_rx_dbm} dBm`),
      h("td", {}, o.speed_down_mbps == null ? "—" : `${o.speed_down_mbps} Mbps`),
      h("td", {}, opBtn)));
  }
  tbl.appendChild(tb);
  box.appendChild(tbl);
}

function mainActionText(s) {
  return ({
    address_error: "更正地址", rejected: "处理端口不足", reserved: "去排期",
    scheduled: "现场勘线", rescheduled: "现场勘线", user_absent: "改期",
    optical_failed: "整改复测", surveyed: "设备/测速/验收",
    speed_failed: "整改复测", expired: "重新核查", activated: "查看",
    draft: "查看",
  })[s] || "查看";
}

// ---- 工单详情（勘线/设备/测速 工作台）----
async function openOrder(id) {
  const o = await api("GET", `/api/orders/${id}`);
  const devices = await api("GET", "/api/devices");
  const buildings = await api("GET", "/api/buildings");
  const content = h("div", {});

  // 基本信息
  content.appendChild(h("div", { class: "row", style: "justify-content:space-between" },
    h("div", {}, h("strong", { style: "font-size:16px" }, o.order_no), " ", pill(o.status)),
    h("span", { class: "muted" },
      `${o.customer_name} ${o.customer_phone} · ${o.address || "地址未匹配"} ${o.room_no}`)));

  const problemBanner = problemOf(o);
  if (problemBanner) content.appendChild(problemBanner);

  // 状态操作按钮
  const actions = h("div", { class: "row", style: "margin:12px 0" });
  if (o.status === "address_error") actions.appendChild(fixAddressBtn(o));
  if (o.status === "rejected" || o.status === "expired")
    actions.appendChild(h("button", { class: "sm green", onclick: () => recheck(o) }, "重新核查端口"));
  if (o.status === "reserved") actions.appendChild(h("button", { class: "sm", onclick: () => scheduleModal(o) }, "预约排期"));
  if (["scheduled", "rescheduled", "user_absent", "optical_failed", "speed_failed"]
      .includes(o.status)) {
    if (["scheduled", "rescheduled"].includes(o.status))
      actions.appendChild(h("button", { class: "sm ghost", onclick: () => absent(o) }, "登记用户不在"));
    actions.appendChild(h("button", { class: "sm ghost", onclick: () => rescheduleModal(o) }, "改期"));
  }
  if (actions.children.length) content.appendChild(actions);

  // 同一视区：端口号 + 光功率（现场勘线工作台）
  if (["scheduled", "rescheduled", "optical_failed", "surveyed", "speed_failed"].includes(o.status)) {
    content.appendChild(surveyZone(o, buildings));
  }

  // 设备绑定 / 换绑
  if (["surveyed", "speed_failed"].includes(o.status)) {
    content.appendChild(deviceZone(o, devices));
  }

  // 测速
  if (["surveyed", "speed_failed"].includes(o.status) && o.router_sn) {
    content.appendChild(speedZone(o));
  }

  // 验收
  if (["surveyed", "speed_failed"].includes(o.status)) {
    content.appendChild(acceptZone(o));
  }
  if (o.status === "activated") content.appendChild(
    h("div", { class: "banner ok", style: "margin-top:12px" },
      `✓ 用户已于 ${fmtTime(o.confirmed_at)} 本人验收签收，业务正式开通。`));

  // 现场照片
  if (o.photos.length) {
    content.appendChild(h("h3", { style: "margin:18px 0 6px;font-size:15px" }, "现场照片"));
    content.appendChild(h("div", {}, ...o.photos.map((p) =>
      h("img", { class: "photo-thumb", src: `/photos/order_${o.id}/${p.filename}`,
                 title: p.caption || "" }))));
  }

  // 模拟短信
  api("GET", `/api/orders/${o.id}/sms`).then((sms) => {
    if (!sms.length) return;
    content.appendChild(h("h3", { style: "margin:18px 0 6px;font-size:15px" }, "模拟短信记录"));
    content.appendChild(h("div", {}, ...sms.map((m) =>
      h("div", { class: "banner info", style: "margin-bottom:6px" },
        `[${fmtTime(m.created_at)}] → ${m.phone}：${m.content}`))));
  });

  // 时间线
  content.appendChild(h("h3", { style: "margin:18px 0 6px;font-size:15px" }, "处理记录"));
  content.appendChild(h("ul", { class: "timeline" }, ...o.events.map((e) =>
    h("li", {},
      h("span", { class: "t-time" }, fmtTime(e.at)),
      h("span", { class: "t-action" }, e.action),
      h("span", { class: "t-actor" }, `（${actorCn(e.actor)}）`),
      e.detail ? h("div", { class: "muted" }, e.detail) : null))));

  modal(`工单详情 #${o.id}`, content);
}

function actorCn(a) {
  return { dispatcher: "受理员", engineer: "工程师", customer: "用户", system: "系统" }[a] || a;
}

function problemOf(o) {
  if (o.status === "rejected")
    return h("div", { class: "banner warn", style: "margin-top:12px" },
      "⚠ 端口已满：该楼栋分光器无空闲端口，暂不能受理；可等待扩容/释放后点“重新核查端口”。");
  if (o.status === "address_error")
    return h("div", { class: "banner warn", style: "margin-top:12px" },
      "⚠ 地址写错：标准地址库未匹配到该楼栋，请更正地址后重新核查资源。");
  if (o.status === "user_absent")
    return h("div", { class: "banner warn", style: "margin-top:12px" },
      "⚠ 用户不在：上门无人，需联系用户改期；端口继续为该工单保留。");
  if (o.status === "optical_failed")
    return h("div", { class: "banner warn", style: "margin-top:12px" },
      `⚠ 光功率不达标：实测 ${o.optical_rx_dbm} dBm，合格阈值 ${META.optical.rx_min_dbm} ~ ${META.optical.rx_max_dbm} dBm，整改熔接/跳纤后复测。`);
  if (o.status === "speed_failed") {
    const th = o.speed_threshold.down_mbps;
    return h("div", { class: "banner warn", style: "margin-top:12px" },
      `⚠ 测速不达标：下行 ${o.speed_down_mbps} Mbps，阈值 ≥ ${th} Mbps（签约 ${o.bandwidth_mbps}Mbps 的 90%），可换设备/查线路后复测。`);
  }
  if (o.status === "expired")
    return h("div", { class: "banner info", style: "margin-top:12px" },
      "预占超时：端口已自动释放回资源池，重新核查后可再次预占。");
  return null;
}

function fixAddressBtn(o) {
  return h("button", {
    class: "sm green", onclick: async () => {
      const v = prompt("请输入更正后的标准地址（如：阳光花园1栋）", o.address);
      if (!v) return;
      try {
        await api("POST", `/api/orders/${o.id}/fix-address`, { address: v });
        toast("地址已更正并重新核查端口"); closeModal(); refreshAll(); openOrder(o.id);
      } catch (e) { toast(e.message, false); }
    }
  }, "更正地址并重新核查");
}

// ---- 现场勘线：端口号与光功率在同一视区 ----
function surveyZone(o, buildings) {
  const zone = h("div", { class: "survey-zone", style: "margin-top:14px" });
  zone.appendChild(h("h3", { style: "margin:0 0 10px;font-size:15px" },
    "现场勘线（端口号 · 光功率 同屏读取）"));

  const bldSel = h("select", {});
  const splSel = h("select", {});
  const portSel = h("select", {});

  // 勘线前工单的 splitter_id 为空，需从资源树中找出本工单已预占的端口做预选
  const reservedOf = (b) => {
    if (!b) return null;
    for (const fl of b.floors_data)
      for (const s of fl.splitters)
        for (const p of s.ports)
          if (p.order_id === o.id) return { splitter_id: s.id, port_no: p.port_no };
    return null;
  };
  const bld0 = buildings.find((b) => b.id === o.building_id) || buildings[0];
  const fillBuildings = () => {
    bldSel.innerHTML = "";
    buildings.forEach((b) => bldSel.appendChild(
      h("option", { value: b.id, selected: b.id === (o.building_id || bld0?.id) }, b.name)));
  };
  const fillSplitters = () => {
    const b = buildings.find((x) => String(x.id) === bldSel.value);
    const r = reservedOf(b);
    splSel.innerHTML = "";
    (b?.floors_data || []).forEach((fl) => fl.splitters.forEach((s) =>
      splSel.appendChild(h("option", {
        value: s.id,
        selected: s.id === (o.splitter_id || r?.splitter_id)
      }, `${b.name} ${fl.floor}层 ${s.name}`))));
  };
  const fillPorts = () => {
    const b = buildings.find((x) => String(x.id) === bldSel.value);
    const sid = Number(splSel.value);
    const sp = b?.floors_data.flatMap((fl) => fl.splitters).find((s) => s.id === sid);
    const r = reservedOf(b);
    portSel.innerHTML = "";
    (sp?.ports || []).forEach((p) => portSel.appendChild(h("option", {
      value: p.port_no,
      selected: (sid === o.splitter_id && p.port_no === o.splitter_port) ||
                (sid === r?.splitter_id && p.port_no === r.port_no),
      disabled: p.order_id && p.order_id !== o.id
    }, `${p.port_no} — ${PORT_CN[p.status]}${p.order_id ? `（工单#${p.order_id}）` : ""}`)));
  };
  fillBuildings(); fillSplitters(); fillPorts();
  bldSel.addEventListener("change", () => { fillSplitters(); fillPorts(); });
  splSel.addEventListener("change", fillPorts);

  const fiber = h("textarea", { rows: 2, placeholder: "光纤入户路径，如：3层弱电井→入户门上方→客厅电视柜" });
  fiber.value = o.fiber_path || "";
  const rx = h("input", { type: "number", step: "0.1", placeholder: "如 -20.5",
                         value: o.optical_rx_dbm ?? "" });
  const photoInput = h("input", { type: "file", accept: "image/*", multiple: true });

  // 同视区：左选端口，右读光功率（实时对照阈值变红/绿）
  const live = readingCard("实时接收光功率", rx.value || "—", "dBm", null,
    `合格阈值：${META.optical.rx_min_dbm} ~ ${META.optical.rx_max_dbm} dBm`);
  rx.addEventListener("input", () => {
    const v = parseFloat(rx.value);
    const ok = !isNaN(v) && v >= META.optical.rx_min_dbm && v <= META.optical.rx_max_dbm;
    live.className = `reading ${rx.value === "" ? "" : ok ? "ok" : "bad"}`;
    live.querySelector(".value").textContent = rx.value || "—";
    live.querySelector(".threshold").textContent = rx.value === ""
      ? `合格阈值：${META.optical.rx_min_dbm} ~ ${META.optical.rx_max_dbm} dBm`
      : ok ? `✓ 在阈值 ${META.optical.rx_min_dbm} ~ ${META.optical.rx_max_dbm} dBm 内`
           : `✗ 超出阈值！合格区间 ${META.optical.rx_min_dbm} ~ ${META.optical.rx_max_dbm} dBm`;
  });
  rx.dispatchEvent(new Event("input"));

  const grid = h("div", { class: "grid cols-2", style: "align-items:stretch" },
    h("div", {},
      h("label", { class: "field" }, "楼栋", bldSel),
      h("label", { class: "field" }, "分光器", splSel),
      h("label", { class: "field" }, "分光器端口号（一户一端口）", portSel),
      h("label", { class: "field" }, "光纤入户路径", fiber)),
    h("div", { style: "display:flex;flex-direction:column;gap:10px" }, live,
      h("label", { class: "field" }, "光功率读数 (dBm)", rx),
      h("label", { class: "field" }, "现场照片（弱电井/成端/标签）", photoInput)));
  zone.appendChild(grid);

  const canEdit = ["scheduled", "rescheduled", "optical_failed"].includes(o.status);
  zone.appendChild(h("div", { class: "row", style: "margin-top:10px" },
    h("button", {
      class: canEdit ? "" : "ghost",
      disabled: canEdit ? null : "disabled",
      onclick: async () => {
        try {
          const fd = new FormData();
          fd.append("fiber_path", fiber.value.trim());
          fd.append("splitter_id", splSel.value);
          fd.append("splitter_port", portSel.value);
          fd.append("optical_rx", rx.value);
          for (const file of photoInput.files) fd.append("photos", file, file.name);
          await apiForm(`/api/orders/${o.id}/survey`, fd);
          toast("勘线记录成功，端口已确认占用"); closeModal(); refreshAll(); openOrder(o.id);
        } catch (e) {
          toast(e.message, false);
          closeModal(); refreshAll(); openOrder(o.id);
        }
      }
    }, canEdit ? "提交勘线 / 整改复测" : "勘线已完成（端口 confirmed）"),
    h("button", { class: "ghost sm", onclick: () => { rx.value = "-31.2"; rx.dispatchEvent(new Event("input")); } },
      "填入不达标样例(-31.2)")));
  return zone;
}

// ---- 设备绑定 / 换绑 ----
function deviceZone(o, devices) {
  const zone = h("div", { class: "card soft", style: "margin-top:14px" });
  const stock = devices.filter((d) => d.status === "stock");
  zone.appendChild(h("h3", { style: "margin:0 0 8px;font-size:15px" }, "设备绑定"));
  if (o.router_sn) {
    zone.appendChild(h("div", { class: "kv" },
      h("dt", {}, "当前路由器"), h("dd", {}, h("strong", {}, o.router_sn))));
    const sel = h("select", {}, ...stock.map((d) =>
      h("option", { value: d.router_sn }, `${d.router_sn}（${d.model}）`)));
    if (!stock.length) sel.appendChild(h("option", { value: "" }, "库存无可用设备"));
    zone.appendChild(h("div", { class: "row", style: "margin-top:10px" },
      h("label", { class: "field", style: "margin:0;flex:1" }, "更换为（旧机自动回库）", sel),
      h("button", {
        class: "sm", onclick: async () => {
          if (!sel.value) return;
          try {
            await api("POST", `/api/orders/${o.id}/rebind-device`, { router_sn: sel.value });
            toast(`设备已换绑：${o.router_sn} → ${sel.value}`);
            closeModal(); refreshAll(); openOrder(o.id);
          } catch (e) { toast(e.message, false); }
        }
      }, "确认换绑")));
  } else {
    const sel = h("select", {}, h("option", { value: "" }, "选择库存路由器"),
      ...stock.map((d) => h("option", { value: d.router_sn },
        `${d.router_sn}（${d.model}）`)));
    zone.appendChild(h("div", { class: "row" }, sel,
      h("button", {
        class: "sm green", onclick: async () => {
          try {
            await api("POST", `/api/orders/${o.id}/bind-device`, { router_sn: sel.value });
            toast("设备绑定成功"); closeModal(); refreshAll(); openOrder(o.id);
          } catch (e) { toast(e.message, false); }
        }
      }, "绑定设备")));
  }
  return zone;
}

// ---- 测速 ----
function speedZone(o) {
  const zone = h("div", { class: "card soft", style: "margin-top:14px" });
  zone.appendChild(h("h3", { style: "margin:0 0 10px;font-size:15px" }, "网络测速（模拟适配器）"));
  const grid = h("div", { class: "grid cols-3" });
  const downTh = o.speed_threshold.down_mbps;
  const upTh = Math.round(o.bandwidth_mbps * 0.3 * 0.9 * 10) / 10;
  const downOk = o.speed_down_mbps != null && o.speed_down_mbps >= downTh;
  const upOk = o.speed_up_mbps != null && o.speed_up_mbps >= upTh;
  grid.appendChild(readingCard("下行速率", o.speed_down_mbps, "Mbps",
    o.speed_down_mbps == null ? null : downOk,
    o.speed_down_mbps == null ? `达标阈值 ≥ ${downTh} Mbps（签约 90%）`
      : `${downOk ? "✓ 达标" : `✗ 不达标，阈值 ≥ ${downTh} Mbps`}`));
  grid.appendChild(readingCard("上行速率", o.speed_up_mbps, "Mbps",
    o.speed_up_mbps == null ? null : upOk,
    o.speed_up_mbps == null ? `参考阈值 ≥ ${upTh} Mbps`
      : `${upOk ? "✓ 达标" : `✗ 不达标，阈值 ≥ ${upTh} Mbps`}`));
  grid.appendChild(readingCard("签约带宽", o.bandwidth_mbps, "Mbps · " + o.plan_name, null,
    "达标线 = 签约带宽 × 90%"));
  zone.appendChild(grid);
  zone.appendChild(h("div", { class: "row", style: "margin-top:10px" },
    h("button", {
      class: "sm green", onclick: async () => {
        try {
          await api("POST", `/api/orders/${o.id}/speed-test`, { force_fail: false });
          toast("测速完成"); closeModal(); refreshAll(); openOrder(o.id);
        } catch (e) { toast(e.message, false); }
      }
    }, "正常测速"),
    h("button", {
      class: "sm ghost", onclick: async () => {
        try {
          const r = await api("POST", `/api/orders/${o.id}/speed-test`, { force_fail: true });
          toast(`已模拟线路异常：${r.speed_down_mbps}Mbps < 阈值 ${r.speed_threshold.down_mbps}Mbps`);
          closeModal(); refreshAll(); openOrder(o.id);
        } catch (e) { toast(e.message, false); }
      }
    }, "模拟线路异常测速"),
    h("span", { class: "muted" }, "未通过测速的工单不能转已开通")));
  return zone;
}

// ---- 验收签收 ----
function acceptZone(o) {
  const zone = h("div", { class: "survey-zone", style: "margin-top:14px" });
  zone.appendChild(h("h3", { style: "margin:0 0 6px;font-size:15px" }, "用户验收签收"));
  const ready = o.status === "surveyed" && o.speed_down_mbps != null &&
    o.speed_down_mbps >= o.speed_threshold.down_mbps;
  if (!ready) {
    zone.appendChild(h("div", { class: "banner warn" },
      "测速尚未通过，不能签收开通；请完成整改并复测达标后，由用户本人在用户端签收。"));
  }
  const code = h("input", { placeholder: "短信中的 6 位验证码", maxlength: 6, style: "max-width:220px" });
  zone.appendChild(h("div", { class: "row" },
    code,
    h("button", {
      class: "sm green", onclick: async () => {
        try {
          await api("POST", `/api/orders/${o.id}/accept`,
            { actor: "customer", code: code.value.trim() });
          toast("用户本人签收成功，业务已开通");
          closeModal(); refreshAll(); openOrder(o.id);
        } catch (e) { toast(e.message, false); }
      }
    }, "用户本人签收"),
    h("button", {
      class: "sm ghost", title: "演示权限拦截", onclick: async () => {
        try {
          await api("POST", `/api/orders/${o.id}/accept`,
            { actor: "engineer", code: code.value.trim() || "000000" });
        } catch (e) { toast("已拦截：" + e.message, false); }
      }
    }, "工程师代签（演示拦截）"),
    h("span", { class: "muted" },
      ready ? `演示环境验证码：${o.accept_code}（已模拟短信下发给用户）` : "测速达标后生成验证码")));
  return zone;
}

function refreshAll() { renderMap(); renderSchedule(); renderOrders(); }

// ================================================================ 用户端

async function initUser() {
  META = await api("GET", "/api/meta");
  const input = document.getElementById("lookup");
  input.value = "BB-SEED-WAIT";
  const go = async () => {
    const v = input.value.trim();
    if (!v) return;
    try {
      const orders = await api("GET", "/api/orders?key=" + encodeURIComponent(v));
      renderUserOrders(orders);
    } catch (e) { toast(e.message, false); }
  };
  document.getElementById("lookup-btn").addEventListener("click", go);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  go();
}

async function renderUserOrders(orders) {
  const box = document.getElementById("user-orders");
  box.innerHTML = "";
  if (!orders.length) {
    box.appendChild(h("div", { class: "banner warn" }, "未查询到工单，请输入工单号或预留手机号。"));
    return;
  }
  for (const brief of orders) {
    const o = await api("GET", `/api/orders/${brief.id}`);
    const card = h("div", { class: "card" });
    card.appendChild(h("div", { class: "row", style: "justify-content:space-between" },
      h("div", {}, h("strong", { style: "font-size:16px;color:var(--blue-900)" }, o.order_no),
        " ", pill(o.status)),
      h("span", { class: "muted" }, `${o.address || ""} ${o.room_no} · ${o.plan_name || ""}`)));

    // 1) 预约
    const appoint = h("div", { class: "card soft" },
      h("h2", { class: "section" }, "预约信息"),
      h("div", { class: "kv" },
        h("dt", {}, "上门日期"), h("dd", {}, o.visit_date || "待受理员排期"),
        h("dt", {}, "时段"), h("dd", {}, o.slot || "—"),
        h("dt", {}, "工程师"), h("dd", {}, o.engineer || "—")));
    if (o.status === "user_absent") appoint.appendChild(
      h("div", { class: "banner warn", style: "margin-top:10px" },
        "工程师上门时您不在场，已为您登记，请联系受理台改期。"));
    if (["scheduled", "rescheduled"].includes(o.status)) appoint.appendChild(
      h("div", { class: "banner ok", style: "margin-top:10px" },
        `请在 ${o.visit_date} ${o.slot} 留人在场，工程师 ${o.engineer} 将上门。`));
    card.appendChild(appoint);

    // 2) 测速
    const speed = h("div", { class: "card soft" },
      h("h2", { class: "section" }, "测速结果"));
    if (o.speed_down_mbps == null) {
      speed.appendChild(h("p", { class: "muted" }, "工程师尚未完成测速。"));
    } else {
      const downTh = o.speed_threshold.down_mbps;
      const passed = o.speed_down_mbps >= downTh;
      const grid = h("div", { class: "grid cols-3" });
      grid.appendChild(readingCard("下行速率", o.speed_down_mbps, "Mbps", passed,
        passed ? `✓ 达标（阈值 ≥ ${downTh} Mbps）`
               : `✗ 不达标！阈值 ≥ ${downTh} Mbps，正在整改`));
      grid.appendChild(readingCard("上行速率", o.speed_up_mbps, "Mbps", null, "上行参考读数"));
      grid.appendChild(readingCard("签约带宽", o.bandwidth_mbps, "Mbps", null,
        `达标线 = 签约带宽 × ${META.speed_ratio * 100}%`));
      speed.appendChild(grid);
    }
    card.appendChild(speed);

    // 3) 验收结果
    const accept = h("div", { class: "card soft" },
      h("h2", { class: "section" }, "验收结果"));
    if (o.status === "activated") {
      accept.appendChild(h("div", { class: "banner ok" },
        `✓ 验收通过，已于 ${fmtTime(o.confirmed_at)} 正式开通。感谢您的使用！`));
    } else if (o.status === "surveyed" && o.speed_down_mbps != null &&
               o.speed_down_mbps >= o.speed_threshold.down_mbps) {
      const code = h("input", { placeholder: "输入 6 位签收验证码", maxlength: 6 });
      accept.appendChild(h("div", { class: "banner info" },
        `测速已达标（下行 ${o.speed_down_mbps}Mbps）。签收验证码已通过模拟短信发送到 ${o.customer_phone}。`));
      accept.appendChild(h("div", { class: "row", style: "margin-top:10px" },
        code, h("button", {
          class: "green", onclick: async () => {
            try {
              await api("POST", `/api/orders/${o.id}/accept`,
                { actor: "customer", code: code.value.trim() });
              toast("签收成功，宽带已开通");
              const fresh = await api("GET", `/api/orders/${o.id}`);
              card.replaceWith(await buildUserCard(fresh.id));
            } catch (e) { toast(e.message, false); }
          }
        }, "确认验收并签收")));
      accept.appendChild(h("div", { class: "muted", style: "margin-top:8px" },
        `（演示环境验证码：${o.accept_code}；工程师无权代签）`));
    } else {
      accept.appendChild(h("p", { class: "muted" },
        "测速达标后，可在此输入短信验证码完成本人签收；未测速/不达标时不显示签收入口。"));
      const map = {
        address_error: "您的安装地址核查未通过，受理员将联系您核对。",
        rejected: "当前楼栋端口已满，我们将在扩容后与您联系。",
        reserved: "端口已为您预留，等待受理员排期。",
        scheduled: "已完成预约，请等待工程师上门。",
        user_absent: "上次上门您不在场，请改期后留人在场。",
        optical_failed: `现场光功率 ${o.optical_rx_dbm} dBm 不达标（阈值 ${META.optical.rx_min_dbm}~${META.optical.rx_max_dbm} dBm），工程师正在整改。`,
        speed_failed: `测速 ${o.speed_down_mbps}Mbps 未达阈值 ${o.speed_threshold.down_mbps}Mbps，工程师正在整改复测。`,
        expired: "资源预留超时，请联系受理台重新安排。",
      };
      if (map[o.status]) accept.appendChild(
        h("div", { class: "banner warn" }, map[o.status]));
    }
    card.appendChild(accept);
    box.appendChild(card);
  }
}

async function buildUserCard(id) {
  const wrap = h("div");
  await renderUserOrders([{ id }]);
  return document.getElementById("user-orders").firstChild;
}

// ================================================================ 启动

document.addEventListener("DOMContentLoaded", () => {
  if (document.body.dataset.page === "installer") initInstaller();
  if (document.body.dataset.page === "user") initUser();
});
