const state = {
  activeTab: "overview",
  accountMarket: "US",
  side: "BUY",
  config: null,
  decisionEntries: [],
  selectedDecisionIndex: null,
  decisionPage: 1,
  decisionTotalPages: 1,
  newsSignals: [],
  newsPayload: null,
  newsPage: 1,
  newsTotalPages: 1,
  riskAllowedCodes: [],
  myWatchlistTimer: null,
};

const el = (id) => document.getElementById(id);

function setActiveTab(tab) {
  let nextPanel = document.querySelector(`[data-tab-panel="${tab}"]`);
  if (!nextPanel) {
    tab = "overview";
    nextPanel = document.querySelector(`[data-tab-panel="${tab}"]`);
  }
  if (!nextPanel) return;
  state.activeTab = tab;

  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });

  document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.tabPanel === tab);
  });

  try {
    window.localStorage.setItem("futu-paper-ai-tab", tab);
  } catch {
    // Local storage can be blocked by browser privacy settings.
  }
}

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
    renderRiskEditor(payload.config);
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

async function refreshNewsSignals({ silent = false } = {}) {
  try {
    const payload = await api("/api/news-signals?limit=50");
    renderNewsSignals(payload);
    if (!silent) showOutput(payload);
  } catch (err) {
    el("newsSignalList").innerHTML = `<div class="empty">News signals unavailable</div>`;
    if (!silent) showOutput(err);
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

function normalizeRiskCode(value) {
  const text = String(value || "").trim().toUpperCase().replace(/\s+/g, "");
  if (!text) return "";
  const hk = text.match(/^HK\.?(\d{1,5})$/);
  if (hk) return `HK.${hk[1].padStart(5, "0")}`;
  const us = text.match(/^US\.?([A-Z][A-Z0-9.\-]{0,9})$/);
  if (us) return `US.${us[1]}`;
  if (/^[A-Z][A-Z0-9.\-]{0,9}$/.test(text)) return `US.${text}`;
  return text;
}

function renderRiskCodeList() {
  const list = el("riskCodeList");
  if (!state.riskAllowedCodes.length) {
    list.innerHTML = `<div class="empty compact-empty">未配置白名单代码</div>`;
    return;
  }
  list.innerHTML = state.riskAllowedCodes
    .map(
      (code) => `
        <div class="risk-code-item">
          <span>${html(code)}</span>
          <button type="button" data-risk-code-remove="${html(code)}" title="删除" aria-label="删除 ${html(code)}">×</button>
        </div>
      `
    )
    .join("");
  list.querySelectorAll("[data-risk-code-remove]").forEach((button) => {
    button.addEventListener("click", () => {
      state.riskAllowedCodes = state.riskAllowedCodes.filter((code) => code !== button.dataset.riskCodeRemove);
      renderRiskCodeList();
    });
  });
}

function addRiskCode() {
  const input = el("riskCodeInput");
  const code = normalizeRiskCode(input.value);
  if (!code) return;
  if (!state.riskAllowedCodes.includes(code)) {
    state.riskAllowedCodes = [...state.riskAllowedCodes, code].sort();
  }
  input.value = "";
  renderRiskCodeList();
}

function renderRiskEditor(config) {
  if (!config?.risk) return;
  const risk = config.risk;
  el("riskRequireWhitelist").checked = Boolean(risk.require_whitelist);
  el("riskAllowSell").checked = Boolean(risk.allow_sell);
  el("riskAllowMarketOrders").checked = Boolean(risk.allow_market_orders);
  state.riskAllowedCodes = [...(risk.allowed_codes || [])].map(normalizeRiskCode).filter(Boolean).sort();
  renderRiskCodeList();
  el("riskMaxOrderUS").value = risk.max_order_value?.US ?? "";
  el("riskMaxOrderHK").value = risk.max_order_value?.HK ?? "";
  el("riskMaxQtyUS").value = risk.max_qty?.US ?? "";
  el("riskMaxQtyHK").value = risk.max_qty?.HK ?? "";
}

function riskPayloadFromForm() {
  return {
    allowed_markets: ["US", "HK"],
    allowed_codes: state.riskAllowedCodes,
    require_whitelist: el("riskRequireWhitelist").checked,
    allow_sell: el("riskAllowSell").checked,
    allow_market_orders: el("riskAllowMarketOrders").checked,
    max_order_value: {
      US: Number(el("riskMaxOrderUS").value || 0),
      HK: Number(el("riskMaxOrderHK").value || 0),
    },
    max_qty: {
      US: Number(el("riskMaxQtyUS").value || 0),
      HK: Number(el("riskMaxQtyHK").value || 0),
    },
  };
}

async function saveRiskConfig() {
  try {
    const payload = await api("/api/risk-config", {
      method: "POST",
      body: JSON.stringify({ risk: riskPayloadFromForm() }),
    });
    state.config = payload.config;
    renderRisk(payload.config);
    renderRiskEditor(payload.config);
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
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

function renderNewsSignals(payload) {
  const signals = payload.signals || [];
  state.newsPayload = payload;
  state.newsSignals = signals;
  state.newsPage = 1;
  if (!payload.enabled) {
    el("newsSignalList").innerHTML = `<div class="empty">AUTONEWS_DB_PATH is not set</div>`;
    renderNewsPager(0, 0);
    return;
  }
  if (!signals.length) {
    el("newsSignalList").innerHTML = `<div class="empty">${html(payload.message || "No recent high-impact signals")}</div>`;
    renderNewsPager(0, Number(payload.available_count || 0));
    return;
  }
  renderFilteredNewsSignals();
}

function newsSearchText(signal) {
  return [
    signal.title,
    signal.summary,
    signal.so_what,
    signal.topic_name,
    signal.direction,
    ...(signal.tickers || []),
    ...(signal.normalized_tickers || []),
    ...(signal.matched_codes || []),
    ...(signal.asset_classes || []),
    ...(signal.affected_markets || []),
  ]
    .join(" ")
    .toLowerCase();
}

function filteredNewsSignals() {
  const query = (el("newsSearch")?.value || "").trim().toLowerCase();
  const match = el("newsMatch")?.value || "ALL";
  const minImpact = Number(el("newsMinImpact")?.value || 0);
  return state.newsSignals.filter((signal) => {
    if (match !== "ALL" && signal.match_type !== match) return false;
    if (Number(signal.impact_score || 0) < minImpact) return false;
    if (query && !newsSearchText(signal).includes(query)) return false;
    return true;
  });
}

function renderNewsPager(filteredCount, availableCount) {
  const page = state.newsPage || 1;
  const totalPages = state.newsTotalPages || 1;
  el("newsSignalSummary").textContent = `${filteredCount} / ${availableCount || state.newsSignals.length}`;
  el("newsPageInfo").textContent = `${page} / ${totalPages}`;
  el("prevNewsPage").disabled = page <= 1;
  el("nextNewsPage").disabled = page >= totalPages;
}

function renderFilteredNewsSignals() {
  const list = el("newsSignalList");
  const payload = state.newsPayload || {};
  const signals = filteredNewsSignals();
  const pageSize = Number(el("newsPageSize")?.value || 20);
  state.newsTotalPages = Math.max(1, Math.ceil(signals.length / pageSize));
  state.newsPage = Math.min(Math.max(state.newsPage || 1, 1), state.newsTotalPages);
  const start = (state.newsPage - 1) * pageSize;
  const pageRows = signals.slice(start, start + pageSize);
  renderNewsPager(signals.length, Number(payload.available_count || state.newsSignals.length));
  if (!pageRows.length) {
    list.innerHTML = `<div class="empty">没有匹配的新闻信号</div>`;
    return;
  }
  list.innerHTML = signals
    .slice(start, start + pageSize)
    .map((signal) => {
      const tickers = (signal.tickers || []).slice(0, 5).join(", ") || "无明确标的";
      const assets = (signal.asset_classes || []).slice(0, 4).join(", ") || signal.topic_name;
      const matched = (signal.matched_codes || []).join(", ");
      const match = matched ? `${signal.match_type}: ${matched}` : signal.match_type;
      return `
        <article class="signal-item">
          <div class="signal-top">
            <div class="signal-title">${html(signal.title)}</div>
            <div class="signal-badges">
              <span class="signal-match">${html(match)}</span>
              <span class="signal-score">${html(signal.impact_score)}</span>
            </div>
          </div>
          <div class="signal-meta">
            <span>${html(fmtTime(signal.created_at))}</span>
            <span>${html(tickers)}</span>
            <span>${html(assets)}</span>
          </div>
          ${signal.direction ? `<div class="signal-direction">${html(signal.direction)}</div>` : ""}
          ${signal.so_what ? `<div class="learning-note">${html(signal.so_what)}</div>` : ""}
        </article>
      `;
    })
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

function detailText(value, fallback = "无") {
  const text = fmt(value);
  return `<p class="detail-text">${html(text === "-" ? fallback : text)}</p>`;
}

function detailList(items, fallback = "无") {
  const rows = Array.isArray(items) ? items.filter((item) => item !== null && item !== undefined && String(item).trim()) : [];
  if (!rows.length) return detailText(fallback);
  return `
    <ul class="detail-list">
      ${rows.map((item) => `<li>${html(item)}</li>`).join("")}
    </ul>
  `;
}

function detailKvGrid(items) {
  const rows = items.filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!rows.length) return detailText("无");
  return `
    <div class="detail-kv-grid">
      ${rows
        .map(
          ([label, value]) => `
            <div class="detail-kv-item">
              <div class="detail-kv-label">${html(label)}</div>
              <div class="detail-kv-value">${html(value)}</div>
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

function detailSection(title, body) {
  return `
    <section class="detail-section">
      <h3>${html(title)}</h3>
      ${body}
    </section>
  `;
}

function compactText(value, limit = 220) {
  const text = fmt(value);
  if (text.length <= limit) return text;
  return `${text.slice(0, limit).trim()}...`;
}

function safeUrl(value) {
  const text = String(value || "").trim();
  return /^https?:\/\//i.test(text) ? text : "";
}

function parseLegacyNewsNote(note, index) {
  const text = String(note || "");
  const readPart = (key) => {
    const match = text.match(new RegExp(`${key}=([^|]+)`));
    return match ? match[1].trim() : "";
  };
  const readToken = (key) => {
    const match = text.match(new RegExp(`${key}=([^|\\s]+)`));
    return match ? match[1].trim() : "";
  };
  return {
    id: `legacy-${index}`,
    title: readPart("title") || compactText(text, 90),
    summary: readPart("summary") || text,
    so_what: readPart("so_what"),
    impact_score: readToken("impact"),
    confidence: readToken("confidence"),
    tickers: readPart("tickers") ? readPart("tickers").split(",").map((item) => item.trim()).filter(Boolean) : [],
    matched_codes: [],
    match_type: readToken("match") || "note",
    affected_markets: readPart("markets") ? readPart("markets").split(",").map((item) => item.trim()).filter(Boolean) : [],
    asset_classes: readPart("assets") ? readPart("assets").split(",").map((item) => item.trim()).filter(Boolean) : [],
    direction: readPart("direction"),
    horizon: readPart("horizon"),
    created_at: "",
    url: "",
  };
}

function renderDetailNewsSignals(signals, notes) {
  const rows = Array.isArray(signals) && signals.length
    ? signals.slice(0, 8)
    : (Array.isArray(notes) ? notes.slice(0, 6).map(parseLegacyNewsNote) : []);
  if (!rows.length) return detailText("无");

  return `
    <div class="detail-news-grid">
      ${rows
        .map((signal, index) => {
          const tickers = (signal.matched_codes || signal.normalized_tickers || signal.tickers || []).slice(0, 4).join(", ") || "无明确标的";
          const assets = (signal.asset_classes || []).slice(0, 3).join(", ") || signal.topic_name || "未分类";
          const markets = (signal.affected_markets || []).slice(0, 3).join(", ");
          const url = safeUrl(signal.url);
          const match = signal.matched_codes?.length ? `${signal.match_type}: ${signal.matched_codes.join(", ")}` : signal.match_type;
          return `
            <details class="detail-news-card" ${index === 0 ? "open" : ""}>
              <summary>
                <span class="detail-news-main">
                  <span class="detail-news-title">${html(signal.title || "无标题")}</span>
                  <span class="detail-news-meta">${html(fmtTime(signal.created_at))} · ${html(tickers)} · ${html(assets)}</span>
                </span>
                <span class="detail-news-badges">
                  <span class="signal-match">${html(match || "note")}</span>
                  ${signal.impact_score !== "" && signal.impact_score !== undefined ? `<span class="signal-score">${html(signal.impact_score)}</span>` : ""}
                </span>
              </summary>
              <div class="detail-news-body">
                ${signal.summary ? `<p>${html(signal.summary)}</p>` : ""}
                ${signal.so_what ? `<p><strong>影响：</strong>${html(signal.so_what)}</p>` : ""}
                <div class="detail-news-facts">
                  ${signal.direction ? `<span>方向 ${html(signal.direction)}</span>` : ""}
                  ${signal.horizon ? `<span>周期 ${html(signal.horizon)}</span>` : ""}
                  ${markets ? `<span>市场 ${html(markets)}</span>` : ""}
                  ${signal.confidence !== "" && signal.confidence !== undefined ? `<span>置信 ${html(signal.confidence)}</span>` : ""}
                </div>
                ${url ? `<a class="detail-news-link" href="${html(url)}" target="_blank" rel="noopener noreferrer">打开来源</a>` : ""}
              </div>
            </details>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderDetailCandidates(candidates) {
  const rows = Array.isArray(candidates) ? candidates.slice(0, 8) : [];
  if (!rows.length) return detailText("无");
  return `
    <div class="detail-candidate-grid">
      ${rows
        .map((candidate) => {
          const change = Number(candidate.change_pct ?? candidate.change_rate);
          const cls = changeClass(change);
          const changeText = Number.isFinite(change) ? `${change.toFixed(2)}%` : "-";
          return `
            <article class="detail-candidate">
              <div class="detail-candidate-top">
                <span class="detail-candidate-code">${html(candidate.code)}</span>
                <span class="detail-candidate-change ${cls}">${html(changeText)}</span>
              </div>
              <div class="detail-candidate-meta">
                <span>${html(candidate.name || candidate.watch_name || candidate.market || "-")}</span>
                <span>${html(candidate.last_price)}</span>
                <span>Vol ${html(candidate.volume)}</span>
              </div>
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderDecisionDetail(row) {
  const card = el("decisionDetailCard");
  const status = el("decisionDetailStatus");
  if (!card || !status) return;

  if (!row) {
    status.className = "detail-status";
    status.textContent = "未选择";
    card.innerHTML = `<div class="empty">未选择决策</div>`;
    return;
  }

  const decision = row.decision || {};
  const action = String(decision.action || "UNKNOWN").toLowerCase();
  const blocked = Array.isArray(row.blocked_reasons) ? row.blocked_reasons : [];
  const order = row.order || null;
  const execution = row.execution || null;
  const usage = row.gemini_usage || {};
  const newsNotes = Array.isArray(row.news_notes) ? row.news_notes : [];
  const newsSignals = Array.isArray(row.news_signals) ? row.news_signals : [];
  const outputTokens = (Number(usage.candidates_token_count) || 0) + (Number(usage.thoughts_token_count) || 0);

  status.className = `detail-status ${html(action)}`;
  status.textContent = `${decision.action || "UNKNOWN"} · ${fmt(decision.confidence)}%`;

  card.innerHTML = `
    <article class="detail-hero ${html(action)}">
      <div class="detail-hero-top">
        <div>
          <div class="detail-title">${html(decision.code || "NONE")}</div>
          <div class="detail-subtitle">${html(fmtTime(row.timestamp || row.ts))} · ${html(row.mode)} · ${html(executionLabel(row))}</div>
        </div>
        <span class="decision-action ${html(action)}">${html(decision.action || "UNKNOWN")}</span>
      </div>
    </article>

    ${detailSection("决策理由", detailText(decision.reason))}
    ${detailSection("证据", detailList(decision.evidence))}
    ${detailSection(
      "风险与失效条件",
      detailKvGrid([
        ["风险", decision.risk],
        ["失效条件", decision.invalidation],
        ["时间周期", decision.time_horizon],
        ["最大模拟金额", decision.max_notional ? fmtUsd(Number(decision.max_notional)) : "0"],
      ])
    )}
    ${blocked.length ? detailSection("阻止原因", `<div class="blocked-row">${blocked.map((item) => `<span>${html(item)}</span>`).join("")}</div>`) : ""}
    ${detailSection(
      "订单",
      order
        ? detailKvGrid([
            ["方向", order.side],
            ["代码", order.code],
            ["数量", order.qty],
            ["价格", order.price],
            ["类型", order.order_type],
            ["理由", order.reason],
          ])
        : detailText("未生成订单")
    )}
    ${detailSection(
      "执行",
      execution
        ? detailKvGrid([
            ["状态", execution.ok ? "OK" : "失败"],
            ["模式", execution.mode],
            ["订单号", execution.order_id || execution.data?.order_id],
            ["消息", execution.message || execution.error],
          ])
        : detailText("未执行")
    )}
    ${detailSection("候选标的", renderDetailCandidates(row.candidates || []))}
    ${detailSection("新闻摘要", renderDetailNewsSignals(newsSignals, newsNotes))}
    ${detailSection(
      "Gemini 用量",
      detailKvGrid([
        ["Input Tokens", usage.prompt_token_count],
        ["Output Tokens", outputTokens || ""],
        ["Total Tokens", usage.total_token_count],
      ])
    )}
    ${decision.learning_note ? `<div class="detail-note">${html(decision.learning_note)}</div>` : ""}
  `;
}

function selectDecision(index, { scroll = false } = {}) {
  if (index < 0 || index >= state.decisionEntries.length) return;
  state.selectedDecisionIndex = index;
  document.querySelectorAll("[data-decision-index]").forEach((button) => {
    const selected = Number(button.dataset.decisionIndex) === index;
    button.closest(".decision-item")?.classList.toggle("selected", selected);
  });
  renderDecisionDetail(state.decisionEntries[index]);
  if (scroll && window.matchMedia("(max-width: 920px)").matches) {
    el("decisionDetailCard")?.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function renderDecisions(rows) {
  const list = el("decisionList");
  if (!rows.length) {
    list.innerHTML = `<div class="empty">No decisions yet</div>`;
    state.selectedDecisionIndex = null;
    renderDecisionDetail(null);
    return;
  }
  if (!Number.isInteger(state.selectedDecisionIndex) || state.selectedDecisionIndex >= rows.length) {
    state.selectedDecisionIndex = 0;
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
        <article class="decision-item ${index === state.selectedDecisionIndex ? "selected" : ""}">
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
          <button type="button" class="detail-button" data-decision-index="${index}">查看详情</button>
        </article>
      `;
    })
    .join("");

  list.querySelectorAll("[data-decision-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.decisionIndex);
      selectDecision(index, { scroll: true });
    });
  });
  renderDecisionDetail(rows[state.selectedDecisionIndex]);
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
    await refreshNewsSignals({ silent: true });
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
    state.selectedDecisionIndex = null;
    refreshDecisions();
  });
  el("decisionEnd").addEventListener("change", () => {
    state.decisionPage = 1;
    state.selectedDecisionIndex = null;
    refreshDecisions();
  });
  el("decisionAction").addEventListener("change", () => {
    state.decisionPage = 1;
    state.selectedDecisionIndex = null;
    refreshDecisions();
  });
  el("decisionPageSize").addEventListener("change", () => {
    state.decisionPage = 1;
    state.selectedDecisionIndex = null;
    refreshDecisions();
  });
  el("prevDecisionPage").addEventListener("click", () => {
    state.decisionPage = Math.max(1, state.decisionPage - 1);
    state.selectedDecisionIndex = null;
    refreshDecisions();
  });
  el("nextDecisionPage").addEventListener("click", () => {
    state.decisionPage = Math.min(state.decisionTotalPages, state.decisionPage + 1);
    state.selectedDecisionIndex = null;
    refreshDecisions();
  });
  el("refreshGeminiUsage").addEventListener("click", refreshGeminiUsage);
  el("refreshNewsSignals").addEventListener("click", () => refreshNewsSignals());
  el("newsSearch").addEventListener("input", () => {
    state.newsPage = 1;
    renderFilteredNewsSignals();
  });
  el("newsMatch").addEventListener("change", () => {
    state.newsPage = 1;
    renderFilteredNewsSignals();
  });
  el("newsMinImpact").addEventListener("change", () => {
    state.newsPage = 1;
    renderFilteredNewsSignals();
  });
  el("newsPageSize").addEventListener("change", () => {
    state.newsPage = 1;
    renderFilteredNewsSignals();
  });
  el("prevNewsPage").addEventListener("click", () => {
    state.newsPage = Math.max(1, state.newsPage - 1);
    renderFilteredNewsSignals();
  });
  el("nextNewsPage").addEventListener("click", () => {
    state.newsPage = Math.min(state.newsTotalPages, state.newsPage + 1);
    renderFilteredNewsSignals();
  });
  el("refreshConfig").addEventListener("click", refreshStatus);
  el("saveRiskConfig").addEventListener("click", saveRiskConfig);
  el("addRiskCode").addEventListener("click", addRiskCode);
  el("riskCodeInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addRiskCode();
    }
  });
  el("validateOrder").addEventListener("click", validateOrder);
  el("executeOrder").addEventListener("click", executeOrder);
  el("runGemini").addEventListener("click", runGemini);
  el("clearOutput").addEventListener("click", () => showOutput({}));
}

function bindTabs() {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => setActiveTab(button.dataset.tab));
  });

  try {
    setActiveTab(window.localStorage.getItem("futu-paper-ai-tab") || state.activeTab);
  } catch {
    setActiveTab(state.activeTab);
  }
}

async function init() {
  bindTabs();
  bindSegments();
  bindButtons();
  await refreshStatus();
  await refreshSnapshot();
  await refreshMyWatchlist({ silent: true });
  await refreshAccount();
  await refreshPositions();
  await refreshDecisions();
  await refreshGeminiUsage();
  await refreshNewsSignals({ silent: true });
  state.myWatchlistTimer = window.setInterval(() => refreshMyWatchlist({ silent: true }), 20000);
}

init();
