/* 宽带新装受理 —— 零依赖单页应用 */
'use strict';

let state = { tab: 'map', role: 'clerk', meta: null, buildings: null, detail: {} };

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const STATUS_CLASS = {
  pending: 'default', address_invalid: 'address_invalid', shortage: 'shortage',
  scheduled: 'scheduled', reschedule: 'reschedule', expired: 'expired',
  power_failed: 'power_failed', surveyed: 'default', device_bound: 'default',
  speed_failed: 'speed_failed', speed_passed: 'passed', activated: 'activated',
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'X-Role': state.role, ...(opts.body ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  $('#toast-root').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

function statusBadge(status) {
  const label = state.meta.status[status] || status;
  const cls = STATUS_CLASS[status] || 'default';
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function dialog(title, bodyHtml, onOk, okText = '确定') {
  const root = $('#dialog-root');
  root.innerHTML = `
    <div class="dialog-mask">
      <div class="dialog">
        <h3>${esc(title)}</h3>${bodyHtml}
        <div class="dlg-actions">
          <button class="btn ghost" id="dlg-cancel">取消</button>
          <button class="btn" id="dlg-ok">${esc(okText)}</button>
        </div>
      </div>
    </div>`;
  $('#dlg-cancel').onclick = () => (root.innerHTML = '');
  $('#dlg-ok').onclick = async () => {
    try {
      await onOk(root);
      root.innerHTML = '';
    } catch (e) { toast(e.message, true); }
  };
  root.querySelector('.dialog-mask').addEventListener('click', e => {
    if (e.target.classList.contains('dialog-mask')) root.innerHTML = '';
  });
}

async function loadMeta() { state.meta = await api('/api/meta'); }
async function loadBuildings(force) {
  if (force || !state.buildings) state.buildings = await api('/api/buildings');
  return state.buildings;
}
async function orderDetail(id, force) {
  if (force) delete state.detail[id];
  if (!state.detail[id]) state.detail[id] = await api(`/api/orders/${id}`);
  return state.detail[id];
}
function invalidate(id) { delete state.detail[id]; }

/* ---------------- 资源地图 ---------------- */
async function renderMap() {
  const buildings = await loadBuildings(true);
  const m = $('#main');
  m.innerHTML = `
    <h2 class="page-title">资源地图</h2>
    <p class="page-desc">楼栋分光器资源总览，点击楼层可展开/收起；空闲（绿）、预占（橙）、已确认（蓝）。</p>
    <div class="map-toolbar">
      <select id="bld-select">${buildings.map((b, i) => `<option value="${b.id}">${esc(b.name)}（${esc(b.address)}）</option>`).join('')}</select>
      <div class="legend">
        <span><i style="background:var(--green)"></i>空闲</span>
        <span><i style="background:var(--amber)"></i>预占</span>
        <span><i style="background:var(--blue-600)"></i>已确认</span>
      </div>
    </div>
    <div id="map-body"></div>`;
  const draw = () => {
    const b = buildings.find(x => x.id === Number($('#bld-select').value));
    const full = b.stats.free === 0;
    $('#map-body').innerHTML = `
      <div class="stat-row">
        <div class="stat"><div class="num">${b.total}</div><div class="label">端口总数</div></div>
        <div class="stat ok"><div class="num">${b.stats.free}</div><div class="label">空闲</div></div>
        <div class="stat warn"><div class="num">${b.stats.reserved}</div><div class="label">预占中</div></div>
        <div class="stat"><div class="num">${b.stats.confirmed}</div><div class="label">已确认</div></div>
      </div>
      ${full ? `<div class="fail-reason" style="font-size:13px">该楼栋端口已满，新申请将进入<b>端口不足·待扩容</b>队列。这是预置的端口不足样例楼栋。</div>` : ''}
      ${b.floors.map(f => `
        <div class="floor" data-floor="${f.floor}">
          <div class="floor-head">
            <span class="arrow">▼</span><b>${f.floor} 楼</b>
            <span class="count">空闲 ${f.ports.filter(p => p.status === 'free').length} / ${f.ports.length} 口</span>
          </div>
          <div class="floor-body">
            ${f.ports.map(p => `
              <div class="port-cell ${p.status}" title="${esc(p.splitter_no)}-P${p.port_no}&#10;${state.meta.port_status[p.status]}${p.customer_name ? '&#10;' + esc(p.customer_name) : ''}">
                <div class="pno">P${p.port_no}</div>
                <div class="pst">${esc(state.meta.port_status[p.status])}${p.order_no ? '<br>' + esc(p.order_no) : ''}</div>
              </div>`).join('')}
          </div>
        </div>`).join('')}`;
    $('#map-body').querySelectorAll('.floor-head').forEach(h => {
      h.onclick = () => h.parentElement.classList.toggle('collapsed');
    });
  };
  $('#bld-select').onchange = draw;
  draw();
}

/* ---------------- 工单卡片通用 ---------------- */
function smsBanners(d) {
  if (!d.sms || !d.sms.length) return '';
  return d.sms.slice(-2).map(s =>
    `<div class="sms-banner"><span class="tag">模拟短信 → ${esc(s.phone)}</span><br>${esc(s.content)}</div>`
  ).join('');
}

function orderCard(d, actionsHtml, extraHtml = '') {
  const o = d;
  return `
    <div class="card order-card" data-id="${o.id}">
      <div class="row"><span class="k">工单</span><span class="order-no">${esc(o.order_no)}</span>
        <span class="right">${statusBadge(o.status)}</span></div>
      <div class="row"><span class="k">用户</span>${esc(o.customer_name)} ${esc(o.customer_phone)}</div>
      <div class="row"><span class="k">地址</span>${esc(o.address)} ${esc(o.room || '')}</div>
      <div class="row"><span class="k">套餐</span>${esc(state.meta.plans[o.plan].name)}</div>
      <div class="row"><span class="k">预约时段</span>${esc(o.scheduled_window || o.expect_window || '—')}</div>
      ${o.port ? `<div class="row"><span class="k">端口</span>${esc(o.port.splitter_no)}-P${o.port.port_no}（${esc(state.meta.port_status[o.port.status])}）</div>` : ''}
      ${o.fail_reason ? `<div class="fail-reason">${esc(o.fail_reason)}</div>` : ''}
      ${extraHtml}
      ${smsBanners(d)}
      <div class="actions">${actionsHtml}</div>
      <details class="mt8"><summary class="muted" style="cursor:pointer;font-size:12px">流转记录</summary>
        <ul class="timeline">${o.events.slice().reverse().map(e => `<li><b>${esc(e.actor)}·${esc(e.action)}</b> ${esc(e.detail || '')}</li>`).join('')}</ul>
      </details>
    </div>`;
}

const btn = (act, label, cls = 'btn sm', disabled = false) =>
  `<button class="${cls}" data-act="${act}" ${disabled ? 'disabled' : ''}>${label}</button>`;

/* ---------------- 安装排程 ---------------- */
const SCHEDULE_STATES = ['pending', 'address_invalid', 'shortage', 'scheduled', 'reschedule', 'expired'];

function scheduleCardHtml(o) {
  const acts = [];
  if (['pending', 'address_invalid', 'shortage', 'expired'].includes(o.status)) acts.push(btn('check', '核查端口资源'));
  if (['pending', 'address_invalid'].includes(o.status)) acts.push(btn('address', '修改地址'));
  if (['scheduled', 'reschedule', 'expired', 'shortage'].includes(o.status)) acts.push(btn('reschedule', '预约改期'));
  if (o.status === 'scheduled') acts.push(btn('absent', '登记：用户不在', 'btn sm danger'));
  return orderCard(o, acts.join(' ') || '<span class="muted" style="font-size:12px">暂无可执行操作</span>');
}

async function renderSchedule() {
  const m = $('#main');
  m.innerHTML = `
    <h2 class="page-title">安装排程</h2>
    <p class="page-desc">受理新装申请、核查端口资源；处理地址写错、端口不足、用户不在与预约改期。</p>
    <div class="card" style="margin-bottom:18px">
      <h3>新装申请</h3>
      <form id="new-form">
        <div class="form-grid">
          <label class="field"><span>用户姓名 *</span><input name="customer_name" required></label>
          <label class="field"><span>联系电话 *</span><input name="customer_phone" placeholder="11 位手机号" required></label>
          <label class="field"><span>安装地址 *</span><input name="address" placeholder="如 阳光路88号阳光花园1栋" required></label>
          <label class="field"><span>楼栋房号</span><input name="room" placeholder="如 602"></label>
          <label class="field"><span>期望上门时段</span><input name="expect_window" placeholder="如 周六 09:00-11:00"></label>
          <label class="field"><span>套餐 *</span>
            <select name="plan">${Object.entries(state.meta.plans).map(([k, v]) => `<option value="${k}">${esc(v.name)}</option>`).join('')}</select>
          </label>
        </div>
        <button class="btn" type="submit">提交申请</button>
        <span class="muted" style="font-size:12px;margin-left:10px">提示：地址填“锦云大厦B座”可复现端口不足；填任意无法匹配的地址可复现地址异常。</span>
      </form>
    </div>
    <div class="filters" id="sched-filters">
      ${['全部', ...SCHEDULE_STATES.map(s => state.meta.status[s])].map((x, i) =>
        `<button data-i="${i}" class="${i === 0 ? 'active' : ''}">${esc(x)}</button>`).join('')}
    </div>
    <div class="cards" id="sched-list"></div>`;

  $('#new-form').onsubmit = async (e) => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.target).entries());
    try {
      const o = await api('/api/orders', { method: 'POST', body: JSON.stringify(fd) });
      toast(`申请已创建：${o.order_no}`);
      e.target.reset();
      draw();
    } catch (err) { toast(err.message, true); }
  };

  let filter = 0;
  $('#sched-filters').onclick = (e) => {
    if (e.target.dataset.i !== undefined) {
      filter = Number(e.target.dataset.i);
      $('#sched-filters').querySelectorAll('button').forEach((b, i) => b.classList.toggle('active', i === filter));
      draw();
    }
  };

  async function draw() {
    const orders = await api('/api/orders');
    state.detail = {};
    let list = orders.filter(o => SCHEDULE_STATES.includes(o.status));
    if (filter > 0) list = list.filter(o => o.status === SCHEDULE_STATES[filter - 1]);
    const detailed = await Promise.all(list.map(o => orderDetail(o.id)));
    const html = detailed.map(d => scheduleCardHtml(d)).join('') ||
      '<p class="muted">当前筛选下没有工单。</p>';
    $('#sched-list').innerHTML = html;
    $('#sched-list').querySelectorAll('.card').forEach(card => bindScheduleCard(card, draw));
  }
  draw();
}

