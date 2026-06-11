const state = {
  activeTab: "portfolio",
  accountMarket: "US",
  side: "BUY",
  config: null,
  decisionEntries: [],
  selectedDecisionIndex: null,
  decisionPage: 1,
  decisionTotalPages: 1,
  decisionPortfolioId: "ALL",
  evaluation: null,
  evaluationPortfolioId: "ALL",
  equityHiddenPortfolioIds: new Set(),
  newsSignals: [],
  newsPayload: null,
  newsPage: 1,
  newsTotalPages: 1,
  riskAllowedCodes: [],
  portfolios: [],
  activePortfolioId: "",
  chatMessages: [],
  chatBusy: false,
  myWatchlistTimer: null,
};

const el = (id) => document.getElementById(id);

function setActiveTab(tab) {
  if (tab === "overview" || tab === "debug") {
    tab = tab === "debug" ? "settings" : "portfolio";
  }
  let nextPanel = document.querySelector(`[data-tab-panel="${tab}"]`);
  if (!nextPanel) {
    tab = "portfolio";
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

function fmtHkd(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "HKD -";
  return `HKD ${number.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtPct(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const sign = number > 0 ? "+" : "";
  return `${sign}${number.toFixed(2)}%`;
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

const EXTENDED_SESSION_LABELS = {
  pre: "盘前",
  after: "盘后",
  overnight: "夜盘",
};

function renderExtendedSessionPill(extendedSession, { compact = false } = {}) {
  const session = extendedSession || {};
  const key = session.signal_session;
  const price = Number(session.price);
  if (!key || !Number.isFinite(price) || price <= 0) return "";
  const change = Number(session.change_rate);
  const cls = changeClass(change);
  const label = EXTENDED_SESSION_LABELS[key] || key;
  const changeText = Number.isFinite(change) ? `${change.toFixed(2)}%` : "-";
  const volume = Number(session.volume);
  const volumeText = Number.isFinite(volume) && volume > 0 ? `Vol ${fmt(volume)}` : "";
  return `
    <div class="extended-session-pill ${compact ? "compact" : ""}">
      <span>${html(label)}</span>
      <strong>${html(price)}</strong>
      <em class="${cls}">${html(changeText)}</em>
      ${compact || !volumeText ? "" : `<small>${html(volumeText)}</small>`}
    </div>
  `;
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

async function refreshPortfolios({ silent = false } = {}) {
  try {
    const payload = await api("/api/portfolios");
    renderPortfolios(payload);
    if (!silent) showOutput(payload);
  } catch (err) {
    el("portfolioList").innerHTML = `<div class="empty">Portfolios unavailable</div>`;
    el("portfolioPositions").innerHTML = `<div class="empty">Positions unavailable</div>`;
    renderChatPortfolioOptions();
    if (!silent) showOutput(err);
  }
}

async function refreshDecisions() {
  const params = new URLSearchParams({
    page: String(state.decisionPage),
    page_size: el("decisionPageSize").value,
    action: el("decisionAction").value,
  });
  const portfolioId = el("decisionPortfolio")?.value || state.decisionPortfolioId || "";
  const start = el("decisionStart").value;
  const end = el("decisionEnd").value;
  if (portfolioId && portfolioId !== "ALL") params.set("portfolio_id", portfolioId);
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

async function refreshEvaluation() {
  const params = new URLSearchParams({ limit: "500" });
  const portfolioId = el("evaluationPortfolio")?.value || state.evaluationPortfolioId || "";
  const start = el("evaluationStart")?.value || "";
  const end = el("evaluationEnd")?.value || "";
  if (portfolioId && portfolioId !== "ALL") params.set("portfolio_id", portfolioId);
  if (start) params.set("date_start", start);
  if (end) params.set("date_end", end);
  try {
    const payload = await api(`/api/evaluation?${params.toString()}`);
    state.evaluation = payload;
    renderEvaluation(payload);
    if (state.activeTab === "decisions" && Number.isInteger(state.selectedDecisionIndex)) {
      renderDecisionDetail(state.decisionEntries[state.selectedDecisionIndex]);
    }
  } catch (err) {
    state.evaluation = null;
    el("evaluationSummary").innerHTML = `<div class="empty">Evaluation unavailable</div>`;
    el("decisionTrackingList").innerHTML = `<div class="empty">Decision tracking unavailable</div>`;
    drawEquityChart([]);
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
          ${renderExtendedSessionPill(row.extended_session)}
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
          ${renderExtendedSessionPill(row.extended_session)}
        </article>
      `;
    })
    .join("");

  grid.querySelectorAll("[data-watch-remove]").forEach((button) => {
    button.addEventListener("click", () => removeMyWatch(button.dataset.watchRemove));
  });
}

function activePortfolio() {
  return state.portfolios.find((portfolio) => portfolio.id === state.activePortfolioId) || state.portfolios[0] || null;
}

function applyModeLabel(mode) {
  return {
    observe: "仅观察",
    manual: "手动应用",
    auto: "自动应用",
  }[String(mode || "manual").toLowerCase()] || "手动应用";
}

function portfolioKindLabel(kind) {
  return {
    actual: "实际仓位镜像",
    paper: "模拟实验盘",
  }[String(kind || "paper").toLowerCase()] || "模拟实验盘";
}

function futuSyncLabel(portfolio) {
  if (!portfolio?.futu_sync_enabled) return "";
  const pending = Number(portfolio.futu_sync_pending_count || 0);
  return pending > 0 ? `富途同步 · ${pending} 待回写` : "富途同步";
}

function operationSourceLabel(source) {
  return {
    auto: "AI 自动",
    manual: "AI 手动",
    futu_sync: "富途回写",
    user_trade: "本人交易",
    user: "本人操作",
  }[String(source || "").toLowerCase()] || "操作";
}

function applicationLabel(application) {
  const status = String(application?.status || "").toLowerCase();
  return {
    pending: "待手动应用",
    applied: "已应用",
    already_applied: "已应用",
    futu_submitted: "富途已提交",
    partially_applied: "部分成交",
    futu_submit_failed: "富途提交失败",
    local_apply_failed: "本地反写失败",
    skipped: "仅观察",
    blocked: "已阻止",
    failed: "应用失败",
    not_applicable: "无需应用",
  }[status] || "未生成应用状态";
}

function renderCashEffects(effects) {
  const rows = Array.isArray(effects) ? effects : [];
  if (!rows.length) return "";
  return rows
    .map((effect) => {
      const amount = Number(effect.amount);
      const sign = Number.isFinite(amount) && amount > 0 ? "+" : "";
      const target = effect.target_currency ? ` -> ${effect.target_amount} ${effect.target_currency}` : "";
      return `${sign}${effect.amount} ${effect.currency}${target}`;
    })
    .join("；");
}

function renderChatPortfolioOptions() {
  const select = el("chatPortfolio");
  if (!select) return;
  const current = select.value || state.activePortfolioId;
  select.innerHTML = state.portfolios
    .map((portfolio) => `<option value="${html(portfolio.id)}">${html(portfolio.name)}</option>`)
    .join("");
  select.value = state.portfolios.some((portfolio) => portfolio.id === current) ? current : state.activePortfolioId;
}

