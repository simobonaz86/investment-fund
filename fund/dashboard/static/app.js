const MODEL_OPTIONS = [
  "anthropic/claude-haiku-4-5-20251001",
  "anthropic/claude-sonnet-4-5-20250929",
  "anthropic/claude-opus-4-7",
];

// Track last seen message id per room so we can detect new messages
const lastSeenId = { principals: 0, board: 0 };

async function fetchJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = "";
    try { detail = JSON.stringify(await r.json()); } catch { detail = await r.text(); }
    throw new Error(`${url} → ${r.status} ${detail}`);
  }
  if (r.status === 204) return null;
  return await r.json();
}

function fmtPct(v) { return v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`; }
function fmtUsd(v) { return v == null ? "—" : `$${Number(v).toFixed(2)}`; }

function escapeHtml(s) {
  return (s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

async function loadHealth() {
  const pill = document.getElementById("health-pill");
  try {
    const h = await fetchJSON("/api/health");
    pill.textContent = `v${h.version} · ${h.status}`;
    pill.className = "pill ok";
  } catch {
    pill.textContent = "offline";
    pill.className = "pill err";
  }
}

async function loadBudget() {
  const el = document.getElementById("budget");
  try {
    const b = await fetchJSON("/api/budget");
    if (b.error) { el.innerHTML = `<p>${b.error}</p>`; return; }
    el.innerHTML = ["ceo", "hr", "kevin"].map(pool => {
      const p = b[pool];
      const pct = p.allocated ? (p.spent / p.allocated) * 100 : 0;
      const color = pct > 80 ? "var(--red)" : pct > 60 ? "var(--yellow)" : "var(--green)";
      return `<div class="budget-row">
        <div class="label"><span>${pool.toUpperCase()}</span>
          <span>${fmtUsd(p.spent)} / ${fmtUsd(p.allocated)} (${pct.toFixed(0)}%)</span></div>
        <div class="bar"><span style="width:${Math.min(pct,100)}%; background:${color}"></span></div>
      </div>`;
    }).join("");
  } catch (e) { el.textContent = "error: " + e.message; }
}

async function loadModels() {
  const tbody = document.querySelector("#models-tbl tbody");
  const rows = await fetchJSON("/api/models");
  tbody.innerHTML = rows.map(r => {
    const isSpec = r.role.startsWith("specialist");
    const boardOnly = ["ceo", "kevin", "hr"].includes(r.role);
    return `<tr>
      <td><span class="role">${r.role}</span></td>
      <td>
        <select data-role="${r.role}" data-spec="${isSpec}" data-boardonly="${boardOnly}">
          ${MODEL_OPTIONS.map(m => `<option ${m === r.model ? "selected" : ""}>${m}</option>`).join("")}
        </select>
      </td>
      <td>${r.selected_by}</td>
      <td><button onclick="saveModel('${r.role}')">Save</button></td>
    </tr>`;
  }).join("");
}

async function saveModel(role) {
  const sel = document.querySelector(`select[data-role="${role}"]`);
  const boardOnly = sel.dataset.boardonly === "true";
  const selected_by = boardOnly ? "board" : "ceo";
  try {
    await fetchJSON("/api/models", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({role, model: sel.value, selected_by})
    });
    await loadModels();
  } catch (e) { alert("Failed: " + e.message); }
}

async function loadOrg() {
  const org = await fetchJSON("/api/org");
  const el = document.getElementById("org");
  const principals = org.principals.map(p => `<span class="role">${p}</span>`).join(" ");
  const roster = org.roster.length
    ? org.roster.map(r => `<div style="margin:4px 0">
        <span class="role">${r.role}</span>
        <span class="status-${r.status}">${r.status}</span>
        <span style="color:var(--muted);font-size:11px">${r.model || ""}</span>
      </div>`).join("")
    : "<p style='color:var(--muted);font-size:13px'>No specialists active.</p>";
  el.innerHTML = `<p style="font-size:13px"><strong>Permanent:</strong> ${principals}</p>
                  <p style="font-size:13px;margin-top:12px"><strong>Specialists:</strong></p>${roster}`;
}

async function loadKevin() {
  const data = await fetchJSON("/api/kevin-audit");
  const warn = document.getElementById("kevin-warning");
  warn.innerHTML = data.unsurfaced_warning_count
    ? `<div class="warn">⚠ ${data.unsurfaced_warning_count} Kevin actions never surfaced — silent bug!</div>`
    : "";
  const tbody = document.querySelector("#kevin-tbl tbody");
  tbody.innerHTML = data.log.slice(0, 20).map(r => `<tr>
    <td>${r.created_at.slice(5, 19)}</td>
    <td>${r.action}</td>
    <td>${r.target_type}${r.target_id ? `:${r.target_id}` : ""}</td>
    <td>${r.surfaced_in_chat ? "✓" : "—"}</td>
    <td>${r.surfaced_in_dashboard ? "✓" : "—"}</td>
    <td>${r.board_notified ? "✓" : "—"}</td>
    <td>${escapeHtml((r.reason || "").slice(0, 80))}</td>
  </tr>`).join("");
}

function renderChat(elementId, msgs, room) {
  const el = document.getElementById(elementId);
  // Preserve scroll position if user scrolled up
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;

  // msgs come newest-first from API; render oldest-first visually
  const ordered = msgs.slice().reverse();
  const html = ordered.map(m => {
    const isNew = m.id > lastSeenId[room];
    return `<div class="chat-msg ${m.sender} ${isNew ? "flash" : ""}">
      <span class="sender ${m.sender}">${m.sender}</span>
      <span class="ts">${m.created_at.slice(5, 19).replace("T", " ")}</span>
      <div class="body">${escapeHtml(m.body ?? m.message)}</div>
      ${m.thread ? `<div class="thread">thread: ${escapeHtml(m.thread)}</div>` : ""}
    </div>`;
  }).join("");
  el.innerHTML = html;

  if (ordered.length) {
    lastSeenId[room] = Math.max(...ordered.map(m => m.id));
  }
  if (atBottom) el.scrollTop = el.scrollHeight;
}

async function loadChat() {
  try {
    const [principals, board] = await Promise.all([
      fetchJSON("/api/chat?room=principals&limit=50"),
      fetchJSON("/api/chat?room=board&limit=50"),
    ]);
    renderChat("chat-principals", principals, "principals");
    renderChat("chat-board", board, "board");
  } catch (e) {
    console.error("chat load failed:", e);
  }
}

async function sendBoard() {
  const ta = document.getElementById("board-msg");
  const msg = ta.value.trim();
  if (!msg) return;
  const btn = event.target;
  btn.disabled = true;
  try {
    await fetchJSON("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message: msg})
    });
    ta.value = "";
    await loadChat();
  } catch (e) { alert("Send failed: " + e.message); }
  finally { btn.disabled = false; }
}

async function replyNow() {
  const btn = event.target;
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "Waiting for CEO…";
  try {
    const res = await fetchJSON("/api/chat/reply-now", {method: "POST"});
    await loadChat();
    if (res.processed === 0) {
      btn.textContent = "Nothing new to reply to";
      setTimeout(() => btn.textContent = orig, 2000);
    } else {
      btn.textContent = orig;
    }
  } catch (e) {
    alert("Reply-now failed: " + e.message);
    btn.textContent = orig;
  } finally {
    btn.disabled = false;
  }
}

async function loadReports() {
  const rows = await fetchJSON("/api/reports");
  const tbody = document.querySelector("#reports-tbl tbody");
  tbody.innerHTML = rows.map(r => `<tr>
    <td>${r.kind}</td>
    <td>${r.period_start.slice(0,10)} → ${r.period_end.slice(0,10)}</td>
    <td>${fmtUsd(r.pnl_usd)} (${fmtPct(r.pnl_pct)})</td>
    <td>${r.sharpe ?? "—"}</td>
    <td>${r.max_drawdown ?? "—"}%</td>
    <td>${fmtPct(r.benchmark_pnl_pct)}</td>
  </tr>`).join("");
}

async function runReport(kind) {
  try {
    await fetchJSON("/api/reports/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind})
    });
    await loadReports();
  } catch (e) { alert("Failed: " + e.message); }
}

async function runHR() {
  try {
    await fetchJSON("/api/hr/run", {method: "POST"});
    await loadChat();
  } catch (e) { alert("Failed: " + e.message); }
}

// Ctrl/Cmd+Enter sends Board message
document.addEventListener("DOMContentLoaded", () => {
  const ta = document.getElementById("board-msg");
  if (ta) {
    ta.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        document.querySelector("button[onclick='sendBoard()']").click();
      }
    });
  }
});

async function refreshAll() {
  loadHealth(); loadBudget(); loadModels(); loadOrg();
  loadKevin(); loadChat(); loadReports();
}

refreshAll();
setInterval(refreshAll, 5000);