function bindScheduleCard(card, redraw) {
  card.querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = async () => {
      const id = Number(card.dataset.id);
      const act = b.dataset.act;
      try {
        if (act === 'check') {
          await api(`/api/orders/${id}/check`, { method: 'POST', body: '{}' });
          toast('端口资源核查完成');
        } else if (act === 'absent') {
          await api(`/api/orders/${id}/absent`, { method: 'POST', body: '{}' });
          toast('已登记用户不在，预占端口释放');
        } else if (act === 'address') {
          const d = await orderDetail(id);
          dialog('修改地址', `
            <label class="field"><span>安装地址 *</span><input id="dlg-addr" value="${esc(d.address)}"></label>
            <label class="field"><span>房号</span><input id="dlg-room" value="${esc(d.room || '')}"></label>`,
            async (root) => {
              await api(`/api/orders/${id}/address`, {
                method: 'POST',
                body: JSON.stringify({ address: $('#dlg-addr', root).value, room: $('#dlg-room', root).value }),
              });
              toast('地址已修正并重新核查'); redraw();
            });
          return;
        } else if (act === 'reschedule') {
          dialog('预约改期', `<label class="field"><span>新的上门时段 *</span><input id="dlg-window" placeholder="如 下周六 09:00-11:00"></label>`,
            async (root) => {
              await api(`/api/orders/${id}/reschedule`, {
                method: 'POST',
                body: JSON.stringify({ window: $('#dlg-window', root).value }),
              });
              toast('改期成功，已重新核查并预占端口'); redraw();
            }, '确认改期');
          return;
        }
        redraw();
      } catch (err) { toast(err.message, true); }
    };
  });
}

