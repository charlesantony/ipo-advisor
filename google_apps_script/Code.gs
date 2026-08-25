const SHEET_NAME = "Subscribers";
const PROP_SHEET_ID = "SUBSCRIBER_SHEET_ID";
const PROP_ALERT_KEY = "EMAIL_ALERT_KEY";

function setup() {
  const props = PropertiesService.getScriptProperties();

  let sheetId = props.getProperty(PROP_SHEET_ID);
  let ss;
  if (!sheetId) {
    ss = SpreadsheetApp.create("IPO Advisor Subscribers");
    sheetId = ss.getId();
    props.setProperty(PROP_SHEET_ID, sheetId);
  } else {
    ss = SpreadsheetApp.openById(sheetId);
  }

  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
  }

  if (sheet.getLastRow() === 0) {
    sheet.appendRow([
      "email",
      "status",
      "subscribed_at",
      "unsubscribe_token"
    ]);
  }

  let alertKey = props.getProperty(PROP_ALERT_KEY);
  if (!alertKey) {
    alertKey =
      Utilities.getUuid().replace(/-/g, "") +
      Utilities.getUuid().replace(/-/g, "");
    props.setProperty(PROP_ALERT_KEY, alertKey);
  }

  console.log("Subscriber sheet: " + ss.getUrl());
  console.log("EMAIL_ALERT_KEY: " + alertKey);
}

function showConfig() {
  const props = PropertiesService.getScriptProperties();
  const sheetId = props.getProperty(PROP_SHEET_ID);
  console.log(
    "Subscriber sheet: " +
    (sheetId
      ? SpreadsheetApp.openById(sheetId).getUrl()
      : "Run setup() first")
  );
  console.log(
    "EMAIL_ALERT_KEY: " +
    (props.getProperty(PROP_ALERT_KEY) || "Run setup() first")
  );
  console.log(
    "Web app URL: " +
    (ScriptApp.getService().getUrl() || "Deploy as Web app first")
  );
}

function doPost(e) {
  const action = _param(e, "action");

  if (action === "subscribe") {
    return _subscribe(_param(e, "email"));
  }

  if (action === "notify_batch") {
    return _notifyBatch(e);
  }

  if (action === "notify") {
    return _notify(e);
  }

  return _json({
    ok: false,
    error: "Unsupported action"
  });
}

function doGet(e) {
  const action = String(
    (e && e.parameter && e.parameter.action) || ""
  ).trim();

  if (action === "unsubscribe") {
    return _unsubscribe(
      String(e.parameter.email || ""),
      String(e.parameter.token || "")
    );
  }

  return HtmlService.createHtmlOutput(
    "<h2>IPO Advisor email alerts</h2>" +
    "<p>The notification service is running.</p>"
  );
}

function _subscribe(rawEmail) {
  const email = _cleanEmail(rawEmail);
  if (!email) {
    return _json({
      ok: false,
      error: "Invalid email"
    });
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);

  try {
    const sheet = _sheet();
    const rows = sheet.getDataRange().getValues();

    for (let i = 1; i < rows.length; i++) {
      if (
        String(rows[i][0]).toLowerCase() === email
      ) {
        sheet.getRange(i + 1, 2).setValue("ACTIVE");
        _sendConfirmation(
          email,
          String(rows[i][3] || "")
        );
        return _json({
          ok: true,
          status: "already_subscribed"
        });
      }
    }

    const token =
      Utilities.getUuid().replace(/-/g, "") +
      Utilities.getUuid().replace(/-/g, "");

    sheet.appendRow([
      email,
      "ACTIVE",
      new Date(),
      token
    ]);

    _sendConfirmation(email, token);

    return _json({
      ok: true,
      status: "subscribed"
    });
  } finally {
    lock.releaseLock();
  }
}

