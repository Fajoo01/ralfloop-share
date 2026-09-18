# Bot-tazzi Motor Judge isolated rollout — 2026-09-18

## Goal

Deploy Judge changes on port 19196 without moving `/home/sibilla-cumana/ralfloop-production/current`, restarting production DS4 on 19194, or changing Ralfloop backend code.

## Isolation model

The Judge systemd unit now uses:

- `WorkingDirectory=/home/sibilla-cumana/ralfloop-motor-judge/current`
- `PYTHONPATH=/home/sibilla-cumana/ralfloop-motor-judge/current`

The dedicated symlink may point at an immutable release under `/home/sibilla-cumana/ralfloop-production/releases/<commit>`. The global production `current` symlink is not moved.

## Rollout tool

`tools/deploy_bottazzi_motor_judge_release.py` is plan-only by default. Mutation requires explicit `--apply` and root privileges.

Preflight checks release identity, manifest, Judge module, dedicated systemd code-root, candidate service preflight, and the live 19194 listener PID.

Apply sequence:

1. snapshot current Judge target, env and systemd unit;
2. force `BOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD=0`;
3. atomically set Judge `previous` and `current` symlinks;
4. reload systemd and restart only `bottazzi-motor-judge.service`;
5. verify 19194 PID is unchanged;
6. run graduated prefill canaries around 180, 240 and 300 exact DS4 tokens, then a small canary again;
7. verify 19194 PID again.

Any startup failure, 19194 PID change or canary failure restores the old target/env/unit and restarts only the Judge.

## Current state

The rollout code is prepared and tested only. No dedicated Judge symlink exists yet, no unit/env file has been changed live, and neither 19194 nor 19196 has been restarted by this release work.