/* ---------------- 现场勘线 ---------------- */
function thresholdStrip(latest) {
  const power = latest.optical_power;
  const min = state.meta.optical.rx_min, max = state.meta.optical.rx_max;
  const pct = Math.min(100, Math.max(0, ((power - -40) / (0 - -40)) * 100));
  const good = power >= min && power <= max;
  return `
    <div class="same-view">
      <div>
        <div class="muted" style="font-size:12px">ONU 收光功率</div>
        <div class="big-read ${good ? 'good' : 'bad'}">${power} <span style="font-size:14px">dBm</span></div>
        <div class="threshold-strip"><i style="position:absolute;left:${pct}%;top:-5px;width:3px;height:20px;background:${good ? 'var(--blue-900)' : 'var(--red)'};border-radius:2px"></i></div>
        <div class="reading-note">合格区间 <b>${min} ~ ${max} dBm</b>
          ${good ? '，<b style="color:var(--green)">达标</b>' : '，当前读数 <b class="bad">不达标</b>（阈值同时显示）'}</div>
      </div>
      <div>
        <div class="muted" style="font-size:12px">分光器端口（同一视区）</div>
        <div style="font-size:18px;font-weight:700;margin:6px 0" class="${latest.power_passed ? 'good' : 'bad'}">${esc(latest.splitter_no)}-P${latest.port_no}</div>
        <div class="muted" style="font-size:12.5px">端口状态随勘线由“预占”转“已确认”，超时释放不再生效。</div>
      </div>
    </div>`;
}

