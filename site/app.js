const $ = id => document.getElementById(id);

const fmt = (v, suffix="") => {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n)
    ? `${n.toFixed(2).replace(/\.00$/,"")}${suffix}`
    : String(v);
};

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}[c]));

const trend = v => Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "";

const actionClass = a => ({
  "STRONG SUBSCRIBE":"strong",
  "SUBSCRIBE":"subscribe",
  "BORDERLINE":"borderline",
  "AVOID":"avoid",
  "LOCKED":"watch",
  "WATCH":"watch",
  "NOT READY":"watch",
}[a] || "watch");

const publicActionLabel = a =>
  a === "LOCKED" ? "🔒 Relevant data not available" : (a || "NOT READY");

const PUBLISHABLE_ACTIONS = new Set([
  "STRONG SUBSCRIBE",
  "SUBSCRIBE",
  "BORDERLINE",
  "AVOID",
]);

function hasObservedValue(v) {
  return v !== null && v !== undefined && v !== "";
}

function effectiveLiveSignal(n) {
  const r = n.recommendation || {};
  const p = r.predictions || {};
  const action = String(r.action || "").trim().toUpperCase();

  if (r.public_signal?.locked || action === "LOCKED") {
    return {locked:true, action:"LOCKED"};
  }

  const status = String(n.status || "").trim().toUpperCase();
  const startDate = String(n.start_date || "");
  const today = istDateKey();

  if (status !== "LIVE") return {locked:true, action:"LOCKED"};
  if (startDate && startDate > today) return {locked:true, action:"LOCKED"};
  if (!PUBLISHABLE_ACTIONS.has(action)) return {locked:true, action:"LOCKED"};

  // Client-side safety fallback for already-deployed/stale JSON. The backend
  // performs the authoritative public-signal gate. Until that JSON is
  // regenerated, never reveal a model call when the current market evidence
  // needed by the public gate is absent.
  if (!hasObservedValue(p.total_subscription_x)) {
    return {locked:true, action:"LOCKED"};
  }

  return {locked:false, action};
}

function effectiveTrackerSignal(r) {
  const action = String(r.model_action || "").trim().toUpperCase();

  if (r.public_signal?.locked || action === "LOCKED") {
    return {locked:true, action:"LOCKED"};
  }

  const openDate = String(r.issue_open || "");
  const today = istDateKey();

  if (openDate && openDate > today) return {locked:true, action:"LOCKED"};
  if (!hasObservedValue(r.total_x)) return {locked:true, action:"LOCKED"};
  if (!PUBLISHABLE_ACTIONS.has(action)) return {locked:true, action:"LOCKED"};

  return {locked:false, action};
}

function istParts(value = new Date()) {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const out = {};
  for (const p of parts) {
    if (p.type !== "literal") out[p.type] = p.value;
  }
  return out;
}

function istDateKey(value = new Date()) {
  const p = istParts(value);
  return p ? `${p.year}-${p.month}-${p.day}` : "";
}

function istClockMinutes(value = new Date()) {
  const p = istParts(value);
  return p ? Number(p.hour) * 60 + Number(p.minute) : 0;
}

function formatIstTimestamp(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).formatToParts(d);
  const get = type => parts.find(p => p.type === type)?.value || "";
  return `${get("day")} ${get("month")} ${get("year")}, ` +
    `${get("hour")}:${get("minute")} ${get("dayPeriod").toUpperCase()} IST`;
}

function timestampMs(value) {
  if (!value) return 0;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? 0 : d.getTime();
}

function newestTimestamp(...values) {
  let best = "";
  let bestMs = 0;
  for (const value of values) {
    const ms = timestampMs(value);
    if (ms > bestMs) {
      best = value;
      bestMs = ms;
    }
  }
  return best;
}

function ageMinutes(value) {
  const ms = timestampMs(value);
  if (!ms) return null;
  return Math.max(0, Math.floor((Date.now() - ms) / 60000));
}

