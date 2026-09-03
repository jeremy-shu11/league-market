const state = {
  token: localStorage.getItem("leagueMarketToken") || "",
  session: null,
  markets: [],
  selectedMarketId: null,
  order: null,
  orderQuote: null,
  marketDetails: {},
  portfolio: null,
  fund: null,
  leaderboard: [],
  leaderboardSummary: null,
  ticker: [],
  managers: [],
  identity: null,
  participants: [],
  invites: [],
  resolutionMarkets: [],
  adminOverview: null,
  adminSession: null,
  adminDashboard: null,
  adminView: "inbox",
  adminSettingsTab: "league",
  adminAction: null,
  allocationChart: null,
  fundChart: null,
  marketChart: null,
  notices: [],
  tourStep: 0,
  activeTab: "home",
  homeMarketFilter: "movers",
  homeWidgets: {
    watch: true,
    positions: true,
    tape: true
  },
  mobileNavOpen: false,
  isRefreshing: false,
  detailLoadingId: null,
  detailErrors: {},
  lastFocusedElement: null,
  navCollapsed: localStorage.getItem("leagueMarketNavCollapsed") === "true",
  theme: localStorage.getItem("leagueMarketTheme") || "dark"
};

const tourSteps = [
  {
    title: "Welcome to the league exchange",
    body: "Everyone gets 10,000 play credits. You are trading probabilities on your fantasy league, not placing real-money bets."
  },
  {
    title: "Markets are contracts",
    body: "A market has clear outcomes, a close rule, and a resolution source. Champion and top-scorer markets can have many outcomes; playoff markets are YES or NO."
  },
  {
    title: "Prices are probabilities",
    body: "A price of 23 means the market implies roughly a 23% chance. If that outcome wins, each share pays 100 credits."
  },
  {
    title: "The market maker fills trades",
    body: "V1 uses LMSR, so trades execute instantly. Larger orders move the price more because they push against market liquidity."
  },
  {
    title: "Your edge becomes receipts",
    body: "The portfolio and leaderboard track cash, open value, and profit as market prices move."
  }
];

const $ = (selector) => document.querySelector(selector);
function hydrateIcons() {
  if (!window.lucide?.createIcons) return;
  window.lucide.createIcons({
    attrs: {
      "aria-hidden": "true",
      "stroke-width": 2
    }
  });
}
const fmt = (value) => `${Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
const money = (value) => {
  const number = Number(value || 0);
  return `${number < 0 ? "-" : ""}$${fmt(Math.abs(number))}`;
};
const pct = (value) => `${Math.round(Number(value || 0) * 100)}%`;
const probabilityWidth = (value) => `${Math.max(1, Math.min(100, Math.round(Number(value || 0) * 100)))}%`;
const emptyState = (title, body, action = "") => `
  <div class="empty-state" role="status">
    <strong>${escapeHtml(title)}</strong>
    <span>${escapeHtml(body)}</span>
    ${action}
  </div>
`;
const loadingState = (title = "Loading market data") => `
  <div class="loading-state" role="status" aria-live="polite">
    <span class="loading-indicator" aria-hidden="true"></span>
    <div>
      <strong>${escapeHtml(title)}</strong>
      <span>Updating prices, balances, and league activity.</span>
    </div>
  </div>
`;
const errorState = (title, body, action = "") => `
  <div class="error-state" role="alert">
    <strong>${escapeHtml(title)}</strong>
    <span>${escapeHtml(body)}</span>
    ${action}
  </div>
`;
const allocationPct = (value, total) => {
  const percent = (Number(value || 0) / Number(total || 1)) * 100;
  if (percent > 0 && percent < 1) return "<1%";
  return `${Math.round(percent)}%`;
};
const initials = (value) => String(value || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
const ledgerLabels = {
  buy: "Bought shares",
  sell: "Sold shares",
  settlement: "Market settlement",
  void_refund: "Voided market refund",
  seed_refresh_refund: "Market refresh refund"
};

const fundEntryLabels = {
  add_fee: "Add fee",
  adjustment: "Adjustment",
  expense: "Expense",
  income: "Income",
  trade_fee: "Trade fee"
};

const fundSourceLabels = {
  manual: "Commissioner entry",
  market_payout: "Prize payout",
  sleeper: "Sleeper fees"
};

function titleize(value) {
  return String(value || "Activity")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function ledgerTitle(entry) {
  return ledgerLabels[entry.entry_type] || titleize(entry.entry_type);
}

function ledgerDetail(entry) {
  const note = String(entry.note || "");
  const match = note.match(/^(buy|sell)\s+([\d.]+)\s+shares/i);
  if (match) {
    const verb = match[1].toLowerCase() === "buy" ? "Bought" : "Sold";
    return `${verb} ${fmt(match[2])} shares`;
  }
  if (entry.entry_type === "seed_refresh_refund") return "Returned credits from refreshed seeded markets";
  if (entry.entry_type === "void_refund") return "Returned credits from a voided market";
  if (entry.entry_type === "settlement") return "Winning shares paid in market credits";
  return note || "Account activity";
}

function fundEntryLabel(value) {
  return fundEntryLabels[value] || titleize(value || "fund entry");
}

function fundSourceLabel(value) {
  return fundSourceLabels[value] || titleize(value || "fund ledger");
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  const toggle = $("#theme-toggle .nav-label");
  if (toggle) toggle.textContent = state.theme === "dark" ? "Light Mode" : "Dark Mode";
}

function isMobileNavigation() {
  return window.matchMedia("(max-width: 980px)").matches;
}

function setButtonBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.idleHtml = button.innerHTML;
    button.textContent = label || "Working...";
  } else if (button.dataset.idleHtml) {
    button.innerHTML = button.dataset.idleHtml;
    delete button.dataset.idleHtml;
    hydrateIcons();
  }
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
}

function setAppStatus(kind = "", message = "") {
  const root = $("#app-status");
  if (!root) return;
  root.className = `app-status${kind ? ` ${kind}` : " hidden"}`;
  root.innerHTML = kind ? `
    <span class="app-status-icon" aria-hidden="true"></span>
    <strong>${escapeHtml(message)}</strong>
    ${kind === "error" ? '<button type="button" data-retry-refresh>Retry</button>' : ""}
  ` : "";
  root.querySelector("[data-retry-refresh]")?.addEventListener("click", () => refreshAll({ announce: true }));
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers["X-Participant-Token"] = state.token;
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_) {
    payload = {};
  }
  if (!response.ok) {
    const error = new Error(payload.detail || `Request failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function join(event) {
  event.preventDefault();
  $("#join-error").textContent = "";
  const submit = $("#join-submit");
  setButtonBusy(submit, true, "Opening Market...");
  try {
    const payload = await api("/api/auth/join", {
      method: "POST",
      body: JSON.stringify({
        display_name: $("#display-name").value,
        invite_code: $("#invite-code").value,
        league_id: $("#join-league-id").value,
        sleeper_username: $("#sleeper-username").value
      })
    });
    state.token = payload.token;
    localStorage.setItem("leagueMarketToken", state.token);
    notify(`Welcome, ${payload.participant.display_name}. Your 10,000 credit bankroll is live.`, "success");
    localStorage.removeItem("leagueMarketTourSeen");
    await boot();
  } catch (error) {
    $("#join-error").textContent = error.message;
  } finally {
    setButtonBusy(submit, false);
  }
}

async function boot() {
  if (!state.token) {
    $("#join-view").classList.remove("hidden");
    $("#app-view").classList.add("hidden");
    return;
  }
  $("#app-view").setAttribute("aria-busy", "true");
  try {
    await refreshAll({ announce: true });
    $("#join-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    if (!localStorage.getItem("leagueMarketTourSeen")) showTour(0);
  } catch (error) {
    if (error.status === 401) {
      localStorage.removeItem("leagueMarketToken");
      state.token = "";
      $("#join-view").classList.remove("hidden");
      $("#app-view").classList.add("hidden");
      $("#join-error").textContent = "Your session expired. Enter the league again.";
    } else {
      $("#join-view").classList.add("hidden");
      $("#app-view").classList.remove("hidden");
      setAppStatus("error", `Market data could not load: ${error.message}`);
    }
  } finally {
    $("#app-view").setAttribute("aria-busy", "false");
  }
}

async function refreshAll({ announce = false } = {}) {
  if (state.isRefreshing) return;
  state.isRefreshing = true;
  const refreshButton = $("#refresh-button");
  setButtonBusy(refreshButton, true, "Refreshing...");
  $("#app-view")?.setAttribute("aria-busy", "true");
  if (announce) setAppStatus("loading", "Refreshing the market desk...");
  try {
    const [session, markets, portfolio, leaderboard, ticker] = await Promise.all([
      api("/api/session"),
      api("/api/markets"),
      api("/api/portfolio"),
      api("/api/leaderboard"),
      api("/api/ticker")
    ]);
    state.session = session;
    state.markets = markets.markets;
    state.portfolio = portfolio;
    state.leaderboard = leaderboard.leaderboard;
    state.leaderboardSummary = leaderboard.summary || null;
    state.ticker = ticker.items || [];
    if (!state.selectedMarketId && state.markets.length) state.selectedMarketId = state.markets[0].id;
    render();
    setAppStatus();
  } catch (error) {
    setAppStatus("error", `Refresh failed: ${error.message}`);
    throw error;
  } finally {
    state.isRefreshing = false;
    setButtonBusy(refreshButton, false);
    $("#app-view")?.setAttribute("aria-busy", "false");
  }
}

function notify(message, tone = "info") {
  const notice = { id: Date.now() + Math.random(), message, tone };
  state.notices.unshift(notice);
  state.notices = state.notices.slice(0, 4);
  renderNotifications();
  window.setTimeout(() => {
    state.notices = state.notices.filter((item) => item.id !== notice.id);
    renderNotifications();
  }, 5200);
}

function renderNotifications() {
  const center = $("#notification-center");
  if (!center) return;
  center.innerHTML = state.notices.map((notice) => `
    <article class="site-notice ${notice.tone}">
      <span>${notice.tone === "success" ? "FILLED" : notice.tone === "warn" ? "WATCH" : "TAPE"}</span>
      <strong>${escapeHtml(notice.message)}</strong>
    </article>
  `).join("");
}

function exposureTone(value, total) {
  const ratio = Number(value || 0) / Math.max(1, Number(total || 0));
  if (ratio >= 0.25) return "high";
  if (ratio >= 0.1) return "medium";
  return "low";
}

function openDialog(dialog, focusSelector) {
  if (!dialog) return;
  state.lastFocusedElement = document.activeElement;
  document.querySelector(".app-shell").inert = true;
  dialog.classList.remove("hidden");
  window.requestAnimationFrame(() => {
    (dialog.querySelector(focusSelector) || dialog.querySelector("[tabindex='-1']") || dialog).focus();
  });
}

function closeDialog(dialog) {
  if (!dialog) return;
  dialog.classList.add("hidden");
  document.querySelector(".app-shell").inert = false;
  if (state.lastFocusedElement?.isConnected) state.lastFocusedElement.focus();
  state.lastFocusedElement = null;
}

function trapDialogFocus(event, dialog) {
  if (event.key !== "Tab" || dialog.classList.contains("hidden")) return;
  const focusable = [...dialog.querySelectorAll(
    "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex='-1'])"
  )].filter((element) => element.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function showTour(step = 0) {
  state.tourStep = Math.max(0, Math.min(step, tourSteps.length - 1));
  renderTour();
  openDialog($("#onboarding-overlay"), "#tour-close");
}

function closeTour() {
  closeDialog($("#onboarding-overlay"));
  localStorage.setItem("leagueMarketTourSeen", "true");
}

function renderTour() {
  const step = tourSteps[state.tourStep];
  $("#tour-step-count").textContent = `${state.tourStep + 1} / ${tourSteps.length}`;
  $("#tour-content").innerHTML = `
    <p class="eyebrow">Gameplay walkthrough</p>
    <h2 id="tour-title">${escapeHtml(step.title)}</h2>
    <p>${escapeHtml(step.body)}</p>
    <div class="tour-demo">
      <span class="demo-price">${state.tourStep === 2 ? "23" : state.tourStep === 3 ? "+7 pts" : "100"}</span>
      <span>${state.tourStep === 2 ? "implied probability" : state.tourStep === 3 ? "price movement" : "credit payout"}</span>
    </div>
  `;
  $("#tour-prev").disabled = state.tourStep === 0;
  $("#tour-next").textContent = state.tourStep === tourSteps.length - 1 ? "Start Trading" : "Next";
}

function render() {
  applyTheme();
  const activeMarkets = state.markets.filter((market) => market.status === "open").length;
  $("#app-view").classList.toggle("nav-collapsed", state.navCollapsed);
  $("#app-view").classList.toggle("mobile-nav-open", state.mobileNavOpen);
  $("#app-view").classList.toggle("dashboard-active", state.activeTab === "home");
  document.querySelectorAll(".tabs button").forEach((item) => {
    const active = item.dataset.tab === state.activeTab;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  const mobileNavigation = isMobileNavigation();
  $("#nav-toggle").setAttribute("aria-expanded", String(mobileNavigation ? state.mobileNavOpen : !state.navCollapsed));
  $("#nav-toggle").setAttribute("aria-label", mobileNavigation
    ? `${state.mobileNavOpen ? "Close" : "Open"} navigation menu`
    : `${state.navCollapsed ? "Expand" : "Collapse"} navigation`);
  $("#account-name").textContent = state.session?.participant?.display_name || "-";
  const league = state.session?.league || {};
  $("#tape-league").textContent = `${league.name || "THE LEAGUE"} ${league.season || "2026"}`.toUpperCase();
  if ($("#league-id-input") && !$("#league-id-input").dataset.touched) {
    $("#league-id-input").value = league.league_id || "1326428061876371456";
  }
  if ($("#join-league-id") && !$("#join-league-id").dataset.touched) {
    $("#join-league-id").value = league.league_id || $("#join-league-id").value || "1326428061876371456";
  }
  $("#summary-cash").textContent = money(state.session?.cash);
  $("#summary-open").textContent = money(state.session?.open_value);
  $("#summary-net").textContent = money(state.session?.net_worth);
  $("#summary-markets").textContent = activeMarkets;
  renderMarkets();
  renderHome();
  renderPortfolio();
  renderLeaderboard();
  renderManagers();
  renderIdentity();
  renderCommissionerManagement();
  renderLaunchChecklist();
  renderAdminShell();
  renderTicker();
  hydrateIcons();
}

function renderLaunchChecklist() {
  const root = $("#launch-checklist");
  if (!root) return;
  if (state.adminOverview) {
    const overview = state.adminOverview;
    const projectionFresh = Number(overview.data?.projection_age_hours ?? Infinity) <= 24;
    const modelReady = Number(overview.model?.coverage || 0) >= 0.95;
    const openMarkets = Number(overview.markets?.counts?.open || 0);
    const linked = Number(overview.people?.linked || 0);
    const items = [
      { label: "Projection feed", detail: projectionFresh ? "Fresh" : "Refresh required", done: projectionFresh },
      { label: "Model coverage", detail: overview.model?.coverage != null ? pct(overview.model.coverage) : "Unavailable", done: modelReady },
      { label: "Open contracts", detail: `${openMarkets} markets`, done: openMarkets > 0 },
      { label: "Claimed identities", detail: `${linked} of ${overview.people?.participants || 0}`, done: linked > 0 && Number(overview.people?.unlinked || 0) === 0 }
    ];
    const complete = items.filter((item) => item.done).length;
    root.innerHTML = `
      <div class="launch-progress">
        <div><span>Launch gates</span><strong>${complete} / ${items.length}</strong></div>
        <div class="fund-meter"><i style="width: ${probabilityWidth(complete / items.length)}"></i></div>
      </div>
      <div class="launch-steps">
        ${items.map((item, index) => `
          <article class="${item.done ? "done" : ""}">
            <b>${index + 1}</b>
            <div><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.detail)}</span></div>
          </article>
        `).join("")}
      </div>
    `;
    return;
  }
  const leagueId = $("#league-id-input")?.value?.trim() || state.session?.league?.league_id || "";
  const activeMarkets = state.markets.filter((market) => market.status === "open").length;
  const items = [
    {
      label: "League ID",
      detail: leagueId || "Enter a Sleeper league ID",
      done: Boolean(leagueId)
    },
    {
      label: "Sleeper Managers",
      detail: `${state.managers.length} loaded`,
      done: state.managers.length > 0
    },
    {
      label: "Markets",
      detail: `${activeMarkets} active markets`,
      done: activeMarkets > 0
    },
    {
      label: "Participants",
      detail: `${state.participants.length} joined · ${state.invites.length} invite codes`,
      done: state.participants.length > 0 || state.invites.length > 0
    }
  ];
  const complete = items.filter((item) => item.done).length;
  root.innerHTML = `
    <div class="launch-progress">
      <div>
        <span>Launch Readiness</span>
        <strong>${complete} / ${items.length}</strong>
      </div>
      <div class="fund-meter"><i style="width: ${probabilityWidth(complete / items.length)}"></i></div>
    </div>
    <div class="launch-steps">
      ${items.map((item, index) => `
        <article class="${item.done ? "done" : ""}">
          <b>${index + 1}</b>
          <div>
            <strong>${escapeHtml(item.label)}</strong>
            <span>${escapeHtml(item.detail)}</span>
          </div>
        </article>
      `).join("")}
    </div>
  `;
}

function adminHeaders() {
  const code = $("#admin-code")?.value || "";
  return code ? { "X-Admin-Code": code } : {};
}

function formatAdminAge(hours) {
  if (hours == null || !Number.isFinite(Number(hours))) return "Unavailable";
  const value = Number(hours);
  if (value < 1) return `${Math.max(1, Math.round(value * 60))}m ago`;
  if (value < 48) return `${Math.round(value)}h ago`;
  return `${Math.round(value / 24)}d ago`;
}

function currentAdminLeagueId() {
  return $("#league-id-input")?.value?.trim() || state.session?.league?.league_id || "1326428061876371456";
}

function adminTime(value) {
  if (!value) return "Not recorded";
  const elapsed = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(elapsed)) return "Not recorded";
  return formatAdminAge(Math.max(0, elapsed / 3600000));
}

function renderAdminShell() {
  const locked = !state.adminSession?.authenticated;
  $("#admin-lock-view")?.classList.toggle("hidden", !locked);
  $("#admin-inbox-view")?.classList.toggle("hidden", locked || state.adminView === "settings");
  $("#admin-settings-view")?.classList.toggle("hidden", locked || state.adminView !== "settings");
  if (!locked) {
    renderAdminDashboard();
    renderAdminSettings();
  }
  hydrateIcons();
}

function renderAdminDashboard() {
  const dashboard = state.adminDashboard;
  const root = $("#admin-inbox-root");
  const automationRoot = $("#admin-automation-root");
  if (!root || !automationRoot) return;
  $("#admin-league-name").textContent = dashboard?.league?.name || "League commissioner";
  $("#admin-summary").textContent = dashboard?.summary || "Checking for action...";
  if (!dashboard) {
    root.innerHTML = loadingState("Checking commissioner actions");
    automationRoot.innerHTML = "";
    return;
  }
  root.innerHTML = dashboard.all_clear ? `
    <div class="admin-all-clear">
      <i data-lucide="circle-check-big" aria-hidden="true"></i>
      <div><strong>Nothing needs your attention</strong><span>Markets and league automation are operating normally.</span></div>
    </div>
  ` : `
    <div class="admin-inbox-list">
      ${dashboard.actions.map((action) => `
        <article class="admin-inbox-item ${escapeHtml(action.severity)}">
          <span class="admin-action-mark" aria-hidden="true"></span>
          <div><strong>${escapeHtml(action.title)}</strong><span>${escapeHtml(action.detail)}</span></div>
          <button type="button" data-admin-action-id="${escapeHtml(action.id)}">${escapeHtml(action.action_label)}</button>
        </article>
      `).join("")}
    </div>
  `;
  const automation = ["pipeline", "lifecycle", "backup"].map((key) => dashboard.automation?.[key]).filter(Boolean);
  automationRoot.innerHTML = `
    <span>Automation</span>
    ${automation.map((job) => `<span class="automation-state ${escapeHtml(job.state)}"><i aria-hidden="true"></i>${escapeHtml(job.label)} · ${escapeHtml(adminTime(job.last_success_at))}</span>`).join("")}
  `;
  root.querySelectorAll("[data-admin-action-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = dashboard.actions.find((item) => item.id === button.dataset.adminActionId);
      if (action) handleAdminInboxAction(action, button);
    });
  });
  hydrateIcons();
}