function surveyCardHtml(o) {
  const latest = o.surveys[o.surveys.length - 1];
  const acts = [];
  if (['scheduled', 'power_failed'].includes(o.status)) {
    acts.push(btn('survey-form', latest ? '重新勘线 / 整改复测' : '录入勘线', 'btn sm green'));
    acts.push(btn('photo', '上传现场照片', 'btn sm ghost'));
  }
  const extra = `
    ${latest ? `<div class="mt12">
        ${thresholdStrip(latest)}
        <div class="muted mt8" style="font-size:12.5px">光纤入户路径：${esc(latest.fiber_path)}（工程师 ${esc(latest.engineer || '')}）</div>
      </div>` : ''}
    ${o.photos.length ? `<div class="photos">${o.photos.map(p => `<img src="/photos/${esc(p.filename)}" alt="现场照片">`).join('')}</div>` : ''}`;
  return orderCard(o, acts.join(' '), extra);
}

async function renderSurvey() {
  const m = $('#main');
  const isEng = state.role === 'engineer';
  m.innerHTML = `
    <h2 class="page-title">现场勘线</h2>
    <p class="page-desc">工程师上门记录光纤入户路径、分光器端口与光功率。端口号与光功率在同一视区；失败读数同时显示阈值。</p>
    ${!isEng ? `<div class="locked-note">🔒 当前角色为「${state.meta.roles[state.role]}」，勘线记录仅工程师可提交（可切换右上角角色体验）。</div>` : ''}
    <div class="cards mt16" id="survey-list"></div>`;
  const orders = (await api('/api/orders')).filter(o =>
    ['scheduled', 'power_failed', 'surveyed'].includes(o.status));
  const detailed = await Promise.all(orders.map(o => orderDetail(o.id)));
  $('#survey-list').innerHTML = detailed.map(d => surveyCardHtml(d)).join('') ||
    '<p class="muted">暂无可勘线的工单。</p>';
  $('#survey-list').querySelectorAll('.card').forEach(card => bindSurveyCard(card));
}

