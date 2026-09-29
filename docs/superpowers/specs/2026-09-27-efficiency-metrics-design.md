# Efficiency & productivity metrics — design

**Date:** 2026-09-27
**Status:** Approved (Jason, 2026-09-27). Plan: [token-burn plan](../../plans/2026-09-27-efficiency-metrics-plan.md); Synkhos plans and all card filing handed off.
**Repos:** `framingeinstein/token-burn` (collectors, outcomes, joins, dashboard, team mode) · `synkhos/factory` (`skills/rating-cards`, `filing-cards` changes).
**Follow-on:** Synkhos ops-dashboard integration (§9), handed to a Synkhos session ([handoff](../../handoffs/2026-09-27-synkhos-ops-dashboard.md)).

## 1. Goal

Keep the dashboard's core unchanged and add a layer that answers three questions:

1. **Tune the workflow.** Where are tokens wasted (context bloat, rework, the wrong model, dead ends), and do changes help?
2. **Judge the factory.** Is autonomous work getting cheaper and more reliable per shipped issue, and how does it compare with interactive work?
3. **Report value.** Give clients and partners (e.g. 10 Federal) a defensible "value for spend" number.

It has to work for one developer plus the factory today, and for teams (Ricky next; a 10 Federal rollout means 3+ developers plus the factory).

### Principles

- **Lines of code are never a metric.** Less code for the same result is good.
- **Raw PR and issue counts are never a headline.** They ignore size and complexity.
- **Every derived number shows its coverage** ("covers 94% of factory $ · 31% of local $").
- **Per-unit costs are distributions** (median and 90th percentile, per repo), never means.
- **Per-developer views are for visibility and coaching, not ranking.** Tables are alphabetical, and per-person cost per point is a drill-down only.
- **No free text leaves a machine.** Shared data carries numbers and identifiers, never prompts or session titles.
- **The existing dashboard never gets worse.** Every new source degrades to "unavailable, with a reason".

## 2. Actors and requesters

Every usage record carries an **actor**:

| Actor | Source |
|---|---|
| `human:<github-login>` | the `gh api user` login configured on the collecting machine (`token-burn` config key `actor`) |
| `factory:<runner>` | factory transcripts; runner from the bucket path |

Factory records also carry **`on_behalf_of`**, the card's requester, resolved as:

1. the `**Requested by:** <login>` line in the card header (written at filing time, §7.3);
2. otherwise, **the login that created the issue** (the GitHub author).

Records resolved by rule 2 are flagged `requester_source: "author"`. The dashboard shows each developer's "via fallback" share, so cards filed through a shared account are visible rather than silently credited to that account.

For each developer this gives two separate numbers:
- **Hands-on:** spend from their own sessions.
- **Commissioned:** factory spend, and points shipped, on cards they requested.

The **approver** (who applied `approved` or moved the card to Ready) is read from the issue timeline for reference. It is not used for attribution.

## 3. Metrics

### A. Efficiency within the tokens themselves (exact, 100% of spend, no GitHub)

| Metric | Definition |
|---|---|
| Cache hit rate | `cr / (cr + cc + in)` |
| Context growth | median `in + cc + cr` per call, bucketed by call number within a session (1, 2–5, 6–20, 21–50, 51+) |
| Output share | `out / (in + out + cc + cr)` |
| Model fit | Opus/Fable $ in sessions joined to an issue rated ≤2 pts (§5.3); unjoined sessions are excluded. Shown as a table of candidates, not a score |

### B. Rework and waste

| Metric | Definition | Coverage |
|---|---|---|
| Rework share | factory $ on `ci-fix` + `conflict` turns ÷ factory $ on `impl` turns | factory: exact once turn kind is known (from the `factory.job.turn` event or the job doc `turns[]`, §9) |
| Dead-end $ | $ on: issues closed as `not_planned`; factory turns that ended in timeout or escalation; local branches with no merged PR 14 days after their last session | factory exact; local partial |
| Durable-merge rate | merged PRs with no revert PR and no reopen of the linked issue within 14 days ÷ merged PRs | PRs in configured orgs |

