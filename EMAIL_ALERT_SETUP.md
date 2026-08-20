# Email subscription setup — Google Apps Script

This version uses a small Google Apps Script service so visitors can enter their
email address on the IPO Advisor page without exposing subscriber emails in the
public GitHub repository.

The same service sends the closing-day alert email from your Google account.

## 1. Create the Apps Script

1. Open `https://script.google.com/` while signed in to the Google account that
   should send the alerts.
2. Create a **New project**.
3. Replace the default code with `google_apps_script/Code.gs`.
4. Save the project as `IPO Advisor Alerts`.
5. Select the `setup` function and click **Run** once.
6. Approve the Google permissions.
7. Open **Execution log**.

The log shows:

- a private Google Sheet URL containing subscribers
- `EMAIL_ALERT_KEY`

Copy the `EMAIL_ALERT_KEY`. Do not put this key in the public repository.

## 2. Deploy as a Web app

In Apps Script choose:

`Deploy → New deployment → Web app`

Use:

- **Execute as:** Me
- **Who has access:** Anyone

Deploy and copy the URL ending in `/exec`.

This is your `SUBSCRIBE_ENDPOINT`.

## 3. GitHub configuration

Repository:

`Settings → Secrets and variables → Actions`

### Repository secret

Create:

`EMAIL_ALERT_KEY`

Value: the key printed by `setup()`.

### Repository variables

Create:

`SUBSCRIBE_ENDPOINT`

Value: the Apps Script `/exec` URL.

Also keep:

`PUBLIC_DASHBOARD_URL = https://charlesantony.github.io/ipo-advisor/`

## 4. Refresh the site configuration

After adding the variable and secret, manually run:

`Actions → Bootstrap and Deploy IPO Advisor → Run workflow`

The generated `site/data/config.json` will contain only the public Apps Script
web-app endpoint. The secret alert key is never written to the site.

## 5. Test subscription

Open IPO Advisor and enter your own email.

You should receive:

`IPO Advisor email alerts — subscription confirmed`

The confirmation email includes an unsubscribe link.

## 6. Test a notification without waiting for 2:30 PM

The 2:30 workflow only sends an alert when an IPO closing that day has a V1 call
of `SUBSCRIBE` or `STRONG SUBSCRIBE`.

For a normal end-to-end test, run:

`Actions → 2:30 PM IPO Decision and Email → Run workflow`

A manual run does not wait until 2:30 PM.

If no qualifying IPO closes that day, the workflow correctly logs
`EMAIL_NO_ALERTS` and sends nothing.

## Notes

- Subscriber email addresses are stored in the private Google Sheet created by
  Apps Script, not in GitHub Pages or the public repository.
- Google Apps Script / Gmail sending quotas apply. This setup is appropriate for
  a small trial subscriber list.
- V1 remains frozen and V2 remains shadow-only.