function renderDecisionPortfolioOptions({ followActive = false } = {}) {
  const select = el("decisionPortfolio");
  if (!select) return;
  const portfolioIds = new Set(state.portfolios.map((portfolio) => portfolio.id));
  const current = followActive ? state.activePortfolioId : state.decisionPortfolioId || select.value || "ALL";
  select.innerHTML = [
    `<option value="ALL">全部模拟盘</option>`,
    ...state.portfolios.map((portfolio) => `<option value="${html(portfolio.id)}">${html(portfolio.name)}</option>`),
  ].join("");
  select.value = portfolioIds.has(current) || current === "ALL" ? current : state.activePortfolioId || "ALL";
  state.decisionPortfolioId = select.value;
}

function renderEvaluationPortfolioOptions({ followActive = false } = {}) {
  const select = el("evaluationPortfolio");
  if (!select) return;
  const portfolioIds = new Set(state.portfolios.map((portfolio) => portfolio.id));
  const current = followActive ? state.activePortfolioId : state.evaluationPortfolioId || select.value || "ALL";
  select.innerHTML = [
    `<option value="ALL">全部模拟盘</option>`,
    ...state.portfolios.map((portfolio) => `<option value="${html(portfolio.id)}">${html(portfolio.name)}</option>`),
  ].join("");
  select.value = portfolioIds.has(current) || current === "ALL" ? current : state.activePortfolioId || "ALL";
  state.evaluationPortfolioId = select.value;
}

function renderPortfolios(payload) {
  state.portfolios = payload.portfolios || [];
  state.activePortfolioId = payload.active_id || state.portfolios[0]?.id || "";
  const list = el("portfolioList");
  if (!state.portfolios.length) {
    list.innerHTML = `<div class="empty">No portfolios</div>`;
    renderPortfolioDetails(null, payload.quote_error);
    renderChatPortfolioOptions();
    renderDecisionPortfolioOptions();
    renderEvaluationPortfolioOptions();
    return;
  }

  list.innerHTML = state.portfolios
    .map((portfolio) => {
      const active = portfolio.id === state.activePortfolioId;
      const totals = Object.entries(portfolio.totals_by_currency || {})
        .map(([currency, row]) => `${currency} ${fmt(row.market_value || row.cost_value || 0)}`)
        .join(" · ");
      const syncLabel = futuSyncLabel(portfolio);
      const kindLabel = portfolioKindLabel(portfolio.portfolio_kind);
      return `
        <article class="portfolio-item ${active ? "active" : ""}">
          <button type="button" class="portfolio-main" data-portfolio-active="${html(portfolio.id)}">
            <span class="portfolio-name">${html(portfolio.name)}</span>
            <span class="portfolio-meta">${html(kindLabel)} · ${html(applyModeLabel(portfolio.apply_mode))}${syncLabel ? ` · ${html(syncLabel)}` : ""} · ${html(portfolio.position_count || 0)} positions${totals ? ` · ${html(totals)}` : ""}</span>
          </button>
          <button type="button" class="portfolio-delete" data-portfolio-delete="${html(portfolio.id)}" title="删除模拟盘" aria-label="删除 ${html(portfolio.name)}">×</button>
        </article>
      `;
    })
    .join("");

  list.querySelectorAll("[data-portfolio-active]").forEach((button) => {
    button.addEventListener("click", () => setActivePortfolio(button.dataset.portfolioActive));
  });
  list.querySelectorAll("[data-portfolio-delete]").forEach((button) => {
    button.addEventListener("click", () => deletePortfolio(button.dataset.portfolioDelete));
  });

  renderPortfolioDetails(activePortfolio(), payload.quote_error);
  renderChatPortfolioOptions();
  renderDecisionPortfolioOptions();
  renderEvaluationPortfolioOptions();
}

function renderPortfolioDetails(portfolio, quoteError = "") {
  el("activePortfolioTitle").textContent = portfolio ? `${portfolio.name} · 持仓` : "持仓";
  el("portfolioQuoteStatus").textContent = quoteError ? "行情异常" : "OpenD 行情";
  el("portfolioQuoteStatus").className = `detail-status ${quoteError ? "sell" : "buy"}`;
  renderPortfolioSummary(portfolio, quoteError);
  renderPortfolioPositions(portfolio);
  renderPortfolioOperations(portfolio);
  renderPortfolioTradeAction(portfolio);
}

