# Pilot — 3 tasks × N/B2

Purpose: prove the pipeline end to end before the registered run — Harbor
builds the task image, installs Claude Code pinned at 2.1.235, the
subscription token authenticates inside the container, the task's own skills
are injected, the payload reaches the treated arm and not the control, and
`collect.py` reads reward, cost, tokens, turns and wall-clock back out.

Tasks are the first three of the registered order (`setup-fuzzing-py`,
`spring-boot-jakarta-migration`, `manufacturing-codebook-normalization`).
Six sessions, $3.73 metered. Not a result: three pairs decide nothing, and the
report says so. The pilot trials are not part of `run/`.

What the pilot changed in the harness before the run started:

- agent install timed out at 360 s with three containers on an 8 GB host →
  `--agent-setup-timeout-multiplier 3` and two concurrent trials;
- Harbor splices `--append-system-prompt` into a shell command unquoted, so a
  multi-line payload broke bash → the value is handed over shell-quoted and
  unquoted before hashing (delivery check 3/3 vs 0/3 below).