function relativeAge(value) {
  const mins = ageMinutes(value);
  if (mins === null) return "";
  if (mins < 1) return "just now";
  if (mins === 1) return "1 min ago";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours === 1) return "1 hr ago";
  return `${hours} hrs ago`;
}

function isIstMarketWindow(value = new Date()) {
  const weekday = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
  }).format(value);
  if (!["Mon", "Tue", "Wed", "Thu", "Fri"].includes(weekday)) {
    return false;
  }
  const minutes = istClockMinutes(value);
  return minutes >= 10 * 60 && minutes <= 17 * 60 + 15;
}

function liveFreshnessText(data) {
  const value = data.fetched_at_ist;
  if (!value) return "No live snapshot yet.";

  const age = ageMinutes(value);
  let text = `Updated: ${formatIstTimestamp(value)}`;
  const relative = relativeAge(value);
  if (relative) text += ` · ${relative}`;
  if (isIstMarketWindow() && age !== null && age > 45) {
    text += " · ⚠ Data may be delayed";
  }
  return text;
}

function subscriptionEvidenceText(n) {
  const v = n.subscription_validation || {};
  if (!v.complete) return "";

  if (v.status === "CARRIED_FORWARD") {
    const age = relativeAge(v.observed_at_ist);
    return age ? `Last known · ${age}` : "Last known value";
  }

  if (v.status === "FALLBACK") {
    if (String(v.source_kind || "").startsWith("NSE")) {
      return "Exchange fallback";
    }
    return "Fallback verified";
  }

  return "";
}

function gmpDisplayText(value, quality="") {
  if (hasObservedValue(value)) return fmt(value, "%");

  const q = String(quality || "").toUpperCase();
  if (
    q.includes("ZERO_UNVERIFIED") ||
    q.includes("NOT_AVAILABLE") ||
    q === "VERIFIED_ABSENT"
  ) {
    return "Not available";
  }
  return "—";
}

function lockedDataMessage(n) {
  const v = n.subscription_validation || {};
  if (v.status === "ZERO_NOT_READY") {
    return "Bidding data not available yet.";
  }
  return "Relevant data not available.";
}

async function loadJson(path, fallback={}) {
  try {
    const r = await fetch(`${path}?t=${Date.now()}`, {cache:"no-store"});
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  } catch (e) {
    console.warn(path, e);
    return fallback;
  }
}

function setupTabs() {
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab")
        .forEach(x => x.classList.remove("active"));
      document.querySelectorAll(".tabpane")
        .forEach(x => x.classList.remove("active"));
      btn.classList.add("active");
      $(`tab-${btn.dataset.tab}`).classList.add("active");
    });
  });
}

function isClosingToday(n) {
  return Boolean(n.end_date) && String(n.end_date) === istDateKey();
}

function finalityText(n, r) {
  if (r.action === "LOCKED" || r.public_signal?.locked) {
    return "Relevant data not available.";
  }

  const endDate = String(n.end_date || "");
  const today = istDateKey();
  const label = String(r.finality?.label || "");
  const canonical = Boolean(r.finality?.canonical) ||
    label.includes("CANONICAL_1430");

  if (!endDate) {
    return "Closing date unavailable";
  }
  if (endDate < today) {
    return canonical
      ? "Bidding closed · final 2:30 PM call retained"
      : "Bidding closed";
  }
  if (endDate > today) {
    return "Early signal · final call on closing day at 2:30 PM IST";
  }

  const segment = String(n.type || "").toUpperCase();
  const isSme = segment === "SME";
  const closeMinutes = isSme ? 16 * 60 : 17 * 60;
  const closeText = isSme ? "4:00 PM IST" : "5:00 PM IST";
  const nowMinutes = istClockMinutes();

  if (nowMinutes >= closeMinutes) {
    return canonical
      ? `Bidding closed · final 2:30 PM call retained`
      : `Bidding closed · latest decision update pending`;
  }
  if (nowMinutes >= 14 * 60 + 30) {
    return canonical
      ? `Final call captured at 2:30 PM IST · bidding closes at ${closeText}`
      : `2:30 PM decision update pending · bidding closes at ${closeText}`;
  }
  return `Closes today · final call at 2:30 PM IST · bidding closes at ${closeText}`;
}

