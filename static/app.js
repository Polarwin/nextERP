/* nextERP mobile app — plain JS SPA */
"use strict";

const view = document.getElementById("view");
const titleEl = document.getElementById("title");
const backBtn = document.getElementById("back-btn");
const toastEl = document.getElementById("toast");

let stack = []; // navigation stack of render functions
let currentTab = "orders";

/* ---------------- helpers ---------------- */

async function api(path, opts = {}) {
  // relative path so the app works under a prefix (e.g. /luciatrading/)
  const r = await fetch(path.replace(/^\//, ""), {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

function toast(msg, ms = 2500) {
  toastEl.textContent = msg;
  toastEl.classList.remove("hidden");
  clearTimeout(toastEl._t);
  toastEl._t = setTimeout(() => toastEl.classList.add("hidden"), ms);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// safe JS string literal inside a double-quoted HTML attribute
function jsq(s) {
  return JSON.stringify(String(s ?? "")).replace(/"/g, "&quot;");
}

function money(n) {
  return n == null ? "" : "¥" + Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function statusBadge(status, docstatus) {
  if (docstatus === 2 || status === "Cancelled") return '<span class="badge grey">已取消</span>';
  if (docstatus === 0 || status === "Draft") return '<span class="badge orange">草稿</span>';
  const map = {
    "Completed": ["green", "已完成"],
    "To Deliver and Bill": ["blue", "待出货待开票"],
    "To Deliver": ["blue", "待出货"],
    "To Bill": ["orange", "待开票"],
    "Closed": ["grey", "已关闭"],
  };
  const [cls, zh] = map[status] || ["blue", status];
  return `<span class="badge ${cls}">${esc(zh)}</span>`;
}

function openPdf(doctype, name) {
  window.open(`api/pdf?doctype=${encodeURIComponent(doctype)}&name=${encodeURIComponent(name)}`, "_blank");
}

// Share the PDF as a real file (WeChat etc.) via the Web Share API;
// falls back to a plain download when file-sharing is unsupported.
// File/title show the customer name, e.g. "壹杯葡萄酒商店 SAL-ORD-....pdf"
async function sharePdf(doctype, name, btn, customer) {
  const label = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = "生成 PDF 中…"; }
  try {
    const r = await fetch(`api/pdf?doctype=${encodeURIComponent(doctype)}&name=${encodeURIComponent(name)}&download=1`);
    if (!r.ok) {
      const b = await r.json().catch(() => ({}));
      throw new Error(b.error || `HTTP ${r.status}`);
    }
    const blob = await r.blob();
    const label2 = `${(customer || "").trim()} ${name}`.trim();
    const file = new File([blob], `${label2}.pdf`, { type: "application/pdf" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: label2 });
    } else {
      // fallback: trigger a download so it lands in Files/Downloads
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${label2}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
      toast("PDF 已下载，可在文件中分享");
    }
  } catch (e) {
    if (e.name !== "AbortError") toast("分享失败：" + e.message, 4000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

function setTitle(t, showBack) {
  titleEl.textContent = t;
  backBtn.classList.toggle("hidden", !showBack);
}

function push(renderFn, ...args) {
  stack.push([renderFn, args]);
  renderFn(...args);
}

function pop() {
  stack.pop();
  const top = stack[stack.length - 1];
  if (top) top[0](...top[1]);
  else showTab(currentTab);
}

backBtn.onclick = pop;

/* ---------------- theme (dark mode) ---------------- */

const themeBtn = document.getElementById("theme-btn");

function applyTheme(mode) {
  // mode: "dark" | "light" | null (follow system, e.g. night mode)
  document.documentElement.classList.toggle("dark", mode === "dark");
  document.documentElement.classList.toggle("light", mode === "light");
  themeBtn.textContent = mode === "dark" ? "☀️" : mode === "light" ? "🌙" : "🌓";
  if (mode) localStorage.setItem("theme", mode);
  else localStorage.removeItem("theme");
}

themeBtn.onclick = () => {
  const cur = localStorage.getItem("theme");
  // cycle: auto -> dark -> light -> auto
  applyTheme(cur === null ? "dark" : cur === "dark" ? "light" : null);
};

applyTheme(localStorage.getItem("theme"));

/* ---------------- orders ---------------- */

async function renderOrders() {
  currentTab = "orders";
  setTitle("销售订单", false);
  view.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const rows = await api("/api/orders");
    const newBtn = `<button class="btn" onclick="push(renderNewOrder)">＋ 新建订单</button>`;
    const searchBox = `<input class="search-input" id="order-q" placeholder="搜索订单：客户名 / 拼音 / 单号…" autocomplete="off">
      <button class="btn warn hidden" id="stock-alert-btn" onclick="push(renderAlerts)"></button>
      <div id="order-list"></div>`;
    view.innerHTML = newBtn + searchBox;
    const renderList = (list) => {
      document.getElementById("order-list").innerHTML = list.length ? list.map(o => `
      <div class="card clickable" onclick="openOrder('${esc(o.name)}')">
        <div class="row">
          <span class="customer">${esc(o.customer_name)}</span>
          ${statusBadge(o.status, o.docstatus)}
        </div>
        <div class="row meta">
          <span class="name">${esc(o.name)}</span>
          <span>${esc(o.transaction_date)} · ${money(o.grand_total)}</span>
        </div>
        <div class="progress"><div style="width:${Math.min(100, o.per_delivered || 0)}%"></div></div>
        <div class="meta">已出货 ${Math.round(o.per_delivered || 0)}%</div>
      </div>`).join("") : '<div class="empty">没有匹配的订单</div>';
    };
    renderList(rows);
    // server-side search so pinyin works (一杯 / yibei / yb all match 壹杯)
    let searchT;
    document.getElementById("order-q").oninput = e => {
      clearTimeout(searchT);
      const q = e.target.value.trim();
      searchT = setTimeout(async () => {
        try {
          renderList(await api(`/api/orders?q=${encodeURIComponent(q)}`));
        } catch (err) { /* keep current list on error */ }
      }, 300);
    };
    // low-stock banner (non-blocking)
    api("/api/stock_alerts").then(data => {
      const active = (data.alerts || []).filter(a => !a.disarmed);
      const btn = document.getElementById("stock-alert-btn");
      if (!btn) return;
      if (active.length) {
        btn.textContent = `🔔 库存预警：${active.length} 个商品低于 ${data.threshold} 瓶`;
        btn.classList.remove("hidden");
      }
    }).catch(() => {});
  } catch (e) {
    view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

/* ---------------- stock alerts ---------------- */

async function renderAlerts() {
  setTitle("库存预警", true);
  view.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await api("/api/stock_alerts");
    const active = data.alerts.filter(a => !a.disarmed);
    const disarmed = data.alerts.filter(a => a.disarmed);
    const row = (a, isDisarmed) => `
      <div class="item-row${isDisarmed ? " disarmed" : ""}">
        <div class="item-info">
          <div class="item-name">${esc(a.item_name)}</div>
          <div class="item-code">${esc(a.item_code)}${isDisarmed
            ? ` · ${a.disarmed_until === "forever" ? "永久关闭" : "暂停至 " + esc(a.disarmed_until)}` : ""}</div>
        </div>
        <span class="qty-static" style="color:${a.total_qty <= 0 ? "#dc2626" : "#d97706"}">${a.total_qty} 瓶</span>
        ${isDisarmed
          ? `<button class="link-btn" onclick="rearm('${esc(a.item_code)}')">恢复</button>`
          : `<div style="display:flex;flex-direction:column">
               <button class="link-btn" onclick="disarm('${esc(a.item_code)}', 30)">30天</button>
               <button class="link-btn" onclick="disarm('${esc(a.item_code)}', 0)">永久</button>
             </div>`}
      </div>`;
    view.innerHTML = `
      <div class="section-title">库存低于 ${data.threshold} 瓶（全部仓库合计）</div>
      <div class="card">${active.length ? active.map(a => row(a, false)).join("") : '<div class="empty">没有低库存商品 🎉</div>'}</div>
      ${disarmed.length ? `
        <div class="section-title">已关闭提醒</div>
        <div class="card">${disarmed.map(a => row(a, true)).join("")}</div>` : ""}
    `;
  } catch (e) {
    view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function disarm(code, days) {
  try {
    await api("/api/stock_alerts/disarm", {
      method: "POST",
      body: JSON.stringify({ item_code: code, mode: days ? "days" : "forever", days }),
    });
    toast(days ? `已暂停提醒 30 天` : `已永久关闭提醒`);
    renderAlerts();
  } catch (e) { toast("操作失败：" + e.message, 4000); }
}

async function rearm(code) {
  try {
    await api("/api/stock_alerts/rearm", {
      method: "POST",
      body: JSON.stringify({ item_code: code }),
    });
    toast("已恢复提醒");
    renderAlerts();
  } catch (e) { toast("操作失败：" + e.message, 4000); }
}

async function renderOrderDetail(name) {
  setTitle(name, true);
  view.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const o = await api(`/api/orders/${encodeURIComponent(name)}`);
    const draft = o.docstatus === 0;
    const items = (o.items || []).map(it => `
      <div class="item-row">
        <div class="item-info">
          <div class="item-name">${esc(it.item_name)}</div>
          <div class="item-code">${esc(it.item_code)} · 已出 ${it.delivered_qty || 0} / ${it.qty} ${esc(it.uom || "")}</div>
        </div>
        <span class="qty-static">${it.qty}</span>
      </div>`).join("");
    view.innerHTML = `
      <div class="card detail-head">
        <div class="row"><span class="customer">${esc(o.customer_name)}</span>${statusBadge(o.status, o.docstatus)}</div>
        <div class="kv"><span>日期</span><b>${esc(o.transaction_date)}</b></div>
        <div class="kv"><span>小计</span><b>${money(o.net_total)}</b></div>
        <div class="kv"><span>费用/税费</span><b>${money(o.total_taxes_and_charges)}</b></div>
        <div class="kv"><span>总计</span><b>${money(o.grand_total)}</b></div>
      </div>
      <div class="section-title">明细</div>
      <div class="card">${items || '<div class="empty">无明细</div>'}</div>
      <div class="section-title">物流方式</div>
      <div class="card">${draft
        ? `<select id="so-shipping-rule" class="search-input"></select>`
        : `<b>${esc(o.shipping_rule || "—")}</b>`}</div>
      <div class="section-title">费用 / 税费${draft ? "（可编辑）" : ""}</div>
      <div class="card" id="so-charges"></div>
      ${draft ? `<button class="btn secondary" id="save-so-charges">💾 保存修改</button>` : ""}
      <button class="btn secondary" onclick="openPdf('Sales Order','${esc(o.name)}')">🖨 打印订单 PDF</button>
      <button class="btn secondary" onclick='sharePdf("Sales Order", ${jsq(o.name)}, this, ${jsq(o.customer_name)})'>📤 分享订单 PDF（微信）</button>
      ${draft
        ? `<button class="btn danger" id="submit-so">✅ 提交订单并打印 PDF</button>` : ""}
      ${o.docstatus === 1 && !["Completed", "Closed", "Cancelled"].includes(o.status)
        ? `<button class="btn danger" id="mkdn">🚚 创建出货单</button>` : ""}
    `;
    window._order = o;
    loadOrderMeta().then(() => {
      if (draft) {
        const sel = document.getElementById("so-shipping-rule");
        sel.innerHTML = `<option value="">（无）</option>` +
          orderMeta.shipping_rules.map(r =>
            `<option value="${esc(r)}" ${r === o.shipping_rule ? "selected" : ""}>${esc(r)}</option>`).join("");
        sel.onchange = () => { window._order.shipping_rule = sel.value; };
        renderChargesEditor("so-charges", window._order.taxes);
      } else {
        renderChargesReadonly("so-charges", o.taxes);
      }
    });
    const mk = document.getElementById("mkdn");
    if (mk) mk.onclick = () => makeDelivery(o.name, mk);
    const sub = document.getElementById("submit-so");
    if (sub) sub.onclick = () => submitOrder(o.name, sub);
    const save = document.getElementById("save-so-charges");
    if (save) save.onclick = () => saveOrderCharges(o.name, save);
  } catch (e) {
    view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

/* ------------- charges (费用/税费) editor ------------- */

let orderMeta = null;
async function loadOrderMeta() {
  if (!orderMeta) orderMeta = await api("/api/order_meta");
  return orderMeta;
}

function renderChargesReadonly(boxId, taxes) {
  const box = document.getElementById(boxId);
  if (!box) return;
  box.innerHTML = (taxes || []).length ? taxes.map(t => `
    <div class="item-row">
      <div class="item-info">
        <div class="item-name">${esc(t.description || t.account_head)}</div>
        <div class="item-code">${esc(t.account_head)}</div>
      </div>
      <span class="qty-static">${money(t.tax_amount)}</span>
    </div>`).join("") : '<div class="empty">无费用</div>';
}

// charges: array of row objects (may be full ERP child rows or new plain ones)
function renderChargesEditor(boxId, charges) {
  const box = document.getElementById(boxId);
  if (!box) return;
  const accountOpts = sel => orderMeta.charge_accounts.map(a =>
    `<option value="${esc(a)}" ${a === sel ? "selected" : ""}>${esc(a.replace(/ - LTL$/, ""))}</option>`).join("");
  box.innerHTML = charges.map((t, i) => `
    <div class="item-row">
      <div class="item-info">
        <select class="search-input" onchange="chSetAccount(${boxId === "so-charges" ? 1 : 0}, ${i}, this.value)">
          ${accountOpts(t.account_head)}
        </select>
        <input class="search-input" style="margin-top:6px" placeholder="描述（如 德邦+冰袋）"
          value="${esc(t.description || "")}" onchange="chSetDesc(${boxId === "so-charges" ? 1 : 0}, ${i}, this.value)">
      </div>
      <div class="stepper">
        <input class="rate-input" type="number" inputmode="decimal" min="0" step="0.01"
          value="${t.tax_amount || 0}" onchange="chSetAmount(${boxId === "so-charges" ? 1 : 0}, ${i}, this.value)">
        <button class="link-btn del" onclick="chRemove(${boxId === "so-charges" ? 1 : 0}, ${i})">✕</button>
      </div>
    </div>`).join("")
    + `<button class="link-btn" onclick="chAdd(${boxId === "so-charges" ? 1 : 0})">＋ 添加费用</button>`;
}

// src: 1 = draft order detail (window._order), 0 = new-order form (no.charges)
function chRows(src) { return src === 1 ? window._order.taxes : no.charges; }
function chRerender(src) {
  renderChargesEditor(src === 1 ? "so-charges" : "no-charges", chRows(src));
}
function chSetAccount(src, i, v) { chRows(src)[i].account_head = v; }
function chSetDesc(src, i, v) { chRows(src)[i].description = v; }
function chSetAmount(src, i, v) { chRows(src)[i].tax_amount = Math.max(0, parseFloat(v) || 0); }
function chRemove(src, i) { chRows(src).splice(i, 1); chRerender(src); }
function chAdd(src) {
  chRows(src).push({ account_head: orderMeta.charge_accounts[0],
                     description: "", tax_amount: 0 });
  chRerender(src);
}

async function saveOrderCharges(name, btn) {
  btn.disabled = true;
  try {
    await api(`/api/orders/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({
        shipping_rule: window._order.shipping_rule || "",
        taxes: window._order.taxes,
      }),
    });
    toast("已保存");
    renderOrderDetail(name);
  } catch (e) {
    toast("保存失败：" + e.message, 5000);
    btn.disabled = false;
  }
}

async function submitOrder(name, btn) {
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    // persist any unsaved charge edits before submitting
    await api(`/api/orders/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({
        shipping_rule: window._order.shipping_rule || "",
        taxes: window._order.taxes,
      }),
    });
    await api(`/api/orders/${encodeURIComponent(name)}/submit`, { method: "POST" });
    toast(`订单 ${name} 已提交`);
    openPdf("Sales Order", name);
    renderOrderDetail(name);
  } catch (e) {
    toast("提交失败：" + e.message, 5000);
    btn.disabled = false;
    btn.textContent = "✅ 提交订单并打印 PDF";
  }
}

async function makeDelivery(orderName, btn) {
  btn.disabled = true;
  btn.textContent = "创建中…";
  try {
    const dn = await api(`/api/orders/${encodeURIComponent(orderName)}/make_delivery`, { method: "POST" });
    toast(`已创建出货单 ${dn.name}`);
    document.querySelector('[data-tab="deliveries"]').classList.add("active");
    stack = [];
    push(renderDeliveryDetail, dn.name);
  } catch (e) {
    toast("创建失败：" + e.message, 4000);
    btn.disabled = false;
    btn.textContent = "🚚 创建出货单";
  }
}

/* ---------------- deliveries ---------------- */

async function renderDeliveries() {
  currentTab = "deliveries";
  setTitle("销售出货", false);
  view.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const rows = await api("/api/deliveries");
    if (!rows.length) { view.innerHTML = '<div class="empty">没有出货单</div>'; return; }
    view.innerHTML = rows.map(d => `
      <div class="card clickable" onclick="openDelivery('${esc(d.name)}')">
        <div class="row">
          <span class="customer">${esc(d.customer_name)}</span>
          ${statusBadge(d.status, d.docstatus)}
        </div>
        <div class="row meta">
          <span class="name">${esc(d.name)}</span>
          <span>${esc(d.posting_date)} · ${money(d.grand_total)}</span>
        </div>
      </div>`).join("");
  } catch (e) {
    view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function renderDeliveryDetail(name) {
  setTitle(name, true);
  view.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const d = await api(`/api/deliveries/${encodeURIComponent(name)}`);
    const draft = d.docstatus === 0;
    const items = (d.items || []).map((it, i) => `
      <div class="item-row">
        <div class="item-info">
          <div class="item-name">${esc(it.item_name)}</div>
          <div class="item-code">${esc(it.item_code)}</div>
          ${draft
            ? `<select class="wh-select search-input" data-idx="${i}"></select>`
            : `<div class="wh">仓库：${esc(it.warehouse || "—")} · ${esc(it.uom || "")}</div>`}
        </div>
        ${draft ? `
        <div class="stepper">
          <button onclick="stepQty(${i}, -1)">−</button>
          <input type="number" inputmode="decimal" min="0" step="1" value="${it.qty}"
                 id="qty-${i}" onchange="setQty(${i}, this.value)">
          <button onclick="stepQty(${i}, 1)">＋</button>
        </div>` : `<span class="qty-static">${it.qty} ${esc(it.uom || "")}</span>`}
      </div>`).join("");
    view.innerHTML = `
      <div class="card detail-head">
        <div class="row"><span class="customer">${esc(d.customer_name)}</span>${statusBadge(d.status, d.docstatus)}</div>
        <div class="kv"><span>日期</span><b>${esc(d.posting_date)}</b></div>
        <div class="kv"><span>金额</span><b>${money(d.grand_total)}</b></div>
      </div>
      <div class="section-title">明细${draft ? "（可编辑数量）" : ""}</div>
      <div class="card">${items || '<div class="empty">无明细</div>'}</div>
      ${draft ? `
        <button class="btn secondary" id="save-draft">💾 保存草稿</button>
        <button class="btn danger" id="submit-print">✅ 提交出货并打印 PDF（扣库存）</button>
      ` : `
        <button class="btn secondary" onclick="openPdf('Delivery Note','${esc(d.name)}')">🖨 打印出货单 PDF</button>
        <button class="btn secondary" onclick='sharePdf("Delivery Note", ${jsq(d.name)}, this, ${jsq(d.customer_name)})'>📤 分享出货单 PDF（微信）</button>
      `}
    `;
    window._dn = d;
    if (draft) {
      document.getElementById("save-draft").onclick = () => saveDraft(d.name);
      document.getElementById("submit-print").onclick = () => submitAndPrint(d.name);
      loadWarehouses().then(() => {
        document.querySelectorAll(".wh-select").forEach(sel => {
          const i = Number(sel.dataset.idx);
          sel.innerHTML = warehouseOptions(window._dn.items[i].warehouse);
          sel.onchange = () => { window._dn.items[i].warehouse = sel.value; };
        });
      });
    }
  } catch (e) {
    view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function stepQty(i, delta) {
  const inp = document.getElementById(`qty-${i}`);
  const v = Math.max(0, (parseFloat(inp.value) || 0) + delta);
  inp.value = v;
  setQty(i, v);
}

function setQty(i, v) {
  const n = Math.max(0, parseFloat(v) || 0);
  if (window._dn && window._dn.items[i]) window._dn.items[i].qty = n;
}

async function saveDraft(name) {
  try {
    await api(`/api/deliveries/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ items: window._dn.items }),
    });
    toast("草稿已保存");
    renderDeliveryDetail(name);
  } catch (e) {
    toast("保存失败：" + e.message, 4000);
  }
}

async function submitAndPrint(name) {
  if (!confirm("提交出货单将扣减库存，确定提交并打印？")) return;
  const btn = document.getElementById("submit-print");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    await api(`/api/deliveries/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ items: window._dn.items }),
    });
    await api(`/api/deliveries/${encodeURIComponent(name)}/submit`, { method: "POST" });
    toast("已提交，库存已扣减");
    openPdf("Delivery Note", name);
    renderDeliveryDetail(name);
  } catch (e) {
    toast("提交失败：" + e.message, 5000);
    btn.disabled = false;
    btn.textContent = "✅ 提交出货并打印 PDF（扣库存）";
  }
}

/* ---------------- new order ---------------- */

// form state while building an order
let no = null; // {customer, customer_name, delivery_date, warehouse, shipping_rule, items:[...], charges:[...]}
let warehousesCache = null; // {warehouses: [...], default: "..."}

async function loadWarehouses() {
  if (!warehousesCache) warehousesCache = await api("/api/warehouses");
  return warehousesCache;
}

function warehouseOptions(selected) {
  return (warehousesCache.warehouses || []).map(w =>
    `<option value="${esc(w.name)}" ${w.name === selected ? "selected" : ""}>${esc(w.warehouse_name)}</option>`
  ).join("");
}

function renderNewOrder() {
  setTitle("新建订单", true);
  if (!no) {
    const tomorrow = new Date(Date.now() + 864e5).toISOString().slice(0, 10);
    no = { customer: null, customer_name: null, delivery_date: tomorrow,
           warehouse: null, shipping_rule: null, items: [], charges: [] };
  }
  view.innerHTML = `
    <button class="btn secondary" id="voice-order" style="margin-top:0">🎤 语音下单（客户 + 商品 + 数量）</button>
    <div id="voice-panel" class="card hidden">
      <textarea id="voice-text" class="search-input" rows="3"
        placeholder="点输入框，用键盘自带的 🎤 听写（或直接打字），例如：壹杯，雷司令三瓶，GG两瓶"></textarea>
      <button class="btn" id="voice-parse">解析并填入表单</button>
    </div>
    <div class="section-title">客户</div>
    <div class="card">
      <div id="cust-picked" class="${no.customer ? "" : "hidden"}">
        <div class="row">
          <span class="customer">${esc(no.customer_name || "")}</span>
          <button class="link-btn" onclick="clearCustomer()">更换</button>
        </div>
      </div>
      <div id="cust-search" class="${no.customer ? "hidden" : ""}">
        <input class="search-input" id="cust-q" placeholder="搜索客户名称…" autocomplete="off">
        <div id="cust-results"></div>
        <button class="link-btn" onclick="push(renderNewCustomer)">＋ 新建客户</button>
      </div>
    </div>
    <div class="section-title">交货日期</div>
    <div class="card"><input type="date" id="no-date" class="search-input" value="${no.delivery_date}"></div>
    <div class="section-title">发货仓库</div>
    <div class="card"><select id="no-warehouse" class="search-input"></select></div>
    <div class="section-title">物流方式</div>
    <div class="card"><select id="no-shipping-rule" class="search-input"></select></div>
    <div class="section-title">商品</div>
    <div class="card">
      <div id="no-items"></div>
      <input class="search-input" id="item-q" placeholder="搜索商品编号或名称…" autocomplete="off">
      <div id="item-results"></div>
    </div>
    <div class="section-title">费用 / 税费</div>
    <div class="card" id="no-charges"></div>
    <div class="card"><div class="row"><b>合计</b><b id="no-total">¥0</b></div></div>
    <button class="btn secondary" id="no-save">💾 保存草稿</button>
    <button class="btn danger" id="no-submit">✅ 提交订单并打印 PDF</button>
  `;
  renderNoItems();
  bindSearch("cust-q", "cust-results", "/api/customers?q=", renderCustomerHits);
  bindSearch("item-q", "item-results", "/api/items?q=", renderItemHits);
  document.getElementById("no-date").onchange = e => { no.delivery_date = e.target.value; };
  document.getElementById("no-save").onclick = () => saveNewOrder(false);
  document.getElementById("no-submit").onclick = () => saveNewOrder(true);
  document.getElementById("voice-order").onclick = () => {
    const p = document.getElementById("voice-panel");
    p.classList.toggle("hidden");
    if (!p.classList.contains("hidden"))
      document.getElementById("voice-text").focus();
  };
  document.getElementById("voice-parse").onclick = (e) => parseVoice(e.target);
  loadWarehouses().then(() => {
    const sel = document.getElementById("no-warehouse");
    if (!sel) return;
    if (!no.warehouse) no.warehouse = warehousesCache.default;
    sel.innerHTML = warehouseOptions(no.warehouse);
    sel.onchange = () => { no.warehouse = sel.value; };
  });
  loadOrderMeta().then(() => {
    const sr = document.getElementById("no-shipping-rule");
    if (!sr) return;
    sr.innerHTML = `<option value="">（无）</option>` +
      orderMeta.shipping_rules.map(r =>
        `<option value="${esc(r)}" ${r === no.shipping_rule ? "selected" : ""}>${esc(r)}</option>`).join("");
    sr.onchange = () => { no.shipping_rule = sr.value; };
    renderChargesEditor("no-charges", no.charges);
  });
}

function renderNoItems() {
  const box = document.getElementById("no-items");
  if (!box) return;
  box.innerHTML = no.items.map((it, i) => `
    <div class="item-row">
      <div class="item-info">
        <div class="item-name">${esc(it.item_name)}</div>
        <div class="item-code">${esc(it.item_code)} · ${esc(it.uom || "")}</div>
        <div class="wh">单价 <input class="rate-input" type="number" inputmode="decimal"
          min="0" step="0.01" value="${it.rate}" onchange="noSetRate(${i}, this.value)"></div>
      </div>
      <div class="stepper">
        <button onclick="noStepQty(${i}, -1)">−</button>
        <input type="number" inputmode="decimal" min="0" step="1" value="${it.qty}"
               id="no-qty-${i}" onchange="noSetQty(${i}, this.value)">
        <button onclick="noStepQty(${i}, 1)">＋</button>
      </div>
      <button class="link-btn del" onclick="noRemove(${i})">✕</button>
    </div>`).join("");
  const total = no.items.reduce((s, it) => s + (it.qty || 0) * (it.rate || 0), 0)
    + no.charges.reduce((s, c) => s + (c.tax_amount || 0), 0);
  const t = document.getElementById("no-total");
  if (t) t.textContent = money(total);
}

function noStepQty(i, d) {
  const inp = document.getElementById(`no-qty-${i}`);
  const v = Math.max(0, (parseFloat(inp.value) || 0) + d);
  inp.value = v;
  noSetQty(i, v);
}
function noSetQty(i, v) { no.items[i].qty = Math.max(0, parseFloat(v) || 0); renderNoItems(); }
function noSetRate(i, v) { no.items[i].rate = Math.max(0, parseFloat(v) || 0); renderNoItems(); }
function noRemove(i) { no.items.splice(i, 1); renderNoItems(); }

function clearCustomer() {
  no.customer = null; no.customer_name = null;
  document.getElementById("cust-picked").classList.add("hidden");
  document.getElementById("cust-search").classList.remove("hidden");
}

let searchTimers = {};
function bindSearch(inputId, resultsId, urlPrefix, renderHits) {
  const inp = document.getElementById(inputId);
  const box = document.getElementById(resultsId);
  if (!inp || !box) return;
  inp.oninput = () => {
    clearTimeout(searchTimers[inputId]);
    const q = inp.value.trim();
    searchTimers[inputId] = setTimeout(async () => {
      try {
        const rows = await api(urlPrefix + encodeURIComponent(q));
        box.innerHTML = renderHits(rows);
      } catch (e) { box.innerHTML = ""; }
    }, 250);
  };
}

function renderCustomerHits(rows) {
  return rows.map(c => `
    <div class="hit" onclick="pickCustomer('${esc(c.name)}','${esc(c.customer_name)}')">
      ${esc(c.customer_name)}</div>`).join("");
}

function pickCustomer(name, display) {
  no.customer = name; no.customer_name = display;
  document.getElementById("cust-picked").classList.remove("hidden");
  document.getElementById("cust-picked").querySelector(".customer").textContent = display;
  document.getElementById("cust-search").classList.add("hidden");
}

function renderItemHits(rows) {
  return rows.map(it => `
    <div class="hit" onclick='pickItem(${JSON.stringify(it.item_code)})'>
      <b>${esc(it.item_code)}</b> ${esc(it.item_name)}</div>`).join("");
}

async function pickItem(code) {
  const existing = no.items.find(it => it.item_code === code);
  if (existing) { existing.qty += 1; renderNoItems(); return; }
  let info = { rate: 0 };
  try {
    info = await api(`/api/item_price?item_code=${encodeURIComponent(code)}`
      + (no.customer ? `&customer=${encodeURIComponent(no.customer)}` : ""));
  } catch (e) { /* keep rate 0 */ }
  // fetch display name/uom from last search results
  const hits = await api(`/api/items?q=${encodeURIComponent(code)}`);
  const it = hits.find(h => h.item_code === code) || { item_code: code, item_name: code };
  no.items.push({ item_code: code, item_name: it.item_name,
                  uom: it.stock_uom, qty: 1, rate: info.rate || 0 });
  document.getElementById("item-q").value = "";
  document.getElementById("item-results").innerHTML = "";
  renderNoItems();
}

async function saveNewOrder(submit) {
  if (!no.customer) { toast("请先选择客户"); return; }
  const items = no.items.filter(it => it.qty > 0);
  if (!items.length) { toast("请添加至少一个商品"); return; }
  const btn = document.getElementById(submit ? "no-submit" : "no-save");
  btn.disabled = true;
  try {
    const o = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({
        customer: no.customer,
        delivery_date: no.delivery_date,
        warehouse: no.warehouse,
        shipping_rule: no.shipping_rule || undefined,
        charges: no.charges.filter(c => c.tax_amount > 0 || c.description),
        items: items.map(it => ({ item_code: it.item_code, qty: it.qty, rate: it.rate })),
        submit,
      }),
    });
    toast(submit ? `订单 ${o.name} 已提交` : `订单 ${o.name} 已保存为草稿`);
    no = null;
    if (submit) openPdf("Sales Order", o.name);
    stack = [];
    push(renderOrderDetail, o.name);
  } catch (e) {
    toast("失败：" + e.message, 5000);
    btn.disabled = false;
  }
}

/* ---------------- voice order dictation (Kimi parsing) ---------------- */
// Transcription happens at the OS keyboard level (iOS/Android dictation mic),
// which works in any browser incl. WeChat — no Web Speech API needed.

async function parseVoice(btn) {
  const text = document.getElementById("voice-text").value.trim();
  if (!text) { toast("先听写或输入订单内容"); return; }
  btn.disabled = true;
  btn.textContent = "解析中…";
  try {
    const d = await api("/api/parse_order", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    applyParsedOrder(d, text);
  } catch (e) {
    toast("解析失败：" + e.message, 5000);
  } finally {
    btn.disabled = false;
    btn.textContent = "解析并填入表单";
  }
}

function applyParsedOrder(d, text) {
  if (!no) return;
  if (d.customer) {
    no.customer = d.customer.name;
    no.customer_name = d.customer.customer_name;
  }
  for (const it of d.items || []) {
    const ex = no.items.find(x => x.item_code === it.item_code);
    if (ex) ex.qty += it.qty;
    else no.items.push({ item_code: it.item_code, item_name: it.item_name,
                         uom: it.uom, qty: it.qty, rate: it.rate });
  }
  renderNewOrder();
  const parts = [];
  if (d.customer) parts.push("客户 " + d.customer.customer_name);
  parts.push((d.items || []).length + " 个商品");
  if (d.unmatched && d.unmatched.length)
    parts.push("未识别：" + d.unmatched.join("、"));
  if (d.notes && d.notes !== "null") parts.push("备注：" + d.notes);
  toast(`「${text}」→ ` + parts.join("，") + "，请核对", 6000);
}

/* ---------------- new customer ---------------- */

let nc = null; // {customer_name, customer_group, territory, price_list}
let custMeta = null;

function renderNewCustomer() {
  setTitle("新建客户", true);
  if (!nc) nc = { customer_name: "", customer_group: "Distributor",
                  territory: "Shanghai", price_list: "" };
  view.innerHTML = `
    <div class="section-title">客户名称</div>
    <div class="card"><input class="search-input" id="nc-name" placeholder="例如：D 某某贸易公司"
      value="${esc(nc.customer_name)}" autocomplete="off"></div>
    <div class="section-title">客户群组</div>
    <div class="card"><select id="nc-group" class="search-input"></select></div>
    <div class="section-title">区域</div>
    <div class="card"><select id="nc-territory" class="search-input"></select></div>
    <div class="section-title">价格表（可选）</div>
    <div class="card"><select id="nc-pricelist" class="search-input"></select></div>
    <button class="btn danger" id="nc-save">✅ 创建客户</button>
  `;
  document.getElementById("nc-name").oninput = e => { nc.customer_name = e.target.value; };
  document.getElementById("nc-save").onclick = saveNewCustomer;
  (custMeta ? Promise.resolve(custMeta) : api("/api/customer_meta")).then(meta => {
    custMeta = meta;
    const g = document.getElementById("nc-group");
    const t = document.getElementById("nc-territory");
    const pl = document.getElementById("nc-pricelist");
    if (!g) return;
    g.innerHTML = meta.customer_groups.map(x =>
      `<option ${x === nc.customer_group ? "selected" : ""}>${esc(x)}</option>`).join("");
    t.innerHTML = meta.territories.map(x =>
      `<option ${x === nc.territory ? "selected" : ""}>${esc(x)}</option>`).join("");
    pl.innerHTML = `<option value="">（默认 ${esc(meta.default_price_list)}）</option>`
      + meta.price_lists.map(x =>
        `<option ${x === nc.price_list ? "selected" : ""}>${esc(x)}</option>`).join("");
    g.onchange = () => { nc.customer_group = g.value; };
    t.onchange = () => { nc.territory = t.value; };
    pl.onchange = () => { nc.price_list = pl.value; };
  });
}

async function saveNewCustomer() {
  if (!nc.customer_name.trim()) { toast("请输入客户名称"); return; }
  const btn = document.getElementById("nc-save");
  btn.disabled = true;
  try {
    const c = await api("/api/customers", {
      method: "POST",
      body: JSON.stringify({
        customer_name: nc.customer_name.trim(),
        customer_group: nc.customer_group,
        territory: nc.territory,
        default_price_list: nc.price_list || undefined,
      }),
    });
    toast(`客户 ${c.customer_name} 已创建`);
    const created = { name: c.name, customer_name: c.customer_name };
    nc = null;
    pop(); // back to new-order form
    pickCustomer(created.name, created.customer_name);
  } catch (e) {
    toast("创建失败：" + e.message, 4000);
    btn.disabled = false;
  }
}

/* ---------------- navigation ---------------- */

function openOrder(name) { push(renderOrderDetail, name); }
function openDelivery(name) { push(renderDeliveryDetail, name); }

function showTab(tab) {
  currentTab = tab;
  stack = [];
  document.querySelectorAll(".tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
  if (tab === "orders") renderOrders();
  else renderDeliveries();
}

document.querySelectorAll(".tab").forEach(b => {
  b.onclick = () => showTab(b.dataset.tab);
});

renderOrders();