function bindSurveyCard(card) {
  card.querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = async () => {
      const id = Number(card.dataset.id);
      const act = b.dataset.act;
      if (act === 'photo') {
        dialog('上传现场照片', `
          <label class="field"><span>选择照片（png/jpg，存本地 photos 目录）</span>
          <input type="file" id="dlg-file" accept="image/png,image/jpeg"></label>`,
          async (root) => {
            const f = $('#dlg-file', root).files[0];
            if (!f) throw new Error('请选择照片文件');
            const fd = new FormData(); fd.append('photo', f);
            const r = await fetch(`/api/orders/${id}/photos`, {
              method: 'POST', headers: { 'X-Role': state.role }, body: fd,
            });
            const data = await r.json();
            if (!r.ok) throw new Error(data.error);
            toast('照片已保存到本地目录');
            renderSurvey();
          }, '上传');
        return;
      }
      if (act === 'survey-form') {
        dialog('现场勘线记录', `
          <label class="field"><span>光纤入户路径 *</span>
            <textarea id="dlg-path" rows="2" placeholder="如 楼道竖井→客厅弱电箱→电视柜"></textarea></label>
          <div class="same-view" style="margin:6px 0 4px">
            <div>
              <label class="field" style="margin:0"><span>分光器编号 *</span><input id="dlg-splitter" value="F1-SP1"></label>
              <label class="field" style="margin:8px 0 0"><span>端口号 *（与功率同视区）</span><input id="dlg-port" type="number" value="2"></label>
            </div>
            <div>
              <label class="field" style="margin:0"><span>光功率 dBm *</span><input id="dlg-power" type="number" step="0.1" value="-19.5"></label>
              <div class="reading-note mt8">合格收光区间 <b>${state.meta.optical.rx_min} ~ ${state.meta.optical.rx_max} dBm</b>；
                填 -30 等越界值可复现不达标整改。</div>
            </div>
          </div>`,
          async (root) => {
            const payload = {
              fiber_path: $('#dlg-path', root).value,
              splitter_no: $('#dlg-splitter', root).value,
              port_no: $('#dlg-port', root).value,
              optical_power: $('#dlg-power', root).value,
            };
            await api(`/api/orders/${id}/survey`, { method: 'POST', body: JSON.stringify(payload) });
            toast('勘线已提交');
            renderSurvey();
          }, '提交勘线');
      }
    };
  });
}

/* ---------------- 设备绑定 ---------------- */
function deviceCardHtml(o) {
  const acts = [];
  if (o.status === 'surveyed') acts.push(btn('bind', '绑定路由器', 'btn sm green'));
  if (['device_bound', 'speed_failed', 'speed_passed'].includes(o.status)) acts.push(btn('replace', '设备更换', 'btn sm ghost'));
  const active = o.devices.filter(d => d.status === 'active').map(d => esc(d.router_sn)).join('、') || '—';
  const history = o.devices.length > 1 ? `<div class="device-history">换绑记录：` +
    o.devices.map(d => d.status === 'replaced'
      ? `<span class="replaced">${esc(d.router_sn)}</span><span class="arrow">→</span>`
      : `<b style="color:var(--blue-700)">${esc(d.router_sn)}</b>`).join('') + `</div>` : '';
  return orderCard(o, acts.join(' '), `
    <div class="row"><span class="k">当前设备</span><b>${active}</b></div>${history}`);
}

