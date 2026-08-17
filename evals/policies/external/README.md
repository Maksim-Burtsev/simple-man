# External comparison policies

Third-party policies vendored here so benchmark arms are auditable and
reproducible from this repository alone. These are **comparison inputs**, not
part of the Simple Man skill, and they are not installed by anything here.

## `caveman-SKILL.md`

- Upstream: <https://github.com/JuliusBrussee/caveman>, `skills/caveman/SKILL.md`
- Pinned at repository commit `766dce6b1394ebb56a3090748d5a0240a5aefb36`
- `sha256` of the vendored file: `1eddf7055618153869975678d9ff36635602a3aa333f8b4cc0787f12de75b6f8`
- License: MIT. The upstream `LICENSE` scopes MIT to the repository except the
  Engine-linked directories listed in its `LICENSING.md` (`engine/`, `proxy/`,
  `cacheengine/`, `rewriter/`, `browse/`, `mcp/`, `shrink/`, cavemem Go core,
  `shared/platform/`); `skills/caveman/` is not among them.

The name is used descriptively, to identify the policy being compared.

### Historical note

The earlier comparison in `evals/reports/codex-skill-comparison.md` (May 2026)
used a different, older revision of that file, `sha256`
`6a93e68b5d843ab6da3290dfe81cfdf26de166be7f3feca5acb52744f63db593` (73 lines vs
89 here). That revision was never committed, which is one reason the old report
is not reproducible and is kept as historical evidence only. New benchmark runs
use the pinned copy in this directory.

## Updating a pin

Replace the file, then update the upstream commit, `sha256`, and license note
above in the same commit. Never update the bytes without updating the hash.
