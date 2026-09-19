# Handoff Bot-tazzi — code/worktree maintenance — 2026-09-19

Repo privato: `Fajoo01/ralfloop-bottazzi`
Branch: `fix/code-worktree-maintenance-20260919`
Worktree: `/home/bandi/ralfloop-code-maintenance-20260919`
HEAD prima di questo handoff: `127435a`

## Correzioni gia implementate
- routing negazioni: `do not restart`, `never`, `without` non diventano piu azioni protette;
- state/audit dir e self-base URL del backend resi configurabili per worktree/istanze isolate;
- `patch_allowed + local_maintenance + cwd Git` collegato al coding harness reale;
- worktree Git limitati a root locali allowlisted e fail-closed fuori scope;
- patch locali non passano piu inutilmente dal GPU handoff;
- coding worker dedicato configurabile, default `sibilla-cumana`;
- test mirati: `28 passed`.

## Commit gia pushati SOLO su `private`
`59becf0`, `d697df0`, `377d936`, `1017e28`, `127435a`.

## Blocco residuo verificato
Il coding harness arriva al worker, ma `pi` con `agentcpm-local / AgentCPM-Explore` va in timeout: `rc=124`, zero output.
`pi --list-models` espone anche `deepseek-local / deepseek-v4-flash`; il canary read-only era ancora in esecuzione/lento quando si e deciso il cambio chat.
Non dichiarare Bot-tazzi live corretto finche il worker non completa una patch reale sul worktree.

## Prossimo passo
Validare un provider `pi` locale funzionante o rendere il provider del coding harness configurabile/fallback; poi eseguire un canary reale su Baffoflix, passare i test, quality gate e solo dopo promuovere Bot-tazzi in produzione.