async function renderDevice() {
  const m = $('#main');
  m.innerHTML = `
    <h2 class="page-title">设备绑定</h2>
    <p class="page-desc">录入光猫/路由器编号完成绑定；设备更换时旧编号自动解绑并保留换绑记录。同一路由器不能同时绑定两户。</p>
    <div class="cards mt16" id="device-list"></div>`;
  const orders = (await api('/api/orders')).filter(o =>
    ['surveyed', 'device_bound', 'speed_failed', 'speed_passed'].includes(o.status));
  const detailed = await Promise.all(orders.map(o => orderDetail(o.id)));
  $('#device-list').innerHTML = detailed.map(deviceCardHtml).join('') || '<p class="muted">暂无待绑定设备的工单。</p>';
  $('#device-list').querySelectorAll('.card').forEach(card => {
    card.querySelectorAll('button[data-act]').forEach(b => {
      b.onclick = () => {
        const id = Number(card.dataset.id);
        const replacing = b.dataset.act === 'replace';
        dialog(replacing ? '设备更换' : '绑定路由器', `
          <label class="field"><span>${replacing ? '新路由器编号（SN）*' : '路由器编号（SN）*'}</span>
            <input id="dlg-sn" placeholder="如 RT-AX56U-0099"></label>
          ${replacing ? '<div class="reading-note">更换后旧设备立即解绑，已完成的测速结果作废，需重新测速。</div>' : ''}`,
          async (root) => {
            const path = replacing ? 'device-replace' : 'device';
            await api(`/api/orders/${id}/${path}`, {
              method: 'POST', body: JSON.stringify({ router_sn: $('#dlg-sn', root).value }),
            });
            toast(replacing ? '设备已换绑' : '设备绑定成功');
            renderDevice();
          }, replacing ? '确认更换' : '绑定');
      };
    });
  });
}

/* ---------------- 测速与签收 ---------------- */
function speedCardHtml(o) {
  const acts = [];
  if (['device_bound', 'speed_failed'].includes(o.status)) {
    acts.push(btn('speed-pass', '模拟测速', 'btn sm green'));
    acts.push(btn('speed-fail', '模拟不达标', 'btn sm danger'));
  }
  if (o.status === 'speed_passed') {
    acts.push(btn('resend', '重新发送签收码', 'btn sm ghost'));
    acts.push(btn('signoff', '验收签收', 'btn sm green'));
  }
  const rows = o.speed_tests.map(t => `
    <tr>
      <td>下行 <span class="${t.down_mbps >= t.threshold_down ? 'good' : 'bad'}">${t.down_mbps}</span></td>
      <td>上行 <span class="${t.up_mbps >= t.threshold_up ? 'good' : 'bad'}">${t.up_mbps}</span></td>
      <td>时延 ${t.ping_ms}ms</td>
      <td class="muted">阈值 ≥${t.threshold_down}/≥${t.threshold_up} Mbps</td>
      <td>${t.passed ? '<span class="badge passed">达标</span>' : '<span class="badge failed">不达标·禁止开通</span>'}</td>
    </tr>`).join('');
  const extra = `
    ${rows ? `<table class="speed-table"><tbody>${rows}</tbody></table>` : ''}
    ${o.status === 'speed_passed' && state.role !== 'customer' ? `
      <div class="locked-note">🔒 工程师不能代替用户签收。请切换右上角角色为「用户」，凭短信验证码由用户本人验收。</div>` : ''}
    ${o.status === 'activated' ? `<div class="sms-banner" style="border-color:var(--green);background:var(--green-weak)">✅ 用户已验收签收，工单正式开通。</div>` : ''}`;
  return orderCard(o, acts.join(' '), extra);
}

