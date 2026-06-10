const state = {
  accountMarket: "US",
  side: "BUY",
  config: null,
  decisionEntries: [],
  decisionPage: 1,
  decisionTotalPages: 1,
  myWatchlistTimer: null,
};

const el = (id) => document.getElementById(id);

function showOutput(payload) {
  el("outputBox").textContent = JSON.stringify(payload, null, 2);
}

function fmt(value) {
  if (value === null || value === undefined || value === "N/A") return "-";
  if (typeof value === "number") {
    return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  return String(value);
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fmt(value);
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtUsd(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "$0";
  return `$${number.toFixed(number < 0.01 ? 4 : 2)}`;
}

function html(value) {
  return fmt(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function rowChangePct(row) {
  const direct = Number(row.change_rate ?? row.change_pct);
  if (Number.isFinite(direct)) return direct;
  const last = Number(row.last_price);
  const prev = Number(row.prev_close_price);
  if (Number.isFinite(last) && Number.isFinite(prev) && prev > 0) {
    return ((last - prev) / prev) * 100;
  }
  return null;
}

function changeClass(value) {
  if (!Number.isFinite(value)) return "flat";
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await res.json();
  if (!res.ok) {
    throw payload;
  }
  return payload;
}

async function refreshStatus() {
  try {
    const payload = await api("/api/status");
    state.config = payload.config;
    const dot = document.querySelector(".status-dot");
    dot.className = `status-dot ${payload.ok ? "ok" : "bad"}`;
    el("statusText").textContent = payload.ok ? "OpenD connected" : "Check OpenD";
    renderRisk(payload.config);
    showOutput(payload);
  } catch (err) {
    document.querySelector(".status-dot").className = "status-dot bad";
    el("statusText").textContent = "Offline";
    showOutput(err);
  }
}

async function refreshSnapshot() {
  const codes = el("codesInput").value
    .split(",")
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean)
    .join(",");
  try {
    const payload = await api(`/api/snapshot?codes=${encodeURIComponent(codes)}`);
    renderQuotes(payload.data || []);
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function refreshAccount() {
  const currency = state.accountMarket === "US" ? "USD" : "HKD";
  try {
    const payload = await api(`/api/account?market=${state.accountMarket}&currency=${currency}`);
    renderAccount((payload.data || [])[0] || {});
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function refreshPositions() {
  try {
    const payload = await api(`/api/positions?market=${state.accountMarket}`);
    renderPositions(payload.data || []);
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function refreshMyWatchlist({ silent = false } = {}) {
  try {
    const payload = await api("/api/my-watchlist");
    renderMyWatchlist(payload);
    if (!silent) showOutput(payload);
  } catch (err) {
    el("myWatchGrid").innerHTML = `<div class="empty">Watchlist unavailable</div>`;
    if (!silent) showOutput(err);
  }
}

async function refreshDecisions() {
  const params = new URLSearchParams({
    page: String(state.decisionPage),
    page_size: el("decisionPageSize").value,
    action: el("decisionAction").value,
  });
  const start = el("decisionStart").value;
  const end = el("decisionEnd").value;
  if (start) params.set("date_start", start);
  if (end) params.set("date_end", end);
  try {
    const payload = await api(`/api/decisions?${params.toString()}`);
    state.decisionEntries = payload.entries || [];
    state.decisionPage = payload.page || 1;
    state.decisionTotalPages = payload.total_pages || 1;
    renderDecisions(state.decisionEntries);
    renderDecisionPager(payload);
  } catch (err) {
    el("decisionList").innerHTML = `<div class="empty">Decision history unavailable</div>`;
    showOutput(err);
  }
}

async function refreshGeminiUsage() {
  try {
    const payload = await api("/api/gemini-usage");
    renderGeminiUsage(payload);
  } catch (err) {
    el("geminiUsageGrid").innerHTML = `<div class="empty">Usage unavailable</div>`;
    showOutput(err);
  }
}

async function addMyWatch() {
  const codeInput = el("myWatchCode");
  const code = codeInput.value.trim().toUpperCase();
  if (!code) return;
  try {
    const payload = await api("/api/my-watchlist/add", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    codeInput.value = "";
    renderMyWatchlist(payload);
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function removeMyWatch(code) {
  try {
    const payload = await api("/api/my-watchlist/remove", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    renderMyWatchlist(payload);
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

function renderQuotes(rows) {
  const grid = el("quoteGrid");
  if (!rows.length) {
    grid.innerHTML = `<div class="empty">No quotes</div>`;
    return;
  }
  grid.innerHTML = rows
    .map(
      (row) => `
        <article class="quote-item">
          <div class="quote-code">${html(row.code)}</div>
          <div class="quote-name">${html(row.name)}</div>
          <div class="quote-price">${html(row.last_price)}</div>
          <div class="quote-meta">
            <span>Bid ${html(row.bid_price)}</span>
            <span>Ask ${html(row.ask_price)}</span>
            <span>Vol ${html(row.volume)}</span>
          </div>
        </article>
      `
    )
    .join("");
}

function renderMyWatchlist(payload) {
  const grid = el("myWatchGrid");
  const quotes = payload.quotes || [];
  const items = payload.items || [];
  const quoteByCode = new Map(quotes.map((row) => [String(row.code || "").toUpperCase(), row]));
  const rows = items.map((item) => ({ ...item, ...(quoteByCode.get(String(item.code).toUpperCase()) || {}) }));

  if (!rows.length) {
    grid.innerHTML = `<div class="empty">No watchlist symbols</div>`;
    return;
  }

  const warning = payload.quote_error ? `<div class="empty">${html(payload.quote_error)}</div>` : "";
  grid.innerHTML = warning + rows
    .map((row) => {
      const change = rowChangePct(row);
      const cls = changeClass(change);
      const displayName = row.name || row.watch_name || row.code;
      const sector = row.sector || row.watch_sector || row.market;
      return `
        <article class="watch-item">
          <button type="button" class="watch-remove" data-watch-remove="${html(row.code)}" title="删除" aria-label="删除 ${html(row.code)}">×</button>
          <div class="watch-code">${html(row.code)}</div>
          <div class="watch-name">${html(displayName)}</div>
          <div class="watch-price-row">
            <span class="watch-price">${html(row.last_price)}</span>
            <span class="watch-change ${cls}">${Number.isFinite(change) ? `${change.toFixed(2)}%` : "-"}</span>
          </div>
          <div class="watch-meta">
            <span>${html(sector)}</span>
            <span>Bid ${html(row.bid_price)}</span>
            <span>Ask ${html(row.ask_price)}</span>
          </div>
        </article>
      `;
    })
    .join("");

  grid.querySelectorAll("[data-watch-remove]").forEach((button) => {
    button.addEventListener("click", () => removeMyWatch(button.dataset.watchRemove));
  });
}

function renderAccount(row) {
  const metrics = [
    ["Cash", row.cash],
    ["Power", row.power],
    ["Assets", row.total_assets],
    ["Market Val", row.market_val],
  ];
  el("accountGrid").innerHTML = metrics
    .map(
      ([label, value]) => `
        <div class="metric">
          <div class="metric-label">${label}</div>
          <div class="metric-value">${html(value)}</div>
        </div>
      `
    )
    .join("");
}

function renderPositions(rows) {
  const body = el("positionsBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty">No positions</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td>${html(row.code)}</td>
          <td>${html(row.qty)}</td>
          <td>${html(row.market_val)}</td>
          <td>${html(row.pl_ratio)}</td>
        </tr>
      `
    )
    .join("");
}

function renderRisk(config) {
  if (!config) return;
  const risk = config.risk || {};
  const codes = risk.allowed_codes || [];
  const codeSummary = `${codes.length} codes · ${codes.slice(0, 6).join(", ")}${codes.length > 6 ? " ..." : ""}`;
  const items = [
    ["Markets", (risk.allowed_markets || []).join(", ")],
    ["Codes", codeSummary],
    ["Market Orders", risk.allow_market_orders ? "ON" : "OFF"],
    ["Whitelist", risk.require_whitelist ? "ON" : "OFF"],
    ["US Max", risk.max_order_value?.US],
    ["HK Max", risk.max_order_value?.HK],
  ];
  el("riskList").innerHTML = items
    .map(
      ([label, value]) => `
        <div class="risk-item">
          <span class="risk-label">${label}</span>
          <span class="risk-value">${html(value)}</span>
        </div>
      `
    )
    .join("");
}

function renderGeminiUsage(payload) {
  const tokens = payload.tokens || {};
  const price = payload.price || {};
  const measured = `${html(payload.measured_calls)} / ${html(payload.calls)}`;
  const items = [
    ["Today Calls", payload.calls],
    ["Measured", measured],
    ["Input Tokens", tokens.prompt_token_count],
    ["Output Tokens", (Number(tokens.candidates_token_count) || 0) + (Number(tokens.thoughts_token_count) || 0)],
    ["Paid Today", fmtUsd(payload.paid_estimate_usd)],
    ["Paid / Day", fmtUsd(payload.projected_paid_usd_per_day)],
    ["Input $/1M", price.input_per_1m],
    ["Output $/1M", price.output_per_1m],
  ];
  el("geminiUsageGrid").innerHTML = items
    .map(
      ([label, value]) => `
        <div class="metric">
          <div class="metric-label">${label}</div>
          <div class="metric-value">${html(value)}</div>
        </div>
      `
    )
    .join("");
}

function executionLabel(row) {
  if (row.execution?.ok && row.execution?.mode === "paper_execute") return "已执行";
  if (row.execution?.ok && row.execution?.mode === "paper_dry_run") return "Dry-run";
  if (row.execution && row.execution.ok === false) return "执行失败";
  if ((row.blocked_reasons || []).length) return "已阻止";
  if (row.order) return "待执行";
  return "未下单";
}

function renderDecisions(rows) {
  const list = el("decisionList");
  if (!rows.length) {
    list.innerHTML = `<div class="empty">No decisions yet</div>`;
    return;
  }
  list.innerHTML = rows
    .map((row, index) => {
      const decision = row.decision || {};
      const action = String(decision.action || "UNKNOWN").toLowerCase();
      const candidates = (row.candidates || [])
        .slice(0, 3)
        .map((item) => `<span class="candidate-chip">${html(item.code)} ${html(item.change_pct)}%</span>`)
        .join("");
      const blocked = (row.blocked_reasons || [])
        .map((item) => `<span>${html(item)}</span>`)
        .join("");
      return `
        <article class="decision-item">
          <div class="decision-top">
            <div>
              <div class="decision-code">${html(decision.code || "NONE")}</div>
              <div class="decision-time">${html(fmtTime(row.timestamp || row.ts))} · ${html(row.mode)}</div>
            </div>
            <div class="decision-badges">
              <span class="decision-action ${html(action)}">${html(decision.action || "UNKNOWN")}</span>
              <span class="decision-confidence">${html(decision.confidence)}%</span>
            </div>
          </div>
          <p class="decision-reason">${html(decision.reason)}</p>
          <div class="decision-meta">
            <span>${html(executionLabel(row))}</span>
            ${row.order ? `<span>${html(row.order.side)} ${html(row.order.qty)} @ ${html(row.order.price)}</span>` : ""}
          </div>
          ${candidates ? `<div class="candidate-row">${candidates}</div>` : ""}
          ${blocked ? `<div class="blocked-row">${blocked}</div>` : ""}
          ${decision.learning_note ? `<div class="learning-note">${html(decision.learning_note)}</div>` : ""}
          <button type="button" class="detail-button" data-decision-index="${index}">详情</button>
        </article>
      `;
    })
    .join("");

  list.querySelectorAll("[data-decision-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.decisionIndex);
      showOutput(state.decisionEntries[index] || {});
    });
  });
}

function renderDecisionPager(payload) {
  const total = payload.total || 0;
  const page = payload.page || 1;
  const totalPages = payload.total_pages || 1;
  el("decisionPageInfo").textContent = `${page} / ${totalPages} · ${total}`;
  el("prevDecisionPage").disabled = page <= 1;
  el("nextDecisionPage").disabled = page >= totalPages;
}

function currentIntent() {
  return {
    code: el("orderCode").value.trim().toUpperCase(),
    side: state.side,
    qty: Number(el("orderQty").value),
    price: Number(el("orderPrice").value),
    order_type: el("orderType").value,
    reason: el("orderReason").value.trim(),
  };
}

async function validateOrder() {
  try {
    const payload = await api("/api/validate", {
      method: "POST",
      body: JSON.stringify(currentIntent()),
    });
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function executeOrder() {
  const intent = currentIntent();
  const ok = window.confirm(`模拟下单确认：${intent.side} ${intent.qty} ${intent.code} @ ${intent.price}`);
  if (!ok) return;
  try {
    const payload = await api("/api/place", {
      method: "POST",
      body: JSON.stringify({ intent, execute: true }),
    });
    showOutput(payload);
    await refreshAccount();
    await refreshPositions();
  } catch (err) {
    showOutput(err);
  }
}

async function runGemini() {
  const notes = el("newsNotes").value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
  try {
    const payload = await api("/api/ai/once", {
      method: "POST",
      body: JSON.stringify({ execute: el("aiExecute").checked, notes }),
    });
    showOutput(payload);
    await refreshDecisions();
    await refreshGeminiUsage();
    await refreshAccount();
    await refreshPositions();
  } catch (err) {
    showOutput(err);
  }
}

function bindSegments() {
  document.querySelectorAll("[data-account-market]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.accountMarket = button.dataset.accountMarket;
      document.querySelectorAll("[data-account-market]").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      await refreshAccount();
      await refreshPositions();
    });
  });

  document.querySelectorAll("[data-side]").forEach((button) => {
    button.addEventListener("click", () => {
      state.side = button.dataset.side;
      document.querySelectorAll("[data-side]").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
    });
  });
}

function bindButtons() {
  el("refreshSnapshot").addEventListener("click", refreshSnapshot);
  el("refreshMyWatchlist").addEventListener("click", () => refreshMyWatchlist());
  el("addMyWatch").addEventListener("click", addMyWatch);
  el("myWatchCode").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addMyWatch();
    }
  });
  el("refreshPositions").addEventListener("click", refreshPositions);
  el("refreshDecisions").addEventListener("click", refreshDecisions);
  el("decisionStart").addEventListener("change", () => {
    state.decisionPage = 1;
    refreshDecisions();
  });
  el("decisionEnd").addEventListener("change", () => {
    state.decisionPage = 1;
    refreshDecisions();
  });
  el("decisionAction").addEventListener("change", () => {
    state.decisionPage = 1;
    refreshDecisions();
  });
  el("decisionPageSize").addEventListener("change", () => {
    state.decisionPage = 1;
    refreshDecisions();
  });
  el("prevDecisionPage").addEventListener("click", () => {
    state.decisionPage = Math.max(1, state.decisionPage - 1);
    refreshDecisions();
  });
  el("nextDecisionPage").addEventListener("click", () => {
    state.decisionPage = Math.min(state.decisionTotalPages, state.decisionPage + 1);
    refreshDecisions();
  });
  el("refreshGeminiUsage").addEventListener("click", refreshGeminiUsage);
  el("refreshConfig").addEventListener("click", refreshStatus);
  el("validateOrder").addEventListener("click", validateOrder);
  el("executeOrder").addEventListener("click", executeOrder);
  el("runGemini").addEventListener("click", runGemini);
  el("clearOutput").addEventListener("click", () => showOutput({}));
}

async function init() {
  bindSegments();
  bindButtons();
  await refreshStatus();
  await refreshSnapshot();
  await refreshMyWatchlist({ silent: true });
  await refreshAccount();
  await refreshPositions();
  await refreshDecisions();
  await refreshGeminiUsage();
  state.myWatchlistTimer = window.setInterval(() => refreshMyWatchlist({ silent: true }), 20000);
}

init();