function renderPortfolioSummary(portfolio, quoteError = "") {
  const target = el("portfolioSummary");
  if (!portfolio) {
    target.innerHTML = `<div class="empty">未选择模拟盘</div>`;
    return;
  }
  const totals = Object.entries(portfolio.totals_by_currency || {});
  const cashByCurrency = portfolio.cash_by_currency || {};
  const cashRows = Object.entries(cashByCurrency);
  const fxRates = portfolio.fx_to_hkd || {};
  const fxRateText = Object.entries(fxRates)
    .filter(([currency]) => currency !== "HKD")
    .map(([currency, value]) => `${currency}=${fmt(value)}`)
    .join(" · ");
  const fxLabel = portfolio.fx_ok ? "Futu OpenD FX" : "默认FX";
  const cashCurrencies = Array.from(new Set([portfolio.base_currency, ...Object.keys(cashByCurrency), "HKD", "USD", "CNY", "CNH"].filter(Boolean)));
  const selectedCashCurrency = portfolio.base_currency || cashCurrencies[0] || "HKD";
  const cashCards = cashRows.length
    ? cashRows
        .map(
          ([currency, value]) => `
            <div class="metric">
              <div class="metric-label">Cash ${html(currency)}</div>
              <div class="metric-value">${html(fmt(value || 0))}</div>
            </div>
          `
        )
        .join("")
    : `
      <div class="metric">
        <div class="metric-label">Cash ${html(portfolio.base_currency)}</div>
        <div class="metric-value">${html(fmt(portfolio.cash || 0))}</div>
      </div>
    `;
  const totalCards = totals.length
    ? totals
        .map(([currency, row]) => {
          const pl = Number(row.pl_value || 0);
          const cls = changeClass(pl);
          return `
            <div class="metric">
              <div class="metric-label">${html(currency)} Market / P&L</div>
              <div class="metric-value">${html(fmt(row.market_value || 0))}</div>
              <div class="portfolio-pl ${cls}">${html(fmt(pl))}</div>
            </div>
          `;
        })
        .join("")
    : `<div class="metric"><div class="metric-label">Positions</div><div class="metric-value">0</div></div>`;
  target.innerHTML = `
    <div class="metric-grid">
      ${cashCards}
      ${totalCards}
    </div>
    <div class="portfolio-fx-strip ${portfolio.fx_ok ? "live" : "fallback"}">
      <span>${html(fxLabel)}</span>
      <strong>${html(fxRateText || "HKD=1")}</strong>
      ${portfolio.fx_error ? `<small>${html(portfolio.fx_error)}</small>` : ""}
    </div>
    <div class="portfolio-cash-editor">
      <label>
        模拟现金
        <div class="portfolio-cash-row">
          <select id="activePortfolioCashCurrency" aria-label="现金币种">
            ${cashCurrencies.map((currency) => `<option value="${html(currency)}" ${currency === selectedCashCurrency ? "selected" : ""}>${html(currency)}</option>`).join("")}
          </select>
          <input id="activePortfolioCash" type="number" min="0" step="1" value="${html(cashByCurrency[selectedCashCurrency] ?? 0)}">
        </div>
      </label>
      <button type="button" class="secondary compact" id="savePortfolioCash">保存现金</button>
    </div>
    <div class="portfolio-settings">
      <label>
        仓位口径
        <select id="activePortfolioKind">
          <option value="paper" ${!portfolio.portfolio_kind || portfolio.portfolio_kind === "paper" ? "selected" : ""}>模拟实验盘</option>
          <option value="actual" ${portfolio.portfolio_kind === "actual" ? "selected" : ""}>实际仓位镜像</option>
        </select>
      </label>
      <label>
        AI 应用模式
        <select id="activePortfolioMode">
          <option value="observe" ${portfolio.apply_mode === "observe" ? "selected" : ""}>仅观察</option>
          <option value="manual" ${!portfolio.apply_mode || portfolio.apply_mode === "manual" ? "selected" : ""}>手动应用</option>
          <option value="auto" ${portfolio.apply_mode === "auto" ? "selected" : ""}>自动应用</option>
        </select>
      </label>
      <label class="portfolio-sync-toggle" title="应用模拟盘订单时先提交富途模拟单，并用实际成交反写本地">
        <input id="activePortfolioFutuSync" type="checkbox" ${portfolio.futu_sync_enabled ? "checked" : ""}>
        <span>同步富途模拟盘</span>
        ${portfolio.futu_sync_pending_count ? `<em>${html(portfolio.futu_sync_pending_count)} 待回写</em>` : ""}
      </label>
      <div class="portfolio-clone-actions">
        <button type="button" class="secondary compact" data-clone-mode="manual">克隆手动盘</button>
        <button type="button" class="secondary compact" data-clone-mode="auto">克隆自动盘</button>
      </div>
    </div>
    ${quoteError ? `<div class="chat-warning">${html(quoteError)}</div>` : ""}
  `;
  el("savePortfolioCash")?.addEventListener("click", savePortfolioCash);
  el("activePortfolioCashCurrency")?.addEventListener("change", () => {
    const currency = el("activePortfolioCashCurrency").value;
    el("activePortfolioCash").value = cashByCurrency[currency] ?? 0;
  });
  el("activePortfolioKind")?.addEventListener("change", updateActivePortfolioMode);
  el("activePortfolioMode")?.addEventListener("change", updateActivePortfolioMode);
  el("activePortfolioFutuSync")?.addEventListener("change", updateActivePortfolioMode);
  target.querySelectorAll("[data-clone-mode]").forEach((button) => {
    button.addEventListener("click", () => cloneActivePortfolio(button.dataset.cloneMode));
  });
  el("activePortfolioCash")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      savePortfolioCash();
    }
  });
}

function renderPortfolioPositions(portfolio) {
  const list = el("portfolioPositions");
  const rows = portfolio?.positions || [];
  if (!rows.length) {
    list.innerHTML = `<div class="empty">还没有持仓。录入你的真实持仓后，AI 对话和自动周期会把它当作组合上下文。</div>`;
    return;
  }
  list.innerHTML = rows
    .map((row) => {
      const plRatio = Number(row.pl_ratio);
      const cls = changeClass(plRatio);
      return `
        <article class="portfolio-position">
          <div class="portfolio-position-main">
            <div>
              <div class="portfolio-position-code">${html(row.code)}</div>
              <div class="portfolio-position-name">${html(row.name || row.note || row.market)}</div>
            </div>
            <div class="portfolio-position-price">
              <span>${html(row.last_price ?? "-")}</span>
              <small>${html(row.price_source || "无行情")}</small>
            </div>
          </div>
          ${renderExtendedSessionPill(row.extended_session)}
          <div class="portfolio-position-grid">
            <span>数量 ${html(row.qty)}</span>
            <span>成本 ${html(row.cost_price)} ${html(row.currency)}</span>
            <span>市值 ${html(row.market_value ?? "-")}</span>
            <span class="${cls}">盈亏 ${html(row.pl_value ?? "-")} / ${Number.isFinite(plRatio) ? `${plRatio.toFixed(2)}%` : "-"}</span>
          </div>
          ${row.note ? `<div class="learning-note">${html(row.note)}</div>` : ""}
          <div class="portfolio-position-actions">
            <button type="button" class="secondary compact" data-position-edit="${html(row.code)}">编辑</button>
            <button type="button" class="danger compact" data-position-delete="${html(row.code)}">删除</button>
          </div>
        </article>
      `;
    })
    .join("");

  list.querySelectorAll("[data-position-edit]").forEach((button) => {
    button.addEventListener("click", () => editPortfolioPosition(button.dataset.positionEdit));
  });
  list.querySelectorAll("[data-position-delete]").forEach((button) => {
    button.addEventListener("click", () => deletePortfolioPosition(button.dataset.positionDelete));
  });
}

function tradeOperationFromTrade(trade) {
  const source = String(trade.source || "");
  const side = String(trade.side || "").toUpperCase();
  const code = String(trade.code || "").toUpperCase();
  return {
    id: `trade-${trade.id || trade.decision_id || trade.created_at}`,
    type: "trade",
    source,
    title: {
      auto: "AI 自动应用",
      manual: "AI 手动应用",
      futu_sync: "富途成交回写",
      user_trade: "本人交易记录",
    }[source] || `${side} 交易`,
    summary: `${side} ${fmt(trade.qty)} ${code} @ ${fmt(trade.price)} ${trade.currency || ""}`.trim(),
    code,
    side,
    qty: trade.qty,
    price: trade.price,
    currency: trade.currency,
    decision_id: trade.decision_id,
    trade_id: trade.id,
    payload: { reason: trade.reason, realized_pnl: trade.realized_pnl },
    created_at: trade.created_at,
  };
}

function operationFromSyncOrder(order) {
  const status = String(order.status || "").toLowerCase();
  return {
    id: `sync-${order.order_id || order.id || order.created_at}`,
    type: "futu_sync",
    source: "futu_sync",
    title: {
      futu_submitted: "富途订单已提交",
      partially_applied: "富途部分成交",
      local_apply_failed: "富途成交反写失败",
    }[status] || "富途同步",
    summary: `${order.side || ""} ${fmt(order.dealt_qty || order.qty)} ${order.code || ""}${order.dealt_avg_price ? ` @ ${fmt(order.dealt_avg_price)}` : ""}`.trim(),
    code: order.code,
    side: order.side,
    qty: order.dealt_qty || order.qty,
    price: order.dealt_avg_price || order.price,
    currency: "",
    decision_id: order.decision_id,
    trade_id: "",
    payload: { reason: order.message, order_id: order.order_id },
    created_at: order.updated_at || order.created_at,
  };
}

