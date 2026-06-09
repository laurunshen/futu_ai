const state = {
  accountMarket: "US",
  side: "BUY",
  config: null,
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

function html(value) {
  return fmt(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
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
  el("refreshPositions").addEventListener("click", refreshPositions);
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
  await refreshAccount();
  await refreshPositions();
}

init();