function _sendConfirmation(email, token) {
  const webAppUrl = ScriptApp.getService().getUrl();
  let unsubscribe = "";
  if (webAppUrl && token) {
    unsubscribe =
      webAppUrl +
      "?action=unsubscribe&email=" +
      encodeURIComponent(email) +
      "&token=" +
      encodeURIComponent(token);
  }

  const text = [
    "You are subscribed to IPO Advisor research alerts.",
    "",
    "If a Day-2 signal is Subscribe or Strong Subscribe, you may receive an early alert at about 8:30 PM IST.",
    "If an early alert was sent, you will also receive the closing-day 2:30 PM IST update even if the signal changes.",
    "Otherwise, closing-day email is sent when the 2:30 PM signal is Subscribe or Strong Subscribe.",
    "",
    "This is an experimental research tool, not financial advice.",
    unsubscribe ? "" : null,
    unsubscribe ? "Unsubscribe: " + unsubscribe : null
  ].filter(x => x !== null).join("\n");

  MailApp.sendEmail({
    to: email,
    subject: "IPO Advisor email alerts — subscription confirmed",
    body: text
  });
}

function _notifyBatch(e) {
  const suppliedKey = _param(e, "key");
  const expectedKey = PropertiesService.getScriptProperties()
    .getProperty(PROP_ALERT_KEY);

  if (!expectedKey || suppliedKey !== expectedKey) {
    return _json({ok: false, error: "Unauthorized"});
  }

  let alerts;
  try {
    alerts = JSON.parse(_param(e, "alerts_json") || "[]");
  } catch (err) {
    return _json({ok: false, error: "Invalid alerts_json"});
  }
  if (!Array.isArray(alerts) || !alerts.length) {
    return _json({ok: false, error: "No alerts supplied"});
  }

  const batch = {
    kind: (_param(e, "batch_kind") || "CLOSING").toUpperCase(),
    date: _param(e, "batch_date"),
    dashboardUrl: _param(e, "dashboard_url"),
    alerts: alerts.map(_normalizeBatchAlert)
  };

  const subscribers = _activeSubscribers();
  if (!subscribers.length) {
    return _json({
      ok: true, sent: 0, subscribers: 0,
      alerts: batch.alerts.length,
      message: "No active subscribers"
    });
  }

  let remaining = MailApp.getRemainingDailyQuota();
  let sent = 0;
  const failed = [];
  for (const email of subscribers) {
    if (remaining <= 0) {
      failed.push({email: email, error: "MailApp daily quota exhausted"});
      continue;
    }
    try {
      MailApp.sendEmail({
        to: email,
        subject: _batchSubject(batch),
        body: _batchText(batch),
        htmlBody: _batchHtml(batch)
      });
      sent++;
      remaining--;
    } catch (err) {
      failed.push({email: email, error: String(err)});
    }
  }

  return _json({
    ok: failed.length === 0 && sent === subscribers.length,
    sent: sent,
    subscribers: subscribers.length,
    alerts: batch.alerts.length,
    failed: failed.length,
    quota_remaining: remaining
  });
}

function _normalizeBatchAlert(a) {
  a = a || {};
  return {
    ipoName: String(a.name || "IPO"),
    symbol: String(a.symbol || ""),
    segment: String(a.segment || ""),
    signal: String(a.signal || ""),
    predictedGain: String(a.predicted_gain || "N/A"),
    gmp: String(a.gmp || "N/A"),
    totalSubscription: String(a.total_subscription || "N/A"),
    closingDate: String(a.closing_date || ""),
    alertKind: String(a.alert_kind || ""),
    previousSignal: String(a.previous_signal || "")
  };
}

function _displayDate(value) {
  const m = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return String(value || "Date unavailable");
  const months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
  ];
  return Number(m[3]) + " " + (months[Number(m[2]) - 1] || m[2]) + " " + m[1];
}

