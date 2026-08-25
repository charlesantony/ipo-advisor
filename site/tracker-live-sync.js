/*
 * IPO Advisor tracker/live consistency layer.
 *
 * Loaded after app.js. It deliberately overrides only presentation functions:
 * - tracker lifecycle status is derived from actual dates in IST
 * - active tracker rows use the latest live market data
 * - after 2:30 PM, live.json already retains the canonical decision, so the
 *   tracker continues to display that captured signal while market data updates
 * - Mainboard public guidance uses 4:30 PM IST as the recommended apply-by time
 * - SME retains the 4:00 PM IST bidding-close guidance
 */

function trackerCanon(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\(india\)/g, " ")
    .replace(/\b(limited|ltd)\b/g, " ")
    .replace(/[^a-z0-9]+/g, "");
}

function shortIstDate(dateKey) {
  if (!dateKey) return "";
  const d = new Date(`${dateKey}T00:00:00+05:30`);
  if (Number.isNaN(d.getTime())) return String(dateKey);
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
  }).format(d);
}

function isPubliclyLiveRecord(n) {
  const endDate = String(n?.end_date || "");
  return !endDate || endDate >= istDateKey();
}

const effectiveLiveSignalBeforeV059 = effectiveLiveSignal;
effectiveLiveSignal = function(n) {
  const signal = effectiveLiveSignalBeforeV059(n);
  if (signal.locked) return signal;

  const p = n?.recommendation?.predictions || {};
  const validation = n?.gmp_validation || {};
  if (
    !hasObservedValue(p.gmp_input_pct) &&
    validation.complete !== true
  ) {
    return {locked:true, action:"LOCKED"};
  }
  return signal;
};

function trackerLiveMap(live) {
  const map = new Map();
  for (const n of (live?.records || [])) {
    if (!isPubliclyLiveRecord(n)) continue;
    const key = trackerCanon(n.name || n.symbol);
    if (key) map.set(key, n);
  }
  return map;
}

function trackerLiveRecord(row, liveMap) {
  return liveMap.get(trackerCanon(row.name)) || null;
}

function trackerLifecycleStatus(row, liveRecord=null) {
  const today = istDateKey();
  const nowMinutes = istClockMinutes();

  const segment = String(
    liveRecord?.type || row.ipo_type || ""
  ).trim().toUpperCase();

  const openDate = String(
    liveRecord?.start_date || row.issue_open || ""
  );
  const closeDate = String(
    liveRecord?.end_date || row.issue_close || ""
  );

  const provider = String(row.provider_status || "").toLowerCase();
  const listed = hasObservedValue(row.actual_listing_gain_pct) ||
    provider.includes("listed");

  if (listed) {
    if (!hasObservedValue(row.actual_listing_gain_pct)) {
      return "Listed · Result pending";
    }
    const gain = Number(row.actual_listing_gain_pct);
    return Number.isFinite(gain)
      ? `Listed · ${gain >= 0 ? "+" : ""}${fmt(gain,"%")}`
      : "Listed";
  }

  if (openDate && today < openDate) {
    return `Upcoming · Opens ${shortIstDate(openDate)}`;
  }

  if (closeDate && today > closeDate) {
    return `Bidding closed · Closed ${shortIstDate(closeDate)}`;
  }

  if (openDate && closeDate && today >= openDate && today < closeDate) {
    return `Open · Closes ${shortIstDate(closeDate)}`;
  }

  if (closeDate && today === closeDate) {
    if (segment === "SME") {
      if (nowMinutes >= 16 * 60) {
        return `Bidding closed · ${shortIstDate(closeDate)}`;
      }
      return "Closing today · Bidding closes 4:00 PM IST";
    }

    // Mainboard: 4:30 PM is intentionally guidance for users, not a claim
    // that the exchange's universal bidding session ends at 4:30 PM.
    if (nowMinutes >= 16 * 60 + 30) {
      return `Apply-by time passed · ${shortIstDate(closeDate)}`;
    }
    return "Closing today · Apply by 4:30 PM IST";
  }

  // Avoid stale relative provider text such as "opens in 4d" or
  // "closes tomorrow" when exact dates are unavailable.
  if (provider.includes("open")) return "Upcoming";
  if (provider.includes("close")) return "Open";
  if (provider.includes("allot")) return "Bidding closed";
  return "—";
}

