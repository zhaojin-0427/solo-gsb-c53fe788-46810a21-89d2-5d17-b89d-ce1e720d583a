/* 事件时序矛盾矛盾排查台 —— 原生 JavaScript 前端 */
"use strict";

const KIND_LABELS = {
  earlier: "A 早于 B",
  later: "A 晚于 B",
  after_at_least: "A 晚于 B ≥ n 分钟",
  after_at_most: "A 晚于 B ≤ n 分钟",
};
const KIND_NEEDS_N = new Set(["after_at_least", "after_at_most"]);
const STORE_KEY = "tconsole.branch";

let branches = [];
let currentBranchId = 1;
let state = null;           // {branch, events, relations, solve}
let mergeData = null;       // {preview, sourceId}
let resolutions = { entities: {}, fields: {} };

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    const msg = (data && data.detail) ? data.detail : `请求失败 ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

// ------------------------------------------------------------- 分支

async function loadBranches(selectId) {
  branches = await api("GET", "/api/branches");
  const sel = $("#branchSelect");
  sel.innerHTML = "";
  for (const b of branches) {
    const o = document.createElement("option");
    o.value = b.id;
    o.textContent = `#${b.id} ${b.name}`;
    sel.appendChild(o);
  }
  const saved = Number(localStorage.getItem(STORE_KEY));
  if (branches.some((b) => b.id === saved)) currentBranchId = saved;
  if (!branches.some((b) => b.id === currentBranchId)) currentBranchId = branches[0].id;
  sel.value = currentBranchId;
}

async function switchBranch(id) {
  currentBranchId = Number(id);
  localStorage.setItem(STORE_KEY, currentBranchId);
  await loadState();
}

async function loadState() {
  state = await api("GET", `/api/branches/${currentBranchId}/state`);
  render();
}

// ------------------------------------------------------------- 渲染入口

const coreSet = () => new Set(state.solve.feasible ? [] : state.solve.core);

function render() {
  renderBranchSelect();
  renderBanner();
  renderTimeline();
  renderEventTable();
  renderRelationTable();
  renderEventSelects();
}

function renderBranchSelect() {
  $("#branchSelect").value = currentBranchId;
}

function eventByRef(ref) {
  return state.events.find((e) => e.ref === ref);
}
function nameOf(ref) {
  const e = eventByRef(ref);
  return e ? e.name : `事件#${ref}(已删除)`;
}

// ------------------------------------------------------------- 矛盾横幅

function renderBanner() {
  const banner = $("#banner");
  banner.className = "banner";
  if (state.solve.feasible) {
    banner.classList.add("ok");
    banner.innerHTML = "✓ 当前所有约束可满足，时间轴上为全体可行解的最紧上下界。";
    return;
  }
  banner.classList.add("unsat");
  const core = state.solve.core;
  const chips = core.map((seq) => {
    const isRange = state.events.some((e) => e.ref === seq);
    return `<span class="core-link ${isRange ? "range" : ""}" data-seq="${seq}"
              title="点击定位到该输入项">#${seq} ${isRange ? "时间范围" : "关系"}</span>`;
  }).join("");
  banner.innerHTML =
    `✗ 约束不可满足。项数最少的矛盾集（共 ${core.length} 项，序号字典序最小）：${chips}`;
  banner.querySelectorAll(".core-link").forEach((el) =>
    el.addEventListener("click", () => locateItem(Number(el.dataset.seq))));
}

function locateItem(seq) {
  const isRange = state.events.some((e) => e.ref === seq);
  const selector = isRange
    ? `tr[data-event-ref="${seq}"]`
    : `tr[data-relation-ref="${seq}"]`;
  const row = document.querySelector(selector);
  if (row) {
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.add("selected");
    setTimeout(() => row.classList.remove("selected"), 2400);
  }
  highlightTimeline([seq].concat(
    isRange ? [] : (() => {
      const rel = state.relations.find((r) => r.ref === seq);
      return rel ? [rel.a_ref, rel.b_ref] : [];
    })()
  ));
}

// ------------------------------------------------------------- 时间轴