async function renderSpeed() {
  const m = $('#main');
  m.innerHTML = `
    <h2 class="page-title">测速与签收</h2>
    <p class="page-desc">绑定设备后由工程师发起模拟测速；达标后系统经模拟短信向用户下发签收验证码，<b>未通过测速不能开通，工程师不能代签</b>。</p>
    <div class="cards mt16" id="speed-list"></div>`;
  const orders = (await api('/api/orders')).filter(o =>
    ['device_bound', 'speed_failed', 'speed_passed', 'activated'].includes(o.status));
  const detailed = await Promise.all(orders.map(o => orderDetail(o.id)));
  $('#speed-list').innerHTML = detailed.map(speedCardHtml).join('') || '<p class="muted">暂无待测速工单。</p>';
  $('#speed-list').querySelectorAll('.card').forEach(card => {
    card.querySelectorAll('button[data-act]').forEach(b => {
      b.onclick = async () => {
        const id = Number(card.dataset.id);
        const act = b.dataset.act;
        try {
          if (act.startsWith('speed-')) {
            const r = await api(`/api/orders/${id}/speed-test`, {
              method: 'POST', body: JSON.stringify({ force: act === 'speed-fail' ? 'fail' : 'pass' }),
            });
            toast(r.result.passed ? '测速达标，签收码已短信发送用户' : '测速不达标，不能转开通');
            return renderSpeed();
          }
          if (act === 'resend') {
            const r = await api(`/api/orders/${id}/resend-code`, { method: 'POST', body: '{}' });
            toast(`签收码已重发（模拟环境演示码：${r.demo_code}）`);
            return renderSpeed();
          }
          if (act === 'signoff') {
            dialog('用户验收签收', `
              <label class="field"><span>短信签收验证码 *</span><input id="dlg-code" placeholder="6 位验证码" maxlength="6"></label>
              <div class="reading-note">签收即表示用户现场确认测速结果；工程师角色提交将被系统拒绝。</div>`,
              async (root) => {
                await api(`/api/orders/${id}/signoff`, {
                  method: 'POST', body: JSON.stringify({ code: $('#dlg-code', root).value }),
                });
                toast('用户签收成功，工单已开通');
                renderSpeed();
              }, '确认签收');
          }
        } catch (e) { toast(e.message, true); }
      };
    });
  });
}

/* ---------------- 用户端：只看预约 / 测速 / 验收 ---------------- */

function customerStepIndex(status) {
  if (status === 'activated') return 4;
  if (status === 'speed_passed') return 3;
  if (['device_bound', 'speed_failed'].includes(status)) return 2;
  if (['surveyed', 'power_failed'].includes(status)) return 1;
  return 0;
}

function customerSteps(o) {
  const labels = ['预约成功', '上门勘线', '设备安装', '测速完成', '验收开通'];
  const cur = customerStepIndex(o.status);
  const doneUntil = o.status === 'activated' ? 5 : cur;
  return `<div class="steps">${labels.map((lb, i) =>
    `<div class="step ${i < doneUntil ? 'done' : ''} ${i === cur && o.status !== 'activated' ? 'current' : ''}">${lb}</div>`
  ).join('')}</div>`;
}

