/**
 * IPO Advisor scheduler.
 *
 * GitHub's repository cron did not reliably create scheduled workflow runs in
 * this repository. This Apps Script trigger is therefore the authoritative
 * clock. It invokes the existing GitHub workflow_dispatch endpoints.
 *
 * Required Script Property:
 *   GITHUB_ACTIONS_TOKEN = fine-grained PAT scoped to charlesantony/ipo-advisor
 *                          with repository "Actions: Read and write".
 */

const IPO_GITHUB_OWNER = "charlesantony";
const IPO_GITHUB_REPO = "ipo-advisor";
const IPO_GITHUB_REF = "main";
const IPO_TIMEZONE = "Asia/Kolkata";
const IPO_PROP_GITHUB_TOKEN = "GITHUB_ACTIONS_TOKEN";
const IPO_PROP_SCHEDULER_LEDGER = "GITHUB_SCHEDULER_LEDGER";

function installScheduler() {
  const token = PropertiesService.getScriptProperties()
    .getProperty(IPO_PROP_GITHUB_TOKEN);
  if (!token) {
    throw new Error(
      "Set Script Property GITHUB_ACTIONS_TOKEN before installing the scheduler."
    );
  }

  // Keep exactly one scheduler heartbeat.
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === "schedulerTick") {
      ScriptApp.deleteTrigger(trigger);
    }
  });

  ScriptApp.newTrigger("schedulerTick")
    .timeBased()
    .everyMinutes(5)
    .create();

  console.log("IPO Advisor scheduler installed: every 5 minutes.");
  schedulerStatus();
}

function removeScheduler() {
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === "schedulerTick") {
      ScriptApp.deleteTrigger(trigger);
      removed++;
    }
  });
  console.log("Removed scheduler triggers: " + removed);
}

function schedulerStatus() {
  const props = PropertiesService.getScriptProperties();
  const tokenConfigured = Boolean(
    props.getProperty(IPO_PROP_GITHUB_TOKEN)
  );
  const triggers = ScriptApp.getProjectTriggers()
    .filter(function(trigger) {
      return trigger.getHandlerFunction() === "schedulerTick";
    });

  console.log(
    "GITHUB_ACTIONS_TOKEN configured: " + tokenConfigured
  );
  console.log(
    "schedulerTick triggers: " + triggers.length
  );
  console.log(
    "Current IST: " +
    Utilities.formatDate(
      new Date(),
      IPO_TIMEZONE,
      "yyyy-MM-dd HH:mm:ss"
    )
  );
}

function testGithubDispatch() {
  const result = _ipoDispatchWorkflow(
    "live-refresh.yml",
    {}
  );
  console.log(JSON.stringify(result));
  if (!result.ok) {
    throw new Error(
      "GitHub workflow dispatch failed: HTTP " +
      result.status + " " + result.body
    );
  }
}

