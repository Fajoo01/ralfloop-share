# ARCI write inventory — execution disabled

Date: 2026-09-03. Evidence class: repository/domain inventory only. Endpoint paths and payloads remain `UNKNOWN` unless authoritative evidence exists. No write probing performed; execution count: 0.

| Semantic action | Endpoint | Target | Preconditions | Side effect | Risk | Idempotency | Approval | Verification | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| create member | UNKNOWN | member | validated identity/consent; club scope | creates personal record | high/PII | UNKNOWN | required | exact point read + audit | UNKNOWN |
| update member | UNKNOWN | member | exact `users.id`; fresh version | changes personal record | high/PII | UNKNOWN | required | exact point read diff | UNKNOWN |
| create/request card | UNKNOWN | card/member | exact `users.id`, `club_id`, campaign | creates membership request | high | UNKNOWN | required | exact card read/status | UNKNOWN |
| update card | UNKNOWN | card | exact `cards.id`; fresh status | changes membership data | high | UNKNOWN | required | exact card read diff | UNKNOWN |
| approve card | UNKNOWN | pending card | exact card; status `10`; authorized club | status transition | critical | UNKNOWN | required | status `20`, relation/year checks | UNKNOWN |
| reject card | UNKNOWN | pending card | exact card; status `10`; reason | status transition | critical | UNKNOWN | required | exact rejected status | UNKNOWN |
| expel/revoke card | UNKNOWN | approved card | exact card; explicit legal/admin basis | removes validity | critical | UNKNOWN | required | exact status/expiry read | UNKNOWN |
| bulk member/card import | UNKNOWN | collection | validated file/schema; dry-run | mass create/update | critical/bulk | UNKNOWN | required + second review | reconciliation/count/hash | UNKNOWN |
| export members/cards | frontend action observed; endpoint UNKNOWN | collection | explicit scope and data handling basis | emits PII file | high/exfiltration | repeatable | required | artifact hash/access audit | delete exported artifact if controlled |

Policy: no MCP write tool, no generic REST proxy, no live endpoint discovery by mutation, no execution. Future promotion requires authoritative contract, least privilege, idempotency design, explicit approval, postcondition verification and rollback decision per action.
