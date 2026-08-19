# Security Policy

## Reporting a vulnerability

Report privately through GitHub Security Advisories:
[**Report a vulnerability**](https://github.com/Maksim-Burtsev/simple-man/security/advisories/new).

Please do not open a public issue for a suspected vulnerability first.

Expect an initial response within 7 days. If a report is confirmed, the fix and
an advisory ship together, and the reporter is credited unless they ask not to
be. There is no bug bounty.

## Supported versions

Only the latest release receives fixes. Older tags are historical evidence for
the benchmark and are not patched.

| Version | Supported |
| --- | --- |
| latest release | yes |
| earlier tags | no |

## Scope

In scope:

- `install.sh` — it writes to `${CODEX_HOME:-$HOME/.codex}` and installs the
  skill, so path handling, symlink handling and clobbering of existing files
  are all fair game.
- The plugin bundle under `plugins/simple-man/`, including the manifest.
- The benchmark runner under `evals/`, in particular anything that could move a
  run onto API billing or execute model-authored code outside its fixture.
- Repository automation in `.github/workflows/`.

Out of scope:

- The policy text itself producing an answer you disagree with. Simple Man is a
  communication policy: it changes wording, not permissions. A model that says
  something wrong under it is a quality issue — open a normal issue.
- Prompt injection against the agent that loaded the skill. The skill does not
  add tools, network access or capabilities, so it cannot grant an injection
  anything the host agent did not already allow.
- Vulnerabilities in Claude Code, Codex, or any other host agent. Report those
  to the vendor.
