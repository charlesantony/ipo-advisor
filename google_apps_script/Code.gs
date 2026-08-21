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