function _batchSubject(batch) {
  const date = _displayDate(batch.date);
  return batch.kind === "DAY2"
    ? "IPO Advisor — Day-2 Signals — " + date
    : "IPO Advisor — Closing-Day Final Recommendations — " + date;
}

function _batchTitle(batch) {
  return batch.kind === "DAY2"
    ? "IPO Advisor — Day-2 Signals"
    : "IPO Advisor — Closing-Day Final Recommendations";
}

function _signalChanged(a) {
  return Boolean(
    a.previousSignal && a.signal && a.previousSignal !== a.signal
  );
}

function _batchText(batch) {
  const lines = [_batchTitle(batch), "Date: " + _displayDate(batch.date), ""];
  batch.alerts.forEach(function(a, index) {
    const symbol = a.symbol ? " / " + a.symbol : "";
    lines.push((index + 1) + ". " + a.ipoName + symbol + " (" + a.segment + ")");
    lines.push(
      "Recommendation: " + a.signal +
      (_signalChanged(a) ? " (Day-2: " + a.previousSignal + ")" : "")
    );
    lines.push("Estimated listing gain: " + a.predictedGain);
    lines.push("GMP: " + a.gmp);
    lines.push("Total subscription: " + a.totalSubscription);
    lines.push("Closing date: " + _displayDate(a.closingDate));
    lines.push("");
  });

  lines.push(batch.kind === "DAY2"
    ? "These are Day-2 research signals. The closing-day decision will be refreshed at the scheduled 2:30 PM IST checkpoint."
    : "These are the closing-day checkpoint recommendations. If a Day-2 signal changed, the earlier signal is shown for comparison."
  );
  if (batch.dashboardUrl) {
    lines.push("", "Dashboard: " + batch.dashboardUrl);
  }
  lines.push("", "Research signal only. This is not investment advice. Verify all IPO information independently before applying.");
  return lines.join("\n");
}

function _batchHtml(batch) {
  const rows = batch.alerts.map(function(a) {
    const symbol = a.symbol
      ? '<div style="font-size:11px;color:#667085">' + _html(a.symbol) + '</div>'
      : "";
    const previous = _signalChanged(a)
      ? '<div style="font-size:11px;color:#667085">Day-2: ' + _html(a.previousSignal) + '</div>'
      : "";
    const cell = 'padding:8px;border-bottom:1px solid #e5e7eb;';
    return '<tr>' +
      '<td style="' + cell + '"><strong>' + _html(a.ipoName) + '</strong>' + symbol + '</td>' +
      '<td style="' + cell + '">' + _html(a.segment) + '</td>' +
      '<td style="' + cell + '"><strong>' + _html(a.signal || "NOT READY") + '</strong>' + previous + '</td>' +
      '<td style="' + cell + 'white-space:nowrap">' + _html(a.predictedGain) + '</td>' +
      '<td style="' + cell + 'white-space:nowrap">' + _html(a.gmp) + '</td>' +
      '<td style="' + cell + 'white-space:nowrap">' + _html(a.totalSubscription) + '</td>' +
      '<td style="' + cell + 'white-space:nowrap">' + _html(_displayDate(a.closingDate)) + '</td>' +
      '</tr>';
  }).join("");

  const note = batch.kind === "DAY2"
    ? "These are Day-2 research signals. The closing-day decision will be refreshed at the scheduled 2:30 PM IST checkpoint."
    : "These are the closing-day checkpoint recommendations. If a Day-2 signal changed, the earlier signal is shown below the current recommendation.";
  const dashboard = batch.dashboardUrl
    ? '<p><a href="' + _html(batch.dashboardUrl) + '">Open IPO Advisor</a></p>'
    : "";

  return '<div style="font-family:Arial,sans-serif;line-height:1.45;color:#182230">' +
    '<h2 style="margin-bottom:4px">' + _html(_batchTitle(batch)) + '</h2>' +
    '<p style="margin-top:0;color:#667085">' + _html(_displayDate(batch.date)) + '</p>' +
    '<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;font-size:12px">' +
    '<thead><tr style="background:#f7f9fb;text-align:left">' +
    '<th style="padding:8px">IPO / Symbol</th><th style="padding:8px">Type</th>' +
    '<th style="padding:8px">Recommendation</th><th style="padding:8px">Est. Gain</th>' +
    '<th style="padding:8px">GMP</th><th style="padding:8px">Subscription</th>' +
    '<th style="padding:8px">Closing Date</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
    '<p style="margin-top:14px">' + _html(note) + '</p>' + dashboard +
    '<hr><p style="font-size:12px;color:#666">Research signal only. This is an experimental tool, not investment advice. Verify all IPO information independently before applying.</p></div>';
}