function renderAdminSettings() {
  document.querySelectorAll("[data-admin-settings-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.adminSettingsTab === state.adminSettingsTab);
  });
  document.querySelectorAll("[data-admin-settings-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.adminSettingsPanel !== state.adminSettingsTab);
  });
  $("#admin-demo-controls")?.classList.toggle("hidden", state.adminDashboard?.environment === "production");
  renderAdminHistory();
}

function renderAdminHistory() {
  const dashboard = state.adminDashboard;
  const jobsRoot = $("#admin-job-history");
  const activityRoot = $("#admin-activity-root");
  if (jobsRoot) jobsRoot.innerHTML = `
    <div class="settings-section-head"><div><h3>Recent Jobs</h3></div></div>
    <div class="admin-history-list">${(dashboard?.job_history || []).slice(0, 8).map((job) => `
      <article><span class="status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span><strong>${escapeHtml(titleize(job.job_type))}</strong><small>${escapeHtml(adminTime(job.completed_at || job.started_at))} · ${escapeHtml(job.triggered_by)}</small></article>
    `).join("") || '<p class="empty">No recorded job runs yet.</p>'}</div>`;
  if (activityRoot) activityRoot.innerHTML = `
    <div class="settings-section-head"><div><h3>Recent Activity</h3></div></div>
    <div class="admin-history-list">${(dashboard?.recent_activity || []).slice(0, 8).map((event) => `
      <article><strong>${escapeHtml(titleize(event.action.replaceAll(":", " ")))}</strong><small>${escapeHtml(adminTime(event.created_at))}</small></article>
    `).join("") || '<p class="empty">No commissioner activity yet.</p>'}</div>`;
}

async function loadAdminSession() {
  state.adminSession = await api("/api/admin/session");
  if (!state.adminSession.authenticated) {
    state.adminDashboard = null;
    state.adminView = "inbox";
  }
  renderAdminShell();
  return state.adminSession;
}

async function loadAdminDashboard() {
  const root = $("#admin-inbox-root");
  root?.setAttribute("aria-busy", "true");
  try {
    const params = new URLSearchParams({ league_id: currentAdminLeagueId() });
    state.adminDashboard = await api(`/api/admin/dashboard?${params.toString()}`, { headers: adminHeaders() });
    renderAdminDashboard();
    renderAdminSettings();
    return state.adminDashboard;
  } finally {
    root?.setAttribute("aria-busy", "false");
  }
}

async function loadAdminOverview() {
  return loadAdminDashboard();
}

async function loadAdminConsole() {
  const code = $("#admin-code").value;
  if (!code) {
    $("#admin-lock-error").textContent = "Admin code required";
    $("#admin-code").focus();
    return;
  }
  $("#admin-lock-error").textContent = "";
  try {
    state.adminSession = await api("/api/admin/session", {
      method: "POST",
      body: JSON.stringify({ admin_code: code })
    });
    $("#admin-code").value = "";
    state.adminView = "inbox";
    renderAdminShell();
    await loadAdminDashboard();
  } catch (error) {
    $("#admin-lock-error").textContent = error.message;
    $("#admin-code").focus();
  }
}

async function withAdminButton(button, pendingLabel, callback) {
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = pendingLabel;
  try {
    return await callback();
  } finally {
    button.disabled = false;
    button.innerHTML = original;
    hydrateIcons();
  }
}

function openAdminActionDialog(title, content) {
  const dialog = $("#admin-action-dialog");
  state.lastFocusedElement = document.activeElement;
  $("#admin-action-title").textContent = title;
  $("#admin-action-content").innerHTML = content;
  dialog.classList.remove("hidden");
  hydrateIcons();
  window.requestAnimationFrame(() => dialog.querySelector("button, select, input")?.focus());
}

function closeAdminActionDialog() {
  const dialog = $("#admin-action-dialog");
  dialog.classList.add("hidden");
  $("#admin-action-content").innerHTML = "";
  state.adminAction = null;
  state.lastFocusedElement?.focus?.();
}

async function retryAdminJob(jobType, button) {
  const targets = {
    pipeline: ["/api/admin/pipeline", { league_id: currentAdminLeagueId() }],
    lifecycle: ["/api/admin/lifecycle/run", {}],
    backup: ["/api/admin/maintenance/backup", {}]
  };
  const target = targets[jobType];
  if (!target) {
    state.adminSettingsTab = "system";
    state.adminView = "settings";
    renderAdminShell();
    return;
  }
  await withAdminButton(button, "Retrying...", async () => {
    try {
      await api(target[0], { method: "POST", headers: adminHeaders(), body: JSON.stringify(target[1]) });
      notify(`${titleize(jobType)} completed`, "success");
      await Promise.all([loadAdminDashboard(), refreshAll()]);
    } catch (error) {
      notify(error.message, "warn");
      await loadAdminDashboard().catch(() => {});
    }
  });
}

function handleAdminInboxAction(action, button) {
  state.adminAction = action;
  if (action.type === "job_retry") {
    retryAdminJob(action.payload.job_type, button);
    return;
  }
  if (action.type === "settings_warning") {
    state.adminSettingsTab = action.payload.settings_tab || "system";
    state.adminView = "settings";
    renderAdminShell();
    return;
  }
  if (action.type === "market_resolution") {
    const market = action.payload.market;
    openAdminActionDialog(action.title, `
      <p class="admin-action-intro">Select the official winning outcome. This settlement cannot be reversed from the interface.</p>
      <div class="admin-dialog-options">
        ${(market.candidates || []).map((candidate) => `
          <button type="button" data-admin-resolve-outcome="${candidate.outcome_id}">
            ${outcomeAvatar(candidate)}<span><strong>${escapeHtml(candidate.label)}</strong><small>${pct(candidate.probability)} · ${escapeHtml(evidenceText(candidate))}</small></span>
          </button>
        `).join("")}
      </div>
    `);
    $("#admin-action-content").querySelectorAll("[data-admin-resolve-outcome]").forEach((candidateButton) => {
      candidateButton.addEventListener("click", async () => {
        await withAdminButton(candidateButton, "Resolving...", async () => {
          try {
            await api(`/api/admin/markets/${market.market_id}/resolve`, {
              method: "POST",
              headers: adminHeaders(),
              body: JSON.stringify({ winning_outcome_id: Number(candidateButton.dataset.adminResolveOutcome) })
            });
            closeAdminActionDialog();
            notify("Market resolved", "success");
            delete state.marketDetails[market.market_id];
            await Promise.all([loadAdminDashboard(), refreshAll()]);
            if (state.selectedMarketId === market.market_id) {
              await loadMarketDetail(market.market_id);
            }
          } catch (error) {
            notify(error.message, "warn");
          }
        });
      });
    });
    return;
  }
  if (action.type === "participant_link") {
    const participant = action.payload.participant;
    const managers = state.adminDashboard?.managers || [];
    openAdminActionDialog(action.title, `
      <p class="admin-action-intro">Choose the Sleeper manager represented by this account.</p>
      <label>Sleeper manager<select id="admin-link-manager"><option value="">Select a manager</option>${managers.map((manager) => `<option value="${escapeHtml(manager.user_id)}">${escapeHtml(manager.display_name || manager.username)}${manager.team_name ? ` · ${escapeHtml(manager.team_name)}` : ""}</option>`).join("")}</select></label>
      <button id="admin-link-confirm" class="primary-command" type="button">Link Identity</button>
    `);
    $("#admin-link-confirm").addEventListener("click", async (event) => {
      const managerId = $("#admin-link-manager").value;
      if (!managerId) {
        notify("Choose a Sleeper manager", "warn");
        return;
      }
      await withAdminButton(event.currentTarget, "Linking...", async () => {
        try {
          await api(`/api/admin/participants/${participant.id}/link`, {
            method: "POST",
            headers: adminHeaders(),
            body: JSON.stringify({ sleeper_user_id: managerId })
          });
          closeAdminActionDialog();
          notify("Sleeper identity linked", "success");
          await loadAdminDashboard();
        } catch (error) {
          notify(error.message, "warn");
        }
      });
    });
    return;
  }
  if (action.type === "fund_payout") {
    const payout = action.payload;
    openAdminActionDialog(action.title, `
      <p class="admin-action-intro">Confirm that ${Number(payout.participant_count || 0)} external payouts totaling ${money(payout.total)} have been completed.</p>
      <button id="admin-payout-confirm" class="primary-command" type="button">Mark Payouts Paid</button>
    `);
    $("#admin-payout-confirm").addEventListener("click", async (event) => {
      await withAdminButton(event.currentTarget, "Saving...", async () => {
        try {
          await api(`/api/admin/markets/${payout.market_id}/payouts/pay`, { method: "POST", headers: adminHeaders(), body: "{}" });
          closeAdminActionDialog();
          notify("Payouts marked paid", "success");
          await Promise.all([loadAdminDashboard(), loadFund()]);
        } catch (error) {
          notify(error.message, "warn");
        }
      });
    });
  }
}

function marketGroup(market) {
  const title = String(market.title || "");
  if (title.includes("Commissioners Cup")) return "Cup";
  if (title.includes("Week ")) return "Weekly";
  if (title.includes("Makes Playoffs")) return "Team";
  return "Season";
}

function marketCategory(market) {
  const title = String(market.title || "");
  if (title.includes("Commissioners Cup")) return "cup";
  if (/^Week\s+\d+\s+/.test(title)) return "weekly";
  if (title.includes("Makes Playoffs")) return "playoffs";
  return "season";
}

function marketWeek(market) {
  const match = String(market.title || "").match(/^Week\s+(\d+)\s+/);
  return match ? Number(match[1]) : null;
}

function marketCategoryLabel(category) {
  return {
    season: "Season Long",
    playoffs: "Playoff Futures",
    weekly: "Weekly Markets",
    cup: "Commissioners Cup"
  }[category] || "Markets";
}

function marketCategoryDescription(category, markets = []) {
  if (category === "weekly") {
    const weeks = [...new Set(markets.map(marketWeek).filter(Boolean))].sort((a, b) => a - b);
    return weeks.length ? `Weeks ${weeks[0]}-${weeks[weeks.length - 1]}` : "Weekly high and low score contracts";
  }
  return {
    season: "Championship futures",
    playoffs: "One contract per team",
    cup: "Gold, Silver, and Bronze bracket futures"
  }[category] || "";
}

function sortMarketsForCategory(markets) {
  return [...markets].sort((a, b) => {
    const categoryDelta = ["season", "playoffs", "weekly", "cup"].indexOf(marketCategory(a)) - ["season", "playoffs", "weekly", "cup"].indexOf(marketCategory(b));
    if (categoryDelta) return categoryDelta;
    const weekDelta = (marketWeek(a) || 0) - (marketWeek(b) || 0);
    if (weekDelta) return weekDelta;
    const aLow = String(a.title || "").includes("Lowest") ? 1 : 0;
    const bLow = String(b.title || "").includes("Lowest") ? 1 : 0;
    if (aLow !== bLow) return aLow - bLow;
    return String(a.title || "").localeCompare(String(b.title || ""));
  });
}

function marketMove(market) {
  const top = [...(market.outcomes || [])].sort((a, b) => Number(b.price) - Number(a.price))[0];
  const baseline = Number(top?.prior_probability || (1 / Math.max(1, market.outcomes?.length || 1))) * 100;
  return Number(top?.price || 0) - baseline;
}