function renderPortfolioOperations(portfolio) {
  const target = el("portfolioOperations");
  if (!target) return;
  if (!portfolio) {
    target.innerHTML = `<div class="empty">未选择组合</div>`;
    return;
  }
  const operations = Array.isArray(portfolio.operations) ? portfolio.operations.map((item) => ({ ...item })) : [];
  const operationTradeIds = new Set(operations.map((item) => item.trade_id).filter(Boolean));
  const legacyTradeOps = (portfolio.trades || [])
    .filter((trade) => trade?.id && !operationTradeIds.has(trade.id))
    .map(tradeOperationFromTrade);
  const syncOps = (portfolio.futu_sync_orders || [])
    .filter((order) => !["applied"].includes(String(order.status || "").toLowerCase()))
    .map(operationFromSyncOrder);
  const rows = [...operations, ...legacyTradeOps, ...syncOps]
    .filter((item) => item && item.created_at)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
    .slice(0, 40);

  if (!rows.length) {
    target.innerHTML = `<div class="empty">还没有操作记录</div>`;
    return;
  }

  target.innerHTML = rows
    .map((row) => {
      const source = operationSourceLabel(row.source);
      const meta = [
        row.code,
        row.decision_id ? `决策 ${row.decision_id}` : "",
        row.currency,
      ].filter(Boolean).join(" · ");
      const reason = row.payload?.reason || row.summary || "";
      return `
        <article class="portfolio-operation">
          <div class="portfolio-operation-top">
            <span class="portfolio-operation-title">${html(row.title || source)}</span>
            <span class="portfolio-operation-time">${html(fmtTime(row.created_at))}</span>
          </div>
          <div class="portfolio-operation-summary">${html(row.summary || "-")}</div>
          <div class="portfolio-operation-meta">
            <span>${html(source)}${meta ? ` · ${html(meta)}` : ""}</span>
            ${reason && reason !== row.summary ? `<span>${html(reason)}</span>` : ""}
          </div>
        </article>
      `;
    })
    .join("");
}

function renderPortfolioTradeAction(portfolio) {
  const button = el("recordPortfolioTrade");
  if (!button) return;
  const syncEnabled = Boolean(portfolio?.futu_sync_enabled);
  button.textContent = syncEnabled ? "提交富途模拟单" : "记录本人交易";
  const reasonInput = el("portfolioTradeReason");
  if (reasonInput) {
    reasonInput.placeholder = syncEnabled ? "富途模拟单备注、交易理由" : "本人券商实际买入/卖出、调仓原因";
  }
}

function evaluationForDecision(row) {
  const rows = state.evaluation?.decision_tracking || [];
  const key = String(row?.evaluation_id || row?.decision_id || "");
  if (!key) return null;
  return rows.find((item) => String(item.evaluation_id || item.decision_id || "") === key) || null;
}

function horizonStatusLabel(status) {
  return {
    measured: "已记录",
    pending: "等待",
    missing_quote: "缺行情",
    missing_baseline: "缺基准",
    missing_timestamp: "缺时间",
  }[String(status || "")] || "未知";
}

function renderHorizonPills(horizons) {
  const rows = Array.isArray(horizons) ? horizons : [];
  if (!rows.length) return `<div class="empty compact-empty">暂无追踪</div>`;
  return `
    <div class="horizon-row">
      ${rows
        .map((horizon) => {
          const value = horizon.decision_return_pct ?? horizon.raw_return_pct;
          const cls = horizon.status === "measured" ? changeClass(Number(value)) : "flat";
          const text = horizon.status === "measured" ? fmtPct(value) : horizonStatusLabel(horizon.status);
          return `<span class="horizon-pill ${cls}" title="${html(horizon.due_at || "")}">${html(horizon.days)}D ${html(text)}</span>`;
        })
        .join("")}
    </div>
  `;
}

function renderDecisionFollowups(row) {
  if (!row) return detailText("暂无复盘追踪数据");
  const current = row.current_return_pct === null || row.current_return_pct === undefined
    ? "-"
    : `${fmtPct(row.current_return_pct)} 原始 / ${row.current_decision_return_pct === null || row.current_decision_return_pct === undefined ? "-" : fmtPct(row.current_decision_return_pct)} 方向调整`;
  return `
    ${detailKvGrid([
      ["追踪标的", row.code || "-"],
      ["基准价", row.baseline_price ? `${row.baseline_price} · ${row.baseline_price_source || ""}` : "-"],
      ["当前价", row.current_price || "-"],
      ["当前表现", current],
    ])}
    ${renderHorizonPills(row.horizons)}
  `;
}

function renderEvaluation(payload) {
  const status = el("evaluationStatus");
  if (status) {
    status.className = `detail-status ${payload.ok ? "buy" : "sell"}`;
    status.textContent = payload.ok ? "OpenD 估值" : "行情异常";
  }
  renderEvaluationSummary(payload);
  renderEquity(payload.equity_curves || []);
  renderDecisionTracking(payload.decision_tracking || []);
  showOutput(payload);
}

function renderEvaluationSummary(payload) {
  const target = el("evaluationSummary");
  const metrics = payload.metrics || {};
  const attribution = payload.attribution || {};
  const metricCards = [
    ["决策样本", metrics.decision_count],
    ["已追踪窗口", metrics.measured_horizons],
    ["方向胜率", metrics.win_rate === null || metrics.win_rate === undefined ? "-" : fmtPct(metrics.win_rate)],
    ["平均方向收益", metrics.avg_decision_return_pct === null || metrics.avg_decision_return_pct === undefined ? "-" : fmtPct(metrics.avg_decision_return_pct)],
  ];
  const portfolioRows = (payload.portfolio_summaries || [])
    .map((portfolio) => {
      const nav = portfolio.nav || {};
      const stats = portfolio.trade_stats || {};
      const missing = (nav.missing_quotes || []).join(", ");
      return `
        <article class="evaluation-portfolio">
          <div class="evaluation-portfolio-top">
            <strong>${html(portfolio.name)}</strong>
            <span>${html(applyModeLabel(portfolio.apply_mode))}</span>
          </div>
          <div class="evaluation-nav">${html(fmtHkd(nav.nav_hkd))}</div>
          <div class="evaluation-meta">
            <span>现金 ${html(fmtHkd(nav.cash_hkd))}</span>
            <span>持仓 ${html(fmtHkd(nav.market_value_hkd))}</span>
            <span>交易 ${html(stats.trade_count || 0)}</span>
            <span>换手 ${html(fmtHkd(stats.turnover_hkd || 0))}</span>
          </div>
          ${missing ? `<div class="learning-note">缺行情：${html(missing)}</div>` : ""}
        </article>
      `;
    })
    .join("");
  target.innerHTML = `
    <div class="metric-grid evaluation-metrics">
      ${metricCards
        .map(
          ([label, value]) => `
            <div class="metric">
              <div class="metric-label">${html(label)}</div>
              <div class="metric-value">${html(value)}</div>
            </div>
          `
        )
        .join("")}
    </div>
    <div class="evaluation-portfolios">
      ${portfolioRows || `<div class="empty">暂无组合估值</div>`}
    </div>
    ${renderAttributionSummary(attribution)}
    ${payload.quote_error ? `<div class="chat-warning">${html(payload.quote_error)}</div>` : ""}
  `;
}