async function renderCustomer() {
  const m = $('#main');
  m.innerHTML = `
    <div class="customer-hero">
      <h2>我的宽带安装</h2>
      <p>这里只展示您的预约、测速与验收结果；资源端口等施工信息不对用户展示。</p>
      <div class="lookup">
        <input id="cust-phone" placeholder="输入申请时的手机号，如 13800000002" value="13800000002">
        <button class="btn green" id="cust-query">查询</button>
      </div>
    </div>
    <div id="cust-body"><p class="muted">输入手机号查询您的工单。演示账号：13800000002（测速已通过，签收码见模拟短信 123456）。</p></div>`;
  const draw = async () => {
    const phone = $('#cust-phone').value.trim();
    if (!phone) return;
    const all = await api('/api/orders');
    const mine = all.filter(o => o.customer_phone === phone);
    if (!mine.length) { $('#cust-body').innerHTML = '<p class="muted">未查询到该手机号的工单。</p>'; return; }
    const detailed = await Promise.all(mine.map(o => orderDetail(o.id, true)));
    $('#cust-body').innerHTML = detailed.map(o => {
      const last = o.speed_tests[o.speed_tests.length - 1];
      const signBox = o.status === 'speed_passed' ? `
        <div class="card mt12">
          <h3>验收签收</h3>
          <p class="muted" style="font-size:13px;margin:4px 0 10px">测速已达标，请查收短信中的 6 位签收码，由您本人确认开通。</p>
          <div style="display:flex;gap:8px">
            <input id="cust-code-${o.id}" placeholder="输入签收验证码" maxlength="6" style="max-width:200px">
            <button class="btn green" id="cust-sign-${o.id}">确认签收</button>
            <button class="btn ghost" id="cust-resend-${o.id}">重新获取</button>
          </div>
        </div>` : '';
      return `
      <div class="card">
        <div class="row" style="justify-content:space-between;display:flex">
          <b>工单 ${esc(o.order_no)}</b>${statusBadge(o.status)}</div>
        ${customerSteps(o)}
        <div class="row mt12"><span class="k">套餐</span>${esc(state.meta.plans[o.plan].name)}</div>
        <div class="row"><span class="k">预约时段</span><b>${esc(o.scheduled_window || o.expect_window || '待受理员预约')}</b></div>
        ${['reschedule', 'expired'].includes(o.status) ? `<div class="fail-reason">预约需调整：${esc(o.fail_reason || '')}，受理员将与您联系改期。</div>` : ''}
        ${last ? `<table class="speed-table"><tbody>
          <tr><td>下载速率</td><td class="${last.passed ? 'good' : 'bad'}">${last.down_mbps} Mbps</td><td class="muted">阈值 ≥ ${last.threshold_down}</td></tr>
          <tr><td>上传速率</td><td class="${last.passed ? 'good' : 'bad'}">${last.up_mbps} Mbps</td><td class="muted">阈值 ≥ ${last.threshold_up}</td></tr>
          <tr><td>网络时延</td><td>${last.ping_ms} ms</td><td>${last.passed ? '<span class="badge passed">测速达标</span>' : '<span class="badge failed">未达标</span>'}</td></tr>
        </tbody></table>` : '<p class="muted" style="font-size:13px">工程师尚未完成测速。</p>'}
        ${o.status === 'activated' ? `<div class="sms-banner" style="border-color:var(--green);background:var(--green-weak)">✅ 您已验收，宽带已正式开通。</div>` : ''}
        ${o.fail_reason && ['power_failed', 'speed_failed', 'address_invalid', 'shortage'].includes(o.status) ? `<div class="fail-reason">${esc(o.fail_reason)}</div>` : ''}
        ${smsBanners(o)}
        ${signBox}
      </div>`;
    }).join('');
    detailed.forEach(o => {
      const sb = $(`#cust-sign-${o.id}`), rb = $(`#cust-resend-${o.id}`);
      if (sb) sb.onclick = async () => {
        try {
          await api(`/api/orders/${o.id}/signoff`, {
            method: 'POST', body: JSON.stringify({ code: $(`#cust-code-${o.id}`).value }),
          });
          toast('签收成功，宽带已开通'); draw();
        } catch (e) { toast(e.message, true); }
      };
      if (rb) rb.onclick = async () => {
        const r = await api(`/api/orders/${o.id}/resend-code`, { method: 'POST', body: '{}' });
        toast(`新签收码已发送（演示码：${r.demo_code}）`); draw();
      };
    });
  };
  $('#cust-query').onclick = draw;
  $('#cust-phone').onkeydown = (e) => e.key === 'Enter' && draw();
  draw();
}

/* ---------------- 启动 ---------------- */
const RENDERERS = {
  map: renderMap, schedule: renderSchedule, survey: renderSurvey,
  device: renderDevice, speed: renderSpeed, customer: renderCustomer,
};

$('#tabs').onclick = async (e) => {
  const t = e.target.dataset.tab;
  if (!t || t === state.tab) return;
  state.tab = t;
  $('#tabs').querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  await RENDERERS[t]();
};
$('#role').onchange = async (e) => {
  state.role = e.target.value;
  state.detail = {};
  await RENDERERS[state.tab]();
};

(async function init() {
  await loadMeta();
  await renderMap();
})();