function schedulerTick() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(2000)) {
    return;
  }

  try {
    const now = new Date();
    const dateKey = Utilities.formatDate(
      now, IPO_TIMEZONE, "yyyy-MM-dd"
    );
    const weekday = Utilities.formatDate(
      now, IPO_TIMEZONE, "EEE"
    );
    const clock = Utilities.formatDate(
      now, IPO_TIMEZONE, "HH:mm"
    );
    const minutes = _ipoClockMinutes(clock);
    const isWeekday = [
      "Mon", "Tue", "Wed", "Thu", "Fri"
    ].indexOf(weekday) >= 0;

    const jobs = [];

    // Calendar-day rollover. Run every day.
    _ipoAddDueJob(
      jobs, minutes,
      0 * 60 + 10, 10,
      "rollover",
      "market-refresh.yml",
      {phase: "rollover"}
    );

    if (isWeekday) {
      _ipoAddDueJob(
        jobs, minutes,
        10 * 60 + 5, 10,
        "morning",
        "market-refresh.yml",
        {phase: "morning"}
      );

      [
        [10,35], [11,5], [11,35], [12,5], [12,35],
        [13,5], [13,35], [14,5], [15,5], [15,35],
        [16,35]
      ].forEach(function(t) {
        const label = "live-" +
          String(t[0]).padStart(2, "0") +
          String(t[1]).padStart(2, "0");
        _ipoAddDueJob(
          jobs, minutes,
          t[0] * 60 + t[1], 10,
          label,
          "live-refresh.yml",
          {}
        );
      });

      // Start early. GitHub waits in Python until exactly 2:30 PM.
      _ipoAddDueJob(
        jobs, minutes,
        14 * 60 + 15, 12,
        "decision-1430",
        "ipo-1430.yml",
        {wait_for_checkpoint: "true"}
      );

      _ipoAddDueJob(
        jobs, minutes,
        16 * 60 + 5, 10,
        "sme-close",
        "market-refresh.yml",
        {phase: "sme_close"}
      );

      _ipoAddDueJob(
        jobs, minutes,
        17 * 60 + 5, 10,
        "mainboard-close",
        "market-refresh.yml",
        {phase: "mainboard_close"}
      );

      // Outcome sync does not need an exact minute.
      _ipoAddDueJob(
        jobs, minutes,
        18 * 60 + 5, 15,
        "daily-outcome",
        "daily-sync.yml",
        {}
      );

      // Start early. GitHub waits in Python until exactly 8:30 PM.
      _ipoAddDueJob(
        jobs, minutes,
        20 * 60 + 15, 12,
        "day2-2030",
        "day2-email.yml",
        {wait_for_checkpoint: "true"}
      );
    }

    if (!jobs.length) {
      return;
    }

    let ledger = _ipoLoadLedger();
    ledger = _ipoPruneLedger(ledger, dateKey);

    jobs.forEach(function(job) {
      const ledgerKey = dateKey + "|" + job.id;
      if (ledger[ledgerKey]) {
        return;
      }

      const result = _ipoDispatchWorkflow(
        job.workflow,
        job.inputs
      );

      console.log(
        "SCHEDULER_DISPATCH " +
        ledgerKey + " " +
        JSON.stringify(result)
      );

      if (result.ok) {
        ledger[ledgerKey] =
          Utilities.formatDate(
            new Date(),
            IPO_TIMEZONE,
            "yyyy-MM-dd'T'HH:mm:ss"
          );
      }
    });

    _ipoSaveLedger(ledger);
  } finally {
    lock.releaseLock();
  }
}

function _ipoAddDueJob(
  jobs,
  currentMinutes,
  targetMinutes,
  windowMinutes,
  id,
  workflow,
  inputs
) {
  if (
    currentMinutes >= targetMinutes &&
    currentMinutes < targetMinutes + windowMinutes
  ) {
    jobs.push({
      id: id,
      workflow: workflow,
      inputs: inputs || {}
    });
  }
}

function _ipoDispatchWorkflow(workflowFile, inputs) {
  const token = PropertiesService.getScriptProperties()
    .getProperty(IPO_PROP_GITHUB_TOKEN);

  if (!token) {
    return {
      ok: false,
      status: 0,
      body: "GITHUB_ACTIONS_TOKEN is not configured"
    };
  }

  const url =
    "https://api.github.com/repos/" +
    encodeURIComponent(IPO_GITHUB_OWNER) + "/" +
    encodeURIComponent(IPO_GITHUB_REPO) +
    "/actions/workflows/" +
    encodeURIComponent(workflowFile) +
    "/dispatches";

  const response = UrlFetchApp.fetch(url, {
    method: "post",
    muteHttpExceptions: true,
    contentType: "application/json",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": "Bearer " + token,
      "X-GitHub-Api-Version": "2026-03-10"
    },
    payload: JSON.stringify({
      ref: IPO_GITHUB_REF,
      inputs: inputs || {}
    })
  });

  const status = response.getResponseCode();
  const body = response.getContentText();

  return {
    ok: status === 204,
    status: status,
    body: body
  };
}

function _ipoClockMinutes(clock) {
  const parts = String(clock || "00:00")
    .split(":")
    .map(Number);
  return (parts[0] || 0) * 60 + (parts[1] || 0);
}

function _ipoLoadLedger() {
  const raw = PropertiesService.getScriptProperties()
    .getProperty(IPO_PROP_SCHEDULER_LEDGER);
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object"
      ? value : {};
  } catch (e) {
    return {};
  }
}

function _ipoSaveLedger(ledger) {
  PropertiesService.getScriptProperties()
    .setProperty(
      IPO_PROP_SCHEDULER_LEDGER,
      JSON.stringify(ledger || {})
    );
}

function _ipoPruneLedger(ledger, todayKey) {
  const today = new Date(todayKey + "T00:00:00+05:30");
  const cutoff = new Date(
    today.getTime() - 14 * 24 * 60 * 60 * 1000
  );

  Object.keys(ledger || {}).forEach(function(key) {
    const datePart = key.split("|")[0];
    const valueDate = new Date(
      datePart + "T00:00:00+05:30"
    );
    if (
      Number.isNaN(valueDate.getTime()) ||
      valueDate < cutoff
    ) {
      delete ledger[key];
    }
  });

  return ledger || {};
}
