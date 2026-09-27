# Efficiency & productivity metrics — token-burn plan

**Date:** 2026-09-27
**Spec:** [efficiency-metrics design](../superpowers/specs/2026-09-27-efficiency-metrics-design.md) (§ refs below are to it)
**Repo:** `framingeinstein/token-burn`
**Status:** Decisions resolved; build in progress (subagent-driven). Cards to be filed by the Synkhos session ([handoff](../handoffs/2026-09-27-synkhos-ops-dashboard.md)).
**Companion plans (written by the Synkhos session in company-context):** factory card rating (spec §7, R-1…R-12) and the Synkhos ops dashboard (spec §9).

## Decisions (resolved by Jason, 2026-09-27)

1. **Who builds token-burn cards:** this interactive session, subagent-driven (one implementer per card, then spec-compliance and code-quality review).
2. **Team bucket:** GCP project `synkhos`. Bucket `synkhos-token-burn` (name chosen by this session, same pattern as `synkhos-factory-transcripts`).
3. **Outcome-fetcher credential:** Jason's `gh` token for now. GitHub rate limits are **one pool per user** (every PAT, `gh`, and OAuth or app-user token acting as that user) and **one pool per GitHub App installation** (5,000/hr REST and 5,000 pts/hr GraphQL, scaling to 12,500). The factory uses its own app, so this fetcher's quota floor protects Jason's own pool, not the factory's. **Growth path:** move the fetcher to its own small GitHub App (its own pool per org) if volume or contention grows. The fetcher's credential stays configurable (spec §5.2).
4. **Orgs in scope:** `synkhos`, `FramingEinsteinInc`, `framingeinstein`.
5. **Runner → repo map:** one runner per repo (`wb-impl-<repo>` → `synkhos/<repo>`), `terpsichore` → `synkhos/terpsichore-core`. Confirmed.

## Cards

| # | Title | Scope | Done when | Depends on |
|---|---|---|---|---|
| T1 | T1 · Every session's usage is a validated, shareable record | Each finalised day produces usage records for local sessions and factory runs, carrying actor, requester slot, repo, branch, issue, model and turn kind. The record holds no free text, so it is safe to leave the machine (spec §2, §4.1, §4.3). Factory turn kind is `unknown` until factory#109 lands. | A test builds records from fixture sessions and validates them against the published schema (round trip). A test shows a record carrying any unknown or free-text field is rejected. A session whose working directory has no git remote yields `repo: null`, not an error. A factory fixture yields the runner, issue and repo from its path and the map. Finalised days stay immutable, and a re-finalise appends and the latest line wins. The existing dashboard payload is unchanged. | — |
| T2 | T2 · Efficiency section shows cache hit rate, context growth and output share | A new Efficiency section, with no GitHub data needed, shows cache hit rate over time, the context-growth curve by call-number bucket, and output share. The section says it covers 100% of spend (spec §3A, §6.2). | Unit tests assert each formula on hand-computed fixtures, including a day with zero tokens. A live dashboard screenshot against real archives shows the section. `/api/data` stays under 5 s with a warm cache. Existing sections are visually unchanged. | T1 |
| T3 | T3 · GitHub outcomes are fetched REST-first within a quota floor | One designated machine keeps an outcome cache of PRs and issues for the in-scope orgs, fetched incrementally. It uses REST first, and GraphQL only for "PR closes which issues" when REST can't resolve it and for original bodies during backfill. It never drops a quota below the reserve (spec §5.2). | Tests with stubbed responses show: an unchanged list (304) costs no quota and changes nothing; paging stops at the watermark; a remaining quota below the floor stops the fetch with cache and watermark intact, and the next run resumes; GraphQL is called only in the two named cases. A live run against the in-scope orgs completes and reports calls made per quota type. GitHub unreachable → the cache is kept and the "outcomes as of" time is unchanged. | — |
| T4 | T4 · Spend is joined to PRs, issues and requesters | Usage records are joined local session → PR by repo and branch, PR → issue by closing references (falling back to the branch number), and factory run → issue. Requester comes from `Requested by`, else the author, with the fallback flagged. Revert and reopen within 14 days mark a merge not durable. Everything that doesn't join goes to the unattributed slice (spec §2, §3B, §5.3). | Tests over fixture records and outcomes assert: each join rule; `main`/`HEAD`/no-branch never joins; the requester fallback sets its flag; a revert PR and a reopened issue each make a merge not durable; unattributed $ + attributed $ = total $ for every day. | T1, T3 |
| T5 | T5 · Outcomes section shows cost per complexity point, with its coverage and validity | The Outcomes section shows tiles (points shipped this week, $/pt median and p90, durable-merge rate, rework share, dead-end $), a per-repo table with the validity ρ and its warning badge, the autonomy trend, the dead-end list, model fit, and the unattributed slice. Every derived number states its coverage (spec §3, §6.3, §6.5). | Tests assert median/p90 and Spearman ρ (with ties) on fixtures; that unrated or missing pts are excluded from $/pt and counted in coverage; and the badge rule (ρ < 0.3 or n < 15). A live screenshot shows the section on real data with coverage lines. Outcomes unavailable → sections A and B still render and Outcomes shows "unavailable" with the reason. | T4, factory rating card (R-1…R-11) |
| T6 | T6 · Team mode shows every developer and the factory, without a leaderboard | With a team configured, each machine uploads its validated records to the team bucket, one machine is the outcome fetcher for all, and the dashboard gains a Me/Team scope and a Team section. That section is alphabetical, one row per actor, showing hands-on and commissioned $, points, "via fallback" share, cache hit rate, rework share, dead-end $ and spec quality (spec §2, §5.1, §6.1, §6.4). | An end-to-end test with a fixture team bucket (two developers and the factory) asserts the Team payload. A batch with one invalid record is refused whole, and local archives are unaffected. Bucket unreachable → local data plus "team data as of". No team configured → the payload is byte-identical to today's. A live screenshot shows Team mode with a second test actor. | T1, T5 |

## Not in this cut

- The Synkhos ops dashboard (spec §9). Planned by the Synkhos session in company-context.
- Writing usage records into the MR-3a cost ledger.
- The GitHub App "file on behalf of the user" path for Ask.
- Per-developer cost per point as a headline (it stays a drill-down).
- Cursor outcome attribution (Cursor has no branch or session join today).

## Filing

The Synkhos session files these cards along with the factory and ops-dashboard cards (handoff).
- **Where:** one epic in `framingeinstein/token-burn` with T1–T6 as native sub-issues.
- **Body:** each body is its row plus the spec link and a single `Depends on …` line with real issue refs. The T5 line cites the factory rating card.
- **Status:** everything starts in Backlog; nothing is set to Ready or `approved` without Jason.
