const IST_OFFSET = 5.5 * 60 * 60 * 1000;

function toIST(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    const ist = new Date(d.getTime() + IST_OFFSET);
    return ist.toISOString().replace("T", " ").substring(0, 16) + " IST";
  } catch { return ts; }
}

function fmtINR(v) {
  if (v == null) return "—";
  return new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(v);
}

function pnlClass(v) { return v >= 0 ? "pnl-pos" : "pnl-neg"; }
function pnlSign(v) { return v >= 0 ? "+" : ""; }
function colorClass(v) { return v >= 0 ? "green" : "red"; }

// ── Summary ──────────────────────────────────────────────
async function loadSummary() {
  try {
    const r = await fetch("/api/summary");
    const d = await r.json();

    set("total-pnl", `₹${fmtINR(d.total_pnl)}`, colorClass(d.total_pnl));
    set("today-pnl", `${pnlSign(d.today_pnl)}₹${fmtINR(d.today_pnl)}`, colorClass(d.today_pnl));
    set("today-trades", d.today_trades, "blue");
    set("open-positions", d.open_positions, "blue");
    set("win-rate", `${d.win_rate}%`, d.win_rate >= 50 ? "green" : "red");
    set("total-trades", `${d.winning_trades}W / ${d.losing_trades}L`, "");
    set("total-brokerage", `₹${fmtINR(d.total_brokerage ?? 0)}`, "red");
    set("lessons-count", d.lessons_count, "blue");

    document.getElementById("last-updated").textContent = "Updated " + toIST(new Date().toISOString());
  } catch (e) {
    console.error("Summary load error:", e);
  }
}

function set(id, val, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  el.className = "summary-value" + (cls ? " " + cls : "");
}

// ── Open Positions ────────────────────────────────────────
async function loadPositions() {
  try {
    const r = await fetch("/api/positions");
    const positions = await r.json();
    const container = document.getElementById("positions-grid");
    const header = document.getElementById("positions-header");

    if (header) {
      const cnt = header.querySelector(".count");
      if (cnt) cnt.textContent = positions.length;
    }

    if (!positions.length) {
      container.innerHTML = '<div class="empty">No open positions</div>';
      return;
    }

    container.innerHTML = positions.map(p => {
      const badgeCls = (p.action || "").toLowerCase();
      const conf = p.confidence ? Math.round(p.confidence * 100) + "%" : "—";
      const hasCmp = p.last_cmp != null;
      const cmpVal = hasCmp ? `₹${fmtINR(p.last_cmp)}` : "—";
      const cmpPnl = hasCmp && p.entry_price
        ? (p.action === "BUY" ? p.last_cmp - p.entry_price : p.entry_price - p.last_cmp) * (p.quantity || 0)
        : null;
      const cmpCls = !hasCmp ? "" : (p.last_cmp >= p.entry_price && p.action === "BUY") || (p.last_cmp <= p.entry_price && p.action === "SHORT") ? "green" : "red";
      const cmpChecked = hasCmp ? `CMP as of ${toIST(p.last_cmp_time)}` : "";
      const unrealStr = cmpPnl != null ? ` (${cmpPnl >= 0 ? "+" : ""}₹${fmtINR(cmpPnl)})` : "";
      return `
        <div class="card">
          <div class="card-header">
            <div class="card-symbol">${p.symbol || "—"}</div>
            <span class="card-badge ${badgeCls}">${p.action}</span>
          </div>
          <div class="card-stats">
            <div class="stat-box">
              <div class="stat-label">Entry</div>
              <div class="stat-value blue">₹${fmtINR(p.entry_price)}</div>
            </div>
            <div class="stat-box">
              <div class="stat-label">CMP</div>
              <div class="stat-value ${cmpCls}">${cmpVal}${unrealStr}</div>
            </div>
            <div class="stat-box">
              <div class="stat-label">Qty</div>
              <div class="stat-value">${p.quantity ?? "—"}</div>
            </div>
            <div class="stat-box">
              <div class="stat-label">Stop Loss</div>
              <div class="stat-value red">₹${fmtINR(p.stop_loss)}</div>
            </div>
            <div class="stat-box">
              <div class="stat-label">Target</div>
              <div class="stat-value green">₹${fmtINR(p.target_1)}</div>
            </div>
          </div>
          <div class="card-footer">
            <span class="tag">${p.setup_type || "—"}</span>
            <span class="tag">${p.time_horizon || "—"}</span>
            <span style="float:right">Confidence: <b>${conf}</b></span>
            <br><span style="color:var(--muted);font-size:10px;">Entry: ${toIST(p.entry_time)}</span>
            ${cmpChecked ? `<br><span style="color:var(--muted);font-size:10px;">${cmpChecked}</span>` : ""}
          </div>
        </div>`;
    }).join("");
  } catch (e) {
    console.error("Positions load error:", e);
  }
}

