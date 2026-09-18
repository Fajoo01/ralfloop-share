# Mailchimp / approval checks — 2026-09-18

- Campaign `c76d00282f` was sent at `2026-09-18T08:14:37Z` to 238 recipients.
- The sent provider record still has preview text `Dalle 16:00 in via Privata Federico Jarach 8. Il quartiere siamo noi.` while the body uses the newer slogan. Mailchimp sent campaigns cannot be safely rewritten after send; preserve this as historical evidence.
- Fix for future sends: campaign fingerprint + send approval now bind `preheader` and internal `title`, and provider readback checks them before send. A preheader/title change after approval makes the request stale.
- Approval Telegram rendering now shows Mailchimp subject, preheader and internal title before approval, so this mismatch is visible to the human approver.
- Expired legacy approvals `apr_VAU9P44B` and `apr_ND7NY29C` were marked stale on 2026-09-18. Current send approval `apr_4ASCLJX5` is consumed.
- Meowgram production formatting hotfix is preserved privately as `deploy/patches/meowgram-approval-autoexecute-20260918.patch`; do not push it to the upstream/public Meowgram remote.