function collectFinite() {
  const vals = [];
  for (const e of state.events) {
    if (e.lower_bound !== null) vals.push(e.lower_bound);
    if (e.upper_bound !== null) vals.push(e.upper_bound);
    const tb = state.solve.bounds?.[e.ref];
    if (tb) {
      if (tb[0] !== null && tb[0] !== undefined) vals.push(tb[0]);
      if (tb[1] !== null && tb[1] !== undefined) vals.push(tb[1]);
    }
  }
  return vals;
}

function niceStep(ppm) {
  const targetPx = 80;
  const raw = targetPx / ppm;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10, 20, 50]) {
    if (m * pow >= raw) return m * pow;
  }
  return raw;
}

let tlHighlight = new Set();
function highlightTimeline(refs) {
  tlHighlight = new Set(refs);
  renderTimeline();
}

function renderTimeline() {
  const box = $("#timeline");
  box.innerHTML = "";
  const ppm = Number($("#zoomRange").value);
  const core = coreSet();

  const finite = collectFinite();
  if (finite.length === 0) {
    box.innerHTML = `<div class="tl-none">所有事件的时间均无界；添加带界范围或先后约束后可查看时间轴。</div>`;
    return;
  }
  let lo0 = Math.min(...finite) - 5;
  let hi0 = Math.max(...finite) + 5;
  if (lo0 === hi0) hi0 = lo0 + 10;
  const span = hi0 - lo0;
  const widthPx = Math.max(span * ppm, 300);

  // 标尺
  const ruler = document.createElement("div");
  ruler.className = "tl-row";
  ruler.innerHTML = `<div></div>`;
  const track0 = document.createElement("div");
  track0.className = "tl-track";
  track0.style.width = `${widthPx}px`;
  track0.style.height = "8px";
  const step = niceStep(ppm);
  for (let t = Math.ceil(lo0 / step) * step; t <= hi0; t += step) {
    const tick = document.createElement("div");
    tick.className = "tl-tick";
    tick.style.left = `${(t - lo0) * ppm}px`;
    tick.innerHTML = `<span>${t}</span>`;
    track0.appendChild(tick);
  }
  ruler.appendChild(track0);
  box.appendChild(ruler);

  for (const e of state.events) {
    const row = document.createElement("div");
    row.className = "tl-row";
    if (tlHighlight.has(e.ref)) row.classList.add("selected");
    if (core.has(e.ref)) row.classList.add("core-row");

    const label = document.createElement("div");
    label.className = "tl-label";
    label.innerHTML = `<span class="ref">#${e.ref}</span>${esc(e.name)}`;
    label.title = `#${e.ref} ${e.name}：点击定位相关约束`;
    label.addEventListener("click", () => focusEvent(e.ref));

    const track = document.createElement("div");
    track.className = "tl-track";
    track.style.width = `${widthPx}px`;

    const isEmptyRange = e.lower_bound !== null && e.upper_bound !== null &&
      e.lower_bound > e.upper_bound;

    const pos = (t) => (t - lo0) * ppm;

    // 原始输入范围（虚线浅蓝）
    if (!isEmptyRange && (e.lower_bound !== null || e.upper_bound !== null)) {
      const a = e.lower_bound !== null ? pos(e.lower_bound) : 0;
      const b = e.upper_bound !== null ? pos(e.upper_bound) : widthPx;
      const rIn = document.createElement("div");
      rIn.className = "tl-range input-range";
      rIn.style.left = `${a}px`;
      rIn.style.width = `${Math.max(b - a, 8)}px`;
      rIn.title = `输入范围：${fmtBound(e.lower_bound, "无下界")} ~ ${fmtBound(e.upper_bound, "无上界")}`;
      track.appendChild(rIn);
    }

    if (isEmptyRange) {
      const dot = document.createElement("div");
      dot.className = "tl-empty-range";
      dot.style.left = `${pos(e.lower_bound)}px`;
      dot.title = "空区间（下界 > 上界）";
      track.appendChild(dot);
      addRangeText(track, pos(e.lower_bound) + 14, "空区间", true);
    } else {
      const tb = state.solve.bounds?.[e.ref];
      if (tb) {
        const [lo, hi] = tb;
        if (lo === null && hi === null) {
          addRangeText(track, 8, "无下界 ~ 无上界", false);
        } else {
          const a = lo !== null ? pos(lo) : 0;
          const b = hi !== null ? pos(hi) : widthPx;
          const rt = document.createElement("div");
          rt.className = "tl-range tight" + (core.has(e.ref) ? " core" : "");
          rt.style.left = `${a}px`;
          rt.style.width = `${Math.max(b - a, 8)}px`;
          rt.title = `最紧界：${fmtBound(lo, "无下界")} ~ ${fmtBound(hi, "无上界")}`;
          track.appendChild(rt);
          if (lo === null) addRangeText(track, 6, "无下界", false, true);
          if (hi === null) addRangeText(track, widthPx - 58, "无上界", false, true);
        }
      }
    }

    row.appendChild(label);
    row.appendChild(track);
    box.appendChild(row);
  }
}