// ── Trades Table ──────────────────────────────────────────
async function loadTrades() {
  const dateEl = document.getElementById("filter-date");
  const symEl = document.getElementById("filter-symbol");
  const actEl = document.getElementById("filter-action");

  const params = new URLSearchParams({ limit: 100 });
  if (dateEl && dateEl.value) params.set("date", dateEl.value);
  if (symEl && symEl.value) params.set("symbol", symEl.value);
  if (actEl && actEl.value) params.set("action", actEl.value);

  try {
    const r = await fetch("/api/trades?" + params);
    const trades = await r.json();
    const tbody = document.getElementById("trades-tbody");

    if (!trades.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty">No trades found</td></tr>';
      return;
    }

    tbody.innerHTML = trades.map(t => {
      const netPnl = t.pnl ?? 0;
      const grossPnl = t.gross_pnl ?? netPnl;
      const brok = t.brokerage ?? 0;
      const rowCls = netPnl > 0 ? "win" : netPnl < 0 ? "loss" : "";
      return `
        <tr class="${rowCls}">
          <td>${toIST(t.exit_time)}</td>
          <td><b>${t.symbol || "—"}</b></td>
          <td><span class="tag">${t.action || "—"}</span></td>
          <td>₹${fmtINR(t.entry_price)}</td>
          <td>₹${fmtINR(t.exit_price)}</td>
          <td>${t.quantity ?? "—"}</td>
          <td class="${pnlClass(grossPnl)}">${pnlSign(grossPnl)}₹${fmtINR(grossPnl)}</td>
          <td style="color:var(--muted)">₹${fmtINR(brok)}</td>
          <td class="${pnlClass(netPnl)}"><b>${pnlSign(netPnl)}₹${fmtINR(netPnl)}</b></td>
          <td><span class="tag">${t.setup_type || "—"}</span></td>
          <td style="color:var(--muted);font-size:11px">${t.exit_reason || "—"}</td>
        </tr>`;
    }).join("");
  } catch (e) {
    console.error("Trades load error:", e);
  }
}

// ── Lessons ───────────────────────────────────────────────
async function loadLessons() {
  try {
    const r = await fetch("/api/lessons?limit=15");
    const lessons = await r.json();
    const container = document.getElementById("lessons-list");

    if (!lessons.length) {
      container.innerHTML = '<div class="empty">No lessons yet — agent learns after each closed trade</div>';
      return;
    }

    container.innerHTML = lessons.map(l => `
      <div class="lesson-item">
        <span class="lesson-tag">${l.tag || "general"}</span>
        <div>
          <div class="lesson-text">${l.lesson}</div>
          <div class="lesson-time">${toIST(l.timestamp)}</div>
        </div>
      </div>`).join("");
  } catch (e) {
    console.error("Lessons load error:", e);
  }
}

// ── Logs ──────────────────────────────────────────────────
async function loadLogs() {
  try {
    const r = await fetch("/api/logs?lines=100");
    const data = await r.json();
    const box = document.getElementById("log-box");

    box.innerHTML = data.lines.map(line => {
      let cls = "log-info";
      const l = line.toLowerCase();
      if (l.includes("error") || l.includes("failed") || l.includes("exception")) cls = "log-error";
      else if (l.includes("warning") || l.includes("warn") || l.includes("blocked")) cls = "log-warn";
      else if (l.includes("trade") || l.includes("order") || l.includes("buy") || l.includes("sell") || l.includes("pnl")) cls = "log-trade";
      return `<div class="${cls}">${escHtml(line)}</div>`;
    }).join("");

    box.scrollTop = box.scrollHeight;
  } catch (e) {
    console.error("Logs load error:", e);
  }
}

function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── Set default date filter to today ─────────────────────
function setDefaultDate() {
  const el = document.getElementById("filter-date");
  if (!el) return;
  const now = new Date(Date.now() + IST_OFFSET);
  el.value = now.toISOString().substring(0, 10);
}

// ── Init & auto-refresh ───────────────────────────────────
async function refreshAll() {
  await Promise.all([loadSummary(), loadPositions(), loadLessons(), loadLogs()]);
}

document.addEventListener("DOMContentLoaded", () => {
  setDefaultDate();
  refreshAll();
  loadTrades();

  // Auto-refresh
  setInterval(refreshAll, 30000);
  setInterval(loadTrades, 60000);

  // Filter listeners
  ["filter-date", "filter-symbol", "filter-action"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", loadTrades);
  });
});