function _notify(e) {
  const suppliedKey = _param(e, "key");
  const expectedKey =
    PropertiesService.getScriptProperties()
      .getProperty(PROP_ALERT_KEY);

  if (!expectedKey || suppliedKey !== expectedKey) {
    return _json({
      ok: false,
      error: "Unauthorized"
    });
  }

  const fields = {
    ipoName: _param(e, "ipo_name") || "IPO",
    segment: _param(e, "segment"),
    signal: _param(e, "signal"),
    predictedGain: _param(e, "predicted_gain"),
    gmp: _param(e, "gmp"),
    totalSubscription:
      _param(e, "total_subscription"),
    dashboardUrl: _param(e, "dashboard_url"),
    alertKind: _param(e, "alert_kind") || "CLOSING_DAY",
    previousSignal: _param(e, "previous_signal")
  };

  const subscribers = _activeSubscribers();
  if (!subscribers.length) {
    return _json({
      ok: true,
      sent: 0,
      message: "No active subscribers"
    });
  }

  let remaining = MailApp.getRemainingDailyQuota();
  let sent = 0;
  const failed = [];

  for (const email of subscribers) {
    if (remaining <= 0) {
      break;
    }

    try {
      MailApp.sendEmail({
        to: email,
        subject: _alertSubject(fields),
        body: _alertText(fields),
        htmlBody: _alertHtml(fields)
      });
      sent++;
      remaining--;
    } catch (err) {
      failed.push({
        email: email,
        error: String(err)
      });
    }
  }

  return _json({
    ok: failed.length === 0 &&
        sent === subscribers.length,
    sent: sent,
    subscribers: subscribers.length,
    failed: failed.length,
    quota_remaining: remaining
  });
}

function _alertSubject(f) {
  let prefix = "IPO Advisor closing-day signal";
  if (f.alertKind === "DAY2_EARLY") {
    prefix = "IPO Advisor Day-2 early signal";
  } else if (f.alertKind === "CLOSING_UPDATE") {
    prefix = "IPO Advisor closing-day update";
  }
  return prefix + ": " + f.ipoName + " — " + f.signal;
}

function _alertTitle(f) {
  if (f.alertKind === "DAY2_EARLY") {
    return "IPO Advisor — Day-2 Early Signal";
  }
  if (f.alertKind === "CLOSING_UPDATE") {
    return "IPO Advisor — Closing-Day Update";
  }
  return "IPO Advisor — Closing-Day Signal";
}

function _signalLines(f) {
  if (f.alertKind === "CLOSING_UPDATE" && f.previousSignal) {
    return [
      "Day-2 signal: " + f.previousSignal,
      "Current 2:30 PM signal: " + f.signal
    ];
  }
  if (f.alertKind === "DAY2_EARLY") {
    return [
      "Day-2 signal: " + f.signal,
      "Closing-day decision will be refreshed at 2:30 PM IST."
    ];
  }
  return ["2:30 PM signal: " + f.signal];
}