function addRangeText(track, x, text, danger, inside) {
  const span = document.createElement("span");
  span.className = "tl-none";
  span.textContent = text;
  span.style.position = "absolute";
  span.style.left = `${x}px`;
  span.style.top = "3px";
  if (danger) span.style.color = "var(--danger)";
  if (inside) span.style.color = "#fff";
  track.appendChild(span);
}

function fmtBound(v, unbounded) {
  return v === null || v === undefined ? unbounded : String(v);
}

function focusEvent(ref) {
  document.querySelectorAll("#relationTable tbody tr").forEach((tr) => {
    const rel = state.relations.find((r) => r.ref === Number(tr.dataset.relationRef));
    if (rel && (rel.a_ref === ref || rel.b_ref === ref)) {
      tr.classList.add("selected");
      tr.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => tr.classList.remove("selected"), 2400);
    }
  });
  tlHighlight = new Set([ref]);
  renderTimeline();
}

// ------------------------------------------------------------- 事件表

function fmtTight(ref) {
  const tb = state.solve.bounds?.[ref];
  if (!tb) return "—";
  const [lo, hi] = tb;
  const loHtml = lo === null
    ? `<span class="unbounded">无下界</span>` : esc(lo);
  const hiHtml = hi === null
    ? `<span class="unbounded">无上界</span>` : esc(hi);
  return `${loHtml} ~ ${hiHtml}`;
}

function renderEventTable() {
  const tbody = $("#eventTable tbody");
  tbody.innerHTML = "";
  const core = coreSet();
  for (const e of state.events) {
    const tr = document.createElement("tr");
    tr.dataset.eventRef = e.ref;
    if (core.has(e.ref)) tr.classList.add("core-row");
    tr.innerHTML = `
      <td><span class="ref-badge">#${e.ref}</span></td>
      <td><input class="f-name" value="${esc(e.name)}" maxlength="100"></td>
      <td><input class="f-lb" type="number" value="${e.lower_bound ?? ""}" placeholder="空=无下界"></td>
      <td><input class="f-ub" type="number" value="${e.upper_bound ?? ""}" placeholder="空=无上界"></td>
      <td class="tight-cell">${fmtTight(e.ref)}</td>
      <td>
        <button class="save-btn" title="保存修改">保存</button>
        <button class="del-btn" title="删除事件及其约束">删除</button>
      </td>`;
    tr.querySelector(".save-btn").addEventListener("click", () => saveEvent(e.ref, tr));
    tr.querySelector(".del-btn").addEventListener("click", () => removeEvent(e.ref));
    tbody.appendChild(tr);
  }
}

function readOptInt(value) {
  return value.trim() === "" ? null : Number(value);
}

async function saveEvent(ref, tr) {
  const patch = {
    name: tr.querySelector(".f-name").value.trim(),
    lower_bound: readOptInt(tr.querySelector(".f-lb").value),
    upper_bound: readOptInt(tr.querySelector(".f-ub").value),
  };
  if (!patch.name) return alert("事件名称不能为空");
  for (const [k, v] of Object.entries(patch)) {
    if (v !== null && (!Number.isInteger(v))) {
      return alert("时间界必须是整数（或留空）");
    }
  }
  try {
    state = await api("PATCH", `/api/branches/${currentBranchId}/events/${ref}`, patch);
    render();
  } catch (e) { alert(e.message); }
}

async function removeEvent(ref) {
  if (!confirm(`删除事件 #${ref} 将同时删除引用它的所有约束，确认？`)) return;
  try {
    state = await api("DELETE", `/api/branches/${currentBranchId}/events/${ref}`);
    render();
  } catch (e) { alert(e.message); }
}