function lmsrLogSumExp(values) {
  const max = Math.max(...values);
  return max + Math.log(values.reduce((sum, value) => sum + Math.exp(value - max), 0));
}

function lmsrCost(quantities, index, delta, liquidity, payout = 100, priors = null) {
  const normalized = priors?.length === quantities.length ? priors : quantities.map(() => 1 / quantities.length);
  const before = lmsrLogSumExp(quantities.map((quantity, itemIndex) => quantity / liquidity + Math.log(normalized[itemIndex]))) * liquidity;
  const next = [...quantities];
  next[index] += delta;
  const after = lmsrLogSumExp(next.map((quantity, itemIndex) => quantity / liquidity + Math.log(normalized[itemIndex]))) * liquidity;
  return (after - before) * payout;
}

function lmsrPrices(quantities, liquidity, payout = 100, priors = null) {
  const normalized = priors?.length === quantities.length ? priors : quantities.map(() => 1 / quantities.length);
  const weights = quantities.map((quantity, index) => normalized[index] * Math.exp(quantity / liquidity));
  const total = weights.reduce((sum, value) => sum + value, 0);
  return weights.map((weight) => (weight / total) * payout);
}

function renderHome() {
  const root = $("#home-root");
  if (!root) return;
  const positions = (state.portfolio?.positions || []).filter((position) =>
    Number(position.shares || 0) > 0 && ["open", "closed"].includes(position.status)
  );
  const cash = Number(state.portfolio?.cash || state.session?.cash || 0);
  const openValue = Number(state.session?.open_value || 0);
  const net = Number(state.session?.net_worth || 0);
  const profit = net - 10000;
  const openMarkets = state.markets.filter((market) => market.status === "open");
  const filteredMarkets = openMarkets.filter((market) => {
    if (state.homeMarketFilter === "weekly") return marketCategory(market) === "weekly";
    if (state.homeMarketFilter === "futures") return ["season", "playoffs"].includes(marketCategory(market));
    return true;
  });
  const watch = (state.homeMarketFilter === "movers"
    ? [...filteredMarkets].sort((a, b) => Math.abs(marketMove(b)) - Math.abs(marketMove(a)))
    : sortMarketsForCategory(filteredMarkets)
  ).slice(0, 7);
  const recent = (state.ticker || []).slice(0, 5);
  const isFirstRun = !positions.length && state.markets.some((market) => market.status === "open");
  const allocationTotal = Math.max(1, cash + openValue);
  const cashRatio = Math.max(0, Math.min(1, cash / allocationTotal));
  const exposureRatio = Math.max(0, Math.min(1, openValue / allocationTotal));
  const exposureCells = Math.round(exposureRatio * 60);
  const clusterCenters = [[1, 2], [2, 8], [1, 13]];
  const exposureOrder = Array.from({ length: 60 }, (_, index) => index).sort((a, b) => {
    const distance = (index) => {
      const row = Math.floor(index / 15);
      const column = index % 15;
      return Math.min(...clusterCenters.map(([centerRow, centerColumn]) =>
        Math.abs(row - centerRow) + Math.abs(column - centerColumn)
      ));
    };
    return distance(a) - distance(b) || a - b;
  });
  const exposedIndexes = new Set(exposureOrder.slice(0, exposureCells));
  const bookCells = Array.from({ length: 60 }, (_, index) => `
    <span class="${exposedIndexes.has(index) ? `exposed tone-${index % 3}` : "liquid"}"></span>
  `).join("");
  const widgetOpen = (name) => state.homeWidgets[name] ? "open" : "";
  root.innerHTML = `
    <header class="home-statusbar">
      <div><span class="live-indicator"><i></i>Live exchange</span><strong>${escapeHtml(state.session?.league?.name || "The League")}</strong></div>
      <span>${openMarkets.length} markets open</span>
    </header>
    <section class="home-hero exchange-hero">
      <div class="exchange-title">
        <span>Portfolio equity</span>
        <strong>${money(net)}</strong>
        <small class="${profit >= 0 ? "good" : "bad"}">${profit >= 0 ? "+" : "-"}${money(Math.abs(profit))} net P/L</small>
        <div class="equity-allocation" aria-label="${pct(cashRatio)} cash and ${pct(exposureRatio)} open exposure">
          <i class="exposure" style="width:${Math.round(exposureRatio * 100)}%"></i>
        </div>
        <div class="equity-allocation-labels"><span>${pct(cashRatio)} liquid</span><span>${pct(exposureRatio)} deployed</span></div>
      </div>
      <div class="book-map">
        <header><div><span>Capital map</span><strong>Book composition</strong></div><small>${positions.length} live ${positions.length === 1 ? "position" : "positions"}</small></header>
        <div class="pixel-matrix" aria-hidden="true">${bookCells}</div>
        <footer>
          <span><i class="liquid"></i>Cash ${money(cash)}</span>
          <span><i class="exposed"></i>Open ${money(openValue)}</span>
        </footer>
      </div>
      <div class="desk-kpis">
        <article><span>Available cash</span><strong>${money(cash)}</strong><small>${pct(cashRatio)} of equity</small></article>
        <article><span>Open value</span><strong>${money(openValue)}</strong><small>${pct(exposureRatio)} deployed</small></article>
        <article><span>Live positions</span><strong>${positions.length}</strong><small>Across your book</small></article>
        <article><span>Open markets</span><strong>${openMarkets.length}</strong><small>Trading now</small></article>
      </div>
    </section>
    ${isFirstRun ? `
      <section class="first-run-card">
        <div>
          <span>Start here</span>
          <strong>Your bankroll is live. Pick one market, buy a few shares, then watch your book move.</strong>
        </div>
        <div class="first-run-actions">
          <button type="button" data-home-tab="markets">Browse Markets</button>
          <button type="button" data-home-tab="portfolio">View Portfolio</button>
        </div>
      </section>
    ` : ""}
    <section class="desk-grid">
      <details class="desk-panel desk-widget watch-panel" data-home-widget="watch" ${widgetOpen("watch")}>
        <summary class="widget-summary">
          <span class="widget-title"><i data-lucide="radar" aria-hidden="true"></i><span><strong>Market Watch</strong><small>Live prices and probability leaders</small></span></span>
          <span class="widget-meta"><b>${watch.length}</b><i data-lucide="chevron-down" aria-hidden="true"></i></span>
        </summary>
        <div class="widget-body">
          <div class="widget-toolbar">
            <div class="dashboard-segmented" role="group" aria-label="Market watch filter">
              ${[
                ["movers", "Movers"],
                ["weekly", "Weekly"],
                ["futures", "Futures"]
              ].map(([value, label]) => `<button type="button" data-home-market-filter="${value}" class="${state.homeMarketFilter === value ? "active" : ""}" aria-pressed="${state.homeMarketFilter === value}">${label}</button>`).join("")}
            </div>
            <button class="quiet-command" type="button" data-home-tab="markets">All markets <i data-lucide="arrow-up-right" aria-hidden="true"></i></button>
          </div>
          <div class="asset-list">
            ${watch.map(renderAssetRow).join("") || emptyState("No markets in this view", "Choose another market filter or check back after the next model run.")}
          </div>
        </div>
      </details>
      <details class="desk-panel desk-widget" data-home-widget="positions" ${widgetOpen("positions")}>
        <summary class="widget-summary">
          <span class="widget-title"><i data-lucide="layers-3" aria-hidden="true"></i><span><strong>Positions</strong><small>Current marked value</small></span></span>
          <span class="widget-meta"><b>${positions.length}</b><i data-lucide="chevron-down" aria-hidden="true"></i></span>
        </summary>
        <div class="widget-body">
          <div class="widget-toolbar end"><button class="quiet-command" type="button" data-home-tab="portfolio">Full portfolio <i data-lucide="arrow-up-right" aria-hidden="true"></i></button></div>
          <div class="asset-list">
            ${positions.slice(0, 6).map((position) => `
              <button class="asset-row" type="button" data-market-id="${position.market_id}">
                ${outcomeAvatar(position)}
                <span>
                  <strong>${escapeHtml(position.outcome_label || position.label)}</strong>
                  <small>${escapeHtml(position.title)} · ${fmt(position.shares)} sh</small>
                </span>
                <b>${money(position.market_value)}</b>
              </button>
            `).join("") || emptyState("No holdings yet", "Buy shares in a market to see your portfolio fill in.", `<button type="button" data-home-tab="markets">Find a Market</button>`)}
          </div>
        </div>
      </details>
      <details class="desk-panel desk-widget tape-panel" data-home-widget="tape" ${widgetOpen("tape")}>
        <summary class="widget-summary">
          <span class="widget-title"><i data-lucide="activity" aria-hidden="true"></i><span><strong>Exchange Tape</strong><small>Latest fills and market moves</small></span></span>
          <span class="widget-meta"><b>${recent.length}</b><i data-lucide="chevron-down" aria-hidden="true"></i></span>
        </summary>
        <div class="widget-body">
          <div class="tape-list">
            ${recent.map((item) => `
              <article>
                <strong>${escapeHtml(item.headline || "Market update")}</strong>
                <span>${escapeHtml(item.detail || "")}</span>
                <b class="${Number(item.price_move || 0) >= 0 ? "good" : "bad"}">${item.is_demo ? "Demo" : item.price_move ? money(item.price_move) : "Live"}</b>
              </article>
            `).join("") || emptyState("Tape is quiet", "Trades and market movers will appear here once your league starts trading.")}
          </div>
        </div>
      </details>
    </section>
  `;
  root.querySelectorAll("[data-home-market-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.homeMarketFilter = button.dataset.homeMarketFilter;
      renderHome();
    });
  });
  root.querySelectorAll("[data-home-widget]").forEach((widget) => {
    widget.addEventListener("toggle", () => {
      state.homeWidgets[widget.dataset.homeWidget] = widget.open;
    });
  });
  root.querySelectorAll("[data-home-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.homeTab));
  });
  root.querySelectorAll("[data-market-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedMarketId = Number(button.dataset.marketId);
      activateTab("markets");
      loadMarketDetail(state.selectedMarketId).then(scrollMarketDetailIntoView);
    });
  });
  hydrateIcons();
}

function renderAssetRow(market) {
  const top = [...(market.outcomes || [])].sort((a, b) => Number(b.price) - Number(a.price))[0];
  const move = marketMove(market);
  const topProbability = Number(top?.probability || 0);
  const moveTone = move >= 0 ? "good" : "bad";
  return `
    <button class="asset-row market-quote ${Number(market.id) === Number(state.selectedMarketId) ? "active" : ""}" type="button" data-market-id="${market.id}">
      <span class="quote-avatar">${outcomeAvatar(top || market.outcomes?.[0] || {})}</span>
      <span class="quote-copy">
        <span class="quote-kicker">${marketGroup(market)} · ${escapeHtml(market.status)}</span>
        <strong>${escapeHtml(market.title)}</strong>
        <small>${escapeHtml(top?.label || "-")} leads · ${pct(topProbability)} implied</small>
        <span class="quote-bar" aria-hidden="true"><i style="width: ${probabilityWidth(topProbability)}"></i></span>
      </span>
      <b class="quote-price">
        ${money(top?.price || 0)}
        <small class="${moveTone}">${move >= 0 ? "+" : ""}${money(move)}</small>
      </b>
    </button>
  `;
}

function activateTab(tab) {
  state.activeTab = tab;
  $("#app-view").classList.toggle("dashboard-active", tab === "home");
  document.querySelectorAll(".tabs button").forEach((item) => {
    const active = item.dataset.tab === tab;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  document.querySelectorAll(".tab-view").forEach((view) => view.classList.add("hidden"));
  $(`#${tab}-tab`)?.classList.remove("hidden");
  if (isMobileNavigation()) {
    state.mobileNavOpen = false;
    $("#app-view").classList.remove("mobile-nav-open");
    $("#nav-toggle").setAttribute("aria-expanded", "false");
    $("#nav-toggle").setAttribute("aria-label", "Open navigation menu");
    window.requestAnimationFrame(() => $("#main-content")?.focus({ preventScroll: true }));
  }
  if (tab === "account" && !state.identity) loadIdentity().catch(() => {});
  if (tab === "admin") {
    renderAdminShell();
    loadAdminSession().then((session) => {
      if (session.authenticated) loadAdminDashboard().catch(() => {});
    }).catch(() => renderAdminShell());
  }
}

function scrollMarketDetailIntoView() {
  if (!window.matchMedia("(max-width: 1180px)").matches) return;
  window.requestAnimationFrame(() => {
    const detail = $("#market-detail");
    if (!detail) return;
    const stickyNavBottom = $("#side-panel")?.getBoundingClientRect().bottom || 0;
    const top = detail.getBoundingClientRect().top + window.scrollY - stickyNavBottom - 12;
    window.scrollTo({ top: Math.max(0, top), behavior: "auto" });
  });
}

async function loadFund() {
  const root = $("#fund-root");
  root?.setAttribute("aria-busy", "true");
  if (root && !state.fund) root.innerHTML = loadingState("Loading Fund reserves");
  try {
    const result = await api("/api/fund");
    state.fund = result;
    hydrateFundSettings(result.settings || {});
    renderFund();
    return result;
  } catch (error) {
    if (root) {
      root.innerHTML = errorState("Fund unavailable", error.message, '<button type="button" data-retry-fund>Retry</button>');
      root.querySelector("[data-retry-fund]")?.addEventListener("click", () => loadFund());
    }
    throw error;
  } finally {
    root?.setAttribute("aria-busy", "false");
  }
}

function renderTicker() {
  const items = state.ticker.length ? state.ticker : [{
    side: "watch",
    headline: "Market desk open",
    detail: "Trades and price moves will appear here",
    price_move: 0
  }];
  const tickerHtml = items.map((item) => {
    const move = Number(item.price_move || 0);
    const tone = move > 0 ? "up" : move < 0 ? "down" : "flat";
    const moveText = item.is_demo ? "DEMO" : move ? `${move > 0 ? "+$" : "-$"}${fmt(Math.abs(move))}` : "LIVE";
    return `
      <span class="ticker-item ${tone} ${item.is_demo ? "demo" : ""}">
        <strong>${escapeHtml(moveText)}</strong>
        <em>${escapeHtml(item.headline || "")}</em>
        <small>${escapeHtml(item.detail || "")}</small>
      </span>
    `;
  }).join("");
  $("#ticker-track").innerHTML = tickerHtml + tickerHtml;
}

function renderMarkets() {
  const filter = $("#market-filter").value;
  const categoryFilter = $("#market-group-filter")?.value || "all";
  const markets = sortMarketsForCategory(state.markets.filter((market) => {
    const statusMatch = filter === "all" || market.status === filter;
    const categoryMatch = categoryFilter === "all" || marketCategory(market) === categoryFilter;
    return statusMatch && categoryMatch;
  }));
  const categoryOrder = ["season", "playoffs", "weekly", "cup"];
  const grouped = categoryOrder
    .map((category) => [category, markets.filter((market) => marketCategory(market) === category)])
    .filter(([, items]) => items.length);
  $("#markets-root").innerHTML = grouped.map(([category, items]) => {
    if (category === "weekly") return renderWeeklyMarketGroup(items);
    return renderMarketSection(category, items);
  }).join("") || emptyState("No markets listed", "Commissioners can seed team futures, weekly score markets, and Cup brackets from Admin.", `<button type="button" data-empty-tab="admin">Launch Admin</button>`);

  $("#markets-root").querySelectorAll("[data-market-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedMarketId = Number(button.dataset.marketId);
      renderMarkets();
      loadMarketDetail(state.selectedMarketId).then(scrollMarketDetailIntoView);
    });
  });
  $("#markets-root").querySelector("[data-empty-tab]")?.addEventListener("click", (event) => activateTab(event.target.dataset.emptyTab));
  renderMarketDetail();
}