function renderAttributionSummary(attribution) {
  const rows = Array.isArray(attribution?.sources) ? attribution.sources : [];
  const totalTrades = Number(attribution?.trade_count || 0);
  if (!rows.length) {
    return `
      <section class="attribution-panel">
        <div class="attribution-head">
          <strong>交易归因</strong>
          <span>暂无交易流水</span>
        </div>
      </section>
    `;
  }
  return `
    <section class="attribution-panel">
      <div class="attribution-head">
        <strong>交易归因</strong>
        <span>${html(totalTrades)} 笔 · ${html(fmtHkd(attribution.turnover_hkd || 0))} 成交额 · ${html(fmtHkd(attribution.realized_pnl_hkd || 0))} 已实现</span>
      </div>
      <div class="attribution-table">
        ${rows
          .map((row) => {
            const pnl = Number(row.realized_pnl_hkd || 0);
            return `
              <div class="attribution-row">
                <strong>${html(row.label || row.source || "其他来源")}</strong>
                <span>${html(row.trade_count || 0)} 笔</span>
                <span>买 ${html(row.buy_count || 0)} / 卖 ${html(row.sell_count || 0)}</span>
                <span>${html(fmtHkd(row.turnover_hkd || 0))}</span>
                <span class="${html(changeClass(pnl))}">${html(fmtHkd(pnl))}</span>
              </div>
            `;
          })
          .join("")}
      </div>
    </section>
  `;
}

function renderEquity(curves) {
  const rows = (curves || []).filter((curve) => (curve.points || []).length);
  const colors = chartColors();
  const colorByPortfolio = new Map(rows.map((curve, index) => [String(curve.portfolio_id || ""), colors[index % colors.length]]));
  const visibleRows = rows
    .filter((curve) => !state.equityHiddenPortfolioIds.has(String(curve.portfolio_id || "")))
    .map((curve) => ({ ...curve, chart_color: colorByPortfolio.get(String(curve.portfolio_id || "")) }));
  drawEquityChart(visibleRows);
  const legend = el("equityLegend");
  if (!rows.length) {
    legend.innerHTML = `<div class="empty compact-empty">暂无收益曲线点。新决策会自动写入组合净值基线。</div>`;
    return;
  }
  legend.innerHTML = rows
    .map((curve) => {
      const stats = curve.stats || {};
      const portfolioId = String(curve.portfolio_id || "");
      const hidden = state.equityHiddenPortfolioIds.has(portfolioId);
      const color = colorByPortfolio.get(portfolioId) || colors[0];
      return `
        <button type="button" class="equity-legend-item ${hidden ? "muted" : ""}" data-equity-toggle="${html(portfolioId)}" aria-pressed="${hidden ? "false" : "true"}">
          <span class="legend-swatch" style="background:${hidden ? "#aab4bb" : color}"></span>
          <span>${html(portfolioNameById(curve.portfolio_id))}</span>
          <strong>${html(fmtPct(stats.return_pct))}</strong>
          <em>回撤 ${html(fmtPct(stats.max_drawdown_pct))}</em>
        </button>
      `;
    })
    .join("");
  legend.querySelectorAll("[data-equity-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const portfolioId = String(button.dataset.equityToggle || "");
      if (!portfolioId) return;
      if (state.equityHiddenPortfolioIds.has(portfolioId)) {
        state.equityHiddenPortfolioIds.delete(portfolioId);
      } else {
        state.equityHiddenPortfolioIds.add(portfolioId);
      }
      renderEquity(state.evaluation?.equity_curves || []);
    });
  });
}

function chartColors() {
  return ["#1c6dd0", "#087f5b", "#b26b00", "#c92a2a", "#51606a", "#0b7285"];
}

function portfolioNameById(portfolioId) {
  return state.portfolios.find((portfolio) => portfolio.id === portfolioId)?.name || portfolioId || "组合";
}