A **revert** is a merged PR whose title starts `Revert "` and whose body or timeline references the original PR.

### C. Outcome efficiency

| Metric | Definition |
|---|---|
| Cost per durable merge | attributed $ ÷ durable merges, per repo; median and 90th percentile over issues |
| **Cost per complexity point** | attributed issue $ ÷ issue `pts`, per repo; median and 90th percentile. Issues that are `pts:unrated` or have no `pts` are excluded and counted in coverage |
| Points validity | Spearman ρ between `pts` and attributed $ over the last 90 days of shipped issues, per repo. Shown next to $/pt, with a warning badge when ρ < 0.3 or n < 15 |
| Throughput | complexity points shipped per ISO week, shown next to $ per week |
| Autonomy | factory share of points shipped; human hands-on $ per factory-shipped point (per requester in team mode) |
| Spec quality (per requester) | rework share and `pts:unrated` rate on cards they requested |

"Shipped" means the issue was closed as `completed` with a merged PR linked (§5.3). **Attributed issue $** is the sum of all usage records joined to the issue: factory records for it plus local sessions on its PR branches, both hands-on and commissioned.

### Not built

LOC; raw PR and issue counts as headlines; leaderboards; mean cost per unit.

## 4. Data model

### 4.1 Usage record (the one record type that leaves a machine)

One JSON object per `(day, actor, on_behalf_of, repo, branch, issue, session, model_class, kind)`:

```json
{"v":1,"day":"2026-09-27","actor":"human:framingeinstein","on_behalf_of":null,
 "requester_source":null,"repo":"synkhos/nexus","branch":"feat/71-x","issue":null,
 "session":"a1b2…","model":"claude-opus-4-8","model_class":"opus","kind":"interactive",
 "calls":42,"in":1200,"out":3400,"cc":52000,"cr":880000,"cost_usd":3.21,
 "ctx_buckets":{"1":1,"2-5":4,"6-20":15,"21-50":22,"51+":0}}
```

- **Fields:**
  - `kind` is `interactive` for local sessions, or `impl` | `ci-fix` | `conflict` | `unknown` for the factory.
  - `repo` is `owner/name` or `null`. It's resolved from the session `cwd` via `git remote get-url origin` and cached per cwd. For factory records it comes from a runner→repo map in config.
  - `issue` is set for factory records (from the bucket path) and for local branches that match `feat/<n>-…`.
- **Validation:** a JSON Schema with `additionalProperties: false` and **no free-text fields**. Every string is either an enum or pattern-constrained (`login`, `owner/repo`, branch `^[A-Za-z0-9._/-]{1,200}$`, session ULID/UUID). Upload validates every record and refuses the batch on any failure.
- **Compatibility:** the schema is kept compatible with the Synkhos cost ledger's per-call accounting (MR-3a: `callId`-keyed, `costUsd` derived), so records can later be written there (§9).

### 4.2 Outcome cache (per team)

`outcomes/state.json` (watermarks, ETags) plus `outcomes/prs.jsonl` and `outcomes/issues.jsonl`. **Latest line per key wins**, the same convention as the existing ledgers.

- **PR:** `repo, number, author, head_ref, state, merged_at, closes[], reverted_by, review_rounds`
- **Issue:** `repo, number, author, requested_by, requester_source, state, state_reason, closed_at, reopened_at[], pts, pts_source (rater|human|backfill), labels[], approved_by`

### 4.3 Local archives

The existing `snapshots.jsonl`, `factory-snapshots.jsonl` and `cursor-snapshots.jsonl` stay as they are. Usage records are written alongside them as `usage/<day>.jsonl`, finalised the same way (complete days are immutable; `--refinalize` appends and the latest line wins).

## 5. Pipeline

### 5.1 Collectors (every machine)

`snapshot.py` gains a usage-record pass over the same parsed sessions (local and factory), writing `usage/<day>.jsonl`.