function renderHealth(h, p, live) {
  const progress = p.progress || {};
  const records = live.records || [];
  const selectedToday = records.filter(n => {
    const signal = effectiveLiveSignal(n);
    return isClosingToday(n) &&
      !signal.locked &&
      (signal.action === "STRONG SUBSCRIBE" || signal.action === "SUBSCRIBE");
  }).length;

  $("stats").innerHTML = `
    <div class="stat">
      <small>Live IPOs</small>
      <strong>${fmt(records.length)}</strong>
    </div>
    <div class="stat">
      <small>Subscribe calls today</small>
      <strong>${fmt(selectedToday)}</strong>
    </div>
    <div class="stat">
      <small>2026 IPOs tracked</small>
      <strong>${fmt(h.year_tracker_rows)}</strong>
    </div>`;

  const latestUpdate = newestTimestamp(
    h.generated_at_ist,
    live.fetched_at_ist
  );
  $("generatedAt").textContent = latestUpdate
    ? `Last updated: ${formatIstTimestamp(latestUpdate)}`
    : "Waiting for data…";
}

function renderLive(data) {
  $("liveUpdated").textContent = liveFreshnessText(data);

  const records = [...(data.records || [])].sort((a,b) =>
    (b.recommendation?.action_priority || 0) -
    (a.recommendation?.action_priority || 0)
  );

  $("liveCards").innerHTML = records.length ? records.map(n => {
    const r = n.recommendation || {};
    const p = r.predictions || {};
    const signal = effectiveLiveSignal(n);
    const publicAction = signal.action;
    const closingToday = isClosingToday(n);
    const publicPred = signal.locked ? null : r.primary_prediction_pct;
    const publicConfidence = signal.locked ? null : r.research_confidence;
    return `
      <article class="card">
        <div class="card-head">
          <div class="card-title">
            <h3>${esc(n.name || n.symbol || "IPO")}</h3>
            <div class="muted">${esc(n.type || "")}</div>
          </div>
          <span
            class="action ${actionClass(publicAction)}"
            title="${signal.locked ? "Relevant data not available" : ""}"
          >
            ${esc(publicActionLabel(publicAction))}
          </span>
        </div>

        <div class="metric-grid">
          <div class="metric">
            <small>Estimated gain</small>
            <strong>${fmt(publicPred,"%")}</strong>
          </div>
          <div class="metric">
            <small>Latest GMP</small>
            <strong>${esc(gmpDisplayText(
              p.gmp_input_pct,
              n.gmp_validation?.status || n.gmp_status || ""
            ))}</strong>
          </div>
          <div class="metric">
            <small>Latest subscription</small>
            <strong>${fmt(p.total_subscription_x,"x")}</strong>
            ${subscriptionEvidenceText(n)
              ? `<div class="muted">${esc(subscriptionEvidenceText(n))}</div>`
              : ""}
          </div>
          <div class="metric">
            <small>Confidence</small>
            <strong>${esc(publicConfidence || "—")}</strong>
          </div>
        </div>

        <div class="card-note ${closingToday ? "closing" : ""}">
          ${esc(signal.locked
            ? lockedDataMessage(n)
            : finalityText(n, r))}
        </div>
      </article>`;
  }).join("") :
    `<div class="panel">No live IPO data is available right now.</div>`;
}