function renderMarketSection(category, markets) {
  return `
    <section class="market-section" data-market-section="${category}">
      <div class="market-section-head">
        <div>
          <h3>${escapeHtml(marketCategoryLabel(category))}</h3>
          <span>${escapeHtml(marketCategoryDescription(category, markets))}</span>
        </div>
        <b>${markets.length}</b>
      </div>
      <div class="market-section-list">
        ${markets.map(renderAssetRow).join("")}
      </div>
    </section>
  `;
}

function renderWeeklyMarketGroup(markets) {
  const weeks = [...new Set(markets.map(marketWeek).filter(Boolean))].sort((a, b) => a - b);
  return `
    <section class="market-section weekly-market-section" data-market-section="weekly">
      <div class="market-section-head">
        <div>
          <h3>Weekly Markets</h3>
          <span>Top and lowest scoring team by week</span>
        </div>
        <b>${markets.length}</b>
      </div>
      ${weeks.map((week) => {
        const weekMarkets = markets.filter((market) => marketWeek(market) === week);
        return `
          <div class="week-market-group">
            <div class="week-market-head">
              <h4>Week ${week}</h4>
              <span>${weekMarkets.length} markets</span>
            </div>
            <div class="market-section-list">
              ${weekMarkets.map(renderAssetRow).join("")}
            </div>
          </div>
        `;
      }).join("")}
    </section>
  `;
}

function imageForOutcome(outcome) {
  if (outcome?.image_url) return outcome.image_url;
  const source = String(outcome?.source_ref || "");
  if (!source.startsWith("player:")) return "";
  const playerId = source.replace("player:", "");
  if (!playerId || playerId.startsWith("qb") || playerId.startsWith("rb") || playerId.startsWith("wr") || playerId.startsWith("te")) return "";
  return `https://sleepercdn.com/content/nfl/players/${encodeURIComponent(playerId)}.jpg`;
}

function outcomeTokenKind(outcome) {
  const label = String(outcome?.label || "").trim().toLowerCase();
  const source = String(outcome?.source_ref || "").toLowerCase();
  if (label === "yes" || source.endsWith(":yes")) return "yes";
  if (label === "no" || source.endsWith(":no")) return "no";
  if (imageForOutcome(outcome)) return "player";
  if (source.startsWith("roster:")) return "team";
  return "generic";
}

function renderLeaguePreview(payload) {
  const league = payload?.league || {};
  const teams = payload?.teams || [];
  const managers = payload?.managers || [];
  $("#league-preview").innerHTML = `
    <div class="league-preview-head">
      <strong>${escapeHtml(league.name || "Sleeper League")}</strong>
      <span>${escapeHtml(league.season || "")} · ${escapeHtml(league.league_id || "")}</span>
    </div>
    <div class="league-preview-grid">
      <span>${teams.length} teams</span>
      <span>${managers.length} managers</span>
    </div>
    <div class="manager-list">
      ${teams.slice(0, 12).map((team) => `
        <article>
          <strong>${escapeHtml(team.team_name)}</strong>
          <span>${escapeHtml(team.manager || team.display_name || "Unassigned")}</span>
        </article>
      `).join("")}
    </div>
  `;
  renderLaunchChecklist();
}

function renderManagers(managers = state.managers) {
  state.managers = managers || [];
  const root = $("#managers-root");
  if (!root) return;
  root.innerHTML = state.managers.map((manager) => {
    const avatar = manager.avatar
      ? `https://sleepercdn.com/avatars/thumbs/${encodeURIComponent(manager.avatar)}`
      : "";
    const rosters = (manager.roster_ids || []).length ? `Roster ${(manager.roster_ids || []).join(", ")}` : "League member";
    return `
      <article class="manager-card">
        <span class="manager-avatar ${avatar ? "has-image" : ""}">
          <span>${escapeHtml(initials(manager.display_name || manager.username))}</span>
          ${avatar ? `<img src="${avatar}" alt="" loading="lazy" onerror="this.remove()">` : ""}
        </span>
        <div>
          <strong>${escapeHtml(manager.display_name || manager.username || "Sleeper user")}</strong>
          <span>${escapeHtml(manager.username ? `@${manager.username}` : "Username unavailable")}</span>
          <small>${escapeHtml(manager.team_name || rosters)} · ${escapeHtml(rosters)}</small>
        </div>
        <b>${manager.is_owner ? "Owner" : "Co"}</b>
      </article>
    `;
  }).join("") || `<p class="empty">Sync Sleeper, then load managers.</p>`;
  renderLaunchChecklist();
}

function renderIdentity() {
  const root = $("#identity-root");
  if (!root) return;
  const identity = state.identity;
  if (!identity) {
    root.innerHTML = `<p class="empty">Refresh to load Sleeper managers for this league.</p>`;
    return;
  }
  const current = identity.current;
  root.innerHTML = `
    <div class="identity-current">
      <span>Current claim</span>
      <strong>${current ? escapeHtml(current.display_name || current.username) : "Unclaimed"}</strong>
      <small>${current ? escapeHtml(`${current.team_name || "Sleeper manager"} ${current.username ? `· @${current.username}` : ""}`) : "Claim your Sleeper manager profile so the league can see who is who."}</small>
    </div>
    <div class="identity-list">
      ${(identity.managers || []).map((manager) => {
        const disabled = manager.is_claimed && !manager.is_claimed_by_me;
        const label = manager.is_claimed_by_me ? "Claimed" : disabled ? `Claimed by ${manager.claimed_by?.display_name || "someone"}` : "Claim";
        return `
          <article class="identity-card ${manager.is_claimed_by_me ? "active" : ""}">
            <span class="manager-avatar ${manager.avatar ? "has-image" : ""}">
              <span>${escapeHtml(initials(manager.display_name || manager.username))}</span>
              ${manager.avatar ? `<img src="https://sleepercdn.com/avatars/thumbs/${encodeURIComponent(manager.avatar)}" alt="" loading="lazy" onerror="this.remove()">` : ""}
            </span>
            <div>
              <strong>${escapeHtml(manager.display_name || manager.username || "Sleeper user")}</strong>
              <span>${escapeHtml(manager.team_name || "League manager")} ${manager.username ? `· @${escapeHtml(manager.username)}` : ""}</span>
              <small>${(manager.roster_ids || []).length ? `Roster ${(manager.roster_ids || []).join(", ")}` : "No roster attached"}</small>
            </div>
            <button type="button" data-claim-user="${escapeHtml(manager.user_id)}" ${disabled ? "disabled" : ""}>${escapeHtml(label)}</button>
          </article>
        `;
      }).join("")}
    </div>
  `;
  root.querySelectorAll("[data-claim-user]").forEach((button) => {
    button.addEventListener("click", () => claimIdentity(button.dataset.claimUser));
  });
}

async function loadIdentity() {
  const root = $("#identity-root");
  root?.setAttribute("aria-busy", "true");
  if (root) root.innerHTML = loadingState("Loading Sleeper identities");
  try {
    const result = await api("/api/identity/options");
    state.identity = result;
    renderIdentity();
    return result;
  } catch (error) {
    if (root) {
      root.innerHTML = errorState("Identity options unavailable", error.message, '<button type="button" data-retry-identity>Retry</button>');
      root.querySelector("[data-retry-identity]")?.addEventListener("click", () => loadIdentity());
    }
    throw error;
  } finally {
    root?.setAttribute("aria-busy", "false");
  }
}

async function claimIdentity(userId) {
  try {
    const result = await api("/api/identity/claim", {
      method: "POST",
      body: JSON.stringify({ user_id: userId })
    });
    state.session.participant = result.participant;
    notify("Sleeper identity claimed", "success");
    await loadIdentity();
    await refreshAll();
  } catch (error) {
    notify(error.message, "warn");
  }
}

function renderSetupResult(result) {
  const root = $("#setup-result");
  if (!root) return;
  if (!result) {
    root.innerHTML = "";
    return;
  }
  root.innerHTML = `
    <article>
      <span>League</span>
      <strong>${escapeHtml(result.league?.name || "Sleeper League")}</strong>
    </article>
    <article>
      <span>Imported</span>
      <strong>${result.synced?.teams || 0} teams · ${result.synced?.managers || 0} managers</strong>
    </article>
    <article>
      <span>Markets</span>
      <strong>${result.markets?.total || result.markets?.season || 0} eligible · ${result.markets?.created || 0} new</strong>
    </article>
    <article>
      <span>Model</span>
      <strong>${result.model ? `${Number(result.model.simulations || 0).toLocaleString()} sims · ${pct(result.model.coverage || 0)} coverage` : "Not run"}</strong>
    </article>
  `;
  renderLaunchChecklist();
}

async function loadManagers() {
  const leagueId = $("#league-id-input").value.trim();
  const params = new URLSearchParams({ league_id: leagueId });
  const root = $("#managers-root");
  root?.setAttribute("aria-busy", "true");
  if (root && !state.managers.length) root.innerHTML = loadingState("Loading Sleeper managers");
  try {
    const result = await api(`/api/admin/managers?${params.toString()}`, {
      headers: { "X-Admin-Code": $("#admin-code").value }
    });
    renderManagers(result.managers || []);
    return result;
  } catch (error) {
    if (root) root.innerHTML = errorState("Managers unavailable", error.message);
    throw error;
  } finally {
    root?.setAttribute("aria-busy", "false");
  }
}

async function loadResolutionCenter() {
  const leagueId = $("#league-id-input").value.trim();
  const params = new URLSearchParams({ league_id: leagueId, status: "actionable" });
  const root = $("#resolution-root");
  root?.setAttribute("aria-busy", "true");
  if (root && !state.resolutionMarkets.length) root.innerHTML = loadingState("Loading resolution queue");
  try {
    const result = await api(`/api/admin/resolution-center?${params.toString()}`, {
      headers: { "X-Admin-Code": $("#admin-code").value }
    });
    state.resolutionMarkets = result.markets || [];
    renderResolutionCenter();
    return result;
  } catch (error) {
    if (root) root.innerHTML = errorState("Resolution queue unavailable", error.message);
    throw error;
  } finally {
    root?.setAttribute("aria-busy", "false");
  }
}

async function loadCommissionerManagement() {
  const leagueId = $("#league-id-input").value.trim();
  const params = new URLSearchParams({ league_id: leagueId });
  const roots = [$("#participants-root"), $("#invites-root")].filter(Boolean);
  roots.forEach((root) => root.setAttribute("aria-busy", "true"));
  if (!state.participants.length) roots.forEach((root) => { root.innerHTML = loadingState("Loading commissioner records"); });
  try {
    const [participants, invites] = await Promise.all([
      api(`/api/admin/participants?${params.toString()}`, { headers: { "X-Admin-Code": $("#admin-code").value } }),
      api(`/api/admin/invites?${params.toString()}`, { headers: { "X-Admin-Code": $("#admin-code").value } })
    ]);
    state.participants = participants.participants || [];
    if (participants.managers?.length) state.managers = participants.managers;
    state.invites = invites.invites || [];
    renderCommissionerManagement();
    return { participants, invites };
  } catch (error) {
    roots.forEach((root) => { root.innerHTML = errorState("Commissioner records unavailable", error.message); });
    throw error;
  } finally {
    roots.forEach((root) => root.setAttribute("aria-busy", "false"));
  }
}

function renderCommissionerManagement() {
  const participantsRoot = $("#participants-root");
  const invitesRoot = $("#invites-root");
  if (invitesRoot) {
    invitesRoot.innerHTML = `
      <div class="mini-table">
        <div class="mini-table-head"><span>Invite</span><span>Role</span><span>Uses</span></div>
        ${state.invites.map((invite) => `
          <article>
            <strong>${escapeHtml(invite.code)}</strong>
            <span>${escapeHtml(invite.role)}</span>
            <span>${invite.uses_remaining ?? "∞"}</span>
          </article>
        `).join("") || `<p class="empty">No invite codes yet.</p>`}
      </div>
    `;
  }
  if (!participantsRoot) return;
  const managerOptions = [
    `<option value="">Unlinked</option>`,
    ...state.managers.map((manager) => `<option value="${escapeHtml(manager.user_id)}">${escapeHtml(manager.display_name || manager.username)}${manager.username ? ` (@${escapeHtml(manager.username)})` : ""}</option>`)
  ].join("");
  participantsRoot.innerHTML = `
    <div class="participant-list">
      ${state.participants.map((participant) => `
        <article class="participant-card">
          <div>
            <strong>${escapeHtml(participant.display_name)}</strong>
            <span>${escapeHtml(participant.manager_team_name || participant.manager_display_name || "No Sleeper identity")}</span>
            <small>${money(participant.net_worth)} net · ${participant.trade_count} trades</small>
          </div>
          <select data-role-participant="${participant.id}">
            ${["participant", "commissioner", "admin"].map((role) => `<option value="${role}" ${participant.role === role ? "selected" : ""}>${role}</option>`).join("")}
          </select>
          <select data-link-participant="${participant.id}">
            ${managerOptions}
          </select>
        </article>
      `).join("") || `<p class="empty">No participants yet.</p>`}
    </div>
  `;
  participantsRoot.querySelectorAll("[data-role-participant]").forEach((select) => {
    select.addEventListener("change", () => setParticipantRole(select.dataset.roleParticipant, select.value));
  });
  participantsRoot.querySelectorAll("[data-link-participant]").forEach((select) => {
    const participant = state.participants.find((item) => Number(item.id) === Number(select.dataset.linkParticipant));
    select.value = participant?.sleeper_user_id || "";
    select.addEventListener("change", () => linkParticipant(select.dataset.linkParticipant, select.value));
  });
  renderLaunchChecklist();
}

async function setParticipantRole(participantId, role) {
  try {
    await api(`/api/admin/participants/${participantId}/role`, {
      method: "POST",
      headers: { "X-Admin-Code": $("#admin-code").value },
      body: JSON.stringify({ role })
    });
    notify("Participant role updated", "success");
    await loadCommissionerManagement();
  } catch (error) {
    notify(error.message, "warn");
  }
}

async function linkParticipant(participantId, sleeperUserId) {
  try {
    await api(`/api/admin/participants/${participantId}/link`, {
      method: "POST",
      headers: { "X-Admin-Code": $("#admin-code").value },
      body: JSON.stringify({ sleeper_user_id: sleeperUserId })
    });
    notify(sleeperUserId ? "Sleeper identity linked" : "Sleeper identity unlinked", "success");
    await loadCommissionerManagement();
  } catch (error) {
    notify(error.message, "warn");
  }
}

function evidenceText(candidate) {
  const ev = candidate.evidence || {};
  if (ev.type === "team") {
    return `${ev.wins}-${ev.losses}${ev.ties ? `-${ev.ties}` : ""} · ${fmt(ev.points_for)} PF`;
  }
  if (ev.type === "player") {
    return `${ev.position || "FLEX"} · ${ev.team || "FA"} · ${ev.team_name || "Rostered"}`;
  }
  return "Commissioner evidence";
}

