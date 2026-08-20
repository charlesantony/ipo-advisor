# WhatsApp template example

Create and get approval for a WhatsApp message template in your Meta WhatsApp
Business account.

A simple body matching this package's five variables is:

```text
IPO Research Alert

{{1}} ({{2}})
V1 research signal: {{3}}
Model estimated listing gain: {{4}}
Snapshot: {{5}}

Research signal only. Verify the IPO details before applying.
```

The Action sends these values:

1. IPO name
2. MAINBOARD or SME
3. STRONG SUBSCRIBE or SUBSCRIBE
4. Predicted gain, for example `24.0%`
5. GMP / subscription snapshot plus dashboard URL

Set the approved template name as the GitHub secret
`WHATSAPP_TEMPLATE_NAME`.

This package deliberately uses an approved template rather than relying on a
free-form outbound message.
