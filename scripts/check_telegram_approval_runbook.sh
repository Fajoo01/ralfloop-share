#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RUNBOOK="$ROOT/docs/telegram-domain-approval-lab-runbook.md"

if [ ! -f "$RUNBOOK" ]; then
  printf 'missing runbook: %s\n' "$RUNBOOK" >&2
  exit 1
fi

required=(
  "RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE"
  "RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE"
  "RALFLOOP_TELEGRAM_APPROVAL_REQUIRE_PRIVATE_CHAT"
  "/domain-approvals/requests"
  "/domain-approvals/{request_id}/decision"
  "execute-approved"
  "--dry-run"
  "StarletteRequest"
  "ForwardRef"
  "127.0.0.1:19090"
  "rollback"
)

missing=0
for needle in "${required[@]}"; do
  if ! grep -Fq -- "$needle" "$RUNBOOK"; then
    printf 'missing required runbook text: %s\n' "$needle" >&2
    missing=1
  fi
done

sections=(
  "## 1. Purpose"
  "## 2. Safety invariants"
  "## 3. Runtime layout"
  "## 4. Telegram commands"
  "## 5. Deployment"
  "## 6. Backend readiness"
  "## 7. OpenAPI verification"
  "## 8. ForwardRef troubleshooting"
  "## 9. Runtime files and permissions"
  "## 10. Meowgram restart"
  "## 11. Telegram smoke test"
  "## 12. Approval verification"
  "## 13. Dry-run execution"
  "## 14. Production invariants"
  "## 15. Rollback"
  "## 16. Troubleshooting matrix"
  "## 17. Success criteria"
)

for section in "${sections[@]}"; do
  if ! grep -Fq -- "$section" "$RUNBOOK"; then
    printf 'missing required runbook section: %s\n' "$section" >&2
    missing=1
  fi
done

if [ "$missing" -ne 0 ]; then
  exit 1
fi

printf 'telegram approval runbook OK: %s\n' "$RUNBOOK"