function _alertText(f) {
  return [
    _alertTitle(f),
    "",
    f.ipoName + " (" + f.segment + ")",
    ..._signalLines(f),
    "Estimated listing gain: " + f.predictedGain,
    "GMP: " + f.gmp,
    "Total subscription: " + f.totalSubscription,
    "",
    f.dashboardUrl
      ? "Dashboard: " + f.dashboardUrl
      : "",
    "",
    "Research signal only. This is not investment advice. Verify all IPO information independently before applying."
  ].filter(Boolean).join("\n");
}

function _alertHtml(f) {
  const dashboard = f.dashboardUrl
    ? '<p><a href="' +
      _html(f.dashboardUrl) +
      '">Open IPO Advisor</a></p>'
    : "";

  const signalHtml = _signalLines(f).map(function(line) {
    return '<div><strong>' + _html(line) + '</strong></div>';
  }).join("");

  return (
    '<div style="font-family:Arial,sans-serif;line-height:1.5">' +
    '<h2 style="margin-bottom:6px">' + _html(_alertTitle(f)) + '</h2>' +
    '<h3>' + _html(f.ipoName) + '</h3>' +
    '<p><strong>' + _html(f.segment) + '</strong></p>' +
    '<div style="font-size:17px;margin:12px 0">' + signalHtml + '</div>' +
    '<p>Estimated listing gain: <strong>' +
      _html(f.predictedGain) + '</strong><br>' +
    'GMP: <strong>' + _html(f.gmp) + '</strong><br>' +
    'Total subscription: <strong>' +
      _html(f.totalSubscription) + '</strong></p>' +
    dashboard +
    '<hr>' +
    '<p style="font-size:12px;color:#666">' +
    'Research signal only. This is an experimental tool, not investment advice. ' +
    'Verify all IPO information independently before applying.' +
    '</p></div>'
  );
}

function _unsubscribe(rawEmail, token) {
  const email = _cleanEmail(rawEmail);
  if (!email || !token) {
    return HtmlService.createHtmlOutput(
      "<h3>Invalid unsubscribe link.</h3>"
    );
  }

  const sheet = _sheet();
  const rows = sheet.getDataRange().getValues();

  for (let i = 1; i < rows.length; i++) {
    if (
      String(rows[i][0]).toLowerCase() === email &&
      String(rows[i][3]) === token
    ) {
      sheet.getRange(i + 1, 2)
        .setValue("UNSUBSCRIBED");
      return HtmlService.createHtmlOutput(
        "<h2>Unsubscribed</h2>" +
        "<p>You will no longer receive IPO Advisor alerts.</p>"
      );
    }
  }

  return HtmlService.createHtmlOutput(
    "<h3>The unsubscribe link is invalid or expired.</h3>"
  );
}

function _activeSubscribers() {
  const rows = _sheet().getDataRange().getValues();
  const out = [];
  const seen = {};

  for (let i = 1; i < rows.length; i++) {
    const email =
      String(rows[i][0] || "").trim().toLowerCase();
    const status =
      String(rows[i][1] || "").trim().toUpperCase();

    if (
      email &&
      status === "ACTIVE" &&
      !seen[email]
    ) {
      seen[email] = true;
      out.push(email);
    }
  }

  return out;
}

function _sheet() {
  const sheetId =
    PropertiesService.getScriptProperties()
      .getProperty(PROP_SHEET_ID);

  if (!sheetId) {
    throw new Error(
      "Subscriber sheet is not configured. Run setup() first."
    );
  }

  const ss = SpreadsheetApp.openById(sheetId);
  let sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow([
      "email",
      "status",
      "subscribed_at",
      "unsubscribe_token"
    ]);
  }

  return sheet;
}

function _param(e, name) {
  return String(
    (e && e.parameter && e.parameter[name]) || ""
  ).trim();
}

function _cleanEmail(raw) {
  const value =
    String(raw || "").trim().toLowerCase();

  if (
    value.length < 5 ||
    value.length > 254 ||
    !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)
  ) {
    return "";
  }

  return value;
}

function _json(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function _html(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
