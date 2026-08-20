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
  "WATCH":"watch",
  "NOT READY":"watch",
}[a] || "watch");

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

function finalityText(n, r) {
  const label = String(r.finality?.label || "");
  if (n.is_closing_today) {
    if (
      label.includes("POST_1430") ||
      label.includes("CANONICAL_1430")
    ) {
      return "Final closing-day call captured";
    }
    return "Closes today · final call at 2:30 PM IST";
  }
  return "Early signal · final call on closing day at 2:30 PM IST";
}

function renderHealth(h, p, live) {
  const progress = p.progress || {};
  const records = live.records || [];
  const selectedToday = records.filter(n => {
    const a = n.recommendation?.action;
    return n.is_closing_today &&
      (a === "STRONG SUBSCRIBE" || a === "SUBSCRIBE");
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
      <small>Exact 2:30 results</small>
      <strong>${fmt(progress.exact_listed_rows)} / ${fmt(p.target_exact_listed_rows || 20)}</strong>
    </div>
    <div class="stat">
      <small>2026 IPOs tracked</small>
      <strong>${fmt(h.year_tracker_rows)}</strong>
    </div>`;

  $("generatedAt").textContent = h.generated_at_ist
    ? `Last updated: ${h.generated_at_ist}`
    : "Waiting for data…";
}

function renderLive(data) {
  $("liveUpdated").textContent = data.fetched_at_ist
    ? `Updated: ${data.fetched_at_ist}`
    : "No live snapshot yet.";

  const records = [...(data.records || [])].sort((a,b) =>
    (b.recommendation?.action_priority || 0) -
    (a.recommendation?.action_priority || 0)
  );

  $("liveCards").innerHTML = records.length ? records.map(n => {
    const r = n.recommendation || {};
    const p = r.predictions || {};
    return `
      <article class="card">
        <div class="card-head">
          <div class="card-title">
            <h3>${esc(n.name || n.symbol || "IPO")}</h3>
            <div class="muted">${esc(n.type || "")}</div>
          </div>
          <span class="action ${actionClass(r.action)}">
            ${esc(r.action || "NOT READY")}
          </span>
        </div>

        <div class="metric-grid">
          <div class="metric">
            <small>Estimated gain</small>
            <strong>${fmt(r.primary_prediction_pct,"%")}</strong>
          </div>
          <div class="metric">
            <small>GMP</small>
            <strong>${fmt(p.gmp_input_pct,"%")}</strong>
          </div>
          <div class="metric">
            <small>Total subscription</small>
            <strong>${fmt(p.total_subscription_x,"x")}</strong>
          </div>
          <div class="metric">
            <small>Confidence</small>
            <strong>${esc(r.research_confidence || "—")}</strong>
          </div>
        </div>

        <div class="card-note ${n.is_closing_today ? "closing" : ""}">
          ${esc(finalityText(n, r))}
        </div>
      </article>`;
  }).join("") :
    `<div class="panel">No live IPO data is available right now.</div>`;
}

function renderTracker(t) {
  $("trackerBody").innerHTML = (t.rows || []).map(r => `
    <tr>
      <td><strong>${esc(r.name)}</strong></td>
      <td>${esc(r.ipo_type)}</td>
      <td>${esc(r.provider_status || "—")}</td>
      <td>
        <span class="action table-action ${actionClass(r.model_action)}">
          ${esc(r.model_action || "—")}
        </span>
      </td>
      <td>${esc(r.shadow_v2_action || "—")}</td>
      <td>${fmt(r.primary_prediction_pct,"%")}</td>
      <td>${fmt(r.gmp_used_pct,"%")}</td>
      <td>${fmt(r.total_x,"x")}</td>
      <td class="${trend(r.actual_listing_gain_pct)}">
        ${fmt(r.actual_listing_gain_pct,"%")}
      </td>
      <td>${esc(r.outcome_vs_call || "—")}</td>
    </tr>`).join("") ||
    `<tr><td colspan="10">No tracker rows yet.</td></tr>`;
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
      <small>Exact captures</small>
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
      <td>${esc(r.captured_at_ist || "—")}</td>
      <td>${esc(r.v1_action || "—")}</td>
      <td>${fmt(r.v1_primary_prediction_pct,"%")}</td>
      <td>${esc(r.v2_shadow_action || "—")}</td>
      <td class="${trend(r.actual_listing_gain_pct)}">
        ${fmt(r.actual_listing_gain_pct,"%")}
      </td>
      <td>${esc(r.v1_outcome || "—")}</td>
      <td>${esc(r.v2_outcome || "—")}</td>
    </tr>`).join("") ||
    `<tr><td colspan="9">No exact 2:30 PM samples yet.</td></tr>`;
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

  renderHealth(health, prospective, live);
  renderLive(live);
  renderTracker(tracker);
  renderProspective(prospective);
  renderAudit(audit);
  setupSubscription(config);
}

main();
