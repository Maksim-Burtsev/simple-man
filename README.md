<p align="center">
  <img src="assets/icon.png" alt="Simple Man icon" width="160">
</p>

# Simple Man

> Cut the chatter. Keep the work.

[![CI](https://github.com/Maksim-Burtsev/simple-man/actions/workflows/ci.yml/badge.svg)](https://github.com/Maksim-Burtsev/simple-man/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Release](https://img.shields.io/github/v/release/Maksim-Burtsev/simple-man)](https://github.com/Maksim-Burtsev/simple-man/releases)
[![Output tokens](https://img.shields.io/badge/output_tokens-%E2%88%9232%25-2ea44f)](#benchmark-simple-man-vs-no-policy)

You spend the day *reading* agents, not just running them — and most of what
they write is not work: praise for your question, apologies, recaps of what
just happened, suggestions nobody asked for, three paragraphs around one test.

Simple Man is the other voice at the helm — a captain who has run ships for
decades and tells the crew exactly what they need: the blocker, the fix, the
risk. Nothing else. Less to read, zero flattery, and nothing you act on gets
lost.

The captain is the character; under the hood it is a measured policy of
professional communication — short, factual, to the point — tested on 1,793
preregistered live calls, raw records committed.

<p align="center">
  <img src="assets/benchmark.svg" alt="Median answer length: 833 tokens without a policy vs 520 with Simple Man (−32.4%). Cases keeping every required fact: 66.7% in both arms.">
</p>

| **−32.4% output** | **0 facts lost** |
| --- | --- |
| Median answer drops from 833 to 520 tokens (95% CI [−23.2%, −43.8%]) | Keeps every required fact in 66.7% of cases — identical to the no-policy baseline |

## What you get

- **Answers a third shorter.** −32.4% output tokens across 84 real cases —
  measured, not advertised.
- **Zero lost facts.** Every benchmark case ships a checklist of facts the
  reader acts on — blockers, failed checks, exact identifiers, risks. Simple
  Man keeps every required fact in exactly as many cases as answers written
  with no length pressure at all.
- **Findings that carry their fix.** Location, consequence, one-line fix —
  nothing to follow up on.
- **It knows when *not* to compress.** A requested format is a contract:
  exact counts, order and shape are checked before sending. Tutorials,
  teaching explanations and detailed reports are written in full — the
  shipped skill description triggered with zero false activations on them.

## Install

**Claude Code** — global, for every project:

```bash
npx skills add Maksim-Burtsev/simple-man -g -a claude-code -s simple-man -y
```

Project-level only — drop the `-g`. Invoke it explicitly with `$simple-man`,
or let the agent activate it from the request.

### Portable Agent Skill

The same skill installs into any supported agent by changing `-a`:

```bash
npx skills add Maksim-Burtsev/simple-man -g -a codex -s simple-man -y
```

### Always-on Codex policy

The policy on every turn, without invoking the skill. The installer writes
`${CODEX_HOME:-$HOME/.codex}/AGENTS.md` and installs the skill; rerunning it
updates that block in place:

```bash
curl -fsSL https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/v0.3.1/install.sh | bash
```

For always-on Claude Code, copy [`AGENTS.md.snippet`](./AGENTS.md.snippet)
into your global `~/.claude/CLAUDE.md`.

<details>
<summary>Codex plugin and other agents</summary>

### Codex Plugin

```bash
codex plugin marketplace add Maksim-Burtsev/simple-man --ref v0.3.1
codex plugin add simple-man@simple-man
```

Installing the skill or the plugin makes Simple Man *available*; it
does not enable the always-on policy — only the installer or a copied
snippet does that. See [INSTALL.md](./INSTALL.md) for other agents and
project-level setup.

</details>

## See it

Same model (`claude-sonnet-5`), same prompt. Left is what you read today:

<table>
<tr><th width="50%">No policy — 950 tokens</th><th width="50%">Simple Man — 222 tokens</th></tr>
<tr valign="top">
<td>

**Findings**

**P1 — Panic on missing file in upload handler**<br>
**Location:** `src/handlers/upload.rs:114`

**Issue:** The code calls `.unwrap()` on `Path::new(&filename).canonicalize()`. `canonicalize()` returns an `Err` when the target file does not exist (or is otherwise inaccessible), and the `unwrap()` will panic in that case.

**Impact:** A missing file (e.g., client references a filename that was never uploaded, was deleted, or contains a path that doesn't resolve) crashes the request-handling thread. Depending on the async runtime/executor configuration, this can:
- Terminate only the single request (best case, if caught by a panic boundary), or
- Poison shared state / take down the worker if no panic isolation is in place.

Either way, it's an unhandled-error-as-crash pattern that should never be reachable from untrusted/external input.

**Suggested fix:** Replace `.unwrap()` with proper error propagation, e.g.:

```rust
let canonical = Path::new(&filename)
    .canonicalize()
    .map_err(|e| AppError::BadRequest(
        format!("invalid file path: {e}")
    ))?;
```

Return a 400/404 to the client instead of panicking. Add a test case covering a non-existent filename.

**P2 — New `PgPool` created per request instead of reusing shared pool**<br>
**Location:** `src/db/pool.rs:29`

**Issue:** A new `PgPool` is instantiated on every request rather than reusing a single, shared pool constructed once at startup.

**Impact:** Each `PgPool::new(...)` (or equivalent) opens its own set of connections to Postgres. Under load this:
- Rapidly exhausts available Postgres connections (hits `max_connections` on the DB server), causing connection errors for concurrent requests.
- Adds significant per-request latency from repeated connection/handshake overhead.
- Defeats the purpose of connection pooling entirely, since pools are not meant to be short-lived.

This is a correctness/scalability issue that will manifest as request failures and degraded throughput as concurrency increases, and could also affect other services sharing the same Postgres instance.

**Suggested fix:** Construct the `PgPool` once (e.g., in application startup / `main.rs` or a lazily-initialized static/`OnceCell`), then inject or pass a reference/clone (pools are typically `Arc`-backed and cheaply cloneable) into request handlers via app state (e.g., `web::Data<PgPool>` in Actix, `State<PgPool>` in Axum, etc.). Verify no other call sites have the same per-request instantiation pattern.

</td>
<td>

P1 — `src/handlers/upload.rs:114`: `Path::new(&filename).canonicalize().unwrap()` panics if the file doesn't exist, crashing the request thread. Fix: replace `.unwrap()` with proper error handling (e.g. `.map_err(...)?` returning a 4xx/5xx response) instead of panicking.

P2 — `src/db/pool.rs:29`: creates a new `PgPool` per request instead of reusing a shared pool, exhausting DB connections under load. Fix: construct the `PgPool` once at startup (e.g. in app state) and pass a reference/handle into each request instead of instantiating per-request.

</td>
</tr>
<tr><td colspan="2">Both findings, both locations, both fixes — nothing to follow up on. Every required fact kept on both sides.</td></tr>
</table>

<details>
<summary><b>Status update — 251 → 119 tokens, and root-cause diagnosis — 840 → 338 tokens</b></summary>

<table>
<tr><th width="50%">No policy — 251 tokens</th><th width="50%">Simple Man — 119 tokens</th></tr>
<tr valign="top">
<td>

**Subject: inventory-sync — Status Update**

Hi [Manager],

Quick update on inventory-sync:

- **Kafka consumer rewrite** is deployed to canary.
- **Performance**: canary pods show p99 latency at 340ms, well within our 500ms SLO.
- **Issue**: readiness probe on pod `inventory-sync-7f9d8` is failing with `connection refused` on port 9090.
- **Rollout status**: paused before proceeding to the remaining 90% of pods, pending investigation of the probe failure.

Will update once we've root-caused the connection issue and are ready to resume the rollout.

Thanks,<br>
[Your name]

</td>
<td>

Status: inventory-sync

- Kafka consumer rewrite deployed to canary.
- Canary p99 latency: 340ms (within 500ms SLO).
- Blocker: readiness probe failing on pod inventory-sync-7f9d8 — `connection refused` on port 9090.
- Rollout to remaining 90% paused pending fix.

</td>
</tr>
<tr><td colspan="2">The blocker is labelled as one, and both facts that change what the manager does survive.</td></tr>
</table>

<table>
<tr><th width="50%">No policy — 840 tokens</th><th width="50%">Simple Man — 338 tokens</th></tr>
<tr valign="top">
<td>

**Diagnosis**

**Root cause:** A context deadline budget mismatch — the 200ms timeout attached to the context in `CreateOrderHandler` is smaller than the actual latency required by the downstream work performed inside `pricing.Calculate`.

Specifically:

- `CreateOrderHandler` creates a context with a 200ms deadline (likely via `context.WithTimeout(ctx, 200*time.Millisecond)`) and passes that *same* context through the entire call chain: `inventory.Reserve()` → `pricing.Calculate()`.
- `inventory.Reserve()` consumes ~40ms, leaving only ~160ms of budget remaining on that context by the time `pricing.Calculate()` is invoked.
- `pricing.Calculate()` internally calls a currency conversion API that averages ~350ms.
- Since the context's deadline is shared and cumulative across both calls (not reset or given its own independent timeout for the pricing step), the 200ms deadline is exceeded roughly 150–190ms into the currency conversion call.
- The context's internal timer fires, cancels the context, and the HTTP client (or whatever transport `pricing.Calculate` uses to call the currency conversion API) returns `context deadline exceeded` because the request's ctx.Done() channel closed before the response arrived.

**Exact point of failure:** Inside `pricing.Calculate()`, specifically at the call site where it invokes the currency conversion API using the inherited context (e.g., the `client.Do(req.WithContext(ctx))` or equivalent HTTP call). The deadline check that trips is the `ctx.Err()` / `<-ctx.Done()` evaluation performed by the HTTP client's transport layer during that outbound call, not any logic within `pricing.Calculate` itself.

**Why `inventory.Reserve()` is unaffected:** It completes in ~40ms, well under the 200ms budget, so it returns before the shared context's deadline is reached — the failure only manifests once the *remaining* budget (200ms − reserve time) is insufficient for the currency conversion API's ~350ms average latency.

**Underlying issue in one sentence:** A single 200ms context deadline is being shared across two sequential operations (`inventory.Reserve` + `pricing.Calculate`'s external currency API call) whose combined/individual latency requirements exceed that budget, and there is no per-stage timeout or budget allocation to account for `pricing.Calculate`'s dependency on a ~350ms external call.

</td>
<td>

Root cause: budget mismatch. CreateOrderHandler allocates a single 200ms timeout to the whole request context, but by the time inventory.Reserve() (~40ms) finishes, pricing.Calculate() has ~160ms left — less than the currency conversion API's ~350ms average latency. The timeout is too small for the slowest downstream call it must cover, so every call to the currency conversion API blows the deadline.

Exact failure location: inside pricing.Calculate(), at the call to the currency conversion API (the ctx.Done()/deadline check triggered during or after that HTTP call). inventory.Reserve() is not the problem — it completes well inside the budget.

Not yet diagnosed (would need code to confirm): whether pricing.Calculate derives a fresh sub-context/timeout for the currency API call or just inherits the parent's remaining deadline, and whether the 200ms figure was sized only for inventory.Reserve()'s SLA without accounting for pricing.Calculate()'s external dependency.

</td>
</tr>
<tr><td colspan="2">Same root cause, same exact failure location — and what is still unproven stays explicitly unproven.</td></tr>
</table>

</details>

<details>
<summary><b>Security review with fixes — 1,215 → 276 tokens</b></summary>

<table>
<tr><th width="50%">No policy — 1,215 tokens</th><th width="50%">Simple Man — 276 tokens</th></tr>
<tr valign="top">
<td>

**Security Review Findings**

**Finding 1: Broken Object-Level Authorization (IDOR) — `GET /api/orders/:orderId`**<br>
**Location:** `routes/orders.js:52`<br>
**Class:** CWE-639 (Authorization Bypass Through User-Controlled Key) / OWASP API1:2023 – Broken Object Level Authorization

**Description:**
The endpoint authenticates the request (valid session required) but does not authorize it — it never verifies that the fetched `order.userId` matches `req.session.userId`. Any authenticated user can enumerate or guess `orderId` values (sequential IDs, UUIDs leaked elsewhere, etc.) and retrieve other users' order data.

**Impact:**
- Horizontal privilege escalation: disclosure of other customers' order contents, shipping addresses, pricing, and any PII embedded in the order record.
- If order IDs are sequential/predictable, mass scraping of all orders in the system is trivial.

**Severity:** High

**Finding 2: Broken Object-Level Authorization (IDOR) — `PATCH /api/orders/:orderId/status`**<br>
**Location:** `routes/orders.js:80`<br>
**Class:** CWE-639 / OWASP API1:2023 – Broken Object Level Authorization

**Description:**
Same missing ownership check as Finding 1, but on a state-mutating endpoint. Any authenticated user can modify the `status` field of any other user's order by supplying its `orderId`.

**Impact:**
- Unauthorized write access: an attacker can cancel, mark-shipped, mark-delivered, or otherwise tamper with other users' orders.
- Business-logic abuse (e.g., prematurely marking orders "delivered" to trigger downstream automation, fraud, or dispute leverage).

**Severity:** High

**Finding 3: Missing Input Validation on `status` — `PATCH /api/orders/:orderId/status`**<br>
**Location:** `routes/orders.js:80`<br>
**Class:** CWE-20 (Improper Input Validation) / lack of allow-list enforcement for state transitions

**Description:**
The handler writes the incoming request value directly into `order.status` without validating it against an allow-list of legal states (e.g., `pending`, `paid`, `shipped`, `cancelled`, `delivered`) or enforcing valid state-transition rules.

**Impact:**
- Data integrity corruption: arbitrary/malformed strings can be persisted as order status, breaking downstream logic (fulfillment, reporting, notifications) that assumes a fixed enum.
- Illegal state transitions (e.g., jumping from `pending` directly to `delivered`, or reverting a `cancelled` order) may bypass business rules and any invariants other code paths depend on.
- Depending on how `status` is later used (rendered in UI/emails, used in DB queries, passed to downstream services), an unvalidated string could contribute to secondary issues (stored XSS if reflected unescaped, log injection, etc.) — worth checking consumers of this field.

**Severity:** Medium (compounds with Finding 2 into High when combined — unauthorized *and* unvalidated writes)

**Summary / Remediation Guidance (not exhaustive, findings-only requested but noting root cause)**
Both endpoints need an ownership check (`order.userId === req.session.userId`, returning 403/404) before returning or mutating the resource. The PATCH endpoint additionally needs `status` validated against a fixed enum/allow-list and, ideally, a state-transition guard rather than an unconstrained overwrite.

</td>
<td>

**routes/orders.js:52 — GET /api/orders/:orderId**
- IDOR: authenticated user can read any order by ID (no `order.userId === req.session.userId` check).
- Fix: after fetch, if `order.userId !== req.session.userId`, return 403/404.

**routes/orders.js:80 — PATCH /api/orders/:orderId/status**
- IDOR: same missing ownership check — any authenticated user can modify any other user's order status.
- Fix: same ownership check before update.
- Unvalidated input: `status` accepted as arbitrary string, written directly to `order.status` — allows invalid/unexpected state values (data integrity risk, possible logic bypass if downstream code branches on status).
- Fix: whitelist allowed status values (enum check) before update; reject with 400 otherwise.

</td>
</tr>
</table>

</details>

<details>
<summary><b>Dockerfile + run commands — 2,045 → 443 tokens</b></summary>

**No policy — 2,045 tokens**

Here's a complete, production-ready Dockerfile plus the commands to build and run it.

**Dockerfile**

```dockerfile
# syntax=docker/dockerfile:1
FROM node:20-alpine

WORKDIR /usr/src/app

# Copy only manifest files first for better layer caching
COPY package*.json ./

# Install exact, reproducible deps (requires package-lock.json in context)
RUN npm ci --omit=dev && npm cache clean --force

# Copy application source
COPY . .

# node:20-alpine ships with a built-in non-root 'node' user (uid/gid 1000)
# Give it ownership of the app directory, then switch to it
RUN chown -R node:node /usr/src/app
USER node

ENV NODE_ENV=production
ENV PORT=4000

EXPOSE 4000

# DATABASE_URL and JWT_SECRET are intentionally NOT set here —
# they must be injected at runtime via `docker run -e` / secrets manager

# Adjust to your actual entry point (e.g. dist/main.js, index.js)
CMD ["node", "server.js"]
```

**Build command**

```bash
docker build -t node-api:latest .
```

**Run command**

```bash
docker run -d \
  --name node-api \
  -p 4000:4000 \
  -e DATABASE_URL="postgresql://user:password@host:5432/dbname" \
  -e JWT_SECRET="replace-with-a-strong-secret" \
  --restart unless-stopped \
  node-api:latest
```

**Notes / caveats**

- **Entry point**: `CMD ["node", "server.js"]` assumes your start file is `server.js`. Change it to match your actual entry (e.g. `dist/index.js` if you build TypeScript first — in that case add a `RUN npm run build` step before switching to the non-root user, and copy source before `npm ci` accordingly).
- **package-lock.json required**: `npm ci` fails without a lockfile in the build context. Make sure `package-lock.json` is committed and not excluded by `.dockerignore`.
- **`.dockerignore`**: add one with at least `node_modules`, `.git`, `.env`, `npm-debug.log` to keep the build context small and avoid leaking local secrets into the image.
- **Never bake secrets into the image**: `DATABASE_URL`/`JWT_SECRET` are supplied only at `docker run` time (or via `--env-file`, Docker secrets, or your orchestrator's secret store) — they are not `ENV` instructions in the Dockerfile, so they won't be persisted in image layers.
- **Env file alternative**: instead of `-e` flags, you can use `--env-file .env.production` if you keep a local (gitignored) env file with `DATABASE_URL=...` and `JWT_SECRET=...`.
- **Permissions**: `chown -R node:node` walks the whole app directory at build time; fine for typical API codebases, but if your image is very large consider a multi-stage build to keep the final image lean.

**Simple Man — 443 tokens**

```dockerfile
FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm ci --omit=dev

COPY . .

RUN addgroup -S appgroup && adduser -S appuser -G appgroup \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 4000

CMD ["node", "index.js"]
```

Run:

```bash
docker build -t node-api .

docker run -d \
  -p 4000:4000 \
  -e DATABASE_URL="postgres://user:pass@host:5432/db" \
  -e JWT_SECRET="your-secret" \
  --name node-api \
  node-api
```

Notes:
- `CMD ["node", "index.js"]` — replace `index.js` with your actual entrypoint (e.g. `dist/main.js` if compiled, or `npm start` if you prefer that instead of a direct node call).
- `npm ci --omit=dev` skips devDependencies; drop `--omit=dev` if the app needs a build step post-copy.
- `alpine` lacks `bash`/many glibc-dependent native modules; if any dependency needs native compilation, add `RUN apk add --no-cache python3 make g++` before `npm ci`.

</details>

All pairs above are real captured answers from the committed benchmark run —
nothing is hand-written for this README. Raw records:
[`evals/releases/v0.3.1/`](./evals/releases/v0.3.1/report.md).

## Benchmark: Simple Man vs no policy

This is not a vibe check — every step of the pipeline is built so the numbers
cannot be massaged:

<p align="center">
  <img src="assets/pipeline.svg" alt="How the benchmark works: preregistered gates and corpus, hidden validators and a blind holdout wave, five arms on the same model and prompt, blind pairwise judging in both orderings, every number rebuilt from raw records in CI, and failures published rather than hidden.">
</p>

Two preregistered live runs on `claude-sonnet-5`, 1,793 calls, all raw records
committed under [`evals/releases/`](./evals/releases/) — preregistered by
commit ([v0.3.1](./evals/releases/v0.3.1/preregistration.json)), rebuilt by
`make bench-v3-check`.

<details>
<summary>Full comparison table, controls, methodology, and what did not ship</summary>

Latest run: 84 output cases across 12 categories (38% Russian), 40 activation
cases, 3 real agentic coding fixtures with hidden validators, blind pairwise
judging with position swap, and a holdout wave written by authors who never
saw earlier results.

The shipped policy against its predecessor and controls:

| | previous v0.2 policy | shipped policy | one sentence of "be concise" | no policy |
| --- | ---: | ---: | ---: | ---: |
| Required facts kept | 57.1% | **66.7%** | 67.9% | 66.7% |
| Blind preference vs shipped | 8 wins | **48 wins**, 28 ties | — | — |
| False success claims | — | **0** | — | — |
| Requested format kept | 82.1% | 81.0% | 82.1% | 76.2% |
| Coding fixtures passed | 2/3 | 2/3 | 2/3 | 2/3 |

The previous policy compressed hardest (−66% output) **by dropping required
facts** — that is why it was replaced. The shipped policy restores fact
retention to the no-policy level while still removing a third of output
length.

**On cost, honestly.** Output-token percentages are not session savings: in
real agent sessions most tokens are context and tool traffic, and JetBrains'
[measurement of the caveman skill](https://blog.jetbrains.com/ai/2026/07/speak-to-ai-agents-like-cavemen-tosave-tokens/)
on 86 real tasks found −8.5% session output tokens against an advertised 65%.
Our own three-session coding phase is consistent with that order of magnitude.
If you install Simple Man to cut your bill, one sentence of "be concise" gets
you most of the way — and on this corpus it finishes level with the shipped
policy, which is why that comparison is reported as a tie and not a win.

What a sentence does not give you is a specification: findings that must carry
their location, consequence and one-line fix; refusals that must name the
target, the missing precondition and the safe procedure; failed checks that
must report the exact failure; requested shapes treated as contracts; and a
description that routes away from tutorials and detailed reports. That is the
part you can read, and hold the policy to, in
[`AGENTS.md.snippet`](./AGENTS.md.snippet). Whether it beats a sentence
*category by category* is not settled here: at 7 cases per category this run
cannot decide it, and the "Retention by category" table in the report says so
in the open rather than picking the flattering cells.

**What did not ship, published rather than hidden:** the first candidate
failed its gates outright; the second beat the shipped policy decisively but
only tied the one-sentence control, and its promotion is an explicit owner
decision over the automated gate result, recorded with the trade-offs in
[`DECISION.md`](./evals/releases/v0.3.1/DECISION.md). Gate tables, a
mis-specified gate we scored as failed rather than quietly fixed, and both
runs' full analysis live in [`evals/releases/`](./evals/releases/).

Older Codex-based suites and what runs offline are described in
[`evals/README.md`](./evals/README.md).

</details>

## What it changes — and what it never touches

**Changes:** no preamble, praise, recap or filler; answer first; every review
or security finding carries its location, consequence and one-line fix;
refusing a destructive action names the target, the missing precondition and
the safe procedure; a failed check reports the exact failure and where to look
next; qualifiers survive — "no known remaining risks" is never shortened to
"no remaining risks".

**Never touches:** repository search, usage search, dependency tracing, impact
analysis, validation, test/lint/typecheck effort, proactive detection of
related correctness issues.

## Agent support

The full skill ([`skills/simple-man/SKILL.md`](./skills/simple-man/SKILL.md))
and a compact always-on policy (`AGENTS.md`, generated from
[`AGENTS.md.snippet`](./AGENTS.md.snippet) by `scripts/sync_surfaces.py`) cover
Claude Code, Codex, Gemini CLI, Cursor and any AGENTS.md-compatible agent.

<details>
<summary>Per-agent paths</summary>

| Agent/tool | Path |
| --- | --- |
| Claude Code | `skills/simple-man/SKILL.md`, or `CLAUDE.md` for always-on |
| OpenAI Codex / Agent Skills | `skills/simple-man/SKILL.md`, `AGENTS.md`, `AGENTS.md.snippet` |
| Gemini CLI | `GEMINI.md`, or configure Gemini to read `AGENTS.md` |
| Qwen Code | `AGENTS.md`, optional global skill copy |
| Cursor / Windsurf / Cline / Copilot / Continue / Zed / Junie | `AGENTS.md`, or copy `AGENTS.md.snippet` into that agent's native rule file |
| Amp / OpenCode / Kilo / Roo / Aider / other AGENTS.md agents | `AGENTS.md` |

Always-on project files do not invoke `$simple-man`; they inline the compact
runtime policy to avoid loading full skill overhead on every turn.
Agent-specific dotdir rule files are not committed here — they are
target-project activation files, not the source of the skill.

</details>

## License

MIT — see [LICENSE](./LICENSE).