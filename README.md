# IPO Advisor v0.5.0 — GitHub Pages Edition

This repository is designed to run from a **personal GitHub account**.

No account name is hard-coded.

## Architecture

- **GitHub Pages**: always-available static dashboard
- **GitHub Actions**: scheduled Python execution
- **SQLite committed under `state/`**: persistent public IPO/model state
- **Meta WhatsApp Cloud API**: template alerts to the private recipient list
- **Research Model V1**: frozen
- **SME V2**: shadow-only

GitHub Pages itself is static, so Python does not run inside Pages. The scheduled
GitHub Actions workflows run the Python engine, update JSON files under
`site/data/`, persist the SQLite state, and redeploy the Page.

## Recommended repository name

For the clean root URL:

```text
https://YOUR-PERSONAL-LOGIN.github.io/
```

create the personal repository:

```text
YOUR-PERSONAL-LOGIN.github.io
```

Unzip this package into the repository root and push it to `main`.

If you instead use a normal repository name, GitHub Pages will use:

```text
https://YOUR-PERSONAL-LOGIN.github.io/REPOSITORY-NAME/
```

The dashboard uses relative asset paths, so either form works.

## 1. Enable GitHub Pages

Repository:

```text
Settings → Pages → Build and deployment → Source → GitHub Actions
```

## 2. Run bootstrap once

Open:

```text
Actions → Bootstrap and Deploy IPO Advisor → Run workflow
```

The first run can take several minutes because it initializes the 2024/2025
historical model dataset and the 2026 tracker.

The Action commits generated state back into:

```text
state/
site/data/
```

Do not delete `state/ipo_advisor.db` after bootstrap unless you intentionally
want to rebuild the cloud history.

## 3. WhatsApp Cloud API secrets

Add these under:

```text
Settings → Secrets and variables → Actions → Secrets
```

Required secrets:

```text
WHATSAPP_TOKEN
WHATSAPP_PHONE_NUMBER_ID
WHATSAPP_TEMPLATE_NAME
WHATSAPP_RECIPIENTS
```

`WHATSAPP_RECIPIENTS` stays private as a GitHub secret. Examples:

```text
919876543210,919812345678
```

or JSON:

```json
["919876543210","919812345678"]
```

Use international country code plus number, without relying on a leading `+`.

### Repository variables

Add under Actions **Variables**:

```text
WHATSAPP_GRAPH_VERSION
WHATSAPP_TEMPLATE_LANG
PUBLIC_DASHBOARD_URL
WHATSAPP_DRY_RUN
```

Suggested values:

```text
WHATSAPP_TEMPLATE_LANG = en_US
PUBLIC_DASHBOARD_URL = https://YOUR-PERSONAL-LOGIN.github.io/
WHATSAPP_DRY_RUN = false
```

Set `WHATSAPP_GRAPH_VERSION` to the currently supported Meta Graph API version
for your WhatsApp Cloud API account.

For initial testing:

```text
WHATSAPP_DRY_RUN = true
```

Run the 2:30 workflow manually and inspect the Action log. Then change it to
`false`.

See `WHATSAPP_TEMPLATE_EXAMPLE.md`.

## 4. What happens at 2:30 PM IST

Workflow:

```text
.github/workflows/ipo-1430.yml
```

It is scheduled for **14:15 IST on weekdays**, then waits until **14:30 IST**
inside the runner before fetching the canonical IPO snapshot.

This early-start design reduces the risk of a GitHub scheduler delay causing the
capture to begin only after 2:30.

At 2:30 it:

1. Gets LIVE Mainboard + SME IPOs.
2. Stores the canonical decision snapshot.
3. Runs frozen Research Model V1.
4. Stores SME V2 shadow signal.
5. Finds IPOs **closing today**.
6. Sends WhatsApp only for:
   - `STRONG SUBSCRIBE`
   - `SUBSCRIBE`
7. Updates the static dashboard.
8. Commits the state back to the repository.
9. Redeploys GitHub Pages.

The WhatsApp duplicate ledger prevents the same IPO/action/day alert from being
sent twice when a workflow is retried successfully.

## 5. Daily outcome update

Workflow:

```text
.github/workflows/daily-sync.yml
```

Runs at **18:07 IST on weekdays**.

It updates:

- 2026 IPO status
- actual listing gain
- call outcome
- prospective V1 vs V2 results
- model audit
- GitHub Pages dashboard

## 6. Subscribers

Because GitHub Pages is static, this package intentionally does **not expose a
public phone-number signup database**.

For this GitHub-only edition, the private subscriber list is:

```text
WHATSAPP_RECIPIENTS
```

in GitHub Actions Secrets.

This keeps phone numbers out of a public repository.

A self-service "Subscribe / Unsubscribe" form requires a small backend/webhook
service; it cannot safely be implemented by GitHub Pages alone.

## 7. Important scheduling limitation

GitHub Actions scheduled jobs can occasionally start late. The 14:15-start /
14:30-wait design reduces this risk, but GitHub does not provide a hard
real-time execution guarantee.

The actual snapshot timestamp is stored in the data and can be audited.

## 8. Security

Never commit:

- WhatsApp access tokens
- sender phone-number credentials
- subscriber phone numbers

Keep them in GitHub Secrets.

The SQLite state committed by this project contains IPO/model research state,
not the WhatsApp recipient list.

## 9. Research status

This deployment keeps the existing controls:

- V1 frozen
- V2 shadow-only
- exact 2:30 PM prospective experiment
- 20-listed-observation manual review checkpoint
- no automatic model retuning

WhatsApp alerts report a research signal. They do not place an IPO application
or execute any transaction.