function renderTracker(t) {
  $("trackerBody").innerHTML = (t.rows || []).map(r => {
    const signal = effectiveTrackerSignal(r);
    const publicAction = signal.action;
    const publicPred = signal.locked ? null : r.primary_prediction_pct;
    const publicOutcome = signal.locked ? null : r.outcome_vs_call;

    return `
    <tr>
      <td><strong>${esc(r.name)}</strong></td>
      <td>${esc(r.ipo_type)}</td>
      <td>${esc(r.provider_status || "—")}</td>
      <td>
        <span
          class="action table-action ${actionClass(publicAction)}"
          title="${signal.locked ? "Relevant data not available" : ""}"
        >
          ${esc(publicActionLabel(publicAction))}
        </span>
      </td>
      <td>${fmt(publicPred,"%")}</td>
      <td>${esc(gmpDisplayText(r.gmp_used_pct, r.gmp_quality))}</td>
      <td>${fmt(r.total_x,"x")}</td>
      <td class="${trend(r.actual_listing_gain_pct)}">
        ${fmt(r.actual_listing_gain_pct,"%")}
      </td>
      <td>${esc(publicOutcome || "—")}</td>
    </tr>`;
  }).join("") ||
    `<tr><td colspan="9">No tracker rows yet.</td></tr>`;
}

function renderProspective(p) {
  const g = p.progress || {};
  $("prospectiveStatus").innerHTML = `
    <strong>${esc(p.status || "COLLECTING")}</strong><br>
    ${esc(p.message || "")}`;

  const pct = Math.max(
    0, Math.min(100, Number(g.progress_pct || 0))
  );
  $("progressBar").style.width = `${pct}%`;

  $("prospectiveStats").innerHTML = `
    <div class="stat">
      <small>Checkpoint captures</small>
      <strong>${fmt(g.exact_captured_unique_ipos)}</strong>
    </div>
    <div class="stat">
      <small>Listed</small>
      <strong>${fmt(g.exact_listed_rows)}</strong>
    </div>
    <div class="stat">
      <small>Pending</small>
      <strong>${fmt(g.pending_listing_rows)}</strong>
    </div>
    <div class="stat">
      <small>Progress</small>
      <strong>${fmt(g.progress_pct,"%")}</strong>
    </div>`;

  $("prospectiveBody").innerHTML = (p.samples || []).map(r => `
    <tr>
      <td><strong>${esc(r.name)}</strong></td>
      <td>${esc(r.ipo_type)}</td>
      <td>${r.captured_at_ist ? esc(formatIstTimestamp(r.captured_at_ist)) : "—"}</td>
      <td>${esc(r.v1_action || "—")}</td>
      <td>${fmt(r.v1_primary_prediction_pct,"%")}</td>
      <td>${esc(r.v2_shadow_action || "—")}</td>
      <td class="${trend(r.actual_listing_gain_pct)}">
        ${fmt(r.actual_listing_gain_pct,"%")}
      </td>
      <td>${esc(r.v1_outcome || "—")}</td>
      <td>${esc(r.v2_outcome || "—")}</td>
    </tr>`).join("") ||
    `<tr><td colspan="9">No closing-day checkpoint samples yet.</td></tr>`;
}

function renderAudit(a) {
  const o = a.overall?.overall || {};
  $("auditSummary").innerHTML = `
    <div class="stat">
      <small>Listed rows</small><strong>${fmt(o.listed_rows)}</strong>
    </div>
    <div class="stat">
      <small>Positive rate</small><strong>${fmt(o.positive_rate_pct,"%")}</strong>
    </div>
    <div class="stat">
      <small>≥20% rate</small><strong>${fmt(o.ge_20_rate_pct,"%")}</strong>
    </div>
    <div class="stat">
      <small>MAE</small><strong>${fmt(o.mae_pp," pp")}</strong>
    </div>`;

  const d = a.shadow_v2?.discovery_2026 || {};
  const dp = d.triggered_performance || {};
  $("shadowSummary").innerHTML = `
    Triggers: <strong>${fmt(dp.count)}</strong> ·
    ≥20% hit: <strong>${fmt(dp.ge_20_rate_pct,"%")}</strong> ·
    Avg gain: <strong>${fmt(dp.avg_gain_pct,"%")}</strong> ·
    Worst: <strong>${fmt(dp.worst_gain_pct,"%")}</strong> ·
    Major winners recovered:
    <strong>${fmt(d.recovered_major_winners)} / ${fmt(d.v1_missed_major_winners)}</strong>`;

  const c = a.shadow_v2?.historical_crosscheck_2025 || {};
  const cp = c.triggered_performance || {};
  $("crosscheckSummary").innerHTML = `
    Available SME rows: <strong>${fmt(c.available_2025_sme_rows)}</strong> ·
    Triggers: <strong>${fmt(cp.count)}</strong> ·
    Positive: <strong>${fmt(cp.positive_rate_pct,"%")}</strong> ·
    ≥20%: <strong>${fmt(cp.ge_20_rate_pct,"%")}</strong> ·
    Avg gain: <strong>${fmt(cp.avg_gain_pct,"%")}</strong>`;
}