// ------------------------------------------------------------- 关系表

function renderRelationTable() {
  const tbody = $("#relationTable tbody");
  tbody.innerHTML = "";
  const core = coreSet();
  for (const r of state.relations) {
    const tr = document.createElement("tr");
    tr.dataset.relationRef = r.ref;
    if (core.has(r.ref)) tr.classList.add("core-row");
    tr.innerHTML = `
      <td><span class="ref-badge">#${r.ref}</span></td>
      <td>${eventSelect("f-a", r.a_ref)}</td>
      <td>
        <select class="f-kind">
          ${Object.entries(KIND_LABELS).map(([k, v]) =>
            `<option value="${k}" ${k === r.kind ? "selected" : ""}>${v}</option>`).join("")}
        </select>
      </td>
      <td>${eventSelect("f-b", r.b_ref)}</td>
      <td><input class="f-n" type="number" min="0" step="1" style="width:70px"
            value="${r.n ?? ""}" ${KIND_NEEDS_N.has(r.kind) ? "" : "disabled"}></td>
      <td>
        <button class="save-btn">保存</button>
        <button class="del-btn">删除</button>
      </td>`;
    tr.querySelector(".f-kind").addEventListener("change", (ev) => {
      tr.querySelector(".f-n").disabled = !KIND_NEEDS_N.has(ev.target.value);
    });
    tr.querySelector(".save-btn").addEventListener("click", () => saveRelation(r.ref, tr));
    tr.querySelector(".del-btn").addEventListener("click", () => removeRelation(r.ref));
    tbody.appendChild(tr);
  }
}

function eventSelect(cls, selected) {
  return `<select class="${cls}">
    ${state.events.map((e) =>
      `<option value="${e.ref}" ${e.ref === selected ? "selected" : ""}>#${e.ref} ${esc(e.name)}</option>`
    ).join("")}
  </select>`;
}

async function saveRelation(ref, tr) {
  const a_ref = Number(tr.querySelector(".f-a").value);
  const b_ref = Number(tr.querySelector(".f-b").value);
  const kind = tr.querySelector(".f-kind").value;
  const nRaw = tr.querySelector(".f-n").value.trim();
  const patch = { a_ref, b_ref, kind, n: null };
  if (KIND_NEEDS_N.has(kind)) {
    const n = Number(nRaw);
    if (!Number.isInteger(n) || n < 0) return alert("n 必须是非负整数");
    patch.n = n;
  }
  try {
    state = await api("PATCH",
      `/api/branches/${currentBranchId}/relations/${ref}`, patch);
    render();
  } catch (e) { alert(e.message); }
}

async function removeRelation(ref) {
  try {
    state = await api("DELETE",
      `/api/branches/${currentBranchId}/relations/${ref}`);
    render();
  } catch (e) { alert(e.message); }
}

// ------------------------------------------------------------- 新增表单

function renderEventSelects() {
  const formA = $("#relationForm [name=a_ref]");
  const formB = $("#relationForm [name=b_ref]");
  const curA = Number(formA.value) || (state.events[0]?.ref);
  const curB = Number(formB.value) || (state.events[1]?.ref ?? state.events[0]?.ref);
  const opts = state.events.map((e) =>
    `<option value="${e.ref}">#${e.ref} ${esc(e.name)}</option>`).join("");
  formA.innerHTML = opts;
  formB.innerHTML = opts;
  if (curA) formA.value = curA;
  if (curB) formB.value = curB;
}

$("#eventForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const body = {
    name: f.name.value.trim(),
    lower_bound: readOptInt(f.lower_bound.value),
    upper_bound: readOptInt(f.upper_bound.value),
  };
  if (!body.name) return;
  if (body.lower_bound !== null && !Number.isInteger(body.lower_bound)) return alert("下界必须是整数");
  if (body.upper_bound !== null && !Number.isInteger(body.upper_bound)) return alert("上界必须是整数");
  try {
    state = await api("POST", `/api/branches/${currentBranchId}/events`, body);
    f.reset();
    render();
  } catch (e) { alert(e.message); }
});

