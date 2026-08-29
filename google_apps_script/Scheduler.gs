/**
 * IPO Advisor critical scheduler.
 *
 * Do not rely on GitHub Actions `schedule:` for time-critical IPO alerts.
 * This Apps Script trigger runs every minute and dispatches the existing
 * GitHub workflows through workflow_dispatch.
 *
 * Required Script Property:
 *   GITHUB_DISPATCH_TOKEN
 */

const IPO_SCHEDULER = Object.freeze({
  timezone: "Asia/Kolkata",
  owner: "charlesantony",
  repo: "ipo-advisor",
  ref: "main",
  tickFunction: "ipoCriticalSchedulerTick",
  tokenProperty: "GITHUB_DISPATCH_TOKEN",
  lastTickProperty: "IPO_SCHEDULER_LAST_TICK_IST",
  decisionDateProperty: "IPO_SCHEDULER_1430_DATE",
  decisionAtProperty: "IPO_SCHEDULER_1430_DISPATCHED_AT",
  decisionRunProperty: "IPO_SCHEDULER_1430_RUN_URL",
  day2DateProperty: "IPO_SCHEDULER_DAY2_DATE",
  day2AtProperty: "IPO_SCHEDULER_DAY2_DISPATCHED_AT",
  day2RunProperty: "IPO_SCHEDULER_DAY2_RUN_URL",
  decisionStart: 1430,
  decisionDeadline: 1530,
  day2Start: 2030,
  day2Deadline: 2200,
});


function installCriticalScheduler() {
  uninstallCriticalScheduler();

  ScriptApp.newTrigger(IPO_SCHEDULER.tickFunction)
    .timeBased()
    .everyMinutes(1)
    .create();

  console.log(
    "IPO critical scheduler installed: every minute, timezone gate=" +
    IPO_SCHEDULER.timezone
  );
  return criticalSchedulerStatus();
}


function uninstallCriticalScheduler() {
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (
      trigger.getHandlerFunction &&
      trigger.getHandlerFunction() === IPO_SCHEDULER.tickFunction
    ) {
      ScriptApp.deleteTrigger(trigger);
      removed++;
    }
  });
  console.log("Removed existing IPO critical scheduler triggers: " + removed);
  return removed;
}


function testGithubDispatchAccess() {
  const token = _ipoGithubToken_();

  const workflows = [
    "ipo-1430.yml",
    "day2-email.yml"
  ];

  const result = workflows.map(function(workflow) {
    const url = _ipoWorkflowUrl_(workflow);
    const response = UrlFetchApp.fetch(url, {
      method: "get",
      headers: _ipoGithubHeaders_(token),
      muteHttpExceptions: true,
    });
    const code = response.getResponseCode();
    return {
      workflow: workflow,
      httpStatus: code,
      ok: code >= 200 && code < 300,
    };
  });

  console.log(JSON.stringify(result, null, 2));

  if (result.some(function(item) { return !item.ok; })) {
    throw new Error(
      "GitHub token test failed. Confirm GITHUB_DISPATCH_TOKEN and " +
      "fine-grained Actions: Read and write permission."
    );
  }

  return result;
}


function criticalSchedulerStatus() {
  const props = PropertiesService.getScriptProperties();
  const triggers = ScriptApp.getProjectTriggers()
    .filter(function(trigger) {
      return (
        trigger.getHandlerFunction &&
        trigger.getHandlerFunction() === IPO_SCHEDULER.tickFunction
      );
    })
    .map(function(trigger) {
      return {
        handler: trigger.getHandlerFunction(),
        source: String(trigger.getTriggerSource()),
        id: trigger.getUniqueId(),
      };
    });

  const status = {
    timezone: IPO_SCHEDULER.timezone,
    triggers: triggers,
    tokenConfigured: Boolean(
      props.getProperty(IPO_SCHEDULER.tokenProperty)
    ),
    lastTickIst: props.getProperty(
      IPO_SCHEDULER.lastTickProperty
    ),
    closingCheckpoint: {
      date: props.getProperty(
        IPO_SCHEDULER.decisionDateProperty
      ),
      dispatchedAtIst: props.getProperty(
        IPO_SCHEDULER.decisionAtProperty
      ),
      runUrl: props.getProperty(
        IPO_SCHEDULER.decisionRunProperty
      ),
    },
    day2: {
      date: props.getProperty(
        IPO_SCHEDULER.day2DateProperty
      ),
      dispatchedAtIst: props.getProperty(
        IPO_SCHEDULER.day2AtProperty
      ),
      runUrl: props.getProperty(
        IPO_SCHEDULER.day2RunProperty
      ),
    },
  };

  console.log(JSON.stringify(status, null, 2));
  return status;
}