function renderResolutionCenter() {
  const root = $("#resolution-root");
  if (!root) return;
  const closedMarkets = state.resolutionMarkets.filter((market) => market.status === "closed");
  const openMarkets = state.resolutionMarkets.filter((market) => market.status === "open");
  const visibleMarkets = [...closedMarkets, ...openMarkets.slice(0, 4)];
  const grouped = visibleMarkets.reduce((groups, market) => {
    const bucket = market.status === "open" ? "Upcoming Closes" : "Ready to Resolve";
    groups[bucket] = groups[bucket] || [];
    groups[bucket].push(market);
    return groups;
  }, {});
  root.innerHTML = Object.entries(grouped).map(([group, markets]) => `
    <section class="resolution-group">
      <div class="resolution-group-head">
        <strong>${escapeHtml(group)}</strong>
        <span>${markets.length} markets</span>
      </div>
      ${markets.map((market) => {
    const candidates = (market.candidates || []).slice(0, 3);
    return `
      <article class="resolution-card">
        <div>
          <span class="status ${market.status}">${escapeHtml(market.status)}</span>
          <strong>${escapeHtml(market.title)}</strong>
          <small>${escapeHtml(market.resolution_source)} · ${market.trade_count} trades</small>
        </div>
        ${market.status === "closed" ? `<div class="resolution-candidates">
          ${candidates.map((candidate) => `
            <button type="button" data-resolve-market="${market.market_id}" data-resolve-outcome="${candidate.outcome_id}" data-resolve-label="${escapeHtml(candidate.label)}" data-resolve-title="${escapeHtml(market.title)}">
              ${outcomeAvatar(candidate)}
              <span>
                <strong>${escapeHtml(candidate.label)}</strong>
                <small>${pct(candidate.probability)} · ${escapeHtml(evidenceText(candidate))}</small>
              </span>
            </button>
          `).join("")}
        </div>` : `
          <div class="resolution-actions">
            <span>Winner selection unlocks after trading closes.</span>
            <button type="button" data-resolution-action="close" data-market-id="${market.market_id}" data-market-title="${escapeHtml(market.title)}"><i class="button-icon" data-lucide="lock-keyhole" aria-hidden="true"></i>Close Trading</button>
            <button type="button" data-resolution-action="void" data-market-id="${market.market_id}" data-market-title="${escapeHtml(market.title)}"><i class="button-icon" data-lucide="ban" aria-hidden="true"></i>Void Contract</button>
          </div>
        `}
      </article>
    `;
      }).join("")}
    </section>
  `).join("") + (openMarkets.length > 4 ? `
    <div class="resolution-overflow-note">
      <strong>${openMarkets.length - 4} more open contracts</strong>
      <span>They remain available from Markets and will enter this queue when trading closes.</span>
    </div>
  ` : "") || `<p class="empty">No open or closed markets need resolution.</p>`;
  hydrateIcons();
  root.querySelectorAll("[data-resolve-market]").forEach((button) => {
    button.addEventListener("click", async () => {
      const ok = window.confirm(`Resolve "${button.dataset.resolveTitle}" as "${button.dataset.resolveLabel}"?`);
      if (!ok) return;
      await resolveMarket(Number(button.dataset.resolveMarket), Number(button.dataset.resolveOutcome));
    });
  });
  root.querySelectorAll("[data-resolution-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.resolutionAction;
      if (action === "void" && !window.confirm(`Void "${button.dataset.marketTitle}" and refund its ledger?`)) return;
      await adminMarketAction(action, Number(button.dataset.marketId));
      await loadAdminOverview();
    });
  });
}

function outcomeAvatar(outcome) {
  const image = imageForOutcome(outcome);
  const kind = outcomeTokenKind(outcome);
  if (kind === "yes" || kind === "no") {
    return `<span class="outcome-token ${kind}" aria-label="${kind === "yes" ? "Yes outcome" : "No outcome"}"><span>${kind === "yes" ? "Yes" : "No"}</span></span>`;
  }
  const fallback = `<span class="avatar-fallback">${escapeHtml(initials(outcome?.label))}</span>`;
  if (!image) return `<span class="player-avatar ${kind}">${fallback}</span>`;
  return `<span class="player-avatar ${kind} has-image">${fallback}<img src="${image}" alt="" loading="lazy" onload="this.parentElement.classList.add('loaded')" onerror="this.parentElement.classList.remove('has-image'); this.remove()"></span>`;
}

function selectedMarket() {
  return state.markets.find((market) => Number(market.id) === Number(state.selectedMarketId)) || state.markets[0];
}

async function loadMarketDetail(marketId = state.selectedMarketId) {
  if (!marketId) return;
  state.detailLoadingId = Number(marketId);
  delete state.detailErrors[marketId];
  renderMarketDetail();
  try {
    const result = await api(`/api/markets/${marketId}`);
    state.marketDetails[marketId] = result;
  } catch (error) {
    state.detailErrors[marketId] = error.message;
    notify(error.message, "warn");
  } finally {
    if (Number(state.detailLoadingId) === Number(marketId)) state.detailLoadingId = null;
    renderMarketDetail();
  }
}

function renderMarketPriceChart(points) {
  const canvas = $("#market-price-chart");
  if (!canvas || !window.Chart) return;
  if (state.marketChart) state.marketChart.destroy();
  const styles = getComputedStyle(document.documentElement);
  const muted = styles.getPropertyValue("--muted").trim() || "#8d98a8";
  const grid = styles.getPropertyValue("--gridline-strong").trim() || "rgba(148,163,184,.13)";
  const colors = ["#4fb286", "#58a6ff", "#d6a84b", "#d06b72", "#9c8cc2", "#82b3a3"];
  const grouped = new Map();
  points.forEach((point) => {
    if (!grouped.has(point.outcome_id)) grouped.set(point.outcome_id, { label: point.outcome_label, points: [] });
    grouped.get(point.outcome_id).points.push(point);
  });
  const series = [...grouped.values()]
    .sort((a, b) => Number(b.points.at(-1)?.price_after || 0) - Number(a.points.at(-1)?.price_after || 0))
    .slice(0, 6);
  const sourceLabels = [...new Set(points.map((point) => point.created_at))];
  const labels = sourceLabels.length === 1
    ? [sourceLabels[0], new Date().toISOString()]
    : sourceLabels;
  state.marketChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: series.map((item, index) => ({
        label: item.label,
        data: labels.map((label, labelIndex) => {
          const exact = item.points.find((point) => point.created_at === label)?.price_after;
          if (exact != null) return exact;
          if (sourceLabels.length === 1 && labelIndex === 1) return item.points[0]?.price_after ?? null;
          return null;
        }),
        borderColor: colors[index % colors.length],
        backgroundColor: "transparent",
        spanGaps: true,
        fill: false,
        tension: 0.25,
        pointRadius: labels.length > 12 ? 0 : 2,
        pointHoverRadius: 4
      }))
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { display: false },
        y: { ticks: { color: muted }, grid: { color: grid } }
      },
      plugins: {
        legend: {
          display: true,
          position: "bottom",
          labels: { color: muted, boxWidth: 12, boxHeight: 2, padding: 14 }
        },
        tooltip: {
          callbacks: {
            title: (items) => new Date(labels[items[0].dataIndex]).toLocaleString(),
            label: (item) => `${item.dataset.label}: ${money(item.raw)}`
          }
        }
      }
    }
  });
}

function renderMarketDetail() {
  const baseMarket = selectedMarket();
  if (!baseMarket) {
    $("#market-detail").innerHTML = emptyState("Pick a market", "Select a market from the quote board to view prices, rules, and the trade ticket.");
    return;
  }
  if (Number(state.detailLoadingId) === Number(baseMarket.id) && !state.marketDetails[baseMarket.id]) {
    $("#market-detail").innerHTML = loadingState("Loading market detail");
    return;
  }
  if (state.detailErrors[baseMarket.id] && !state.marketDetails[baseMarket.id]) {
    $("#market-detail").innerHTML = errorState(
      "Market detail unavailable",
      state.detailErrors[baseMarket.id],
      '<button type="button" data-retry-market>Retry</button>'
    );
    $("#market-detail").querySelector("[data-retry-market]")?.addEventListener("click", () => loadMarketDetail(baseMarket.id));
    return;
  }
  const detail = state.marketDetails[baseMarket.id];
  const market = detail?.market || baseMarket;
  const history = detail?.price_history || [];
  const recentTrades = detail?.trades || [];
  const outcomes = market.market_type === "binary"
    ? [...market.outcomes]
    : [...market.outcomes].sort((a, b) => b.price - a.price);
  const totalUserValue = outcomes.reduce((sum, outcome) => sum + Number(outcome.user_value || 0), 0);
  const fundPrize = market.fund_prize || null;
  const winningOutcome = outcomes.find((outcome) => Number(outcome.id) === Number(market.winning_outcome_id));
  const leadOutcome = outcomes[0] || {};
  const metrics = market.metrics || {};
  const modelUpdated = market.model?.updated_at ? new Date(market.model.updated_at).toLocaleString() : "Unavailable";
  const settlesAutomatically = market.origin === "model";
  const automaticSettlementCopy = market.settlement_time
    ? "Settles Tuesday at 1:00 AM ET from finalized Sleeper scores."
    : "Settles automatically from the official Sleeper result.";
  const stateBanner = market.status === "resolved"
    ? `<div class="market-state-banner resolved"><span>Settled</span><strong>${escapeHtml(winningOutcome?.label || "Winning outcome recorded")}</strong><small>Winning play-credit positions have been credited.</small></div>`
    : market.status === "closed"
      ? `<div class="market-state-banner closed"><span>Result pending</span><strong>Trading is closed</strong><small>${settlesAutomatically ? automaticSettlementCopy : "The commissioner will record the final outcome."}</small></div>`
      : market.status === "void"
        ? '<div class="market-state-banner void"><span>Voided</span><strong>Orders are closed</strong><small>Eligible play-credit positions have been refunded.</small></div>'
        : "";
  $("#market-detail").innerHTML = `
    <div class="detail-head">
      <div>
        <div class="detail-status-line"><span class="status ${market.status}">${escapeHtml(market.status)}</span><span>${escapeHtml(market.origin === "model" ? "Model originated" : "Commissioner market")}</span></div>
        <h2>${escapeHtml(market.title)}</h2>
        <p>${escapeHtml(market.resolution_rule)}</p>
      </div>
      <div class="detail-badges">
        ${totalUserValue > 0 ? `<span>${money(totalUserValue)} owned</span>` : ""}
        ${fundPrize ? `<span>${money(fundPrize.prize_pool)} prize pool</span>` : ""}
      </div>
    </div>
    ${stateBanner}
    <section class="market-chart-card market-chart-primary">
      <header class="market-chart-head">
        <div><span>Market history</span><strong>Price tape</strong></div>
        <div class="market-chart-leader"><span>Current leader</span><strong>${escapeHtml(leadOutcome.label || "No leader")}</strong><b>${pct(leadOutcome.probability || 0)}</b></div>
      </header>
      <div class="market-chart-stage"><canvas id="market-price-chart" aria-label="Market price chart"></canvas></div>
      <footer class="market-chart-summary">
        <span><b>${pct(leadOutcome.model_probability || leadOutcome.prior_probability || 0)}</b> model</span>
        <span><b>${Number(metrics.net_flow_24h || 0) >= 0 ? "+" : ""}${fmt(metrics.net_flow_24h || 0)} sh</b> 24h flow</span>
        <span><b>${fmt(metrics.volume || 0)}</b> volume</span>
      </footer>
    </section>
    <div class="outcome-list-head"><div><strong>Trade outcomes</strong><span>Price reflects the market's implied probability.</span></div><span>${outcomes.length} contracts</span></div>
    <div class="outcome-list ${market.market_type === "binary" ? "binary-outcomes" : ""}">
      ${outcomes.map((outcome) => `
        <article class="outcome-row ${outcomeTokenKind(outcome)}">
          ${outcomeAvatar(outcome)}
          <div>
            <strong>${escapeHtml(outcome.label)}</strong>
            <span>${pct(outcome.model_probability)} model${Number(outcome.user_shares || 0) ? ` · You own ${fmt(outcome.user_shares)} sh` : ""}</span>
            <span class="outcome-meter" aria-hidden="true"><i style="width: ${probabilityWidth(outcome.probability)}"></i></span>
          </div>
          <div class="outcome-actions">
            <div class="outcome-price"><strong>${money(outcome.price)}</strong><small>${pct(outcome.probability)} implied</small></div>
            <div class="trade-ticket">
              <button type="button" data-order="${outcome.id}" ${market.status !== "open" ? "disabled" : ""}>${market.status === "open" ? "Trade" : market.status === "resolved" ? "Settled" : "Closed"}</button>
            </div>
          </div>
        </article>
      `).join("")}
    </div>
    ${settlesAutomatically ? `
      <div class="automatic-settlement-note" role="status">
        <i data-lucide="clock-check" aria-hidden="true"></i>
        <span><strong>Automatic settlement</strong>${escapeHtml(automaticSettlementCopy)}</span>
      </div>
    ` : `<details class="market-intelligence">
      <summary><span><strong>Contract and market details</strong><small>Rules, model diagnostics, liquidity, and recent prints</small></span><i data-lucide="chevron-down" aria-hidden="true"></i></summary>
      <div class="market-intelligence-grid">
        <section>
          <h3>Contract</h3>
          <dl>
            <div><dt>Payout</dt><dd>${money(market.payout || 100)} per winning share</dd></div>
            <div><dt>Resolution</dt><dd>${escapeHtml(market.resolution_source || "Commissioner")}</dd></div>
            <div><dt>Fund prize</dt><dd>${fundPrize ? `${money(fundPrize.prize_pool)} · ${escapeHtml(fundPrize.status)}` : "None"}</dd></div>
            <div><dt>Model updated</dt><dd>${escapeHtml(modelUpdated)}</dd></div>
          </dl>
        </section>
        <section>
          <h3>Market health</h3>
          <dl>
            <div><dt>Leader depth</dt><dd>${fmt(leadOutcome.depth_five_points || 0)} sh to +5 pts</dd></div>
            <div><dt>Model coverage</dt><dd>${market.model?.coverage ? pct(market.model.coverage) : "Unavailable"}</dd></div>
            <div><dt>Traders</dt><dd>${fmt(metrics.unique_traders || 0)}</dd></div>
            <div><dt>Concentration</dt><dd>${pct(metrics.trader_concentration || 0)}</dd></div>
          </dl>
        </section>
        <section class="market-recent-prints">
          <h3>Recent prints</h3>
          ${(recentTrades.slice(0, 5)).map((trade) => `
            <article><span>${escapeHtml(trade.display_name)} · ${escapeHtml(trade.outcome_label)}</span><b>${escapeHtml(String(trade.side).toUpperCase())}</b><small>${fmt(trade.shares)} sh</small></article>
          `).join("") || '<p class="empty">No completed orders yet.</p>'}
        </section>
      </div>
    </details>`}
    ${!settlesAutomatically && state.adminSession?.authenticated ? `
      <details class="admin-actions">
        <summary>Commissioner controls</summary>
        <select id="resolve-outcome" ${market.status === "closed" ? "" : "disabled"}>
          ${market.outcomes.map((outcome) => `<option value="${outcome.id}">${escapeHtml(outcome.label)}</option>`).join("")}
        </select>
        <button type="button" data-admin-action="close" ${market.status === "open" ? "" : "disabled"}>Close</button>
        <button type="button" data-admin-action="resolve" ${market.status === "closed" ? "" : "disabled"}>Resolve</button>
        <button type="button" data-admin-action="void" ${["open", "closed"].includes(market.status) ? "" : "disabled"}>Void</button>
      </details>
    ` : ""}
  `;
  hydrateIcons();
  renderMarketPriceChart(history);
  $("#market-detail").querySelectorAll("[data-order]").forEach((button) => {
    button.addEventListener("click", () => openOrderSheet(Number(button.dataset.order)));
  });
  $("#market-detail").querySelectorAll("[data-admin-action]").forEach((button) => {
    button.addEventListener("click", () => adminMarketAction(button.dataset.adminAction, market.id));
  });
}

function orderOutcome() {
  const market = orderMarket();
  return market?.outcomes?.find((outcome) => Number(outcome.id) === Number(state.order?.outcomeId));
}

function orderMarket() {
  if (!state.order) return null;
  return state.marketDetails[state.order.marketId]?.market
    || state.markets.find((market) => Number(market.id) === Number(state.order.marketId))
    || selectedMarket();
}

function orderQuoteKey() {
  if (!state.order) return "";
  return `${state.order.marketId}:${state.order.outcomeId}:${state.order.side}:${Number($("#order-shares")?.value || 0)}`;
}

function sharesForBudgetLocal(quantities, index, budget, liquidity, payout, priors) {
  let low = 0;
  let high = 500;
  if (lmsrCost(quantities, index, high, liquidity, payout, priors) <= budget) return high;
  for (let iteration = 0; iteration < 56; iteration += 1) {
    const midpoint = (low + high) / 2;
    if (lmsrCost(quantities, index, midpoint, liquidity, payout, priors) <= budget) low = midpoint;
    else high = midpoint;
  }
  return low;
}