$("#relationForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const kind = f.kind.value;
  const body = {
    a_ref: Number(f.a_ref.value),
    b_ref: Number(f.b_ref.value),
    kind,
    n: null,
  };
  if (KIND_NEEDS_N.has(kind)) {
    const n = Number(f.n.value);
    if (!Number.isInteger(n) || n < 0) return alert("n 必须是非负整数");
    body.n = n;
  }
  try {
    state = await api("POST", `/api/branches/${currentBranchId}/relations`, body);
    render();
  } catch (e) { alert(e.message); }
});

$("#relationForm [name=kind]").addEventListener("change", (ev) => {
  $("#relationForm [name=n]").disabled = !KIND_NEEDS_N.has(ev.target.value);
});

$("#zoomRange").addEventListener("input", renderTimeline);

// ------------------------------------------------------------- 提交 / 分叉 / 历史

$("#branchSelect").addEventListener("change", (ev) => switchBranch(ev.target.value));

$("#commitBtn").addEventListener("click", async () => {
  const message = prompt("提交说明（可留空）：", "") ?? "";
  try {
    await api("POST", `/api/branches/${currentBranchId}/commit`, { message });
    await loadState();
    alert("版本已提交");
  } catch (e) { alert(e.message); }
});

$("#forkBtn").addEventListener("click", async () => {
  const name = prompt("新分叉名称：", `fork-of-main-${Date.now() % 100000}`);
  if (!name) return;
  try {
    const data = await api("POST", "/api/branches", { name: name.trim() });
    await loadBranches();
    await switchBranch(data.branch.id);
    alert(`已创建分叉 #${data.branch.id}`);
  } catch (e) { alert(e.message); }
});

