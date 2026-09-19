# Bot-tazzi bounded worktree patcher — 2026-09-19

Aggiunto `tools/bounded_worktree_patch.py` per patch deterministiche su worktree Git reali quando il coding worker LLM non e disponibile.

Garanzie:
- worktree assoluto e radice Git obbligatoria;
- root limitate da `RALF_CODE_WORKTREE_ROOTS`;
- worktree deve essere pulito;
- `git apply --check` prima della modifica;
- validator obbligatorio dopo la patch;
- rollback con patch inversa se il validator fallisce;
- `git diff --check` finale.

Canary: patch `one -> two` su worktree temporaneo sotto `/home/bandi`, validator `grep -qx two a.txt`, risultato `applied_and_validated`.

Suite mirata coding/routing: 24 passed.

Restano presenti anche le modifiche per provider/fallback configurabili del coding harness; non sono ancora considerate prova di disponibilita di `agentcpm-local` o `deepseek-local`.