function trackerDisplayState(row, liveRecord=null) {
  if (liveRecord) {
    const rec = liveRecord.recommendation || {};
    const preds = rec.predictions || {};
    const signal = effectiveLiveSignal(liveRecord);
    return {
      signal,
      pred: signal.locked ? null : rec.primary_prediction_pct,
      gmp: preds.gmp_input_pct,
      gmpQuality:
        liveRecord.gmp_validation?.status ||
        liveRecord.gmp_status ||
        "",
      total: preds.total_subscription_x,
      confidence: signal.locked ? null : rec.research_confidence,
      source: "LIVE",
    };
  }

  const signal = effectiveTrackerSignal(row);
  return {
    signal,
    pred: signal.locked ? null : row.primary_prediction_pct,
    gmp: row.gmp_used_pct,
    gmpQuality: row.gmp_quality || "",
    total: row.total_x,
    confidence: signal.locked ? null : row.model_confidence,
    source: row.decision_source || "TRACKER",
  };
}

/*
 * Override the live-card timing text.
 * The canonical signal remains 2:30 PM IST for both market segments.
 */
finalityText = function(n, r) {
  if (r.action === "LOCKED" || r.public_signal?.locked) {
    return "Relevant data not available.";
  }

  const endDate = String(n.end_date || "");
  const today = istDateKey();
  const label = String(r.finality?.label || "");
  const canonical = Boolean(r.finality?.canonical) ||
    label.includes("CANONICAL_1430");
  const nowMinutes = istClockMinutes();

  if (!endDate) return "Closing date unavailable";

  if (endDate < today) {
    return canonical
      ? "Bidding closed · final 2:30 PM research signal retained"
      : "Bidding closed";
  }

  if (endDate > today) {
    return "Early signal · final research signal on closing day at 2:30 PM IST";
  }

  const segment = String(n.type || "").toUpperCase();

  if (segment === "SME") {
    if (nowMinutes >= 16 * 60) {
      return canonical
        ? "Bidding closed · final 2:30 PM research signal retained"
        : "Bidding closed · latest decision update pending";
    }
    if (nowMinutes >= 14 * 60 + 30) {
      return canonical
        ? "Final signal captured at 2:30 PM IST · bidding closes at 4:00 PM IST"
        : "2:30 PM decision update pending · bidding closes at 4:00 PM IST";
    }
    return "Closing today · final signal at 2:30 PM IST · bidding closes at 4:00 PM IST";
  }

  if (nowMinutes >= 16 * 60 + 30) {
    return canonical
      ? "Recommended apply-by time passed · final 2:30 PM research signal retained"
      : "Recommended apply-by time passed · latest decision update pending";
  }
  if (nowMinutes >= 14 * 60 + 30) {
    return canonical
      ? "Final signal captured at 2:30 PM IST · recommended apply by 4:30 PM IST"
      : "2:30 PM decision update pending · recommended apply by 4:30 PM IST";
  }
  return "Closing today · final signal at 2:30 PM IST · recommended apply by 4:30 PM IST";
};

/*
 * Override Tracker rendering:
 * - current live record wins for market inputs and public signal
 * - exact issue dates win for lifecycle/status wording
 * - once the 2:30 canonical signal exists, live.json already carries it, so
 *   later market updates do not overwrite the recorded research decision
 */
renderTracker = function(t) {
  const liveMap = trackerLiveMap(dashboardState.live);

  $("trackerBody").innerHTML = (t.rows || []).map(row => {
    const liveRecord = trackerLiveRecord(row, liveMap);
    const d = trackerDisplayState(row, liveRecord);
    const signal = d.signal;
    const publicAction = signal.action;
    const publicOutcome = signal.locked ? null : row.outcome_vs_call;
    const status = trackerLifecycleStatus(row, liveRecord);

    return `
    <tr>
      <td><strong>${esc(row.name)}</strong></td>
      <td>${esc(row.ipo_type)}</td>
      <td>${esc(status)}</td>
      <td>
        <span
          class="action table-action ${actionClass(publicAction)}"
          title="${signal.locked ? "Relevant data not available" : ""}"
        >
          ${esc(publicActionLabel(publicAction))}
        </span>
      </td>
      <td>${fmt(d.pred,"%")}</td>
      <td>${esc(gmpDisplayText(d.gmp, d.gmpQuality))}</td>
      <td>${fmt(d.total,"x")}</td>
      <td class="${trend(row.actual_listing_gain_pct)}">
        ${fmt(row.actual_listing_gain_pct,"%")}
      </td>
      <td>${esc(publicOutcome || "—")}</td>
    </tr>`;
  }).join("") ||
    `<tr><td colspan="9">No tracker rows yet.</td></tr>`;
};

/*
 * Ensure a newly deployed live.json refresh also updates the Tracker tab,
 * even when year_tracker.json itself has not been regenerated yet.
 */
rerenderLiveView = function() {
  const publicLive = {
    ...dashboardState.live,
    records: (dashboardState.live?.records || [])
      .filter(isPubliclyLiveRecord),
  };
  renderHealth(
    dashboardState.health,
    dashboardState.prospective,
    publicLive
  );
  renderLive(publicLive);
  renderTracker(dashboardState.tracker);
  renderListed(dashboardState.listed);
};