$("#historyBtn").addEventListener("click", async () => {
  try {
    const versions = await api("GET",
      `/api/branches/${currentBranchId}/versions`);
    const tag = { initial: "初始", commit: "提交", fork: "分叉", merge: "合并" };
    $("#historyList").innerHTML = versions.reverse().map((v) => `
      <li>#${v.id}
        <span class="tag ${v.kind}">${tag[v.kind] || v.kind}</span>
        ${esc(v.message)}
        ${v.source_branch_id ? `← 来源分支 #${v.source_branch_id}` : ""}
        <time>${esc(v.created_at)}</time>
      </li>`).join("");
    $("#historyModal").classList.remove("hidden");
  } catch (e) { alert(e.message); }
});
$("#historyCloseBtn").addEventListener("click",
  () => $("#historyModal").classList.add("hidden"));

// ------------------------------------------------------------- 合并

$("#mergeBtn").addEventListener("click", async () => {
  const sel = $("#mergeSource");
  sel.innerHTML = branches
    .filter((b) => b.id !== currentBranchId)
    .map((b) => `<option value="${b.id}">#${b.id} ${esc(b.name)}</option>`)
    .join("");
  $("#mergeStatus").textContent = "选择来源分支后点击「分析合并」。";
  $("#mergeConflicts").innerHTML = "";
  mergeData = null;
  resolutions = { entities: {}, fields: {} };
  $("#mergeModal").classList.remove("hidden");
});
$("#mergeCancelBtn").addEventListener("click",
  () => $("#mergeModal").classList.add("hidden"));

$("#mergeAnalyzeBtn").addEventListener("click", async () => {
  const sourceId = Number($("#mergeSource").value);
  try {
    const preview = await api("POST",
      `/api/branches/${currentBranchId}/merges/preview`,
      { source_id: sourceId, expected_head: state.branch.head_id });
    mergeData = { preview, sourceId };
    resolutions = { entities: {}, fields: {} };
    renderMerge();
  } catch (e) {
    $("#mergeStatus").textContent = "分析失败：" + e.message;
  }
});

function fieldName(f) {
  return ({
    name: "名称", lower_bound: "下界", upper_bound: "上界",
    a_ref: "事件 A", b_ref: "事件 B", kind: "关系类型", n: "n",
  })[f] || f;
}
function fmtVal(c, v) {
  if (v === null || v === undefined) return "空";
  if (c.kind === "relation" && (c.field === "a_ref" || c.field === "b_ref")) {
    return `#${v} ${esc(nameOf(v))}`;
  }
  if (c.field === "kind") return KIND_LABELS[v] || v;
  return esc(v);
}

function renderMerge() {
  const box = $("#mergeConflicts");
  const { preview } = mergeData;
  box.innerHTML = "";
  const cs = preview.conflicts;
  if (cs.length === 0) {
    $("#mergeStatus").innerHTML =
      "✓ 无交叠改动，可直接提交自动合并。" +
      (preview.dropped.length
        ? `<div class="dropped-note">另有 ${preview.dropped.length} 条关系因事件被删除而丢弃。</div>`
        : "");
    return;
  }
  $("#mergeStatus").innerHTML =
    `检测到 ${cs.length} 处冲突，请逐项选择：` +
    (preview.dropped.length
      ? `<div class="dropped-note">另有 ${preview.dropped.length} 条关系因事件被删除而丢弃。</div>`
      : "");

  for (const c of cs) {
    const item = document.createElement("div");
    if (c.type === "field") {
      item.className = "conflict-item field-conflict";
      const who = c.kind === "event" ? `事件 #${c.ref}` : `关系 #${c.ref}`;
      item.innerHTML = `
        <div class="cf-title">同字段冲突 · ${who} · ${fieldName(c.field)}</div>
        <div class="cf-row">基线值：<span class="cf-val">${fmtVal(c, c.base)}</span></div>
        <div class="cf-row">
          <button data-side="mine">采用当前分支：<span class="cf-val">${fmtVal(c, c.mine)}</span></button>
          <button data-side="theirs">采用来源分支：<span class="cf-val">${fmtVal(c, c.theirs)}</span></button>
        </div>`;
      const fkey = `${c.key}.${c.field}`;
      item.querySelectorAll("button").forEach((btn) =>
        btn.addEventListener("click", () => {
          resolutions.fields[fkey] = btn.dataset.side;
          item.querySelectorAll("button").forEach((b) =>
            b.classList.toggle("chosen", b === btn));
        }));
      const cur = resolutions.fields[fkey];
      if (cur) item.querySelector(`[data-side="${cur}"]`).classList.add("chosen");
    } else {
      item.className = "conflict-item entity-conflict";
      const who = c.kind === "event" ? `事件 #${c.ref} ${esc(c.current.name)}`
                                     : `关系 #${c.ref}`;
      item.innerHTML = `
        <div class="cf-title">删除 / 修改冲突 · ${who}</div>
        <div class="cf-row">
          ${c.deleted_by === "mine" ? "当前分支删除、来源分支修改" : "来源分支删除、当前分支修改"}
        </div>
        <div class="cf-row">
          <button data-side="keep">保留修改</button>
          <button data-side="delete">确认删除</button>
        </div>`;
      item.querySelectorAll("button").forEach((btn) =>
        btn.addEventListener("click", () => {
          resolutions.entities[c.key] = btn.dataset.side;
          item.querySelectorAll("button").forEach((b) =>
            b.classList.toggle("chosen", b === btn));
        }));
      const cur = resolutions.entities[c.key];
      if (cur) item.querySelector(`[data-side="${cur}"]`).classList.add("chosen");
    }
    box.appendChild(item);
  }
}

$("#mergeConfirmBtn").addEventListener("click", async () => {
  if (!mergeData) return alert("请先分析合并");
  const cs = mergeData.preview.conflicts;
  const missing = [];
  for (const c of cs) {
    if (c.type === "field") {
      if (!resolutions.fields[`${c.key}.${c.field}`]) missing.push(`字段冲突 ${c.key}.${c.field}`);
    } else if (!resolutions.entities[c.key]) {
      missing.push(`删除冲突 ${c.key}`);
    }
  }
  if (missing.length) return alert("仍有冲突未选择：\n" + missing.join("\n"));
  try {
    state = await api("POST",
      `/api/branches/${currentBranchId}/merges/commit`,
      {
        source_id: mergeData.sourceId,
        expected_head: state.branch.head_id,
        message: `合并分支 #${mergeData.sourceId}`,
        resolutions,
      });
    $("#mergeModal").classList.add("hidden");
    render();
    alert("合并已提交");
  } catch (e) {
    $("#mergeStatus").textContent = "合并提交失败（未写入任何数据）：" + e.message;
  }
});

// ------------------------------------------------------------- 启动

(async function init() {
  try {
    await loadBranches();
    await loadState();
  } catch (e) {
    document.body.insertAdjacentHTML("afterbegin",
      `<div class="banner unsat">启动失败：${esc(e.message)}</div>`);
  }
})();
