/* 事件时序矛盾排查台 — 原生 JS 前端 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  branch: "main",
  headId: null,
  seqCounter: 0,
  events: [],
  relations: [],
  analysis: { satisfiable: true, contradiction: [], bounds: {} },
};
let selectedId = null;        // 当前联动选中（event:<id> 或 relation:<id>）
let zoom = { min: -10, max: 30, manual: false };
let saveTimer = null;
let mergeData = null;

const LS_KEY = "temporal-console:current-branch";

const api = {
  async req(method, url, body) {
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const d = data && typeof data === "object" ? data : {};
      const detail = d.detail && typeof d.detail === "object" ? d.detail
        : (typeof d.detail === "string" ? { message: d.detail } : {});
      throw Object.assign(new Error(detail.message || resp.statusText),
        { status: resp.status, detail });
    }
    return data;
  },
  getState: (branch) => api.req("GET", `/api/state?branch=${encodeURIComponent(branch)}`),
  branches: () => api.req("GET", "/api/branches"),
  newBranch: (name, from) => api.req("POST", "/api/branches", { name, from_branch: from }),
  commit: (branch, base_id, ops, message) =>
    api.req("POST", `/api/branches/${encodeURIComponent(branch)}/commit`, { base_id, ops, message }),
  history: (branch) => api.req("GET", `/api/branches/${encodeURIComponent(branch)}/history`),
  mergePreview: (branch, source, resolutions) =>
    api.req("POST", `/api/branches/${encodeURIComponent(branch)}/merge-preview`,
      { source, resolutions }),
  mergeCommit: (branch, source, resolutions, message) =>
    api.req("POST", `/api/branches/${encodeURIComponent(branch)}/merge`,
      { source, resolutions, message }),
};

// ---------------------------------------------------------------------------
// 文本工具
// ---------------------------------------------------------------------------

function fmtBound(b, side) {
  if (b === null || b === undefined) return side === "lower" ? "无下界" : "无上界";
  return `t=${b}`;
}

const REL_TEXT = {
  earlier: "早于",
  later: "晚于",
  same: "同一时刻于",
  at_least: "晚于…至少",
  at_most: "晚于…至多",
};

function relText(r) {
  const a = evLabel(r.a), b = evLabel(r.b);
  if (r.type === "earlier") return `${a} 早于 ${b}（t${sub(a)}+1 ≤ t${sub(b)}）`;
  if (r.type === "later") return `${a} 晚于 ${b}（t${sub(b)}+1 ≤ t${sub(a)}）`;
  if (r.type === "same") return `${a} 与 ${b} 同一时刻（t${sub(a)} = t${sub(b)}）`;
  if (r.type === "at_least")
    return `${a} 晚于 ${b} 至少 ${r.n} 分钟（t${sub(a)} − t${sub(b)} ≥ max(1,${r.n})）`;
  return `${a} 晚于 ${b} 至多 ${r.n} 分钟（1 ≤ t${sub(a)} − t${sub(b)} ≤ ${r.n}）`;
}

const evMap = () => Object.fromEntries(state.events.map((e) => [e.id, e]));
function evLabel(id) { const e = evMap()[id]; return e ? e.label : "?已删事件?"; }
const sub = (id) => {
  const e = evMap()[id];
  return e ? e.label : "?";
};

function conflictSeqs() {
  return new Set(state.analysis.contradiction.map((c) => {
    const item = c.kind === "event"
      ? state.events.find((e) => e.id === c.id)
      : state.relations.find((r) => r.id === c.id);
    return item ? item.seq : -1;
  }));
}
function isConflict(kind, id) {
  return state.analysis.contradiction.some((c) => c.kind === kind && c.id === id);
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

function render() {
  renderStatus();
  renderEvents();
  renderRelations();
  renderTimeline();
}

function renderStatus() {
  const bar = $("#statusBar");
  bar.classList.remove("ok", "bad");
  const base = `<span>分支 <b>${escapeHtml(state.branch)}</b> · 版本 ${state.headId.slice(0, 8)}</span>`;
  if (state.analysis.satisfiable) {
    bar.className = "statusbar ok";
    bar.innerHTML = base + "<span>✓ 当前约束可满足，已显示全体可行解的最紧上下界</span>";
  } else {
    bar.className = "statusbar bad";
    const names = state.analysis.contradiction.map((c) => {
      const it = c.kind === "event"
        ? state.events.find((e) => e.id === c.id)
        : state.relations.find((r) => r.id === c.id);
      const seq = it ? it.seq : "?";
      const label = c.kind === "event"
        ? `事件「${evLabel(c.id)}」时间范围`
        : relText(it);
      return `<span class="chip" data-kind="${c.kind}" data-id="${c.id}">#${seq} ${escapeHtml(label)}</span>`;
    }).join(" ");
    bar.innerHTML = `${base}<span>✗ 不可满足，最小矛盾集（${state.analysis.contradiction.length} 项）：</span>${names}
      <span class="hint">点击徽章可定位</span>`;
    $$(".chip", bar).forEach((chip) =>
      chip.addEventListener("click", () =>
        locateItem(chip.dataset.kind, chip.dataset.id)));
  }
}

function renderEvents() {
  const ul = $("#eventList");
  ul.innerHTML = "";
  state.events.forEach((ev) => {
    const b = state.analysis.bounds[ev.id];
    const inConflict = isConflict("event", ev.id);
    const li = document.createElement("li");
    li.className = "item" + (inConflict ? " in-conflict" : "")
      + (selectedId === "event:" + ev.id ? " selected" : "");
    li.dataset.id = ev.id;

    const rangeInput = ev.lower === null && ev.upper === null
      ? "时间点未限定"
      : `${ev.lower === null ? "无下界" : ev.lower} ≤ t ≤ ${ev.upper === null ? "无上界" : ev.upper}`;
    const solved = b
      ? `可行域：${fmtBound(b.lower, "lower")} ~ ${fmtBound(b.upper, "upper")}`
      : "可行域：—";

    li.innerHTML = `
      <div class="item-row">
        <span class="item-title">${escapeHtml(ev.label)}</span>
        <span class="seq-badge ${inConflict ? "conflict" : ""}">#${ev.seq}</span>
      </div>
      <div class="item-sub">输入：${escapeHtml(rangeInput)}</div>
      <div class="bounds-line">${escapeHtml(solved)}</div>
      <div class="edit-row" hidden>
        <input class="f-label" value="${escapeHtml(ev.label)}" placeholder="名称">
        <input class="f-lower" type="number" step="1" value="${ev.lower ?? ""}" placeholder="下界空">
        <input class="f-upper" type="number" step="1" value="${ev.upper ?? ""}" placeholder="上界空">
        <button class="btn tiny primary act-save">保存</button>
        <button class="btn tiny danger act-del">删除</button>
      </div>`;
    li.addEventListener("click", (e) => {
      if (e.target.closest(".edit-row")) return;
      toggleEdit(li);
      locateItem("event", ev.id);
    });
    li.querySelector(".act-save").addEventListener("click", () => saveEvent(ev.id, li));
    li.querySelector(".act-del").addEventListener("click", () => deleteEvent(ev));
    ul.appendChild(li);
  });
  syncEventSelects();
}

function toggleEdit(li) {
  const row = li.querySelector(".edit-row");
  row.hidden = !row.hidden;
  $$("#eventList .edit-row").forEach((r) => { if (r !== row) r.hidden = true; });
}

function renderRelations() {
  const ul = $("#relationList");
  ul.innerHTML = "";
  state.relations.forEach((r) => {
    const inConflict = isConflict("relation", r.id);
    const li = document.createElement("li");
    li.className = "item" + (inConflict ? " in-conflict" : "")
      + (selectedId === "relation:" + r.id ? " selected" : "");
    li.dataset.id = r.id;
    li.innerHTML = `
      <div class="item-row">
        <span class="item-title">${escapeHtml(relText(r))}</span>
        <span class="seq-badge ${inConflict ? "conflict" : ""}">#${r.seq}</span>
      </div>
      <div class="edit-row" hidden>
        <select class="f-a">${eventOptions(r.a)}</select>
        <select class="f-type">
          ${["earlier", "later", "same", "at_least", "at_most"]
            .map((t) => `<option value="${t}" ${t === r.type ? "selected" : ""}>${REL_TEXT[t]}</option>`).join("")}
        </select>
        <select class="f-b">${eventOptions(r.b)}</select>
        <input class="f-n" type="number" min="0" step="1" value="${r.n ?? ""}"
               placeholder="n" ${r.type === "at_least" || r.type === "at_most" ? "" : "hidden"}>
        <button class="btn tiny primary act-save">保存</button>
        <button class="btn tiny danger act-del">删除</button>
      </div>`;
    li.addEventListener("click", (e) => {
      if (e.target.closest(".edit-row")) return;
      const row = li.querySelector(".edit-row");
      row.hidden = !row.hidden;
      $$("#relationList .edit-row").forEach((x) => { if (x !== row) x.hidden = true; });
      locateItem("relation", r.id);
    });
    li.querySelector(".f-type").addEventListener("change", (e) => {
      const needN = e.target.value === "at_least" || e.target.value === "at_most";
      li.querySelector(".f-n").hidden = !needN;
    });
    li.querySelector(".act-save").addEventListener("click", () => saveRelation(r.id, li));
    li.querySelector(".act-del").addEventListener("click", () => deleteRelation(r));
    ul.appendChild(li);
  });
}

function eventOptions(selected) {
  return state.events
    .map((e) => `<option value="${e.id}" ${e.id === selected ? "selected" : ""}>${escapeHtml(e.label)}</option>`)
    .join("");
}

function syncEventSelects() {
  const opts = eventOptions(null);
  $("#relationForm select[name=a]").innerHTML = opts;
  $("#relationForm select[name=b]").innerHTML = opts;
}

// ---------------------------------------------------------------------------
// 时间轴
// ---------------------------------------------------------------------------

function computeDomain() {
  if (zoom.manual) return zoom;
  let lo = null, hi = null;
  const consider = (v) => {
    if (v === null || v === undefined) return;
    lo = lo === null ? v : Math.min(lo, v);
    hi = hi === null ? v : Math.max(hi, v);
  };
  state.events.forEach((e) => {
    consider(e.lower); consider(e.upper);
    const b = state.analysis.bounds[e.id];
    if (b) { consider(b.lower); consider(b.upper); }
  });
  if (lo === null) { lo = -10; hi = 30; }
  if (hi === lo) hi = lo + 10;
  const pad = Math.max(2, Math.round((hi - lo) * 0.1));
  return { min: lo - pad, max: hi + pad, manual: false };
}

function renderTimeline() {
  const dom = computeDomain();
  const svg = $("#timeline");
  const evs = state.events;
  const rowH = 38, labelW = 92, padR = 24, headH = 30;
  const width = Math.max(420, $("#timelineScroll").clientWidth - 4);
  const height = headH + evs.length * rowH + 20;
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.innerHTML = "";
  const plotW = width - labelW - padR;
  const x = (t) => labelW + (t - dom.min) / (dom.max - dom.min) * plotW;

  // 网格与刻度
  const span = dom.max - dom.min;
  const step = niceStep(span);
  const NS = "http://www.w3.org/2000/svg";
  const add = (tag, attrs) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    svg.appendChild(el);
    return el;
  };
  add("line", { x1: labelW, x2: labelW, y1: 0, y2: height, stroke: "#2e3a54" });
  for (let t = Math.ceil(dom.min / step) * step; t <= dom.max; t += step) {
    const px = x(t);
    add("line", { x1: px, x2: px, y1: headH - 6, y2: height, stroke: "#24304a" });
    const txt = add("text", { x: px, y: 16, fill: "#8b97ad", "font-size": 11, "text-anchor": "middle" });
    txt.textContent = t;
  }
  // t=0 强调
  if (0 >= dom.min && 0 <= dom.max) {
    add("line", { x1: x(0), x2: x(0), y1: headH - 6, y2: height, stroke: "#3b4c74", "stroke-dasharray": "4 3" });
  }

  evs.forEach((ev, i) => {
    const y = headH + i * rowH + rowH / 2;
    const inConflict = isConflict("event", ev.id);
    const selected = selectedId === "event:" + ev.id;
    const lbl = add("text", {
      x: 8, y: y + 4, fill: inConflict ? "#ff929c" : "#cdd7ec", "font-size": 12,
      class: "tl-label",
    });
    lbl.textContent = ev.label.length > 7 ? ev.label.slice(0, 7) + "…" : ev.label;
    lbl.style.cursor = "pointer";
    lbl.addEventListener("click", () => locateItem("event", ev.id));

    add("line", { x1: labelW, x2: width - padR, y1: y, y2: y, stroke: "#24304a" });

    const b = state.analysis.bounds[ev.id];
    if (b) {
      let x1, x2, tip;
      if (b.lower !== null && b.upper !== null) {
        x1 = x(b.lower); x2 = x(b.upper);
        tip = `${ev.label}: ${b.lower} ≤ t ≤ ${b.upper}`;
      } else if (b.lower !== null) {
        x1 = x(b.lower); x2 = width - padR;
        tip = `${ev.label}: t ≥ ${b.lower}（无上界）`;
      } else if (b.upper !== null) {
        x1 = labelW; x2 = x(b.upper);
        tip = `${ev.label}: t ≤ ${b.upper}（无下界）`;
      } else {
        x1 = labelW; x2 = width - padR;
        tip = `${ev.label}: 无下界 ~ 无上界`;
      }
      const bar = add("rect", {
        x: x1, y: y - 8, width: Math.max(3, x2 - x1), height: 16, rx: 4,
        fill: inConflict ? "#ff5d6c" : "#4f8cff", opacity: selected ? 1 : 0.75,
        stroke: selected ? "#ffb020" : "none", "stroke-width": 2,
      });
      const tt = add("title", {}); tt.textContent = tip;
      bar.appendChild(tt);
      bar.style.cursor = "pointer";
      bar.addEventListener("click", () => locateItem("event", ev.id));
      // 端点
      [[b.lower, b.upper]].forEach(() => {});
      if (b.lower !== null) {
        add("circle", { cx: x(b.lower), cy: y, r: 4, fill: "#22c08a" });
      }
      if (b.upper !== null) {
        add("circle", { cx: x(b.upper), cy: y, r: 4, fill: "#ffb020" });
      }
    } else if (inConflict) {
      const tt = add("text", { x: labelW + 8, y: y + 4, fill: "#ff929c", "font-size": 12 });
      tt.textContent = "⚠ 矛盾项";
    }
  });

  // 选中的关系：在轴上标注其涉及事件
  if (selectedId && selectedId.startsWith("relation:")) {
    const r = state.relations.find((x) => x.id === selectedId.slice(9));
    if (r) {
      [r.a, r.b].forEach((eid) => {
        const idx = evs.findIndex((e) => e.id === eid);
        if (idx >= 0) {
          const yy = headH + idx * rowH;
          add("rect", {
            x: labelW, y: yy + 2, width: plotW, height: rowH - 4, rx: 6,
            fill: "rgba(255,176,32,.08)", stroke: "#ffb020", "stroke-dasharray": "3 3",
          });
        }
      });
    }
  }
}

function niceStep(span) {
  const raw = span / 10;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / pow;
  const f = n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10;
  return f * pow;
}

// ---------------------------------------------------------------------------
// 联动定位
// ---------------------------------------------------------------------------

function locateItem(kind, id) {
  selectedId = `${kind}:${id}`;
  renderEvents();
  renderRelations();
  renderTimeline();
  // 滚动列表到对应项
  const list = kind === "event" ? $("#eventList") : $("#relationList");
  const li = list.querySelector(`.item[data-id="${id}"]`);
  if (li) li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  // 详情
  const detail = $("#timelineDetail");
  if (kind === "event") {
    const ev = state.events.find((e) => e.id === id);
    const b = state.analysis.bounds[id];
    detail.innerHTML = b
      ? `事件 <b>${escapeHtml(ev.label)}</b>：${fmtBound(b.lower, "lower")} ~ ${fmtBound(b.upper, "upper")}`
      : `事件 <b>${escapeHtml(ev.label)}</b> 位于最小矛盾集中。`;
  } else {
    const r = state.relations.find((x) => x.id === id);
    detail.innerHTML = `关系 <b>${escapeHtml(relText(r))}</b>` +
      (isConflict("relation", id) ? "（位于最小矛盾集中）" : "");
  }
}

// ---------------------------------------------------------------------------
// 保存（自动防抖提交，服务端在同一事务复核基线与可满足性）
// ---------------------------------------------------------------------------

function scheduleSave(ops, message) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const payload = await api.commit(state.branch, state.headId, ops, message);
      applyPayload(payload);
      toast("已提交", "good");
    } catch (e) {
      handleSaveError(e);
    }
  }, 350);
}

async function commitNow(ops, message) {
  clearTimeout(saveTimer);
  try {
    const payload = await api.commit(state.branch, state.headId, ops, message);
    applyPayload(payload);
    return true;
  } catch (e) {
    handleSaveError(e);
    return false;
  }
}

function handleSaveError(e) {
  if (e.status === 422 && e.detail?.code === "unsatisfiable") {
    refresh(); // 服务端已回滚；本地仍保留编辑意图，刷新显示最新已提交状态
    toast("提交被拒绝：约束不可满足（事务已回滚，无部分写入）", "bad");
  } else if (e.status === 409 && e.detail?.code === "stale_base") {
    refresh();
    toast("基线版本已变化，已为你刷新；请在新版本上重试", "bad");
  } else {
    toast("保存失败：" + e.message, "bad");
  }
}

function applyPayload(p) {
  state.headId = p.head_id;
  state.seqCounter = p.seq_counter;
  state.events = p.events;
  state.relations = p.relations;
  state.analysis = p.analysis;
  render();
}

// 事件表单
$("#eventForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const op = {
    op: "event_create",
    label: f.label.value,
    lower: f.lower.value === "" ? null : parseInt(f.lower.value, 10),
    upper: f.upper.value === "" ? null : parseInt(f.upper.value, 10),
  };
  f.reset();
  await commitNow([op], "添加事件 " + op.label);
});

function saveEvent(id, li) {
  const op = {
    op: "event_update", id,
    label: li.querySelector(".f-label").value,
    clear: [],
  };
  const lo = li.querySelector(".f-lower").value;
  const hi = li.querySelector(".f-upper").value;
  if (lo === "") op.clear.push("lower"); else op.lower = parseInt(lo, 10);
  if (hi === "") op.clear.push("upper"); else op.upper = parseInt(hi, 10);
  commitNow([op], "修改事件");
}

function deleteEvent(ev) {
  if (!confirm(`删除事件「${ev.label}」？其关联关系将一并删除。`)) return;
  commitNow([{ op: "event_delete", id: ev.id }], "删除事件");
}

// 关系表单
const relForm = $("#relationForm");
relForm.dir.addEventListener("change", () => {
  const needN = relForm.dir.value === "at_least" || relForm.dir.value === "at_most";
  relForm.n.style.display = needN ? "" : "none";
  relForm.querySelector(".n-unit").style.display = needN ? "" : "none";
  renderSemantics();
});
relForm.addEventListener("input", renderSemantics);
function renderSemantics() {
  const f = relForm;
  const a = f.a.options[f.a.selectedIndex]?.text || "A";
  const b = f.b.options[f.b.selectedIndex]?.text || "B";
  const t = f.dir.value;
  const n = parseInt(f.n.value, 10);
  const nn = Number.isFinite(n) ? n : "n";
  let s = "";
  if (t === "earlier") s = `含义：${a} 早于 ${b}，即 t_${a}+1 ≤ t_${b}`;
  else if (t === "later") s = `含义：${a} 晚于 ${b}，即 t_${b}+1 ≤ t_${a}`;
  else if (t === "same") s = `含义：${a} 与 ${b} 同一时刻，即 t_${a} = t_${b}`;
  else if (t === "at_least") s = `含义：t_${a} − t_${b} ≥ max(1, ${nn})（n 为非负整数）`;
  else s = `含义：1 ≤ t_${a} − t_${b} ≤ ${nn}；n=0 时必为矛盾项`;
  $("#relSemantics").textContent = s;
}

relForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = relForm.dir.value;
  const op = { op: "relation_create", type: t, a: relForm.a.value, b: relForm.b.value };
  if (t === "at_least" || t === "at_most") {
    const n = parseInt(relForm.n.value, 10);
    if (!Number.isFinite(n) || n < 0) { toast("n 必须是非负整数", "bad"); return; }
    op.n = n;
  }
  if (op.a === op.b && t !== "same") {
    if (!confirm("A、B 是同一事件，该自反关系会造成矛盾，仍要添加吗？")) return;
  }
  await commitNow([op], "添加关系");
  relForm.n.value = "";
  renderSemantics();
});

function saveRelation(id, li) {
  const type = li.querySelector(".f-type").value;
  const op = {
    op: "relation_update", id,
    type, a: li.querySelector(".f-a").value, b: li.querySelector(".f-b").value,
  };
  if (type === "at_least" || type === "at_most") {
    const n = parseInt(li.querySelector(".f-n").value, 10);
    if (!Number.isFinite(n) || n < 0) { toast("n 必须是非负整数", "bad"); return; }
    op.n = n;
  }
  commitNow([op], "修改关系");
}

function deleteRelation(r) {
  if (!confirm("删除该关系？")) return;
  commitNow([{ op: "relation_delete", id: r.id }], "删除关系");
}

// ---------------------------------------------------------------------------
// 分支
// ---------------------------------------------------------------------------

async function renderBranchTabs() {
  const branches = await api.branches();
  const tabs = $("#branchTabs");
  tabs.innerHTML = "";
  branches.forEach((b) => {
    const btn = document.createElement("button");
    btn.className = "branch-tab" + (b.name === state.branch ? " active" : "");
    btn.textContent = b.name + (b.fork_from ? `⑂${b.fork_from}` : "");
    btn.title = b.fork_from ? `自 ${b.fork_from} @ ${b.fork_point.slice(0, 8)} 分叉` : "";
    btn.addEventListener("click", () => switchBranch(b.name));
    tabs.appendChild(btn);
  });
  $("#mergeSource").innerHTML = branches
    .filter((b) => b.name !== state.branch)
    .map((b) => `<option value="${b.name}">${b.name}</option>`).join("");
}

async function switchBranch(name) {
  state.branch = name;
  localStorage.setItem(LS_KEY, name);
  selectedId = null;
  zoom.manual = false;
  await refresh();
}

$("#btnNewBranch").addEventListener("click", async () => {
  const name = $("#newBranchName").value.trim();
  if (!name) return toast("请输入分支名", "bad");
  try {
    await api.newBranch(name, state.branch);
    $("#newBranchName").value = "";
    await switchBranch(name);
    toast(`已从 ${state.branch} 当前版本分叉`, "good");
  } catch (e) {
    toast(e.message, "bad");
  }
});

$("#btnHistory").addEventListener("click", async () => {
  $("#historyMask").hidden = false;
  const rows = await api.history(state.branch);
  $("#historyList").innerHTML = rows.map((r) => `
    <li>
      <div><b>${escapeHtml(r.message)}</b>
        ${r.kind === "merge" ? `<span class="hint">（合并自 ${escapeHtml(r.merge_source || "")}）</span>` : ""}
      </div>
      <div class="sub">${r.id.slice(0, 8)} · ${r.created_at}
        ${r.parent_id ? "← " + r.parent_id.slice(0, 8) : "（根提交）"}</div>
    </li>`).join("");
});
$("#btnCloseHistory").addEventListener("click", () => ($("#historyMask").hidden = true));

// ---------------------------------------------------------------------------
// 合并
// ---------------------------------------------------------------------------

$("#btnMerge").addEventListener("click", async () => {
  await renderBranchTabs();
  $("#mergeMask").hidden = false;
  mergeData = null;
  $("#mergeConflicts").textContent = "请选择源分支并预览";
  $("#mergeAuto").textContent = "—";
  $("#mergeStatus").textContent = "";
});
$("#btnCloseMerge").addEventListener("click", () => ($("#mergeMask").hidden = true));
$("#btnMergeCancel").addEventListener("click", () => ($("#mergeMask").hidden = true));
$("#btnMergePreview").addEventListener("click", () => doMergePreview({}));

async function doMergePreview(resolutions) {
  const source = $("#mergeSource").value;
  try {
    mergeData = await api.mergePreview(state.branch, source, resolutions);
    renderMergeResult(source);
  } catch (e) {
    toast("预览失败：" + e.message, "bad");
  }
}

function mergeValueText(kind, v) {
  if (v === null) return "清空(无界)";
  if (v === undefined) return "—";
  if (kind === "event") return String(v);
  return String(v);
}

function renderMergeResult(source) {
  const { conflicts, auto_merged, analysis } = mergeData;
  $("#mergeBaseInfo").textContent = `源：${source} · 冲突 ${conflicts.length} 项 · 自动合入若干`;

  const confBox = $("#mergeConflicts");
  confBox.innerHTML = "";
  if (!conflicts.length) {
    confBox.innerHTML = `<div class="hint">无交叠冲突，可直接提交。</div>`;
  }
  conflicts.forEach((c) => {
    const card = document.createElement("div");
    card.className = "conflict-card";
    const isDel = c.field === "__delete__";
    const target = c.kind === "event"
      ? state.events.find((e) => e.id === c.id) || mergeData.merged_preview.events.find((e) => e.id === c.id)
      : state.relations.find((r) => r.id === c.id) || mergeData.merged_preview.relations.find((r) => r.id === c.id);
    const name = target ? (c.kind === "event" ? target.label : relText(target)) : c.id.slice(0, 6);
    const checked = (side) => c.resolution === side ? "checked" : "";
    card.innerHTML = `
      <div class="ttl">${c.kind === "event" ? "事件" : "关系"}「${escapeHtml(name)}」
        — ${isDel ? "删除/修改冲突" : "字段「" + c.field + "」双方都修改为不同值"}
        ${c.resolution ? '<span class="hint">（已选择' + (c.resolution === "ours" ? "当前" : "源") + "）</span>" : ""}</div>
      <div class="conflict-choice">
        <label data-side="ours" class="${c.resolution === "ours" ? "picked" : ""}">
          <input type="radio" name="${c.key}" value="ours" ${checked("ours")}>
          当前分支：<b>${isDel ? (c.ours === null ? "删除" : "保留并修改") : escapeHtml(mergeValueText(c.kind, c.ours))}</b></label>
        <label data-side="theirs" class="${c.resolution === "theirs" ? "picked" : ""}">
          <input type="radio" name="${c.key}" value="theirs" ${checked("theirs")}>
          源分支：<b>${isDel ? (c.theirs === null ? "删除" : "保留并修改") : escapeHtml(mergeValueText(c.kind, c.theirs))}</b></label>
      </div>`;
    $$(`input[name="${CSS.escape(c.key)}"]`, card).forEach((radio) => {
      radio.addEventListener("change", async () => {
        radio.closest("label").classList.add("picked");
        $$(`input[name="${CSS.escape(c.key)}"]`, card).forEach((rr) => {
          if (rr !== radio) rr.closest("label").classList.remove("picked");
        });
        const resolutions = collectResolutions();
        await doMergePreview(resolutions);
      });
    });
    confBox.appendChild(card);
  });

  const autoBox = $("#mergeAuto");
  const items = [...auto_merged.events.map((x) => ({ ...x, kind: "event" })),
                 ...auto_merged.relations.map((x) => ({ ...x, kind: "relation" }))];
  autoBox.innerHTML = items.length
    ? items.map((x) => {
        const tgt = x.kind === "event"
          ? mergeData.merged_preview.events.find((e) => e.id === x.id)
          : mergeData.merged_preview.relations.find((r) => r.id === x.id);
        const txt = tgt ? (x.kind === "event" ? tgt.label : relText(tgt)) : x.id.slice(0, 6);
        const act = { created: "新建", deleted: "删除", modified: "修改" }[x.action];
        return `<div class="auto-item">[${act}] ${escapeHtml(txt)}</div>`;
      }).join("")
    : `<div class="hint">无</div>`;

  const status = $("#mergeStatus");
  status.className = "merge-status " + (analysis.satisfiable ? "good" : "bad");
  status.textContent = analysis.satisfiable
    ? "✓ 合并结果可满足"
    : `✗ 合并结果不可满足，最小矛盾集 ${analysis.contradiction.length} 项：`
      + analysis.contradiction.map((c) => {
          const it = c.kind === "event"
            ? mergeData.merged_preview.events.find((e) => e.id === c.id)
            : mergeData.merged_preview.relations.find((r) => r.id === c.id);
          return it ? "#" + it.seq : "";
        }).filter(Boolean).join(", ");
}

function collectResolutions() {
  const resolutions = {};
  $$("#mergeConflicts input[type=radio]:checked").forEach((r) => {
    resolutions[r.name] = r.value;
  });
  return resolutions;
}

$("#btnMergeCommit").addEventListener("click", async () => {
  const source = $("#mergeSource").value;
  const resolutions = collectResolutions();
  try {
    const p = await api.mergeCommit(state.branch, source, resolutions,
      `合并 ${source}`);
    $("#mergeMask").hidden = true;
    applyPayload(p);
    toast("合并已提交", "good");
  } catch (e) {
    if (e.status === 409 && e.detail?.code === "unresolved_conflicts") {
      toast("仍有冲突未选择", "bad");
    } else if (e.status === 422) {
      toast("合并事务被拒绝：结果不可满足，未写入任何数据", "bad");
      doMergePreview(resolutions);
    } else {
      toast(e.message, "bad");
    }
  }
});

// 缩放
$("#btnZoomIn").addEventListener("click", () => zoomManual(0.8));
$("#btnZoomOut").addEventListener("click", () => zoomManual(1.25));
$("#btnFit").addEventListener("click", () => { zoom.manual = false; renderTimeline(); });
function zoomManual(f) {
  const d = computeDomain();
  const c = (d.min + d.max) / 2;
  const half = (d.max - d.min) / 2 * f;
  zoom = { min: Math.round(c - half), max: Math.round(c + half), manual: true };
  renderTimeline();
}

// ---------------------------------------------------------------------------
// 杂项
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + (kind || "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3200);
}

async function refresh() {
  const p = await api.getState(state.branch);
  applyPayload(p);
  await renderBranchTabs();
}

(async function init() {
  const saved = localStorage.getItem(LS_KEY);
  if (saved) state.branch = saved;
  try {
    await refresh();
    if (state.events.length === 0) renderSemantics();
  } catch (e) {
    toast("初始化失败：" + e.message, "bad");
  }
})();