function quoteOrderLocal() {
  const market = orderMarket();
  const outcome = orderOutcome();
  const amount = Number($("#order-shares").value || 0);
  if (!market || !outcome || !amount) return null;
  const outcomes = market.outcomes || [];
  const index = outcomes.findIndex((item) => Number(item.id) === Number(outcome.id));
  const quantities = outcomes.map((item) => Number(item.quantity || 0));
  const liquidity = Number(market.liquidity || 1);
  const payout = Number(market.payout || 100);
  const priors = outcomes.map((item) => Number(item.prior_probability || (1 / outcomes.length)));
  const shares = state.order.side === "buy"
    ? sharesForBudgetLocal(quantities, index, amount, liquidity, payout, priors)
    : amount;
  const delta = state.order.side === "buy" ? shares : -shares;
  const cost = lmsrCost(quantities, index, delta, liquidity, payout, priors);
  const afterQuantities = [...quantities];
  afterQuantities[index] += delta;
  const afterPrice = lmsrPrices(afterQuantities, liquidity, payout, priors)[index];
  const owned = Number(outcome.user_shares || 0);
  const cash = Number(state.session?.cash || 0);
  const cashImpact = -cost;
  const estimate = Math.abs(cost);
  const error = state.order.side === "buy" && amount > cash + 1e-9
    ? "Insufficient cash for this order."
    : state.order.side === "sell" && shares > owned + 1e-9
      ? "You cannot sell more shares than you own."
      : "";
  return {
    market,
    outcome,
    shares,
    owned,
    cash,
    payout,
    estimate,
    cashImpact,
    afterPrice,
    beforePrice: Number(outcome.price || 0),
    priceImpact: (afterPrice - Number(outcome.price || 0)) / payout,
    averageFill: estimate / Math.max(shares, 1e-9),
    error,
    marketRevision: Number(market.trade_revision || 0),
    source: "local"
  };
}

function normalizedServerQuote(payload) {
  const quote = payload?.quote;
  if (!quote) return null;
  return {
    shares: Number(quote.shares || 0),
    owned: Number(quote.owned_shares || 0),
    cash: Number(quote.cash_available || 0),
    payout: Number(quote.payout || 100),
    estimate: Number(quote.estimated_cost || 0),
    averageFill: Number(quote.average_fill_price || 0),
    cashImpact: Number(quote.cash_delta || 0),
    beforePrice: Number(quote.price_before || 0),
    afterPrice: Number(quote.price_after || 0),
    marketRevision: Number(quote.market_revision || 0),
    priceImpact: Number(quote.price_impact || 0),
    modelProbability: Number(quote.model_probability || 0),
    error: "",
    source: "server"
  };
}

async function fetchOrderQuote(key) {
  if (!state.order) return;
  try {
    const payload = await api(`/api/markets/${state.order.marketId}/quote`, {
      method: "POST",
      body: JSON.stringify({
        outcome_id: state.order.outcomeId,
        side: state.order.side,
        ...(state.order.side === "buy"
          ? { budget: Number($("#order-shares").value || 0) }
          : { shares: Number($("#order-shares").value || 0) })
      })
    });
    if (!state.order || key !== orderQuoteKey()) return;
    state.orderQuote = { key, quote: normalizedServerQuote(payload), pending: false };
    renderOrderSheet();
  } catch (error) {
    if (!state.order || key !== orderQuoteKey()) return;
    state.orderQuote = { key, quote: { ...(quoteOrderLocal() || {}), error: error.message, source: "server" }, pending: false };
    renderOrderSheet();
  }
}

function openOrderSheet(outcomeId, side = "buy") {
  const market = selectedMarket();
  const outcome = market?.outcomes?.find((item) => Number(item.id) === Number(outcomeId));
  if (!market || !outcome) return;
  state.order = { marketId: market.id, outcomeId, side, stage: "edit", pending: false, error: "" };
  state.orderQuote = null;
  $("#order-market").textContent = market.title;
  $("#order-outcome").textContent = outcome.label;
  $("#order-shares").value = side === "buy" ? "100" : "1";
  renderOrderSheet();
  openDialog($("#order-sheet"), "#order-shares");
}

function closeOrderSheet() {
  state.order = null;
  state.orderQuote = null;
  closeDialog($("#order-sheet"));
}

function renderOrderSheet() {
  if (!state.order) return;
  const outcome = orderOutcome();
  const market = orderMarket();
  const buying = state.order.side === "buy";
  $("#order-amount-label").textContent = buying ? "Credits to spend" : "Shares to sell";
  $("#order-shares").min = buying ? "10" : "0.01";
  $("#order-shares").max = buying ? String(Math.floor(Number(state.session?.cash || 10000))) : String(Math.max(0.01, Number(outcome?.user_shares || 0)));
  $("#order-shares").step = buying ? "10" : "0.01";
  const key = orderQuoteKey();
  const localQuote = quoteOrderLocal();
  const quote = state.orderQuote?.key === key ? state.orderQuote.quote : localQuote;
  const amount = Number($("#order-shares").value || 0);
  const price = Number(outcome?.price || 0);
  if (key && state.orderQuote?.key !== key) {
    state.orderQuote = { key, quote: localQuote, pending: true };
    fetchOrderQuote(key);
  }
  const confirming = state.order.stage === "confirm";
  const quotePending = Boolean(state.orderQuote?.pending);
  document.querySelectorAll("[data-order-side]").forEach((button) => {
    button.classList.toggle("active", button.dataset.orderSide === state.order.side);
    button.disabled = confirming || state.order.pending;
  });
  $("#order-shares").disabled = confirming || state.order.pending;
  $("#order-submit").textContent = state.order.pending
    ? "Placing Order..."
    : confirming
      ? `Place ${state.order.side === "buy" ? "Buy" : "Sell"}`
      : `Review ${state.order.side === "buy" ? "Buy" : "Sell"}`;
  const blockingError = localQuote?.error || "";
  $("#order-submit").disabled = Boolean(blockingError) || !amount || quotePending || state.order.pending;
  $("#order-status").textContent = state.order.pending
    ? "Placing order"
    : quotePending
      ? "Updating server quote"
      : confirming
        ? "Order reviewed and ready to place"
        : "Order details ready for review";
  $("#order-estimate").innerHTML = `
    <div class="order-summary">
      ${outcomeAvatar(outcome || {})}
      <div>
        <span>${escapeHtml(market?.title || "Market")}</span>
        <strong>${escapeHtml(outcome?.label || "Outcome")}</strong>
        <small>${pct(outcome?.probability || 0)} implied · ${money(price)} current price</small>
      </div>
    </div>
    <div class="order-review-grid">
      <article><span>${buying ? "Credits spent" : "Estimated proceeds"}${quotePending ? " · updating" : quote?.source === "server" && !quote?.error ? " · verified" : ""}</span><strong>${money(quote?.estimate || 0)}</strong></article>
      <article><span>${buying ? "Contracts received" : "Contracts sold"}</span><strong>${fmt(quote?.shares || 0)}</strong></article>
      <article><span>Average fill</span><strong>${money(quote?.averageFill || 0)}</strong></article>
      <article><span>Implied probability</span><strong>${pct(Number(quote?.beforePrice || price) / Number(quote?.payout || 100))} → ${pct(Number(quote?.afterPrice || price) / Number(quote?.payout || 100))}</strong></article>
      <article><span>Price impact</span><strong class="${Number(quote?.priceImpact || 0) > 0.05 ? "bad" : ""}">${Number(quote?.priceImpact || 0) >= 0 ? "+" : ""}${pct(quote?.priceImpact || 0)}</strong></article>
      <article><span>Max payout if wins</span><strong>${money(Math.max(0, Number(quote?.owned || 0) + (buying ? Number(quote?.shares || 0) : -Number(quote?.shares || 0))) * Number(quote?.payout || 100))}</strong></article>
    </div>
    ${Math.abs(Number(quote?.priceImpact || 0)) > 0.05 ? `<p class="order-warning">Large price move: this order changes implied probability by ${pct(Math.abs(Number(quote.priceImpact)))}.</p>` : ""}
    ${blockingError ? `<p class="order-error">${escapeHtml(blockingError)}</p>` : quote?.error ? `<p class="order-warning">${escapeHtml(`Server quote unavailable: ${quote.error}. The order will be rechecked before execution.`)}</p>` : ""}
    ${state.order.error ? `<p class="order-error">${escapeHtml(state.order.error)}</p>` : ""}
    ${confirming ? `
      <div class="order-confirmation" role="status">
        <div>
          <strong>Ready to place</strong>
          <span>${buying ? `Spend ${money(quote?.estimate || 0)} to buy ${fmt(quote?.shares || 0)} contracts` : `Sell ${fmt(quote?.shares || 0)} contracts for an estimated ${money(quote?.estimate || 0)}`}.</span>
        </div>
        <button type="button" data-edit-order>Edit</button>
      </div>
    ` : ""}
    <details class="order-rules">
      <summary>Rules and resolution</summary>
      <p>${escapeHtml(market?.resolution_rule || "Commissioner resolves this market.")}</p>
      <small>Play-money credits only. Winning shares pay ${money(quote?.payout || market?.payout || 100)} ${market?.origin === "model" ? "after automatic Sleeper settlement" : "after commissioner resolution"}.</small>
    </details>
  `;
  $("#order-estimate").querySelector("[data-edit-order]")?.addEventListener("click", () => {
    if (!state.order) return;
    state.order.stage = "edit";
    state.order.error = "";
    renderOrderSheet();
    $("#order-shares").focus();
  });
}

async function submitOrder() {
  if (!state.order) return;
  const amount = Number($("#order-shares").value || 0);
  if (!amount) return;
  if (state.order.stage !== "confirm") {
    state.order.stage = "confirm";
    state.order.error = "";
    renderOrderSheet();
    $("#order-submit").focus();
    return;
  }
  state.order.pending = true;
  state.order.error = "";
  renderOrderSheet();
  try {
    const side = state.order.side;
    const marketId = state.order.marketId;
    const approvedQuote = state.orderQuote?.quote || quoteOrderLocal();
    const shares = Number(approvedQuote?.shares || 0);
    if (!shares) throw new Error("The order does not purchase any contracts");
    const slippage = Math.max(0.01, Number(approvedQuote?.estimate || 0) * 0.002);
    await api(`/api/markets/${state.order.marketId}/${state.order.side}`, {
      method: "POST",
      body: JSON.stringify({
        outcome_id: state.order.outcomeId,
        shares,
        market_revision: Number(approvedQuote?.marketRevision || 0),
        ...(side === "buy"
          ? { max_cost: Number(approvedQuote?.estimate || 0) + slippage }
          : { min_proceeds: Math.max(0, Number(approvedQuote?.estimate || 0) - slippage) })
      })
    });
    notify(`Filled: ${side === "buy" ? "bought" : "sold"} ${shares} shares`, "success");
    closeOrderSheet();
    await refreshAll();
    await loadMarketDetail(marketId);
    state.selectedMarketId = marketId;
  } catch (error) {
    if (state.order) {
      state.order.pending = false;
      state.order.error = error.message;
      renderOrderSheet();
    }
    notify(error.message, "warn");
  }
}

async function resolveMarket(marketId, outcomeId) {
  const adminCode = $("#admin-code").value;
  try {
    await api(`/api/admin/markets/${marketId}/resolve`, {
      method: "POST",
      headers: { "X-Admin-Code": adminCode },
      body: JSON.stringify({ winning_outcome_id: outcomeId })
    });
    notify("Market resolved", "success");
    delete state.marketDetails[marketId];
    await refreshAll();
    await loadMarketDetail(marketId);
    await loadResolutionCenter();
  } catch (error) {
    $("#admin-status").textContent = error.message;
    notify(error.message, "warn");
  }
}

async function adminMarketAction(action, marketId) {
  const adminCode = $("#admin-code").value;
  if (action === "fund-prize") {
    return assignMarketPrize(marketId);
  }
  if (action === "pay-payouts") {
    return markMarketPayoutsPaid(marketId);
  }
  const options = {
    method: "POST",
    headers: { "X-Admin-Code": adminCode },
    body: action === "resolve" ? JSON.stringify({ winning_outcome_id: Number($("#resolve-outcome").value) }) : "{}"
  };
  try {
    await api(`/api/admin/markets/${marketId}/${action}`, options);
    $("#admin-status").textContent = `Market ${action} complete`;
    notify(`Commissioner ${action}: ${selectedMarket()?.title || "market"}`, "info");
    await refreshAll();
    await loadMarketDetail(marketId);
    if (action === "resolve" || action === "close" || action === "void") {
      loadResolutionCenter().catch(() => {});
    }
  } catch (error) {
    $("#admin-status").textContent = error.message;
    notify(error.message, "warn");
  }
}

async function assignMarketPrize(marketId) {
  try {
    const result = await api(`/api/admin/markets/${marketId}/fund-prize`, {
      method: "POST",
      headers: { "X-Admin-Code": $("#admin-code").value },
      body: JSON.stringify({
        league_id: $("#league-id-input").value.trim(),
        category: $("#fund-prize-category").value,
        prize_pool: Number($("#fund-prize-amount").value || 0),
        payout_mode: "proportional_shares"
      })
    });
    state.fund = result.fund;
    state.marketDetails[marketId] = { ...(state.marketDetails[marketId] || {}), market: result.market };
    $("#admin-status").textContent = "Market prize assigned";
    notify(`Assigned ${money(result.prize.prize_pool)} prize pool`, "success");
    await refreshAll();
    await loadMarketDetail(marketId);
  } catch (error) {
    $("#admin-status").textContent = error.message;
    notify(error.message, "warn");
  }
}

async function markMarketPayoutsPaid(marketId) {
  try {
    const result = await api(`/api/admin/markets/${marketId}/payouts/pay`, {
      method: "POST",
      headers: { "X-Admin-Code": $("#admin-code").value },
      body: "{}"
    });
    state.fund = result.fund;
    state.marketDetails[marketId] = { ...(state.marketDetails[marketId] || {}), market: result.market };
    $("#admin-status").textContent = "Market payouts marked paid";
    notify("Fund payout ledger updated", "success");
    await refreshAll();
    await loadMarketDetail(marketId);
  } catch (error) {
    $("#admin-status").textContent = error.message;
    notify(error.message, "warn");
  }
}

