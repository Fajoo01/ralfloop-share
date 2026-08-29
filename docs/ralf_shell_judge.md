# Ralf Shell Judge MVP

## Threat model

Agent-proposed Bash is untrusted. The deterministic judge parses it without execution,
extracts recursively visible commands, redirects, substitutions, assignments and paths,
then applies a fail-closed allowlist. Parser failure is `DENY`. Unknown commands and
unresolved but potentially authorizable behavior are `REVIEW`. Writes or deletes of
protected/secret paths and excessively broad deletion are non-approvable `DENY`.

The judge assumes installed executables and repository configuration are not malicious.
It does not make external provider actions safe and does not replace sandboxing.

Git readonly forms use canonical local key discovery through hardened `git config`.
External diff/textconv/filter commands, aliases, includes, pagers, worktree config and
relevant attributes force `REVIEW`. All inherited `GIT_*` variables are removed before
execution. Bash runs with `--noprofile --norc` so startup files cannot bypass review.

## Decisions

| Decision | Meaning |
|---|---|
| `ALLOW_READONLY` | Fully understood allowlisted reads; no writes/deletes/external effects. |
| `ALLOW` | Fully understood allowlisted writes inside the explicit writable root. |
| `REVIEW` | Valid AST, but unknown, indirect or high-risk capability. Nothing executes. |
| `DENY` | Parse failure, forbidden construct/path/executable, escape, or too destructive. |

Examples: `git status` is readonly; `echo x > out/x` is a bounded sandbox write;
`rm out/x` requires review; `rm -rf /home` is denied.

## Reviewer mixture

`MixtureShellReviewer` accepts only a structured `REVIEW` result. It exposes no shell,
filesystem or tool interface. Multiple independent classifiers must agree; disagreement
fails closed. A deterministic `DENY` cannot be submitted to reviewers or upgraded.

## Adding safe forms

Add an executable and exact argv constraints to the deterministic policy, then add
positive and adversarial AST tests. Never add a regex parser or a command-name-only
allow rule. Model redirects, path operands, environment influence and hidden execution
before enabling a form.

## Known MVP limits

Dynamic expansion, process substitution, background jobs, nested shells, unknown
executables and unmodeled cwd changes require review. The Go helper is compiled into
each immutable release; it is not built on the hot execution path.
