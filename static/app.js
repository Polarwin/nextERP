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
    // Inside WeChat's built-in browser, sharing a file to WeChat via the
    // system share sheet just loops back to this page (WeChat resumes its
    // own activity instead of opening the friend picker). Skip the share
    // sheet there and go straight to the download fallback.
    const inWechat = /MicroMessenger/i.test(navigator.userAgent);
    if (!inWechat && navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: label2 });
    } else {
      // fallback: trigger a download so it lands in Files/Downloads
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${label2}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
      toast(inWechat
        ? "PDF 已下载：打开下载的文件，点右上角 ⋯ 转发给朋友。（或用浏览器打开本页再分享）"
        : "PDF 已下载，可在文件中分享", 6000);
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
        ? `<button class="btn danger" id="submit-so">✅ 提交订单</button>` : ""}
      ${o.docstatus === 1 && !["Completed", "Closed", "Cancelled"].includes(o.status)
        ? `<div id="delivery-meta" class="card">
             <div class="loading">加载送货地址和联系人…</div>
           </div>
           <button class="btn danger" id="mkdn">🚚 创建出货单</button>` : ""}
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
    if (mk) {
      loadDeliveryMeta(o.customer, o.shipping_address_name, o.contact_person);
      mk.onclick = () => makeDelivery(o.name, mk);
    }
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
  // charge_type is mandatory on ERP tax rows — a row without it fails
  // server-side with MandatoryError: charge_type. Actual = fixed amount.
  chRows(src).push({ account_head: orderMeta.charge_accounts[0],
                     charge_type: "Actual",
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
    renderOrderDetail(name);
  } catch (e) {
    toast("提交失败：" + e.message, 5000);
    btn.disabled = false;
    btn.textContent = "✅ 提交订单";
  }
}

async function loadDeliveryMeta(customer, selectedAddress, selectedContact) {
  const box = document.getElementById("delivery-meta");
  if (!box) return;
  try {
    const meta = await api(`/api/customer_delivery_meta?customer=${encodeURIComponent(customer)}`);
    const addressLabel = a => [a.address_title || a.name, a.address_line1, a.city]
      .filter(Boolean).join(" · ");
    const contactLabel = c => [
      [c.first_name, c.last_name].filter(Boolean).join(" ") || c.name,
      c.mobile_no || c.phone || c.email_id,
    ].filter(Boolean).join(" · ");
    box.innerHTML = `
      <div class="section-title" style="margin-top:0">送货地址</div>
      <select id="dn-address" class="search-input">
        <option value="">使用 ERPNext 默认地址</option>
        ${(meta.addresses || []).map(a => `<option value="${esc(a.name)}"
          ${a.name === selectedAddress ? "selected" : ""}>${esc(addressLabel(a))}</option>`).join("")}
      </select>
      <div class="section-title">联系人</div>
      <select id="dn-contact" class="search-input">
        <option value="">使用 ERPNext 默认联系人</option>
        ${(meta.contacts || []).map(c => `<option value="${esc(c.name)}"
          ${c.name === selectedContact ? "selected" : ""}>${esc(contactLabel(c))}</option>`).join("")}
      </select>`;
  } catch (e) {
    box.innerHTML = `<div class="empty">地址/联系人加载失败：${esc(e.message)}</div>`;
  }
}

