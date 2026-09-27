# Handoff: token-burn as a Synkhos ops dashboard

**From:** token-burn efficiency-metrics design session, 2026-09-27
**To:** a Synkhos session opened in `~/projects/synkhos/`
**Status:** ready to hand off once the efficiency-metrics spec is approved.

## Kickoff prompt (paste into the Synkhos session)

> Use the house-plan skill. Plan the Synkhos **ops dashboard**: token-burn's efficiency and productivity metrics as a house-console area. Source of truth for the metrics and the data contract: `~/projects/token-burn/docs/superpowers/specs/2026-09-27-efficiency-metrics-design.md` (read §3 metrics, §4 data model, §9 Synkhos integration). The console pattern to follow: `company-context/docs/specs/factory/2026-09-26-factory-console-design.md` (FC-1…7). Write the design and plan under company-context, then file the cards with the filing-cards skill. Everything starts in Backlog; don't set Ready.

## What is decided (don't reopen)

- **Metric definitions and the usage-record / outcome-cache contract** are owned by the token-burn spec (§3, §4). The Synkhos dashboard computes the same numbers from the same records; it doesn't redefine them.
- **The browser never reads GitHub or buckets (FC-2).** A server-side job builds a per-tenant **efficiency snapshot**, and the console reads it through the `wb-console` door. Auth is FC-4: house member, owner or operator role.
- **Tenant separation.** One team bucket and one snapshot per tenant (`synkhos` now, `10federal` on rollout). Cross-tenant reads never happen.
- **Per-developer data is visibility, not ranking.** Alphabetical tables, no leaderboard. Each developer shows hands-on vs commissioned $, with "via fallback" requester share.
- **Card complexity** is the `pts:N` label (source of truth), mirrored one way to a Points board field, and set by `synkhos/factory/skills/rating-cards` (token-burn spec §7). The requester comes from `**Requested by:**`, falling back to the issue author.
- **Factory usage source of record** becomes the job doc `turns[]` (FC-3) once factory#109 lands. Until then, use the GCS transcripts in `gs://synkhos-factory-transcripts`.

## What the Synkhos session decides

1. **Placement:** a fourth view of the Factory area (next to Ledger) or its own **Ops** area. Lean: its own area, since it covers human sessions and tenants beyond the factory, but check FC-1's Ledger scope first.
2. **Who builds the snapshot:** the factory dispatcher tick (like FC-2) or a separate scheduled job. The data changes daily, so a 10-minute tick is unnecessary.
3. **Where usage records land long term:** stay in the team bucket, or be written into the MR-3a cost ledger (the §4.1 record is designed to map onto it). Say whether this is in scope or a later card.
4. **How the local collectors authenticate** to upload to the tenant bucket (developer laptops, including 10 Federal staff). Options: per-developer IAM on the bucket, or an upload door behind house auth.
5. **The 10 Federal tenant's bootstrap:** bucket, snapshot doc path, membership.

## Dependencies to cite on the cards

- factory#109: correct turn usage (dedupe by message id, include cache-creation, emit on timeout).
- Factory console F1–F3 (snapshot, job doc `turns[]`, `wb-console` door).
- token-burn build steps 1–4 (spec §11), which provide the usage records and outcome cache the snapshot job reads.
- `rating-cards` + `filing-cards --rate / --requested-by` (factory cards filed from token-burn spec §7).