function renderPortfolio() {
  const positions = state.portfolio?.positions || [];
  const cash = Number(state.portfolio?.cash || state.session?.cash || 0);
  const demo = state.portfolio?.demo || {};
  const realized = Number(state.portfolio?.realized_profit || state.portfolio?.realized_pl || 0);
  const unrealized = Number(state.portfolio?.unrealized_profit || state.portfolio?.unrealized_pl || 0);
  const allocations = [
    { label: "Cash", detail: "Available bankroll", value: cash, color: "#64d2ff" },
    ...positions
      .filter((position) => Number(position.market_value) > 0)
      .map((position, index) => ({
        label: position.outcome_label || position.label,
        detail: position.title,
        value: Number(position.market_value),
        color: ["#30d158", "#ffd60a", "#ff9f0a", "#bf5af2", "#5e5ce6", "#ff453a", "#00c7be"][index % 7]
      }))
  ].filter((item) => item.value > 0);

  const openValue = positions.reduce((sum, position) => sum + Number(position.market_value || 0), 0);
  const totalBook = cash + openValue;
  $("#portfolio-root").innerHTML = `
    ${demo.trade_count ? `
      <div class="demo-banner">
        <strong>Demo season loaded</strong>
        <span>${demo.trade_count} sample trades · ${money(demo.volume)} demo volume</span>
      </div>
    ` : ""}
    <section class="portfolio-desk">
      <div class="portfolio-book">
        <span>Total Book</span>
        <strong>${money(totalBook)}</strong>
        <small>${positions.length} holdings · ${allocationPct(cash, totalBook)} in cash</small>
      </div>
      <div class="portfolio-stats">
        <article><span>Cash</span><strong>${money(cash)}</strong><small>Buying power</small></article>
        <article><span>Open Value</span><strong>${money(openValue)}</strong><small>Marked to market</small></article>
        <article><span>Realized P/L</span><strong class="${realized >= 0 ? "good" : "bad"}">${money(realized)}</strong><small>Closed receipts</small></article>
        <article><span>Unrealized P/L</span><strong class="${unrealized >= 0 ? "good" : "bad"}">${money(unrealized)}</strong><small>Open contracts</small></article>
      </div>
    </section>
    ${renderAllocationChart(allocations)}
    <div class="holdings-head">
      <span>Contract</span>
      <span>Exposure</span>
      <span>Shares</span>
      <span>Value</span>
    </div>
    <div class="positions-list">
      ${positions.map((position) => `
        <article class="position-row holding-row ${exposureTone(position.market_value, totalBook)}">
          ${outcomeAvatar(position)}
          <div>
            <strong>${escapeHtml(position.outcome_label || position.label)}</strong>
            <span>${escapeHtml(position.title)} · ${pct(position.probability || 0)} implied</span>
            <span class="outcome-meter" aria-hidden="true"><i style="width: ${probabilityWidth(position.probability || 0)}"></i></span>
          </div>
          <div><b>${allocationPct(position.market_value, totalBook)}</b><small>${marketGroup(position)}</small></div>
          <div><b>${fmt(position.shares)}</b><small>shares</small></div>
          <div><b>${money(position.market_value)}</b><small>${money(Number(position.price || 0))} last</small></div>
        </article>
      `).join("") || emptyState("No open positions", "Buy shares from a market to see holdings, exposure, and open value.", `<button type="button" data-empty-tab="markets">Find a Market</button>`)}
    </div>
  `;
  renderAllocationCanvas(allocations);
  $("#portfolio-root")?.querySelector("[data-empty-tab]")?.addEventListener("click", (event) => {
    activateTab(event.currentTarget.dataset.emptyTab);
  });

  const ledger = state.portfolio?.ledger || [];
  $("#ledger-root").innerHTML = ledger.map((entry) => `
    <article class="ledger-row ${entry.is_demo ? "demo" : ""}">
      <strong>${escapeHtml(ledgerTitle(entry))}</strong>
      <span>${money(entry.amount)}</span>
      <small>${entry.is_demo ? "Demo · " : ""}${escapeHtml(ledgerDetail(entry))}</small>
    </article>
  `).join("") || emptyState("No ledger entries", "Orders, settlements, and demo activity will appear here as account receipts.");
}

function renderAllocationChart(allocations) {
  const total = allocations.reduce((sum, item) => sum + item.value, 0);
  if (!total) {
    return `<div class="allocation-card">${emptyState("No allocation yet", "Cash and open positions will form your book once the first trade lands.")}</div>`;
  }
  return `
    <div class="allocation-card">
      <div class="allocation-chart">
        <canvas id="allocation-chart" aria-hidden="true"></canvas>
        <div class="allocation-total">
          <span>Total</span>
          <strong>${money(total)}</strong>
        </div>
      </div>
      <div class="allocation-legend">
        ${allocations.map((item) => `
          <article title="${escapeHtml(item.detail)}">
            <span style="--swatch:${item.color}"></span>
            <div>
              <strong>${escapeHtml(item.label)}</strong>
              <small>${escapeHtml(item.detail)}</small>
            </div>
            <b>${allocationPct(item.value, total)}</b>
          </article>
        `).join("")}
      </div>
    </div>
  `;
}

function renderAllocationCanvas(allocations) {
  const canvas = $("#allocation-chart");
  if (!canvas || !window.Chart) return;
  if (state.allocationChart) state.allocationChart.destroy();
  const total = allocations.reduce((sum, item) => sum + item.value, 0);
  const styles = getComputedStyle(document.documentElement);
  const panel = styles.getPropertyValue("--panel").trim() || "#090c11";
  state.allocationChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: allocations.map((item) => item.label),
      datasets: [{
        data: allocations.map((item) => item.value),
        backgroundColor: allocations.map((item) => item.color),
        borderColor: panel,
        borderWidth: 3,
        hoverOffset: 12,
        spacing: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: true,
          callbacks: {
            title: (items) => items[0]?.label || "",
            label: (item) => `${money(item.raw)} · ${allocationPct(item.raw, total)}`
          }
        }
      }
    }
  });
}

function renderFund() {
  const root = $("#fund-root");
  if (!root) return;
  const fund = state.fund;
  if (!fund) {
    root.innerHTML = emptyState("Fund not loaded", "Refresh the Fund to load reserves, fee totals, and the Side Quest pool.", `<button type="button" id="empty-refresh-fund">Refresh Fund</button>`);
    $("#empty-refresh-fund")?.addEventListener("click", async () => {
      try {
        await loadFund();
        notify("Fund refreshed", "success");
      } catch (error) {
        notify(error.message, "warn");
      }
    });
    return;
  }
  const summary = fund.summary || {};
  const settings = fund.settings || {};
  const allocations = fund.allocations || [];
  const teams = fund.teams || [];
  const ledger = fund.ledger || [];
  const marketPrizes = fund.market_prizes || [];
  const available = Number(summary.available_unreserved || 0);
  const sideQuestPool = Number(summary.side_quest_pool || 0);
  const reservedTotal = Number(summary.reserved_total || 0);
  const fundTotal = Number(summary.fund_total || 0);
  const committed = Number(summary.committed_market_reserves || 0);
  const reserveRatio = reservedTotal / Math.max(1, fundTotal);
  const committedRatio = committed / Math.max(1, sideQuestPool);
  hydrateFundSettings(settings);
  root.innerHTML = `
    <section class="fund-hero">
      <article>
        <span>Side Quest Pool</span>
        <strong>${money(sideQuestPool)}</strong>
        <small>Capped prize money after trophies, Cup, events, and buffer.</small>
      </article>
      <article>
        <span>Total Fund</span>
        <strong>${money(fundTotal)}</strong>
        <small>${money(summary.season_fee_total)} synced Sleeper fees this season.</small>
      </article>
      <article>
        <span>Available</span>
        <strong>${money(available)}</strong>
        <small>${money(committed)} committed to market reserves.</small>
      </article>
    </section>
    <section class="fund-risk-strip">
      <article>
        <span>Protected</span>
        <strong>${allocationPct(reservedTotal, fundTotal)}</strong>
        <div class="fund-meter"><i style="width: ${probabilityWidth(reserveRatio)}"></i></div>
      </article>
      <article>
        <span>Allocated Risk</span>
        <strong>${allocationPct(committed, sideQuestPool)}</strong>
        <div class="fund-meter"><i style="width: ${probabilityWidth(committedRatio)}"></i></div>
      </article>
      <article>
        <span>Status</span>
        <strong class="${available > 0 ? "good" : "bad"}">${available > 0 ? "Room to Allocate" : "Fully Reserved"}</strong>
        <small>Real payouts stay commissioner-managed.</small>
      </article>
    </section>
    <section class="fund-layout">
      <div class="fund-main">
        <div class="fund-card reserve-card">
          <div class="section-head"><h2>Protected Money</h2><span>${money(summary.reserved_total)} reserved</span></div>
          <div class="reserve-grid">
            <article><span>Trophies</span><strong>${money(settings.trophy_reserve)}</strong></article>
            <article><span>Commissioners Cup</span><strong>${money(settings.cup_reserve)}</strong></article>
            <article><span>Draft/Event</span><strong>${money(settings.draft_reserve)}</strong></article>
            <article><span>Safety Buffer</span><strong>${money(settings.safety_buffer)}</strong></article>
          </div>
        </div>
        <div class="fund-card">
          <div class="section-head"><h2>Market Allocation</h2><span>Credits trade, dollars cap prizes</span></div>
          <div class="fund-allocation">
            <div class="fund-chart">
              <canvas id="fund-allocation-chart" aria-label="Side quest allocation"></canvas>
            </div>
            <div class="allocation-legend compact">
              ${allocations.map((item, index) => `
                <article>
                  <span style="--swatch:${["#00c805", "#00c7be", "#ffd60a"][index % 3]}"></span>
                  <div>
                    <strong>${escapeHtml(item.label)}</strong>
                    <small>${Math.round(Number(item.pct || 0) * 100)}% · ${item.market_count || 0} markets</small>
                  </div>
                  <b>${money(item.allocated_budget)}</b>
                </article>
              `).join("")}
            </div>
          </div>
        </div>
        <div class="fund-card">
          <div class="section-head"><h2>Team Fees</h2><span>$4 trades · $2 adds</span></div>
          <div class="team-fee-list">
            ${teams.map((team) => `
              <article>
                <strong>${escapeHtml(team.team_name)}</strong>
                <span>${team.entry_count || 0} entries</span>
                <b>${money(team.fee_total)}</b>
              </article>
            `).join("") || emptyState("No team fees synced", "Sync Sleeper fees from Admin to show add and trade contributions by team.")}
          </div>
        </div>
        <div class="fund-card">
          <div class="section-head"><h2>Market Prizes</h2><span>${marketPrizes.length} assigned</span></div>
          <div class="fund-prize-list">
            ${marketPrizes.map((prize) => `
              <article>
                <div>
                  <strong>${escapeHtml(prize.market_title)}</strong>
                  <small>${escapeHtml(prize.category)} · ${escapeHtml(prize.status)} · ${prize.payout_count || 0} payouts</small>
                </div>
                <span>${money(prize.prize_pool)}</span>
                <b class="${Number(prize.pending_total || 0) > 0 ? "good" : ""}">${money(prize.pending_total || prize.payout_total || 0)}</b>
              </article>
            `).join("") || emptyState("No prize pools assigned", "Assign dollars to individual markets from the resolution tools.")}
          </div>
        </div>
      </div>
      <aside class="fund-card fund-ledger">
        <div class="section-head"><h2>Fund Ledger</h2><span>${ledger.length} recent</span></div>
        ${ledger.map((entry) => `
          <article>
            <div>
              <strong>${escapeHtml(entry.description)}</strong>
              <small>${escapeHtml(fundSourceLabel(entry.source))} · ${escapeHtml(fundEntryLabel(entry.entry_type))}</small>
            </div>
            <b class="${Number(entry.amount) >= 0 ? "good" : "bad"}">${money(entry.amount)}</b>
          </article>
        `).join("") || emptyState("No Fund entries", "Manual entries and Sleeper-derived fees will appear here.")}
      </aside>
    </section>
  `;
  renderFundCanvas(allocations);
  renderLaunchChecklist();
}

function hydrateFundSettings(settings = {}) {
  const pairs = [
    ["#fund-starting-balance", settings.starting_balance],
    ["#fund-trophy-reserve", settings.trophy_reserve],
    ["#fund-cup-reserve", settings.cup_reserve],
    ["#fund-draft-reserve", settings.draft_reserve],
    ["#fund-safety-buffer", settings.safety_buffer],
    ["#fund-core-pct", Number(settings.core_pct || 0) * 100],
    ["#fund-team-pct", Number(settings.team_pct || 0) * 100],
    ["#fund-player-pct", Number(settings.player_pct || 0) * 100]
  ];
  pairs.forEach(([selector, value]) => {
    const input = $(selector);
    if (input && value !== undefined && !input.dataset.touched) input.value = String(Math.round(Number(value) * 100) / 100);
  });
}

function renderFundCanvas(allocations) {
  const canvas = $("#fund-allocation-chart");
  if (!canvas || !window.Chart) return;
  if (state.fundChart) state.fundChart.destroy();
  const styles = getComputedStyle(document.documentElement);
  const panel = styles.getPropertyValue("--panel").trim() || "#080a0d";
  state.fundChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: allocations.map((item) => item.label),
      datasets: [{
        data: allocations.map((item) => Number(item.allocated_budget || 0)),
        backgroundColor: ["#00c805", "#00c7be", "#ffd60a"],
        borderColor: panel,
        borderWidth: 3,
        spacing: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "64%",
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) => `${money(item.raw)} side quest budget`
          }
        }
      }
    }
  });
}

async function saveFundSettings() {
  try {
    const result = await api("/api/admin/fund/settings", {
      method: "POST",
      headers: { "X-Admin-Code": $("#admin-code").value },
      body: JSON.stringify({
        league_id: $("#league-id-input").value.trim(),
        starting_balance: Number($("#fund-starting-balance").value || 0),
        trophy_reserve: Number($("#fund-trophy-reserve").value || 0),
        cup_reserve: Number($("#fund-cup-reserve").value || 0),
        draft_reserve: Number($("#fund-draft-reserve").value || 0),
        safety_buffer: Number($("#fund-safety-buffer").value || 0),
        core_pct: Number($("#fund-core-pct").value || 0) / 100,
        team_pct: Number($("#fund-team-pct").value || 0) / 100,
        player_pct: Number($("#fund-player-pct").value || 0) / 100
      })
    });
    state.fund = result;
    renderFund();
    notify("Fund settings saved", "success");
  } catch (error) {
    $("#admin-status").textContent = error.message;
    notify(error.message, "warn");
  }
}

async function syncSleeperFees() {
  try {
    const result = await api("/api/admin/fund/sync-sleeper-fees", {
      method: "POST",
      headers: { "X-Admin-Code": $("#admin-code").value },
      body: JSON.stringify({
        league_id: $("#league-id-input").value.trim(),
        start_round: 1,
        end_round: 18
      })
    });
    state.fund = result.fund;
    renderFund();
    $("#admin-status").textContent = `Synced ${result.created} new Fund fee entries`;
    notify(`Synced ${result.created} Sleeper fee entries`, result.errors?.length ? "warn" : "success");
  } catch (error) {
    $("#admin-status").textContent = error.message;
    notify(error.message, "warn");
  }
}

async function allocateSidequest() {
  try {
    const result = await api("/api/admin/fund/allocate-sidequest", {
      method: "POST",
      headers: { "X-Admin-Code": $("#admin-code").value },
      body: JSON.stringify({ league_id: $("#league-id-input").value.trim() })
    });
    state.fund = result;
    renderFund();
    notify("Side Quest pool allocated", "success");
  } catch (error) {
    $("#admin-status").textContent = error.message;
    notify(error.message, "warn");
  }
}

function renderLeaderboard() {
  const activeRows = state.leaderboard.filter((row) => row.is_active);
  const idleRows = state.leaderboard.filter((row) => !row.is_active);
  if (!activeRows.length) {
    $("#leaderboard-root").innerHTML = emptyState("No standings yet", "Trades and settlements will populate the leaderboard once participants start playing.", `<button type="button" data-empty-tab="markets">Open Markets</button>`);
    $("#leaderboard-root")?.querySelector("[data-empty-tab]")?.addEventListener("click", (event) => {
      activateTab(event.currentTarget.dataset.emptyTab);
    });
    return;
  }
  const leader = activeRows[0];
  const totalVolume = activeRows.reduce((sum, row) => sum + Number(row.open_value || 0), 0);
  const totalTrades = activeRows.reduce((sum, row) => sum + Number(row.trade_count || 0), 0);
  $("#leaderboard-root").innerHTML = `
    <section class="leaderboard-hero">
      <article>
        <span>Current Leader</span>
        <strong>${escapeHtml(leader.display_name)}</strong>
        <small class="${leader.profit >= 0 ? "good" : "bad"}">${leader.profit >= 0 ? "+" : "-"}${money(Math.abs(leader.profit))} P/L</small>
      </article>
      <article>
        <span>Active Traders</span>
        <strong>${activeRows.length}</strong>
        <small>${idleRows.length} waiting to trade</small>
      </article>
      <article>
        <span>Open Exposure</span>
        <strong>${money(totalVolume)}</strong>
        <small>${totalTrades} trades recorded</small>
      </article>
    </section>
    <section class="leaderboard-list" aria-label="Leaderboard standings">
      ${activeRows.map((row) => `
        <article class="${row.rank <= 3 ? "podium" : ""}">
          <div class="rank-badge">${row.rank}</div>
          <div class="leader-name">
            <strong>${escapeHtml(row.display_name)}</strong>
            <span>${row.trade_count || 0} trades${row.demo_trade_count ? ` · ${row.demo_trade_count} demo` : ""}</span>
          </div>
          <div><span>P/L</span><b class="${row.profit >= 0 ? "good" : "bad"}">${row.profit >= 0 ? "+" : "-"}${money(Math.abs(row.profit))}</b></div>
          <div><span>Net</span><b>${money(row.net_worth)}</b></div>
          <div><span>Open</span><b>${money(row.open_value)}</b></div>
          <div><span>Cash</span><b>${money(row.cash)}</b></div>
        </article>
      `).join("")}
    </section>
    ${idleRows.length ? `
      <details class="idle-leaders">
        <summary>${idleRows.length} owners have not traded yet</summary>
        <div>
          ${idleRows.map((row) => `<span>${escapeHtml(row.display_name)}</span>`).join("")}
        </div>
      </details>
    ` : ""}
  `;
}