function setupSubscription(config) {
  const endpoint = String(
    config.subscription_endpoint || ""
  ).trim();
  const form = $("subscribeForm");
  const email = $("subscribeEmail");
  const button = $("subscribeButton");
  const status = $("subscribeStatus");

  if (!endpoint) {
    button.disabled = true;
    status.textContent =
      "Email alerts are being enabled. Please check again shortly.";
    return;
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();

    if ($("subscribeWebsite").value) {
      return;
    }

    const value = email.value.trim().toLowerCase();
    if (!value || !email.checkValidity()) {
      status.textContent = "Enter a valid email address.";
      return;
    }

    button.disabled = true;
    button.textContent = "Subscribing…";
    status.textContent = "";

    try {
      const body = new URLSearchParams({
        action: "subscribe",
        email: value,
      });
      await fetch(endpoint, {
        method: "POST",
        mode: "no-cors",
        headers: {
          "Content-Type":
            "application/x-www-form-urlencoded;charset=UTF-8",
        },
        body,
      });

      localStorage.setItem(
        "ipoAdvisorSubscribedEmail", value
      );
      email.value = "";
      status.textContent =
        "Subscription request sent. Check your inbox for confirmation.";
    } catch (err) {
      status.textContent =
        "Could not submit the subscription. Please try again.";
    } finally {
      button.disabled = false;
      button.textContent = "Subscribe";
    }
  });

  const saved = localStorage.getItem(
    "ipoAdvisorSubscribedEmail"
  );
  if (saved) {
    status.textContent =
      `Subscription previously requested for ${saved}.`;
  }
}

const dashboardState = {
  live: {records: []},
  tracker: {rows: []},
  prospective: {progress: {}, samples: []},
  audit: {},
  health: {},
  config: {subscription_endpoint: ""},
};

function rerenderLiveView() {
  renderHealth(
    dashboardState.health,
    dashboardState.prospective,
    dashboardState.live
  );
  renderLive(dashboardState.live);
}

async function refreshLiveView() {
  const live = await loadJson(
    "data/live.json",
    dashboardState.live
  );
  dashboardState.live = live;
  rerenderLiveView();
}

async function main() {
  setupTabs();

  const [
    live, tracker, prospective, audit, health, config
  ] = await Promise.all([
    loadJson("data/live.json",{records:[]}),
    loadJson("data/year_tracker.json",{rows:[]}),
    loadJson("data/prospective.json",{progress:{},samples:[]}),
    loadJson("data/audit.json",{}),
    loadJson("data/health.json",{}),
    loadJson("data/config.json",{subscription_endpoint:""}),
  ]);

  dashboardState.live = live;
  dashboardState.tracker = tracker;
  dashboardState.prospective = prospective;
  dashboardState.audit = audit;
  dashboardState.health = health;
  dashboardState.config = config;

  rerenderLiveView();
  renderTracker(tracker);
  renderProspective(prospective);
  renderAudit(audit);
  setupSubscription(config);

  // The backend publishes a lightweight live snapshot roughly every 30 min.
  // An open browser checks for a newer deployed JSON file every 5 minutes.
  setInterval(refreshLiveView, 5 * 60 * 1000);

  // Update relative-age / stale-data wording without making a network request.
  setInterval(rerenderLiveView, 60 * 1000);
}

main();