function ipoCriticalSchedulerTick() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    return;
  }

  try {
    const now = new Date();
    const dateKey = Utilities.formatDate(
      now, IPO_SCHEDULER.timezone, "yyyy-MM-dd"
    );
    const timeText = Utilities.formatDate(
      now, IPO_SCHEDULER.timezone, "HHmm"
    );
    const weekday = Utilities.formatDate(
      now, IPO_SCHEDULER.timezone, "EEE"
    );
    const hhmm = Number(timeText);
    const nowIst = Utilities.formatDate(
      now,
      IPO_SCHEDULER.timezone,
      "yyyy-MM-dd'T'HH:mm:ssXXX"
    );

    const props = PropertiesService.getScriptProperties();
    props.setProperty(
      IPO_SCHEDULER.lastTickProperty,
      nowIst
    );

    if (weekday === "Sat" || weekday === "Sun") {
      return;
    }

    if (
      hhmm >= IPO_SCHEDULER.decisionStart &&
      hhmm <= IPO_SCHEDULER.decisionDeadline &&
      props.getProperty(IPO_SCHEDULER.decisionDateProperty) !== dateKey
    ) {
      const dispatched = _ipoDispatchWorkflow_(
        "ipo-1430.yml",
        {
          wait_for_checkpoint: "false",
          record_as_checkpoint: "true",
        }
      );
      _ipoRememberDispatch_(
        props,
        dateKey,
        nowIst,
        dispatched,
        IPO_SCHEDULER.decisionDateProperty,
        IPO_SCHEDULER.decisionAtProperty,
        IPO_SCHEDULER.decisionRunProperty
      );
    }

    if (
      hhmm >= IPO_SCHEDULER.day2Start &&
      hhmm <= IPO_SCHEDULER.day2Deadline &&
      props.getProperty(IPO_SCHEDULER.day2DateProperty) !== dateKey
    ) {
      const dispatched = _ipoDispatchWorkflow_(
        "day2-email.yml",
        {
          wait_for_checkpoint: "false",
        }
      );
      _ipoRememberDispatch_(
        props,
        dateKey,
        nowIst,
        dispatched,
        IPO_SCHEDULER.day2DateProperty,
        IPO_SCHEDULER.day2AtProperty,
        IPO_SCHEDULER.day2RunProperty
      );
    }
  } finally {
    lock.releaseLock();
  }
}


function _ipoRememberDispatch_(
  props,
  dateKey,
  nowIst,
  dispatched,
  dateProperty,
  atProperty,
  runProperty
) {
  props.setProperty(dateProperty, dateKey);
  props.setProperty(atProperty, nowIst);

  if (dispatched.runUrl) {
    props.setProperty(runProperty, dispatched.runUrl);
  } else {
    props.deleteProperty(runProperty);
  }
}


function _ipoDispatchWorkflow_(workflow, inputs) {
  const token = _ipoGithubToken_();
  const url = _ipoWorkflowUrl_(workflow) + "/dispatches";

  const response = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: _ipoGithubHeaders_(token),
    payload: JSON.stringify({
      ref: IPO_SCHEDULER.ref,
      inputs: inputs || {},
    }),
    muteHttpExceptions: true,
  });

  const code = response.getResponseCode();
  const text = response.getContentText() || "";
  let body = {};

  if (text) {
    try {
      body = JSON.parse(text);
    } catch (err) {
      body = { raw: text };
    }
  }

  if (!(code >= 200 && code < 300)) {
    console.error(
      "GitHub workflow dispatch failed: workflow=" + workflow +
      " status=" + code +
      " body=" + text
    );
    throw new Error(
      "GitHub workflow dispatch failed for " + workflow +
      " (HTTP " + code + ")"
    );
  }

  const runUrl =
    body.html_url ||
    body.run_url ||
    "";

  console.log(
    "GitHub workflow dispatch accepted: workflow=" + workflow +
    " status=" + code +
    (runUrl ? " run=" + runUrl : "")
  );

  return {
    workflow: workflow,
    httpStatus: code,
    runUrl: runUrl,
  };
}


function _ipoGithubToken_() {
  const token = PropertiesService.getScriptProperties()
    .getProperty(IPO_SCHEDULER.tokenProperty);

  if (!token) {
    throw new Error(
      "Missing Script Property " +
      IPO_SCHEDULER.tokenProperty
    );
  }
  return token;
}


function _ipoGithubHeaders_(token) {
  return {
    "Accept": "application/vnd.github+json",
    "Authorization": "Bearer " + token,
    "X-GitHub-Api-Version": "2022-11-28",
  };
}


function _ipoWorkflowUrl_(workflow) {
  return (
    "https://api.github.com/repos/" +
    encodeURIComponent(IPO_SCHEDULER.owner) + "/" +
    encodeURIComponent(IPO_SCHEDULER.repo) +
    "/actions/workflows/" +
    encodeURIComponent(workflow)
  );
}