async function makeDelivery(orderName, btn) {
  btn.disabled = true;
  btn.textContent = "创建中…";
  try {
    const address = document.getElementById("dn-address");
    const contact = document.getElementById("dn-contact");
    const dn = await api(`/api/orders/${encodeURIComponent(orderName)}/make_delivery`, {
      method: "POST",
      body: JSON.stringify({
        shipping_address_name: address ? address.value : "",
        contact_person: contact ? contact.value : "",
      }),
    });
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
          <span>${esc(d.posting_date)}</span>
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
      </div>
      <div class="section-title">明细${draft ? "（可编辑数量）" : ""}</div>
      <div class="card">${items || '<div class="empty">无明细</div>'}</div>
      ${draft ? `
        <button class="btn secondary" id="save-draft">💾 保存草稿</button>
        <button class="btn danger" id="submit-dn">✅ 提交出货（扣库存）</button>
        <button class="btn secondary" onclick="openPdf('Delivery Note','${esc(d.name)}')">🖨 打印出货单 PDF</button>
        <button class="btn secondary" onclick='sharePdf("Delivery Note", ${jsq(d.name)}, this, ${jsq(d.customer_name)})'>📤 分享出货单 PDF 文件（微信）</button>
      ` : `
        <button class="btn secondary" onclick="openPdf('Delivery Note','${esc(d.name)}')">🖨 打印出货单 PDF</button>
        <button class="btn secondary" onclick='sharePdf("Delivery Note", ${jsq(d.name)}, this, ${jsq(d.customer_name)})'>📤 分享出货单 PDF 文件（微信）</button>
      `}
    `;
    window._dn = d;
    if (draft) {
      document.getElementById("save-draft").onclick = () => saveDraft(d.name);
      document.getElementById("submit-dn").onclick = () => submitDelivery(d.name);
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

async function submitDelivery(name) {
  if (!confirm("提交出货单将扣减库存，确定提交？")) return;
  const btn = document.getElementById("submit-dn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    await api(`/api/deliveries/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ items: window._dn.items }),
    });
    await api(`/api/deliveries/${encodeURIComponent(name)}/submit`, { method: "POST" });
    toast("已提交，库存已扣减");
    renderDeliveryDetail(name);
  } catch (e) {
    toast("提交失败：" + e.message, 5000);
    btn.disabled = false;
    btn.textContent = "✅ 提交出货（扣库存）";
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
      <div id="prog-wrap" class="hidden" style="margin-bottom:8px">
        <div class="progress" style="height:8px"><div id="prog-bar" style="width:0%;transition:width .12s linear"></div></div>
      </div>
      <div id="joke-box" class="hidden" style="text-align:center;color:var(--muted);font-size:14px;padding:6px 0"></div>
      <button class="btn danger" id="voice-record">🎤 按住说话</button>
      <button class="btn secondary ${(no.voice && no.voice.applied) ? "" : "hidden"}" id="voice-redo">🔁 讲错了，重讲（清除上次填入）</button>
      <div class="meta" style="text-align:center;margin:6px 0">松开后自动识别并填入表单（需 HTTPS 打开）</div>
      <textarea id="voice-text" class="search-input" rows="3"
        placeholder="也可以点这里，用键盘自带的 🎤 听写或打字：壹杯，雷司令三瓶，GG两瓶"></textarea>
      <button class="btn" id="voice-parse">解析并填入表单</button>
    </div>
    <div class="section-title">客户</div>
    <div class="card">
      <div id="cust-picked" class="${no.customer ? "" : "hidden"}">
        <div class="row">
          <span class="customer">${esc(no.customer_name || "")}</span>
          <button class="link-btn" onclick="clearCustomer()">更换</button>
        </div>
        ${no.customer_uncertain
          ? `<div class="meta" id="customer-warning" style="color:#d97706;margin-top:8px">
              ⚠️ 客户识别不确定，请核对
              ${(no.customer_suggestions || []).length ? `
                <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
                  ${no.customer_suggestions.map((c, i) => `
                    <button class="link-btn" onclick="pickCustomerSuggestion(${i})">${esc(c.customer_name)}</button>
                  `).join("")}
                </div>` : ""}
            </div>`
          : ""}
      </div>
      ${no.candidates && !no.customer ? `
      <div id="cust-candidates">
        <div class="meta" style="margin-bottom:6px">是哪个客户？</div>
        ${no.candidates.map((c, i) => `
          <button class="btn secondary" style="margin-top:6px" onclick="pickCandidate(${i})">${esc(c.customer_name)}</button>
        `).join("")}
      </div>` : ""}
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
    <button class="btn danger" id="no-submit">✅ 提交订单</button>
    <button class="btn secondary" id="no-print">🖨 打印 PDF</button>
  `;
  renderNoItems();
  bindSearch("cust-q", "cust-results", "/api/customers?q=", renderCustomerHits);
  bindSearch("item-q", "item-results", "/api/items?q=", renderItemHits);
  document.getElementById("no-date").onchange = e => { no.delivery_date = e.target.value; };
  document.getElementById("no-save").onclick = () => saveNewOrder(false);
  document.getElementById("no-submit").onclick = () => saveNewOrder(true, false);
  document.getElementById("no-print").onclick = () => saveNewOrder(false, true);
  document.getElementById("voice-order").onclick = () => {
    const p = document.getElementById("voice-panel");
    p.classList.toggle("hidden");
    if (!p.classList.contains("hidden"))
      document.getElementById("voice-text").focus();
  };
  document.getElementById("voice-parse").onclick = (e) => parseVoice(e.target);
  bindRecordButton();
  const redo = document.getElementById("voice-redo");
  if (redo) redo.onclick = undoVoice;
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
function noSetRate(i, v) {
  no.items[i].rate = Math.max(0, parseFloat(v) || 0);
  no.items[i].is_free = no.items[i].rate === 0;
  renderNoItems();
}
function noRemove(i) { no.items.splice(i, 1); renderNoItems(); }

function undoVoice() {
  // revert exactly what the last voice parse applied
  const applied = no && no.voice && no.voice.applied;
  if (!applied) return;
  for (const ai of applied.items) {
    const ex = no.items.find(x => x.item_code === ai.item_code);
    if (ex) ex.qty -= ai.qty;
  }
  no.items = no.items.filter(x => x.qty > 0);
  if (applied.customer_set) {
    no.customer = applied.prev_customer;
    no.customer_name = applied.prev_customer_name;
  }
  if (applied.shipping_set) no.shipping_rule = applied.prev_shipping;
  if (applied.freight_set) {
    const ex = no.charges.find(c => c.account_head === "运费 - LTL");
    if (ex) {
      if (applied.prev_freight == null)
        no.charges = no.charges.filter(c => c.account_head !== "运费 - LTL");
      else ex.tax_amount = applied.prev_freight;
    }
  }
  no.voice.applied = null;
  no.candidates = null;
  renderNewOrder();
  toast("已清除上次填入，重新按住说话即可");
}

function clearCustomer() {
  no.customer = null; no.customer_name = null;
  no.customer_uncertain = false;
  no.customer_suggestions = [];
  no.candidates = null;
  document.getElementById("cust-picked").classList.add("hidden");
  document.getElementById("cust-search").classList.remove("hidden");
}

function pickCandidate(i) {
  const c = no.candidates[i];
  no.customer = c.name;
  no.customer_name = c.customer_name;
  no.customer_uncertain = false;
  no.candidates = null;
  renderNewOrder();
  repriceItemsForCustomer(c.name);
  // learn the choice: next time this phrase auto-selects, no picker
  const phrase = no.voice && no.voice.parsed && no.voice.parsed.customer_phrase;
  if (phrase) {
    api("/api/learn", {
      method: "POST",
      body: JSON.stringify({
        parsed: { customer_phrase: phrase },
        final: { customer_name: c.customer_name },
      }),
    }).then(r => {
      if (r.learned && r.learned.length) toast("已记住：" + phrase + " → " + c.customer_name);
    }).catch(() => {});
  }
}

let searchTimers = {};
function bindSearch(inputId, resultsId, urlPrefix, renderHits) {
  const inp = document.getElementById(inputId);
  const box = document.getElementById(resultsId);
  if (!inp || !box) return;
  const search = () => {
    clearTimeout(searchTimers[inputId]);
    const q = inp.value.trim();
    searchTimers[inputId] = setTimeout(async () => {
      try {
        const rows = await api(urlPrefix + encodeURIComponent(q));
        box.innerHTML = renderHits(rows);
        box.querySelectorAll("[data-customer-index]").forEach(hit => {
          hit.onclick = () => {
            const c = rows[Number(hit.dataset.customerIndex)];
            if (c) pickCustomer(c.name, c.customer_name);
          };
        });
        box.querySelectorAll("[data-item-index]").forEach(hit => {
          hit.onclick = () => {
            const it = rows[Number(hit.dataset.itemIndex)];
            if (it) pickItem(it.item_code);
          };
        });
      } catch (e) { box.innerHTML = ""; }
    }, 250);
  };
  inp.oninput = search;
  // Show initial choices on mobile when the user taps the empty field.
  inp.onfocus = () => {
    if (!box.children.length) search();
  };
}

function renderCustomerHits(rows) {
  return rows.map((c, i) => `
    <div class="hit" data-customer-index="${i}">
      ${esc(c.customer_name)}</div>`).join("");
}

function pickCustomer(name, display) {
  no.customer = name; no.customer_name = display;
  no.customer_uncertain = false;
  no.customer_suggestions = [];
  const warning = document.getElementById("customer-warning");
  if (warning) warning.remove();
  document.getElementById("cust-picked").classList.remove("hidden");
  document.getElementById("cust-picked").querySelector(".customer").textContent = display;
  document.getElementById("cust-search").classList.add("hidden");
  repriceItemsForCustomer(name);
}

function pickCustomerSuggestion(i) {
  const customer = (no.customer_suggestions || [])[i];
  if (customer) pickCustomer(customer.name, customer.customer_name);
}

async function repriceItemsForCustomer(customer) {
  if (!no || !customer || !no.items.length) return;
  // A zero-rate duplicate is an intentional promotional/free row. Reprice
  // paid rows only, using the newly selected customer's price list.
  const paidRows = no.items.filter(it => !it.is_free);
  if (!paidRows.length) return;
  const rates = new Map();
  try {
    await Promise.all([...new Set(paidRows.map(it => it.item_code))].map(async code => {
      const info = await api(`/api/item_price?item_code=${encodeURIComponent(code)}`
        + `&customer=${encodeURIComponent(customer)}`);
      rates.set(code, Number(info.rate) || 0);
    }));
    // Ignore stale responses if the user selected another customer meanwhile.
    if (!no || no.customer !== customer) return;
    paidRows.forEach(it => { it.rate = rates.get(it.item_code) || 0; });
    renderNoItems();
    toast("已按新客户价格表更新商品单价");
  } catch (e) {
    toast("客户已更换，但部分商品价格更新失败：" + e.message, 5000);
  }
}

function renderItemHits(rows) {
  return rows.map((it, i) => `
    <div class="hit" data-item-index="${i}">
      <b>${esc(it.item_code)}</b> ${esc(it.item_name)}</div>`).join("");
}

const pendingItems = new Set();

async function pickItem(code) {
  // Pricing and catalogue lookups can be slow. Ignore a repeated mobile tap
  // until the first lookup has produced its row.
  if (pendingItems.has(code)) return;
  const existing = no.items.find(it => it.item_code === code);
  if (existing) {
    // Selecting an item already on the order means adding a separate
    // promotional/free row (for example: buy 12, get 1 free). ERPNext
    // supports duplicate item rows, and keeping it separate lets the user
    // change the free quantity without affecting the paid row.
    no.items.push({ item_code: existing.item_code,
                    item_name: existing.item_name,
                    uom: existing.uom, qty: 1, rate: 0, is_free: true });
    document.getElementById("item-q").value = "";
    document.getElementById("item-results").innerHTML = "";
    renderNoItems();
    toast("已添加赠品行，单价为 ¥0");
    return;
  }
  pendingItems.add(code);
  document.getElementById("item-q").value = "";
  document.getElementById("item-results").innerHTML = "";
  try {
    let info = { rate: 0 };
    try {
      info = await api(`/api/item_price?item_code=${encodeURIComponent(code)}`
        + (no.customer ? `&customer=${encodeURIComponent(no.customer)}` : ""));
    } catch (e) { /* keep rate 0 */ }
    // fetch display name/uom from the catalogue
    const hits = await api(`/api/items?q=${encodeURIComponent(code)}`);
    const it = hits.find(h => h.item_code === code) || { item_code: code, item_name: code };
    no.items.push({ item_code: code, item_name: it.item_name,
                    uom: it.stock_uom, qty: 1, rate: info.rate || 0 });
    renderNoItems();
  } finally {
    pendingItems.delete(code);
  }
}

async function saveNewOrder(submit, printPdf = false) {
  if (!no.customer) { toast("请先选择客户"); return; }
  const items = no.items.filter(it => it.qty > 0);
  if (!items.length) { toast("请添加至少一个商品"); return; }
  const btn = document.getElementById(
    printPdf ? "no-print" : (submit ? "no-submit" : "no-save"));
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
        items: items.map(it => ({ item_code: it.item_code, qty: it.qty, rate: it.rate,
                                  is_free: !!it.is_free })),
        submit,
      }),
    });
    toast(submit ? `订单 ${o.name} 已提交`
      : printPdf ? `订单 ${o.name} 已保存为草稿并打开 PDF`
        : `订单 ${o.name} 已保存为草稿`);
    // auto-learn: diff the voice-parsed draft against what was submitted
    if (no.voice) {
      api("/api/learn", {
        method: "POST",
        body: JSON.stringify({
          text: no.voice.text,
          parsed: no.voice.parsed || {},
          final: {
            customer_name: no.customer_name,
            items: items.map(it => ({ item_code: it.item_code,
                                      item_name: it.item_name, qty: it.qty })),
          },
        }),
      }).then(r => {
        if (r.learned && r.learned.length)
          toast("已记住纠正：" + r.learned.join("，"), 4000);
      }).catch(() => {});
    }
    no = null;
    if (printPdf) openPdf("Sales Order", o.name);
    stack = [];
    push(renderOrderDetail, o.name);
  } catch (e) {
    toast("失败：" + e.message, 5000);
    btn.disabled = false;
  }
}

/* ---------------- voice order dictation ---------------- */
// Two input paths: (1) hold-to-record -> audio upload -> server transcribes
// with local faster-whisper and parses with the kimi/codex CLI (needs HTTPS);
// (2) textarea + OS keyboard dictation, works anywhere incl. WeChat.

let mediaRec = null;
let audioChunks = [];

async function startRecording(btn) {
  if (!window.isSecureContext || !navigator.mediaDevices || !window.MediaRecorder) {
    toast("录音需要 HTTPS 打开（luciatrading.duckdns.org），或用下方键盘听写", 4500);
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    mediaRec = new MediaRecorder(stream);
    mediaRec.ondataavailable = e => audioChunks.push(e.data);
    mediaRec.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(audioChunks, { type: mediaRec.mimeType || "audio/mp4" });
      const durationSec = (Date.now() - recStartTs) / 1000;
      uploadAudio(blob, btn, durationSec);
    };
    mediaRec.start();
    recStartTs = Date.now();
    btn.textContent = "🔴 松开结束";
    btn.classList.add("recording");
  } catch (e) {
    toast("无法打开麦克风：" + e.message, 4000);
  }
}

function stopRecording(btn) {
  if (mediaRec && mediaRec.state === "recording") {
    btn.textContent = "识别中…";
    btn.classList.remove("recording");
    mediaRec.stop();
  }
}

async function uploadAudio(blob, btn, durationSec = 5) {
  // whisper scales with audio length (~0.35x + 1.5s overhead); LLM adds ~15s
  startProgress(Math.max(2, 1.5 + durationSec * 0.35));
  startJokes();
  try {
    const fd = new FormData();
    fd.append("audio", blob, "voice.m4a");
    const r = await fetch("api/parse_audio", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    document.getElementById("voice-text").value = d.text || "";
    applyParsedOrder(d, d.text || "语音");
  } catch (e) {
    toast("识别失败：" + e.message, 5000);
  } finally {
    stopProgress();
    stopJokes();
    btn.disabled = false;
    btn.textContent = "🎤 按住说话";
  }
}

function bindRecordButton() {
  const btn = document.getElementById("voice-record");
  if (!btn) return;
  btn.addEventListener("touchstart", e => { e.preventDefault(); startRecording(btn); });
  btn.addEventListener("touchend", e => { e.preventDefault(); stopRecording(btn); });
  btn.addEventListener("mousedown", () => startRecording(btn));
  btn.addEventListener("mouseup", () => stopRecording(btn));
}

/* ---- waiting progress bar (estimate from recording length + LLM time) ---- */

let progTimer = null;
let recStartTs = 0;

function startProgress(estimateSec) {
  const wrap = document.getElementById("prog-wrap");
  const bar = document.getElementById("prog-bar");
  if (!wrap || !bar) return;
  wrap.classList.remove("hidden");
  const t0 = Date.now();
  clearInterval(progTimer);
  progTimer = setInterval(() => {
    const el = (Date.now() - t0) / 1000;
    // fast path: 0..85% over the estimate; then creep toward 97% (LLM window)
    const pct = el <= estimateSec
      ? (el / estimateSec) * 85
      : Math.min(97, 85 + ((el - estimateSec) / 15) * 12);
    bar.style.width = pct + "%";
  }, 100);
}

function stopProgress() {
  clearInterval(progTimer);
  progTimer = null;
  const wrap = document.getElementById("prog-wrap");
  const bar = document.getElementById("prog-bar");
  if (!wrap || !bar) return;
  bar.style.width = "100%";
  setTimeout(() => { wrap.classList.add("hidden"); bar.style.width = "0"; }, 400);
}

/* ---- waiting-room jokes (slow LLM path only) ---- */

const JOKES = [
  "Lucia 是莎莎的谐音——这家公司从注册那天起就在撒狗粮。",
  "本系统最终解释权归莎莎所有。",
  "给莎莎做的系统，慢一秒钟都算重大事故。",
  "莎莎不傻，傻傻的是我：写个 App 哄她开心。",
  "傻傻分不清楚：莎莎管公司，AI 管找 jiji，我管鼓掌。",
  "壹杯葡萄酒商店和万杯贸易之间，隔着九千九百九十九杯缘分。",
  "万杯：人生得意须尽欢，一次下单一万杯。",
  "爱沐莎：酒还没醒，先爱上这个名字。",
  "雷司酒业：我们不生产雷司令，我们只是雷司令的搬运工。",
  "酒窝家的酒，喝完笑起来更明显。",
  "泰晤士的酒窝：伦敦都没你会装（6 glasses)。",
  "葡道：条条大路通罗马，瓶瓶好酒通胃口。",
  "左泉：左边的泉酿的酒，流到右边的杯里。",
  "AI 正在加班找 jiji，莎莎老板请稍后。",
  "它思考的样子，像极了周一早上没喝咖啡的我。",
  "早C晚A：早上 Coffee，晚上 Alcohol，中间等接口。",
  "说好的 AI 替代人工，结果它先学会了摸鱼。",
  "世界上最远的距离：我在等加载，加载在等我。",
  "我这不是懒，我这是低功耗模式。",
  "雷司令宣言：我不是针对谁，在座的干白都很一般。",
  "GG 雷司令：Grosses Gewächs，翻译过来就是「大大的好喝」。",
  "别人存酒，我存表情包，都是长线投资。",
  "年轻时我以为钱很重要，现在发现，确实如此—— especially 卖酒的钱。",
  "人生就像提现，永远只差一点点。",
  "客服：您的问题我们非常重视，正在为您转接…下一首音乐。",
  "应酬的最高境界：酒杯一碰，订单到手；装的是接单，做的是定制。",
  "甲方爸爸看了这个 App 两眼放光：给我们也整一个！——得，又多一单。",
  "酒桌上没有闲聊，每一句「你们这系统不错」都是商机。",
  "别人应酬伤肝，我应酬顺便做需求调研。",
  "乙方守则：客户举杯我举杯，客户提需求我装醉——醒来还是接了。",
];

let jokeTimer = null;

function startJokes() {
  const box = document.getElementById("joke-box");
  if (!box) return;
  let i = Math.floor(Math.random() * JOKES.length);
  const show = () => { box.textContent = "😄 " + JOKES[i++ % JOKES.length]; };
  box.classList.remove("hidden");
  show();
  jokeTimer = setInterval(show, 6000);
}

function stopJokes() {
  clearInterval(jokeTimer);
  jokeTimer = null;
  const box = document.getElementById("joke-box");
  if (box) box.classList.add("hidden");
}

async function parseVoice(btn) {
  const text = document.getElementById("voice-text").value.trim();
  if (!text) { toast("先听写或输入订单内容"); return; }
  if (no) no.voice = { text, parsed: null };  // log attempt even if parse fails
  btn.disabled = true;
  btn.textContent = "解析中…";
  startProgress(2);  // text path: fast parse ~1s, LLM fallback ~15s
  startJokes();
  try {
    const d = await api("/api/parse_order", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    applyParsedOrder(d, text);
  } catch (e) {
    toast("解析失败：" + e.message, 5000);
  } finally {
    stopProgress();
    stopJokes();
    btn.disabled = false;
    btn.textContent = "解析并填入表单";
  }
}

function applyParsedOrder(d, text) {
  if (!no) return;
  no.voice = { text, parsed: d };  // kept for auto-learning on submit
  no.candidates = null;
  no.customer_uncertain = Boolean(d.customer_uncertain);
  no.customer_suggestions = d.customer_suggestions || [];
  // track exactly what this parse applied, so 🔁重讲 can undo it cleanly
  const applied = { items: [], customer_set: false, prev_customer: no.customer,
                    prev_customer_name: no.customer_name,
                    shipping_set: false, prev_shipping: no.shipping_rule,
                    freight_set: false, prev_freight:
                      (no.charges.find(c => c.account_head === "运费 - LTL") || {}).tax_amount };
  no.voice.applied = applied;
  if (d.customer) {
    applied.customer_set = true;
    no.customer = d.customer.name;
    no.customer_name = d.customer.customer_name;
  } else if (d.customer_candidates && d.customer_candidates.length) {
    // ambiguous customer (e.g. 漾叶) — let the user pick
    no.customer = null;
    no.customer_name = null;
    no.candidates = d.customer_candidates;
  }
  for (const it of d.items || []) {
    const ex = no.items.find(x => x.item_code === it.item_code);
    if (ex) ex.qty += it.qty;
    else no.items.push({ item_code: it.item_code, item_name: it.item_name,
                         uom: it.uom, qty: it.qty, rate: it.rate });
    applied.items.push({ item_code: it.item_code, qty: it.qty });
  }
  if (d.shipping_rule) {
    applied.shipping_set = true;
    no.shipping_rule = d.shipping_rule;
  }
  if (d.freight) {
    applied.freight_set = true;
    const ex = no.charges.find(c => c.account_head === "运费 - LTL");
    if (ex) ex.tax_amount = d.freight;
    else no.charges.push({ account_head: "运费 - LTL", description: "运费",
                           tax_amount: d.freight });
  }
  renderNewOrder();
  const parts = [];
  if (d.customer) parts.push("客户 " + d.customer.customer_name);
  parts.push((d.items || []).length + " 个商品");
  if (d.shipping_rule) parts.push("物流 " + d.shipping_rule);
  if (d.freight) parts.push("运费 ¥" + d.freight);
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
