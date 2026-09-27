# Handoff: efficiency metrics: factory rating, token-burn cards, and the Synkhos ops dashboard

**From:** token-burn efficiency-metrics design session, 2026-09-27 (spec approved by Jason)
**To:** a Synkhos session opened in `~/projects/synkhos/`
**This session owns:** writing the Synkhos plans in company-context, and **filing every card**: factory, ops dashboard and token-burn.

## Kickoff prompt (paste into the Synkhos session)

> Use the house-plan skill. The efficiency & productivity metrics design is approved: `~/projects/token-burn/docs/superpowers/specs/2026-09-27-efficiency-metrics-design.md`. Follow the handoff at `~/projects/token-burn/docs/handoffs/2026-09-27-synkhos-ops-dashboard.md`. It asks for three things:
> 1. A factory plan for card rating and requester (spec §7, decisions R-1…R-12), starting from the draft rows in the handoff.
> 2. A plan for the Synkhos ops dashboard (spec §9), following the factory console pattern (company-context `docs/specs/factory/2026-09-26-factory-console-design.md`, FC-1…7).
> 3. Filing all cards with the filing-cards skill, including the token-burn cards T1–T6 from `~/projects/token-burn/docs/plans/2026-09-27-efficiency-metrics-plan.md`.
>
> Ask me the open decisions before filing anything that depends on them. Everything starts in Backlog; don't set Ready or approved.

## Work items

### 1. Factory plan: card rating and requester

Write `company-context/docs/plans/factory/2026-09-27-card-rating-plan.md`, citing spec §7 (R-1…R-12). Starting rows, in house shape; refine the sizing and wording as house-plan requires:

| # | Title | Scope | Done when | Depends on |
|---|---|---|---|---|
| R1 | R1 · Cards are rated for complexity when they're filed | Filing a card rates it once the body is final, using the fixed rater, rubric and reference set, from title and body only. The card gets a `pts:N` label (or `pts:unrated` with a reason) and a rating comment carrying the versions. Filing never blocks on the rater (R-1…R-6, R-8, R-9, R-11). | The reference-set anchors rate exactly; held-out cards rate within one step. A card with no Done-when is `pts:unrated` with a reason. A simulated rater failure still files the card as `pts:unrated` / `rater-error` and flags it in the read-back row. The read-back row shows `pts`. A live filing in a scratch repo shows the label and comment. | — |
| R2 | R2 · Cards record who requested them | Filing writes `**Requested by:** <login>` in the header (no `@`). It defaults to the filer's login, a delegated filer passes the requester through, and console Ask passes the signed-in member's login (none → no line; consumers fall back to the author) (R-10, R-11). | The read-back row shows `requested by`. A delegated filing carries the delegator's login. An Ask-filed card carries the member's login. A card filed with no requester has no line and no ping. The filing-cards skill text says to pass the requester through. | — |
| R3 | R3 · Points and requester appear as board fields that follow the labels | A **Points** number field and a **Requester** text field on rollup #4 and on each product board, set one way from the label and header line over Projects v2 REST. Drift is reset and reported; a human label change wins, with the actor recorded (R-7). | A board view sums Points per status (screenshot). An edited field is reset on the next reconcile and appears in the drift report. Changing the label updates the field, and the timeline actor is recorded. Calls stay above the quota floor (spec §5.2). | R1, R2 |
| R4 | R4 · Past cards are rated and attributed | Existing issues without `pts:` are rated from their original body, missing requester lines are backfilled from the author, and fields are mirrored. It has a dry run, resumes after interruption, obeys the quota floor, and produces a calibration report re-rating 10% (R-12, spec §7.5). | A dry-run report on one repo lists the planned changes with no writes. A real run on that repo reads back labels, comments and fields. An interrupted run resumes without re-rating. The calibration report shows ≥80% exact and 100% within one step, or lists the misses. | R1, R2, R3 |

**Reference set v1 (approved by Jason, 2026-09-27):**

| pts | card |
|---|---|
| 1 | synkhos/nexus#58 |
| 2 | synkhos/hub#60 |
| 3 | synkhos/knitr#52 |
| 5 | synkhos/factory#115 |
| 5 | synkhos/nexus#84 |
| 8 | synkhos/lattice#304 |
| 8 | synkhos/spine#5 |
| 13 | synkhos/harness#418 |

Chosen on spec complexity; actual spend was only a sanity check. Put this table in the R1 card.

**Still to ask Jason:** whether the rater's own Claude usage is attributed (it's small, but it is spend).

### 2. Ops-dashboard plan

Write the design, and/or the plan under `company-context/docs/plans/<product>/`, per house-plan.

**Already decided (don't reopen):**
- Metric definitions and the usage-record / outcome-cache contract are owned by the token-burn spec (§3, §4). The dashboard computes the same numbers; it doesn't redefine them.
- The browser never reads GitHub or buckets (FC-2): a server-side job builds a per-tenant **efficiency snapshot**, read through the `wb-console` door with FC-4 auth (house member, owner or operator).
- One team bucket and one snapshot per tenant (`synkhos`; `10federal` on rollout). No cross-tenant reads.
- Per-developer data is visibility, not ranking: alphabetical, hands-on vs commissioned $, "via fallback" share.
- Factory usage source of record becomes the job doc `turns[]` (FC-3) once factory#109 lands. Until then, use the GCS transcripts.

**For this session to decide with Jason:**
1. Placement: a fourth Factory view next to Ledger, or its own Ops area (lean: its own area, since it covers human sessions and more than the factory).
2. Who builds the snapshot: the dispatcher tick or a separate daily job.
3. Whether usage records move into the MR-3a cost ledger in this cut or later.
4. How developer laptops (including 10 Federal staff) authenticate to upload to a tenant bucket: per-developer IAM, or an upload door behind house auth.
5. Bootstrapping the 10 Federal tenant: bucket, snapshot path, membership.

### 3. File all cards

Use the **filing-cards** skill for every card:
- Factory: R1–R4 under one factory epic.
- Ops dashboard: its cards under its own epic.
- token-burn: T1–T6 from the token-burn plan, under one epic in `framingeinstein/token-burn`.

Dependency lines use real issue refs:
- token-burn T5 → the factory R1 and R2 issues.
- The ops-dashboard snapshot card → token-burn T6 and factory#109.

If filing-cards can't file into `framingeinstein/token-burn` (it isn't a Synkhos product board), file those issues with the same body shape and read-back, and say so in the report.

## Dependencies to cite

- factory#109: correct turn usage (dedupe by message id, include cache-creation, emit on timeout).
- Factory console F1–F3 (snapshot, job doc `turns[]`, `wb-console` door).
- token-burn T1–T6 (the usage records and outcome cache the snapshot reads).
- Factory R1–R4 (the `pts:` labels and requester the outcome metrics need).