function wireEvents() {
  $("#join-form").addEventListener("submit", join);
  $("#logout-button").addEventListener("click", () => {
    localStorage.removeItem("leagueMarketToken");
    state.token = "";
    location.reload();
  });
  $("#refresh-button").addEventListener("click", async () => {
    try {
      await refreshAll({ announce: true });
      notify("Market desk refreshed", "success");
    } catch (error) {
      notify(error.message, "warn");
    }
  });
  $("#theme-toggle").addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    localStorage.setItem("leagueMarketTheme", state.theme);
    applyTheme();
  });
  $("#order-close").addEventListener("click", closeOrderSheet);
  $("#order-submit").addEventListener("click", submitOrder);
  $("#order-shares").addEventListener("input", () => {
    if (state.order) {
      state.order.stage = "edit";
      state.order.error = "";
    }
    state.orderQuote = null;
    renderOrderSheet();
  });
  $("#order-sheet").addEventListener("click", (event) => {
    if (event.target.id === "order-sheet") closeOrderSheet();
  });
  document.querySelectorAll("[data-order-side]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.order) return;
      state.order.side = button.dataset.orderSide;
      $("#order-shares").value = state.order.side === "buy" ? "100" : "1";
      state.order.stage = "edit";
      state.order.error = "";
      state.orderQuote = null;
      renderOrderSheet();
    });
  });
  const ticker = $(".bottom-ticker");
  const tickerTrack = $("#ticker-track");
  let tickerDragStart = 0;
  let tickerDragOffset = 0;
  ticker.addEventListener("pointerdown", (event) => {
    ticker.setPointerCapture(event.pointerId);
    ticker.classList.add("ticker-held");
    tickerDragStart = event.clientX;
    tickerDragOffset = 0;
  });
  ticker.addEventListener("pointermove", (event) => {
    if (!ticker.classList.contains("ticker-held")) return;
    ticker.classList.add("ticker-dragging");
    tickerDragOffset = event.clientX - tickerDragStart;
    tickerTrack.style.animation = "none";
    tickerTrack.style.transform = `translateX(${tickerDragOffset}px)`;
  });
  const releaseTicker = () => {
    ticker.classList.remove("ticker-held", "ticker-dragging");
    tickerTrack.style.animation = "";
    tickerTrack.style.transform = "";
  };
  ticker.addEventListener("pointerup", releaseTicker);
  ticker.addEventListener("pointercancel", releaseTicker);
  $("#demo-season").addEventListener("click", async () => {
    try {
      const result = await api("/api/demo/populate", {
        method: "POST",
        body: JSON.stringify({ reset_seeded_if_empty: true, reset_existing: true })
      });
      notify(`Loaded ${result.trades} demo trades`, "success");
      await refreshAll();
    } catch (error) {
      notify(error.message, "warn");
    }
  });
  $("#clear-demo").addEventListener("click", async () => {
    try {
      const result = await api("/api/demo/clear", {
        method: "POST",
        body: "{}"
      });
      notify(`Cleared ${result.cleared_trades} demo trades`, "success");
      await refreshAll();
    } catch (error) {
      notify(error.message, "warn");
    }
  });
  $("#refresh-fund")?.addEventListener("click", async () => {
    try {
      await loadFund();
      notify("Fund refreshed", "success");
    } catch (error) {
      notify(error.message, "warn");
    }
  });
  document.querySelectorAll("#admin-tab input[id^='fund-']").forEach((input) => {
    input.addEventListener("input", () => {
      input.dataset.touched = "true";
    });
  });
  $("#save-fund-settings").addEventListener("click", saveFundSettings);
  $("#sync-sleeper-fees").addEventListener("click", syncSleeperFees);
  $("#allocate-sidequest").addEventListener("click", allocateSidequest);
  $("#fund-entry-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/admin/fund/manual-entry", {
        method: "POST",
        headers: { "X-Admin-Code": $("#admin-code").value },
        body: JSON.stringify({
          league_id: $("#league-id-input").value.trim(),
          entry_type: $("#fund-entry-type").value,
          amount: Number($("#fund-entry-amount").value || 0),
          description: $("#fund-entry-description").value
        })
      });
      state.fund = result;
      $("#fund-entry-form").reset();
      renderFund();
      notify("Fund entry added", "success");
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#nav-toggle").addEventListener("click", () => {
    if (isMobileNavigation()) {
      state.mobileNavOpen = !state.mobileNavOpen;
      $("#app-view").classList.toggle("mobile-nav-open", state.mobileNavOpen);
      $("#nav-toggle").setAttribute("aria-expanded", String(state.mobileNavOpen));
      $("#nav-toggle").setAttribute("aria-label", `${state.mobileNavOpen ? "Close" : "Open"} navigation menu`);
      if (state.mobileNavOpen) {
        window.requestAnimationFrame(() => $("#primary-navigation button.active")?.focus());
      }
      return;
    }
    state.navCollapsed = !state.navCollapsed;
    localStorage.setItem("leagueMarketNavCollapsed", String(state.navCollapsed));
    render();
  });
  $("#tour-button").addEventListener("click", () => showTour(0));
  $("#tour-close").addEventListener("click", closeTour);
  $("#tour-prev").addEventListener("click", () => {
    state.tourStep -= 1;
    renderTour();
  });
  $("#tour-next").addEventListener("click", () => {
    if (state.tourStep === tourSteps.length - 1) {
      closeTour();
      return;
    }
    state.tourStep += 1;
    renderTour();
  });
  $("#onboarding-overlay").addEventListener("click", (event) => {
    if (event.target.id === "onboarding-overlay") closeTour();
  });
  document.addEventListener("keydown", (event) => {
    const orderSheet = $("#order-sheet");
    const tour = $("#onboarding-overlay");
    const adminDialog = $("#admin-action-dialog");
    if (event.key === "Escape") {
      if (!adminDialog.classList.contains("hidden")) {
        closeAdminActionDialog();
        return;
      }
      if (!orderSheet.classList.contains("hidden")) {
        closeOrderSheet();
        return;
      }
      if (!tour.classList.contains("hidden")) {
        closeTour();
        return;
      }
      if (state.mobileNavOpen) {
        state.mobileNavOpen = false;
        $("#app-view").classList.remove("mobile-nav-open");
        $("#nav-toggle").setAttribute("aria-expanded", "false");
        $("#nav-toggle").focus();
      }
    }
    trapDialogFocus(event, orderSheet);
    trapDialogFocus(event, tour);
    trapDialogFocus(event, adminDialog);
  });
  window.addEventListener("resize", () => {
    if (!isMobileNavigation() && state.mobileNavOpen) {
      state.mobileNavOpen = false;
      $("#app-view").classList.remove("mobile-nav-open");
    }
  });
  $("#market-filter").addEventListener("change", renderMarkets);
  $("#market-group-filter").addEventListener("change", renderMarkets);
  $("#league-id-input").addEventListener("input", () => {
    $("#league-id-input").dataset.touched = "true";
  });
  $("#join-league-id").addEventListener("input", () => {
    $("#join-league-id").dataset.touched = "true";
  });
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      activateTab(button.dataset.tab);
    });
  });
  $("#refresh-identity").addEventListener("click", async () => {
    try {
      await loadIdentity();
      notify("Sleeper identity options refreshed", "success");
    } catch (error) {
      notify(error.message, "warn");
    }
  });
  $("#admin-session-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await withAdminButton($("#admin-unlock"), "Unlocking...", loadAdminConsole);
  });
  $("#admin-settings-open").addEventListener("click", () => {
    state.adminView = "settings";
    renderAdminShell();
  });
  $("#admin-settings-back").addEventListener("click", () => {
    state.adminView = "inbox";
    renderAdminShell();
  });
  $("#admin-logout").addEventListener("click", async () => {
    await api("/api/admin/session", { method: "DELETE" });
    state.adminSession = { authenticated: false };
    state.adminDashboard = null;
    state.adminView = "inbox";
    renderAdminShell();
    window.requestAnimationFrame(() => $("#admin-code")?.focus());
  });
  document.querySelectorAll("[data-admin-settings-tab]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.adminSettingsTab = button.dataset.adminSettingsTab;
      renderAdminSettings();
      try {
        if (state.adminSettingsTab === "people") await Promise.all([loadManagers(), loadCommissionerManagement()]);
        if (state.adminSettingsTab === "fund") await loadFund();
      } catch (error) {
        notify(error.message, "warn");
      }
    });
  });
  $("#admin-action-close").addEventListener("click", closeAdminActionDialog);
  $("#admin-action-dialog").addEventListener("click", (event) => {
    if (event.target.id === "admin-action-dialog") closeAdminActionDialog();
  });
  $("#refresh-overview").addEventListener("click", (event) => withAdminButton(event.currentTarget, "Refreshing...", async () => {
    try {
      await loadAdminDashboard();
      $("#admin-status").textContent = "Operational status refreshed";
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  }));
  $("#run-lifecycle").addEventListener("click", (event) => withAdminButton(event.currentTarget, "Checking...", async () => {
    try {
      const result = await api("/api/admin/lifecycle/run", { method: "POST", headers: adminHeaders() });
      await Promise.all([loadAdminDashboard(), refreshAll()]);
      $("#admin-status").textContent = `${result.closed} closed · ${result.resolved.length} resolved · ${result.voided.length} voided`;
      notify("Lifecycle check complete", "success");
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  }));
  $("#create-backup").addEventListener("click", (event) => withAdminButton(event.currentTarget, "Backing up...", async () => {
    try {
      const result = await api("/api/admin/maintenance/backup", { method: "POST", headers: adminHeaders() });
      await loadAdminDashboard();
      $("#admin-status").textContent = `Backup created · ${money(result.backup.bytes / 1024 / 1024)} MB`;
      notify("Database backup created", "success");
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  }));
  $("#prune-artifacts").addEventListener("click", (event) => withAdminButton(event.currentTarget, "Pruning...", async () => {
    try {
      const result = await api("/api/admin/maintenance/prune", { method: "POST", headers: adminHeaders() });
      $("#admin-status").textContent = `${result.removed} artifacts removed`;
      notify("Artifact retention applied", "success");
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  }));
  $("#setup-league").addEventListener("click", (event) => withAdminButton(event.currentTarget, "Running Pipeline...", async () => {
    try {
      $("#admin-status").textContent = "Refreshing inputs, validating projections, and calculating odds...";
      const result = await api("/api/admin/pipeline", {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({
          league_id: $("#league-id-input").value.trim()
        })
      });
      renderSetupResult(result);
      renderLeaguePreview({
        league: result.league,
        managers: result.managers,
        teams: result.managers.map((manager) => ({ team_name: manager.team_name, manager: manager.display_name }))
      });
      renderManagers(result.managers || []);
      $("#admin-status").textContent = `Published ${result.league.name}: ${result.markets.total} modeled markets`;
      notify(`League ready with ${result.markets.total} modeled markets`, "success");
      await refreshAll();
      await Promise.all([loadAdminDashboard(), loadCommissionerManagement()]);
      state.identity = null;
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  }));
  $("#preview-league").addEventListener("click", async () => {
    try {
      const leagueId = $("#league-id-input").value.trim();
      const params = new URLSearchParams({ league_id: leagueId });
      const result = await api(`/api/admin/league-preview?${params.toString()}`, {
        headers: { "X-Admin-Code": $("#admin-code").value }
      });
      renderLeaguePreview(result);
      $("#admin-status").textContent = `Previewed ${result.league.name}`;
      notify(`Loaded ${result.teams.length} teams from ${result.league.name}`, "success");
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#sync-sleeper").addEventListener("click", async () => {
    try {
      const leagueId = $("#league-id-input").value.trim();
      const result = await api("/api/admin/sync-sleeper", {
        method: "POST",
        headers: { "X-Admin-Code": $("#admin-code").value },
        body: JSON.stringify({ league_id: leagueId })
      });
      renderLeaguePreview({ league: result.league, managers: result.managers, teams: result.managers.map((manager) => ({ team_name: manager.team_name, manager: manager.display_name })) });
      renderManagers(result.managers || []);
      $("#admin-status").textContent = `Synced ${result.teams} teams and ${result.players} rostered players`;
      notify(`Sleeper sync complete: ${result.teams} teams from ${result.league.name}`, "success");
      await refreshAll();
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#refresh-managers").addEventListener("click", async () => {
    try {
      const result = await loadManagers();
      $("#admin-status").textContent = `Loaded ${result.managers.length} Sleeper managers`;
      notify(`Loaded ${result.managers.length} Sleeper usernames`, "success");
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#refresh-commissioner").addEventListener("click", async () => {
    try {
      const result = await loadCommissionerManagement();
      $("#admin-status").textContent = `Loaded ${result.participants.participants.length} participants`;
      notify("Commissioner management refreshed", "success");
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#invite-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const usesValue = $("#invite-uses").value;
      await api("/api/admin/invites", {
        method: "POST",
        headers: { "X-Admin-Code": $("#admin-code").value },
        body: JSON.stringify({
          code: $("#invite-code-new").value,
          role: $("#invite-role").value,
          uses_remaining: usesValue ? Number(usesValue) : null,
          league_id: $("#league-id-input").value.trim()
        })
      });
      $("#invite-form").reset();
      notify("Invite code created", "success");
      await loadCommissionerManagement();
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#seed-markets").addEventListener("click", async () => {
    try {
      const result = await api("/api/admin/seed-markets", {
        method: "POST",
        headers: { "X-Admin-Code": $("#admin-code").value },
        body: JSON.stringify({ reset_seeded: true, league_id: $("#league-id-input").value.trim() })
      });
      $("#admin-status").textContent = `${result.created} new · ${result.existing || 0} already published`;
      notify(`Published latest eligible markets for ${result.league.name}`, "success");
      try {
        await loadManagers();
      } catch (_) {
        renderManagers();
      }
      await refreshAll();
      await loadResolutionCenter();
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#seed-cup-markets").addEventListener("click", async () => {
    try {
      const result = await api("/api/admin/seed-cup-markets", {
        method: "POST",
        headers: { "X-Admin-Code": $("#admin-code").value },
        body: JSON.stringify({ reset_seeded: true, league_id: $("#league-id-input").value.trim() })
      });
      $("#admin-status").textContent = result.detail || "Cup markets remain in draft";
      notify(result.detail || "Cup markets remain in draft", "success");
      await refreshAll();
      await loadResolutionCenter();
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
  $("#manual-market-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/admin/markets", {
        method: "POST",
        headers: { "X-Admin-Code": $("#admin-code").value },
        body: JSON.stringify({
          title: $("#manual-title").value,
          outcomes: $("#manual-outcomes").value.split("\n"),
          resolution_rule: $("#manual-rule").value || "Commissioner resolves this market.",
          resolution_source: "Commissioner",
          liquidity_label: "medium",
          league_id: $("#league-id-input").value.trim()
        })
      });
      $("#manual-market-form").reset();
      $("#admin-status").textContent = "Manual market created";
      notify("Manual market listed on the exchange", "success");
      await refreshAll();
    } catch (error) {
      $("#admin-status").textContent = error.message;
      notify(error.message, "warn");
    }
  });
}

applyTheme();
wireEvents();
boot();