- **Factory records:** `kind` comes from the matching `factory.job.turn` event, joined by execution. Where that's unavailable, it's `unknown`. Once factory#109/#111 land and the job doc carries `turns[]` with kind and usage, the job doc becomes the preferred source (§9).
- **Team mode:** with a team configured, the collector uploads the day's validated records to `gs://<team-bucket>/usage/<actor>/<day>.jsonl`.

### 5.2 Outcome fetcher (one designated machine per team)

Config `outcomes.fetcher: true` on exactly one machine per team. It writes the outcome cache to `gs://<team-bucket>/outcomes/`, and every other machine reads it from there. Nobody else calls GitHub for outcomes.

**GitHub API policy (the factory rule: REST first, GraphQL only where REST can't):**

| Need | Call | Quota |
|---|---|---|
| Changed issues | `GET /repos/{r}/issues?state=all&since=<watermark>&per_page=100` + `If-None-Match` | core (304 is free) |
| Changed PRs | `GET /repos/{r}/pulls?state=closed&sort=updated&direction=desc`, paging back to the watermark only | core |
| Label actor, reopen/close, PR↔issue links | `GET /repos/{r}/issues/{n}/timeline` for issues whose `updated_at` moved | core |
| Board field mirror (§7) | Projects v2 REST (`/orgs/{o}/projectsV2/{n}/items`, field update) | core |
| PR → issues it closes | REST first: timeline `connected` / `cross-referenced`, then the `feat/<n>-` branch pattern. **GraphQL `closingIssuesReferences` only for PRs neither resolves** | GraphQL, rare |
| Original body at filing (backfill only) | GraphQL `userContentEdits`, batched 50 issues per query | GraphQL, one-off |

- **Quota floor:** before each call, read `x-ratelimit-remaining`. Below the reserve (default 1000 core / 1000 GraphQL points) the fetcher stops, keeps its cache and watermarks, and the next run resumes. The floor applies equally to backfill.
- **Credential:** by default the fetcher uses the machine's `gh` token. It's configurable to a dedicated GitHub App installation. GitHub's pools are **one per user** (every token acting as that user) and **one per app installation**; the factory runs on its own app, so this fetcher only draws on the user's pool. Growth path: a small dedicated app per function gives each function its own pool.
- **Expected volume:** about 50–100 core calls a day for about 8 repos; GraphQL is near zero in steady state.

### 5.3 Joins (pure functions, dashboard build time)

1. **Local session → PR** by `(repo, branch) == (pr.repo, pr.head_ref)`. Branches `main`, `master`, `HEAD` and `null` never join.
2. **PR → issue** by `pr.closes[]`, falling back to the `feat/<n>-` pattern.
3. **Factory record → issue** from the bucket path.
4. **Issue → requester** per §2.
5. Anything that doesn't join goes to the **unattributed** slice ("planning, review and ops"). Nothing is spread across PRs or dropped.

## 6. Dashboard

The existing page and sections are unchanged. With no team and no outcomes configured, the `/api/data` payload is byte-compatible with today's.

1. **Scope bar:** `Me | Team` (Team only when configured) · `All actors | <actor>`, alongside the existing source and metric toggles.
2. **Efficiency:** cache hit rate over time (per actor in team mode), context-growth curve, output share, model-fit table.
3. **Outcomes:**
   - **Tiles:** points shipped this week, $/pt (median and 90th percentile), durable-merge rate, rework share, dead-end $, each with its coverage line.
   - **Per-repo table:** points shipped, $/pt, validity ρ with badge.
   - **Autonomy trend:** factory share of points shipped, human $ per factory-shipped point.
   - **Dead-end list:** top 10 by $, linked to GitHub.
   - **Outcomes stamp:** "outcomes as of …".
4. **Team** (team mode): one row per actor, alphabetical. Columns: hands-on $, commissioned $ and points (with "via fallback" %), cache hit rate, rework share, dead-end $, spec-quality signals.
5. **Unattributed slice:** always shown with its share of $.

It uses the existing visual vocabulary (bars, tables, tiles) and the existing tokens-vs-$ toggle.

## 7. Card rating — a `rating-cards` skill in `synkhos/factory`

It lives next to the `filing-cards` skill in `synkhos/factory` and is installed the same way. This section fixes **decisions and contracts**; how it's built is left to the factory runner.

### 7.1 Rater (decisions)

- **R-1 · Fixed rater.** Ratings come from a fixed model (Sonnet) with a versioned rubric and a versioned **reference set** of about 8 agreed real cards spanning 1–13. They are never the filing agent's own judgement.
- **R-2 · Blind input.** Title and body only. The rater never sees PRs, diffs, comments or cost. For backfill, it rates the body **as originally filed**, not later edits.
- **R-3 · Rubric dimensions.** Uncertainty (new design vs existing pattern) · surface (modules/repos; `cross-repo`) · verification (number of Done-when checks and failure modes) · coupling (dependencies; contract or schema change).
- **R-4 · Scale.** 1/2/3/5/8/13.
  - A body too thin to rate (e.g. no Done-when) is `unrated` with a reason.
  - A rater failure is `unrated` with reason `rater-error`.

### 7.2 Storage (contracts consumers rely on)

- **R-5 · The label `pts:N` is the source of truth** (`pts:unrated` when unrated). It's portable across orgs and boards, and its change history (who, when) comes free from the issue timeline.
- **R-6 · Rating comment:** one comment per rating, carrying the points, a one-line rationale, and the rubric version, reference-set version and model. The markers are machine-readable, so re-rating and calibration can find it.
- **R-7 · Board mirror, one way.** A **Points** number field on rollup #4 and on each product board the card is on, set from the label (never the reverse), over Projects v2 REST. Drift is reset from the label and reported. **Override on the label, not the field.** A human label change wins, and the actor is recorded from the timeline.
- **R-8 · Stability.** An issue is rated once. It's re-rated only on an explicit request naming a new rubric version.

### 7.3 Filing (decisions)

- **R-9 · Rating at filing.** Filing a card rates it once the body is final. **Filing never blocks on the rater:** a failure files the card as `pts:unrated` / `rater-error` and flags it in the read-back row.
- **R-10 · Requester.** Filing records the requester as a `**Requested by:** <login>` line in the card header, next to `**Route:**` (no `@`, so nobody is pinged). It's mirrored one way to a **Requester** text board field.
  - It defaults to the filing machine's GitHub login.
  - A delegated filer passes the requester through.
  - **Console Ask** passes the signed-in member's GitHub login. When Ask has none, the card is still filed and consumers fall back to the author (§2).
- **R-11 · Read-back.** The filing read-back row shows `pts` and `requested by`.

### 7.4 Backfill (decisions)

- **R-12 · Backfill.** Existing issues without `pts:` are rated from their original body.
  - Missing `Requested by:` lines are set from the author and recorded as a backfill.
  - Board fields are mirrored.
  - It supports a dry run, resumes after interruption, and obeys the §5.2 quota floor.

### 7.5 Calibration

Each rubric version re-rates a random 10% of rated issues. Targets: ≥80% exact match and 100% within one step on the scale. Misses are listed for rubric tuning. The dashboard's validity ρ (§3C) is the outcome-side check.

## 8. Error handling

| Failure | Behaviour |
|---|---|
| GitHub unreachable, rate floor hit | keep cache and watermarks; show "outcomes as of …"; sections A and B are unaffected |
| Team bucket unreachable | local data only; "team data as of …" on Team |
| Missing, `unrated` or `rater-error` pts | excluded from $/pt, included in coverage |
| No PR or issue join | unattributed slice |
| Record fails schema | whole upload batch refused, reason logged in `snapshot.log`; local archives unaffected |
| Label ≠ board field | reconcile from the label, drift reported |
| Rater fails at filing | card filed `pts:unrated` / `rater-error`, flagged; picked up by the next backfill |
| No team or outcomes config | today's dashboard, byte-compatible payload |

## 9. Synkhos integration (follow-on; built by a Synkhos session)

token-burn stays the local and personal tool, and the reference implementation of the metrics. The Synkhos **ops dashboard** is a house-console area that follows the factory console's pattern (company-context `docs/specs/factory/2026-09-26-factory-console-design.md`):

- **The browser never reads GitHub or buckets (FC-2).** A server-side job computes an **efficiency snapshot** per tenant from the team bucket (usage records and outcome cache), and the console reads it through the `wb-console` door (FC-4 auth: house member, owner or operator role).
- **Tenant separation:** one team bucket and one snapshot per tenant (`synkhos`, later `10federal`). A tenant never reads another tenant's data.
- **Factory usage converges on the job doc.** Once factory#109 (correct turn usage, cache-creation included, emitted on timeout) and the FC-3 `turns[]` land, the job doc becomes the factory's usage source of record. token-burn's GCS-transcript path is then a cross-check.
- **The cost ledger (MR-3a) is the long-term home for usage records.** §4.1 is designed to map onto it.
- **Relation to the console's Ledger view (FC-1):** the Synkhos session decides whether efficiency belongs as a fourth view or as its own area. This spec only fixes the data contract (§4) and the metric definitions (§3).

The handoff brief for that session is `docs/handoffs/2026-09-27-synkhos-ops-dashboard.md`.

## Amendment 2026-09-27 — uploads through a door; the factory fetches outcomes

Ruled by Jason after the Synkhos plans (company-context `docs/plans/factory/2026-09-27-ops-efficiency-plan.md`, OD-1…OD-7):

- **A-1 · Uploads.** Developer machines upload usage records through the factory's house-auth door (synkhos/factory#171, OD-4), not directly to the bucket. No developer needs a GCP account. This replaces the bucket upload in §5.1.
- **A-2 · Outcome cache.** The factory produces the shared outcome cache itself as part of the snapshot job (synkhos/factory#172), using its own GitHub App (its own quota pool). The designated-fetcher-per-team design in §5.2 no longer applies to team data. token-burn's local outcome fetcher (T3) stays, for the personal dashboard only.
- **A-3 · Team view.** The team view lives in the console's Factory → Efficiency view (lattice#403, OD-1). token-burn's team mode (T6) is **upload only**, plus a link to that view. The local dashboard keeps "Me", including the factory work the developer commissioned. §6.1's `Me | Team` scope and §6.4's Team section are superseded by the console view.

## 10. Testing

TDD, the same style as the existing suite (stdlib, pytest, fixtures).

- **Units:** metric formulas (cache hit rate, output share, context buckets, durable-merge rate, median/p90, Spearman ρ with ties); joins (session→PR, PR→issue fallback, factory path); requester resolution including the author fallback and the `requester_source` flag; revert detection.
- **Contract:**
  - A usage record produced by the collector validates against the schema (the round-trip test).
  - The schema rejects any record carrying an unknown or free-text field.
  - Outcome-cache latest-wins reads.
- **GitHub client:**
  - ETag 304 handling.
  - `since` watermark paging stops at the watermark.
  - **Quota floor:** a stubbed low `x-ratelimit-remaining` stops the fetch with cache and watermark intact.
  - GraphQL is used only for the two listed cases.
- **Rating skill:** verification is set per card in the factory plan (handoff). Its done-when covers: anchors rate exactly and held-out cards within one step; thin body → unrated with a reason; a rater failure doesn't block filing; backfill uses the original body; mirroring is one-way.
- **End to end:** a fixture team bucket with two humans plus the factory, and a fixture outcome cache. `/api/data` returns the Efficiency, Outcomes and Team payloads; with no config, the payload equals today's.
- **Performance:** `/api/data` under 5 s with a warm outcome cache (a regression test using a budget on fixture scale, plus a manual check against real data).

## 11. Build order

1. **token-burn A:** usage records + Efficiency section (no GitHub).
2. **factory:** rating at filing + requester (R-1…R-11), then the backfill run (R-12). Planned and filed by the Synkhos session (handoff).
3. **token-burn B/C:** GitHub client with quota floor, outcome cache, joins, Outcomes section.
4. **token-burn team mode:** actor config and upload through the factory door (Amendment A-1…A-3).
5. **Synkhos ops dashboard:** per §9, planned by the Synkhos session.

Steps 1 and 2 are independent and can run in parallel.