function drawEquityChart(curves) {
  const canvas = el("equityChart");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.floor(rect.width || 900));
  const height = 260;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#fbfcfd";
  ctx.fillRect(0, 0, width, height);

  const series = (curves || [])
    .map((curve) => ({
      ...curve,
      points: (curve.points || [])
        .map((point) => ({ ...point, x: Date.parse(point.timestamp), y: Number(point.nav_hkd) }))
        .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y) && point.y > 0),
    }))
    .filter((curve) => curve.points.length);

  if (!series.length) {
    ctx.fillStyle = "#62707a";
    ctx.font = "13px system-ui";
    ctx.fillText("暂无收益曲线点", 20, 40);
    return;
  }

  const pad = { left: 54, right: 18, top: 18, bottom: 32 };
  const xs = series.flatMap((curve) => curve.points.map((point) => point.x));
  const ys = series.flatMap((curve) => curve.points.map((point) => point.y));
  let minX = Math.min(...xs);
  let maxX = Math.max(...xs);
  let minY = Math.min(...ys);
  let maxY = Math.max(...ys);
  if (minX === maxX) {
    minX -= 86400000;
    maxX += 86400000;
  }
  if (minY === maxY) {
    minY *= 0.98;
    maxY *= 1.02;
  }
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const xScale = (value) => pad.left + ((value - minX) / (maxX - minX)) * plotW;
  const yScale = (value) => pad.top + (1 - (value - minY) / (maxY - minY)) * plotH;

  ctx.strokeStyle = "#d9e0e4";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#62707a";
  ctx.font = "12px system-ui";
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + (plotH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    const value = maxY - ((maxY - minY) / 4) * i;
    ctx.fillText(Math.round(value).toLocaleString(), 8, y + 4);
  }

  const colors = chartColors();
  series.forEach((curve, index) => {
    const color = curve.chart_color || colors[index % colors.length];
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    curve.points.forEach((point, pointIndex) => {
      const x = xScale(point.x);
      const y = yScale(point.y);
      if (pointIndex === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    curve.points.forEach((point) => {
      const x = xScale(point.x);
      const y = yScale(point.y);
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    });
  });
}

function renderDecisionTracking(rows) {
  const target = el("decisionTrackingList");
  if (!target) return;
  const visibleRows = rows.slice(0, 120);
  if (!visibleRows.length) {
    target.innerHTML = `<div class="empty">还没有可评估的 AI 决策</div>`;
    return;
  }
  target.innerHTML = visibleRows
    .map((row) => {
      const directionValue = row.current_decision_return_pct ?? row.current_return_pct;
      const cls = changeClass(Number(directionValue));
      const targetLabel = row.target_source === "top_candidate_for_hold" ? "观察候选" : "决策标的";
      return `
        <article class="decision-tracking-item">
          <div class="decision-tracking-top">
            <div>
              <div class="decision-code">${html(row.code || "NONE")}</div>
              <div class="decision-time">${html(fmtTime(row.timestamp))} · ${html(row.portfolio_name || "-")} · ${html(targetLabel)}</div>
            </div>
            <div class="decision-badges">
              <span class="decision-action ${html(String(row.action || "").toLowerCase())}">${html(row.action)}</span>
              <span class="decision-confidence">${html(row.confidence)}%</span>
            </div>
          </div>
          <div class="decision-tracking-grid">
            <span>基准 ${html(row.baseline_price || "-")}</span>
            <span>当前 ${html(row.current_price || "-")}</span>
            <span class="${cls}">当前 ${html(fmtPct(directionValue))}</span>
            <span>${html(applicationLabel({ status: row.application_status }))}</span>
          </div>
          ${renderHorizonPills(row.horizons)}
        </article>
      `;
    })
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
  const appStatus = String(row.application?.status || "").toLowerCase();
  if (appStatus === "futu_submitted") return "富途待成交";
  if (appStatus === "partially_applied") return "部分成交";
  if (appStatus === "local_apply_failed") return "反写失败";
  if (row.execution?.ok && row.execution?.mode === "paper_execute") return "已执行";
  if (row.execution?.ok && row.execution?.mode === "paper_dry_run") return "Dry-run";
  if (row.execution?.ok && row.execution?.mode === "portfolio_suggestion") return "组合建议";
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

function renderFutuSyncDetail(application) {
  const sync = application?.futu_sync;
  if (!sync) return "";
  const dealtQty = Number(sync.dealt_qty || 0);
  const avgPrice = Number(sync.dealt_avg_price || 0);
  return detailKvGrid([
    ["富途订单号", sync.order_id],
    ["同步状态", applicationLabel({ status: sync.status || application?.status })],
    ["富途成交", dealtQty > 0 && avgPrice > 0 ? `${fmt(dealtQty)} @ ${fmt(avgPrice)}` : ""],
    ["已反写数量", sync.applied_qty ? fmt(sync.applied_qty) : ""],
    ["同步消息", sync.message],
  ]);
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

function renderMarkdown(text) {
  const lines = html(text || "").split(/\r?\n/);
  const parts = [];
  let inList = false;
  const closeList = () => {
    if (inList) {
      parts.push("</ul>");
      inList = false;
    }
  };
  const inline = (value) => value.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  lines.forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      return;
    }
    const heading = trimmed.match(/^#{2,3}\s+(.+)$/);
    if (heading) {
      closeList();
      parts.push(`<h3>${inline(heading[1])}</h3>`);
      return;
    }
    const bullet = trimmed.match(/^[-*]\s+(.+)$/) || trimmed.match(/^\d+\.\s+(.+)$/);
    if (bullet) {
      if (!inList) {
        parts.push("<ul>");
        inList = true;
      }
      parts.push(`<li>${inline(bullet[1])}</li>`);
      return;
    }
    closeList();
    parts.push(`<p>${inline(trimmed)}</p>`);
  });
  closeList();
  return parts.join("");
}

function renderChatMessages() {
  const thread = el("chatThread");
  if (!thread) return;
  if (!state.chatMessages.length) {
    thread.innerHTML = `<div class="empty">输入一支股票或行业，再问它适不适合模拟买入、卖出或继续观察。</div>`;
    return;
  }

  thread.innerHTML = state.chatMessages
    .map((message) => {
      const role = message.role === "user" ? "user" : "assistant";
      const news = role === "assistant" && (message.news_signals?.length || message.news_notes?.length)
        ? `<div class="chat-news">${renderDetailNewsSignals(message.news_signals || [], message.news_notes || [])}</div>`
        : "";
      const webError = message.web_error
        ? `<div class="chat-warning">联网检索失败，已退回只用本地新闻库：${html(message.web_error)}</div>`
        : "";
      return `
        <article class="chat-message ${role}">
          <div class="chat-role">${role === "user" ? "你" : "Gemini"}</div>
          <div class="chat-content">${role === "assistant" ? renderMarkdown(message.content) : `<p>${html(message.content)}</p>`}</div>
          ${webError}
          ${news}
        </article>
      `;
    })
    .join("");
  thread.scrollTop = thread.scrollHeight;
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
              ${renderExtendedSessionPill(candidate.extended_session, { compact: true })}
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderResearchBrief(research) {
  if (!research || typeof research !== "object") return detailText("无");
  const sections = [
    ["行情分析师", research.market_analyst],
    ["新闻分析师", research.news_analyst],
    ["持仓分析师", research.portfolio_analyst],
    ["看多观点", research.bull_case],
    ["看空观点", research.bear_case],
    ["风控复核", research.risk_review],
    ["组合经理", research.manager_summary],
  ].filter(([, value]) => value !== null && value !== undefined && String(value).trim());
  const missing = Array.isArray(research.missing_data)
    ? research.missing_data.filter((item) => item !== null && item !== undefined && String(item).trim())
    : [];
  if (!sections.length && !missing.length) return detailText("无");
  return `
    <div class="research-brief">
      ${sections
        .map(
          ([label, value]) => `
            <div class="research-row">
              <div class="research-label">${html(label)}</div>
              <p>${html(value)}</p>
            </div>
          `
        )
        .join("")}
      ${missing.length ? `
        <div class="research-row">
          <div class="research-label">缺失信息</div>
          ${detailList(missing)}
        </div>
      ` : ""}
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
  const application = row.application || null;
  const usage = row.gemini_usage || {};
  const portfolio = row.portfolio || null;
  const followup = evaluationForDecision(row);
  const newsNotes = Array.isArray(row.news_notes) ? row.news_notes : [];
  const newsSignals = Array.isArray(row.news_signals) ? row.news_signals : [];
  const outputTokens = (Number(usage.candidates_token_count) || 0) + (Number(usage.thoughts_token_count) || 0);
  const applicationStatus = String(application?.status || "").toLowerCase();
  const terminalApplicationStatuses = ["applied", "already_applied", "futu_submitted", "partially_applied", "local_apply_failed"];
  const canApplyDecision = Boolean(
    row.decision_id &&
      order &&
      portfolio?.id &&
      !blocked.length &&
      !terminalApplicationStatuses.includes(applicationStatus)
  );

  status.className = `detail-status ${html(action)}`;
  status.textContent = `${decision.action || "UNKNOWN"} · ${fmt(decision.confidence)}%`;

  card.innerHTML = `
    <article class="detail-hero ${html(action)}">
      <div class="detail-hero-top">
        <div>
          <div class="detail-title">${html(decision.code || "NONE")}</div>
          <div class="detail-subtitle">${html(fmtTime(row.timestamp || row.ts))} · ${html(portfolio?.name || row.source || row.mode)} · ${html(executionLabel(row))}</div>
        </div>
        <span class="decision-action ${html(action)}">${html(decision.action || "UNKNOWN")}</span>
      </div>
    </article>

    ${detailSection("决策理由", detailText(decision.reason))}
    ${detailSection("研究小组", renderResearchBrief(decision.research))}
    ${detailSection("证据", detailList(decision.evidence))}
    ${detailSection(
      "风险与失效条件",
      detailKvGrid([
        ["评级", decision.rating],
        ["组合动作", decision.position_action],
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
    ${detailSection(
      "应用状态",
      detailKvGrid([
        ["状态", applicationLabel(application)],
        ["模式", application?.mode],
        ["消息", application?.message],
        ["流水", application?.trade ? `${application.trade.side} ${application.trade.qty} ${application.trade.code} @ ${application.trade.price}` : ""],
        ["现金变动", renderCashEffects(application?.trade?.cash_effects)],
        ["换汇", application?.trade?.fx?.source_amount ? `${application.trade.fx.source_amount} ${application.trade.fx.source_currency} -> ${application.trade.fx.target_amount} ${application.trade.fx.target_currency} · ${application.trade.fx.source || ""}` : ""],
      ])
    )}
    ${detailSection("复盘追踪", renderDecisionFollowups(followup))}
    ${application?.futu_sync ? detailSection("富途同步", renderFutuSyncDetail(application)) : ""}
    ${canApplyDecision ? `<button type="button" class="primary-wide decision-apply-button" id="applyDecisionToPortfolio">应用到模拟盘</button>` : ""}
    ${portfolio ? detailSection(
      "模拟盘",
      detailKvGrid([
        ["名称", portfolio.name],
        ["口径", portfolioKindLabel(portfolio.portfolio_kind)],
        ["币种", portfolio.base_currency],
        ["现金", portfolio.cash],
        ["FX", portfolio.fx_source || ""],
        ["持仓数", portfolio.position_count],
      ])
    ) : ""}
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
  el("applyDecisionToPortfolio")?.addEventListener("click", applySelectedDecision);
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
      const portfolio = row.portfolio || null;
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
              <div class="decision-time">${html(fmtTime(row.timestamp || row.ts))} · ${html(portfolio?.name || row.source || row.mode)}</div>
            </div>
            <div class="decision-badges">
              <span class="decision-action ${html(action)}">${html(decision.action || "UNKNOWN")}</span>
              <span class="decision-confidence">${html(decision.confidence)}%</span>
            </div>
          </div>
          <p class="decision-reason">${html(decision.reason)}</p>
          <div class="decision-meta">
            <span>${html(executionLabel(row))}</span>
            ${row.application || row.order ? `<span>${html(applicationLabel(row.application))}</span>` : ""}
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

async function applySelectedDecision() {
  const row = state.decisionEntries[state.selectedDecisionIndex];
  if (!row?.decision_id || !row.order || !row.portfolio?.id) return;
  try {
    const payload = await api("/api/decisions/apply", {
      method: "POST",
      body: JSON.stringify({
        decision_id: row.decision_id,
        portfolio_id: row.portfolio.id,
        order: row.order,
      }),
    });
    showOutput(payload);
    await refreshPortfolios({ silent: true });
    await refreshDecisions();
    await refreshEvaluation();
  } catch (err) {
    showOutput(err);
    row.application = {
      ok: false,
      status: "failed",
      message: err?.error || err?.message || "应用失败",
    };
    renderDecisionDetail(row);
  }
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
    await refreshPortfolios({ silent: true });
    await refreshEvaluation();
    await refreshAccount();
    await refreshPositions();
  } catch (err) {
    showOutput(err);
  }
}

async function sendChat() {
  if (state.chatBusy) return;
  const topic = el("chatTopic").value.trim();
  const input = el("chatInput");
  const content = input.value.trim() || (topic ? `聊一下 ${topic}` : "");
  if (!topic && !content) {
    input.focus();
    return;
  }

  state.chatBusy = true;
  el("sendChat").disabled = true;
  el("chatStatus").textContent = "思考中";
  state.chatMessages.push({ role: "user", content });
  input.value = "";
  renderChatMessages();

  try {
    const payload = await api("/api/ai/chat", {
      method: "POST",
      body: JSON.stringify({
        topic,
        messages: state.chatMessages.map((message) => ({ role: message.role, content: message.content })),
        use_news: el("chatUseNews").checked,
        use_web: el("chatUseWeb").checked,
        portfolio_id: el("chatPortfolio").value || state.activePortfolioId,
      }),
    });
    state.chatMessages.push({
      role: "assistant",
      content: payload.reply || payload.error || "没有返回内容。",
      news_signals: payload.news_signals || [],
      news_notes: payload.news_notes || [],
      web_error: payload.web_error || "",
    });
    renderChatMessages();
    showOutput(payload);
    await refreshGeminiUsage();
    el("chatStatus").textContent = payload.ok ? "完成" : "失败";
  } catch (err) {
    state.chatMessages.push({
      role: "assistant",
      content: `这次对话失败了：${err.error || err.message || err}`,
      news_signals: [],
      news_notes: [],
    });
    renderChatMessages();
    showOutput(err);
    el("chatStatus").textContent = "失败";
  } finally {
    state.chatBusy = false;
    el("sendChat").disabled = false;
  }
}

function clearChat() {
  state.chatMessages = [];
  renderChatMessages();
  el("chatStatus").textContent = "就绪";
  el("chatInput").focus();
}

async function createPortfolio() {
  const name = el("portfolioName").value.trim();
  if (!name) {
    el("portfolioName").focus();
    return;
  }
  try {
    const payload = await api("/api/portfolios/create", {
      method: "POST",
      body: JSON.stringify({
        name,
        base_currency: el("portfolioCurrency").value,
        cash: Number(el("portfolioCash").value || 0),
      }),
    });
    el("portfolioName").value = "";
    renderPortfolios(payload);
    renderDecisionPortfolioOptions({ followActive: true });
    renderEvaluationPortfolioOptions({ followActive: true });
    if (state.activeTab === "decisions") {
      state.decisionPage = 1;
      state.selectedDecisionIndex = null;
      await refreshDecisions();
    }
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function setActivePortfolio(portfolioId) {
  if (!portfolioId) return;
  try {
    const payload = await api("/api/portfolios/active", {
      method: "POST",
      body: JSON.stringify({ portfolio_id: portfolioId }),
    });
    renderPortfolios(payload);
    renderDecisionPortfolioOptions({ followActive: true });
    renderEvaluationPortfolioOptions({ followActive: true });
    if (state.activeTab === "decisions") {
      state.decisionPage = 1;
      state.selectedDecisionIndex = null;
      await refreshDecisions();
    }
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function deletePortfolio(portfolioId) {
  if (!portfolioId) return;
  const portfolio = state.portfolios.find((item) => item.id === portfolioId);
  if (!window.confirm(`删除模拟盘：${portfolio?.name || portfolioId}？`)) return;
  try {
    const payload = await api("/api/portfolios/delete", {
      method: "POST",
      body: JSON.stringify({ portfolio_id: portfolioId }),
    });
    renderPortfolios(payload);
    renderEvaluationPortfolioOptions({ followActive: true });
    if (state.activeTab === "decisions") {
      state.decisionPage = 1;
      state.selectedDecisionIndex = null;
      await refreshDecisions();
    }
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function savePortfolioCash() {
  if (!state.activePortfolioId) return;
  const input = el("activePortfolioCash");
  const cash = Number(input?.value || 0);
  if (!Number.isFinite(cash) || cash < 0) {
    input?.focus();
    return;
  }
  try {
    const payload = await api("/api/portfolios/cash", {
      method: "POST",
      body: JSON.stringify({
        portfolio_id: state.activePortfolioId,
        currency: el("activePortfolioCashCurrency")?.value,
        cash,
      }),
    });
    renderPortfolios(payload);
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function updateActivePortfolioMode() {
  if (!state.activePortfolioId) return;
  try {
    const payload = await api("/api/portfolios/settings", {
      method: "POST",
      body: JSON.stringify({
        portfolio_id: state.activePortfolioId,
        portfolio_kind: el("activePortfolioKind")?.value || "paper",
        apply_mode: el("activePortfolioMode").value,
        futu_sync_enabled: Boolean(el("activePortfolioFutuSync")?.checked),
      }),
    });
    renderPortfolios(payload);
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

async function cloneActivePortfolio(mode) {
  const portfolio = activePortfolio();
  if (!portfolio) return;
  const suffix = mode === "auto" ? "Auto" : "Manual";
  try {
    const payload = await api("/api/portfolios/clone", {
      method: "POST",
      body: JSON.stringify({
        portfolio_id: portfolio.id,
        name: `${portfolio.name} - ${suffix}`,
        apply_mode: mode,
      }),
    });
    renderPortfolios(payload);
    renderDecisionPortfolioOptions({ followActive: true });
    renderEvaluationPortfolioOptions({ followActive: true });
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

function portfolioPositionPayload() {
  return {
    code: normalizeRiskCode(el("portfolioPositionCode").value),
    name: el("portfolioPositionName").value.trim(),
    qty: Number(el("portfolioPositionQty").value || 0),
    cost_price: Number(el("portfolioPositionCost").value || 0),
    currency: el("portfolioPositionCurrency").value,
    note: el("portfolioPositionNote").value.trim(),
  };
}

async function savePortfolioPosition() {
  const position = portfolioPositionPayload();
  if (!position.code || position.qty <= 0 || position.cost_price <= 0) {
    el("portfolioPositionCode").focus();
    return;
  }
  try {
    const payload = await api("/api/portfolios/position", {
      method: "POST",
      body: JSON.stringify({ portfolio_id: state.activePortfolioId, position }),
    });
    el("portfolioPositionCode").value = "";
    el("portfolioPositionName").value = "";
    el("portfolioPositionQty").value = "1";
    el("portfolioPositionCost").value = "1";
    el("portfolioPositionNote").value = "";
    renderPortfolios(payload);
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

function portfolioTradePayload() {
  return {
    code: normalizeRiskCode(el("portfolioTradeCode").value),
    side: el("portfolioTradeSide").value,
    qty: Number(el("portfolioTradeQty").value || 0),
    price: Number(el("portfolioTradePrice").value || 0),
    order_type: "LIMIT",
  };
}

async function recordPortfolioTrade() {
  if (!state.activePortfolioId) return;
  const portfolio = activePortfolio();
  const order = portfolioTradePayload();
  if (!order.code || order.qty <= 0 || order.price <= 0) {
    el("portfolioTradeCode").focus();
    return;
  }
  if (portfolio?.futu_sync_enabled) {
    const ok = window.confirm(`提交富途模拟单确认：${order.side} ${order.qty} ${order.code} @ ${order.price}`);
    if (!ok) return;
  }
  try {
    const payload = await api("/api/portfolios/trade", {
      method: "POST",
      body: JSON.stringify({
        portfolio_id: state.activePortfolioId,
        order,
        reason: el("portfolioTradeReason").value.trim(),
      }),
    });
    el("portfolioTradeCode").value = "";
    el("portfolioTradeQty").value = "1";
    el("portfolioTradePrice").value = "1";
    el("portfolioTradeReason").value = "";
    renderPortfolios(payload.portfolio_payload || payload);
    await refreshEvaluation();
    showOutput(payload);
  } catch (err) {
    showOutput(err);
  }
}

function editPortfolioPosition(code) {
  const row = activePortfolio()?.positions?.find((position) => position.code === code);
  if (!row) return;
  el("portfolioPositionCode").value = row.code || "";
  el("portfolioPositionName").value = row.name || "";
  el("portfolioPositionQty").value = row.qty || "";
  el("portfolioPositionCost").value = row.cost_price || "";
  el("portfolioPositionCurrency").value = row.currency || "HKD";
  el("portfolioPositionNote").value = row.note || "";
  el("portfolioTradeCode").value = row.code || "";
  el("portfolioTradePrice").value = row.last_price || row.cost_price || "";
  el("portfolioPositionCode").focus();
}

async function deletePortfolioPosition(code) {
  if (!code || !window.confirm(`删除持仓：${code}？`)) return;
  try {
    const payload = await api("/api/portfolios/position/delete", {
      method: "POST",
      body: JSON.stringify({ portfolio_id: state.activePortfolioId, code }),
    });
    renderPortfolios(payload);
    await refreshEvaluation();
    showOutput(payload);
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
  el("refreshPortfolios").addEventListener("click", () => refreshPortfolios());
  el("createPortfolio").addEventListener("click", createPortfolio);
  el("portfolioName").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      createPortfolio();
    }
  });
  el("savePortfolioPosition").addEventListener("click", savePortfolioPosition);
  el("recordPortfolioTrade").addEventListener("click", recordPortfolioTrade);
  el("portfolioPositionCode").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      savePortfolioPosition();
    }
  });
  el("portfolioTradeCode").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      recordPortfolioTrade();
    }
  });
  el("refreshPositions").addEventListener("click", refreshPositions);
  el("refreshDecisions").addEventListener("click", refreshDecisions);
  el("refreshEvaluation").addEventListener("click", refreshEvaluation);
  el("decisionPortfolio").addEventListener("change", () => {
    state.decisionPortfolioId = el("decisionPortfolio").value;
    state.decisionPage = 1;
    state.selectedDecisionIndex = null;
    refreshDecisions();
  });
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
  el("evaluationPortfolio").addEventListener("change", () => {
    state.evaluationPortfolioId = el("evaluationPortfolio").value;
    if (state.evaluationPortfolioId && state.evaluationPortfolioId !== "ALL") {
      state.equityHiddenPortfolioIds.delete(state.evaluationPortfolioId);
    }
    refreshEvaluation();
  });
  el("evaluationStart").addEventListener("change", refreshEvaluation);
  el("evaluationEnd").addEventListener("change", refreshEvaluation);
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
  el("sendChat").addEventListener("click", sendChat);
  el("clearChat").addEventListener("click", clearChat);
  el("chatInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      sendChat();
    }
  });
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
  renderChatMessages();
  await refreshPortfolios({ silent: true });
  await refreshStatus();
  await refreshSnapshot();
  await refreshMyWatchlist({ silent: true });
  await refreshAccount();
  await refreshPositions();
  await refreshDecisions();
  await refreshEvaluation();
  await refreshGeminiUsage();
  await refreshNewsSignals({ silent: true });
  state.myWatchlistTimer = window.setInterval(() => refreshMyWatchlist({ silent: true }), 20000);
}

init();
