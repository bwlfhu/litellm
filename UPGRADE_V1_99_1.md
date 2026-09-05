# Stable Fork Migration Ledger

Checkpoint: 2026-09-05. Source migration, isolated validation and production acceptance are recorded separately. A migrated package is not automatically approved for deployment

## Baselines And Order

| Item | Frozen reference |
|---|---|
| Upstream | `v1.99.1`, `10f4033437df30b91b5dbf2b64711d0a8683fc52` |
| Production source | `1e102bc6e9759711cf313dd05bb0eb2e9d137d86` |
| Production plus two unshipped followups | `573c767a926884f0a88217dc7d66ce71d3c784fc` |
| Archived mainline | `f11a7c45a1e52959d51d451e43d6919173b02e6b` |
| Integration branch | `litellm_v1991_upgrade`, created from the upstream tag |
| Initial migration commits | `b34803d1ee` (build), `d50bbf85b4` (integrated migration) |
| Subsequent review fixes | Working tree after `d50bbf85b4`; final release commit is not frozen yet |

The preserved tags are `archive-mainline-v197-f11a7c45a1`, `archive-v1.97.0-custom-1e102bc6e9` and `archive-deepseek-followups-573c767a92`. Key installed production modules were matched to source by SHA-256, rather than by the image tag alone

The user's clarified sequence supersedes the earlier plan's `mainline_v199` name and promotion timing: finish necessary code checks and isolated acceptance, replace `mainline`, then deploy and verify production. The 24-hour production observation belongs after deployment, not before the Git update. Neither final mainline replacement nor production rollout has occurred at this checkpoint

Promote only a frozen reviewed commit. Re-read remote mainline and use an explicit force-with-lease against the archived commit. If it moved, preserve and review those commits first. Retain old source tags, image digest, matching configuration and database recovery evidence

## Work Packages

Local checks below refer to focused regression runs. Final combined checks, the matching candidate image and production acceptance remain separate requirements

| Package | Current state | Decision and remaining acceptance |
|---|---|---|
| M01 Build/config | Build demonstrated; release candidate blocked | Preserve upstream image pins, Python 3.13, dependency locks and UI build; retain configurable mirrors. RC3 builds but one of 50 source module hashes differs. Rebuild from the final frozen commit |
| M02 Protocol boundaries | Migrated; local checks passed | Trusted deployment context, Messages paths and fallback cleanup retained. Caller metadata cannot select trusted protocol. Real provider acceptance remains |
| M03 DeepSeek history | Migrated; local checks passed | Preserve final thinking defaults, explicit disable, reasoning replay, matched tool history and controlled missing-reasoning placeholder. Retire intermediate disable policies. Final combined/provider checks remain |
| M04 Provider/model compatibility | Migrated; local checks passed | Keep upstream V4 prices and 393216 output limit. Do not port old thinking_always_on=true: upstream means disable is forbidden. Preserve missing provider fixes, thinking prices and ordinary DeepSeek endpoint followup |
| M05 Responses/streams | Migrated; independent source review passed | Fix item/tool/reasoning state, terminal semantics, multi-step indices and snapshots. Unified source acceptance passed; candidate/provider acceptance remains separate |
| M06 Accounting | Migrated; acceptance incomplete | Preserve selected deployment provider/prices and authoritative costs. Focused regressions exist; final review and cross-surface deterministic usage/cost checks remain |
| M07 Router | Migrated; local checks passed | Preserve upstream structure, order-aware affinity, deployment cooldown, metadata isolation and prebuilt logger synchronization. Final stream fallback acceptance depends on M05/M06 |
| M08 Routing statistics | Migrated; local checks passed | Preserve minute buckets, namespace/hash tags, attempt IDs, terminal deduplication and active leases. Clear stale channel metadata on deployment changes. Real cross-Pod aggregation remains |
| M09 Model status | Migrated; review defect fixed | Admin/viewer access retained. Strict Redis reads return 503 on failure instead of false empty success; health key uses upstream helper. Actual Router Redis acceptance remains |
| M10 SSE/timing | Upstream replacement; local checks passed | Retire old wrapper, use upstream keepalive and minimal timing hooks. Rename setting to sse_keepalive_ping_interval_seconds: 15. Ingress buffering/disconnect/drain validation remains |
| M11 Budget concurrency | Independent source review closed | DB/Redis suite passed 200 tests; nine later actual-helper integration cases passed, including generation-CAS repair, fresh admission, cold seeding and expiry after snapshot. Conditional zero placeholders preserve existing counters. Final full budget rerun is separate; mixed-version rolling release remains prohibited |
| M12 Diagnostics | Migrated; local checks passed | Disabled optional diagnostics no longer emit tool instrumentation. Retain shapes/counts/correlation without full prompts, tool inputs or reasoning. Final combined checks remain |
| M13 UI/OpenAPI | Migrated; partial acceptance | Use upstream form and corrected DeepSeek placeholder source. Eleven component tests passed. Frozen response models describe observability fields. Regenerate final artifacts and verify saved credentials/model calls |
| M14 Identity material | Source material preserved | Keep deploy/casdoor independent. No bootstrap/provisioning or identity upgrade executed. Confirm actual login dependency during runtime acceptance |

Scope is the production fork's final behavior and two unshipped followups. Experimental native Responses transport/session persistence from other branches was not added. Upstream Router, request processor, dependency locks and model catalog were not replaced wholesale by old files

## All 59 Source Commits

Every source commit appears once in this compact mapping. Port means extract final behavior, not replay the original patch. Build changes are in b34803d1ee; integrated source is in d50bbf85b4, followed by review fixes awaiting their final commit reference. Package acceptance above governs every row

| Original commits | Package | Disposition |
|---|---|---|
| `c4ef436efb`, `ee62c5313e` | M02 | Port trusted deployment selection and normalized-model context |
| `76be6636f5` | M02/M07 | Preserve history-error fallback boundaries |
| `8153a20d4a` | M02/M03/M06 | Split protocol, history and billing boundaries |
| `2d86725145` | M02/M03/M07 | Split replay, provider and routing boundaries |
| `2a9525a8a3`, `34d158f6be`, `2bae1d89e1`, `19f122405b`, `0348578487` | M03 | Extract final history/explicit-disable/effective-thinking rules |
| `54c5eedc8d`, `f47073082b`, `535eff9508`, `9c104752e6`, `ef6185b422` | M03 | Preserve replayability, controlled reasoning completion and strictly matched history repair |
| `10ea75a939`, `b3de8d0d57` | M03 | Retire historical intermediate default/incomplete-history disable policies |
| `b5b4eeaf52` | M03 | Retain false-stream omission for Messages |
| `8826cef820`, `b3cacf8b2c` | M03 | Adapt reasoning types and valid tool-history regression |
| `bb894a4452` | M03/M05 | Preserve effective reasoning output consistency |
| `78ce04b68c` | M04 | Retire 384000 expectation; retain upstream 393216 output limit |
| `1e102bc6e9` | M03/M04/M05/M06 | Split compatibility bundle; retain upstream model updates |
| `16ce3cb45f` | M04 | Retain ordinary DeepSeek default endpoint followup |
| `c9322a1082` | M05 | Port missing Responses conversion/lifecycle behavior |
| `6cd4a4ce4c` | M05/M06 | Separate stream state from usage accounting |
| `b57fa85619` | M05/M07/M08/M10 | Split streams, Router, telemetry and timing changes |
| `b1c7f17f3d`, `439ebf07d6`, `dcfb3c7af3`, `68e3d56099`, `df9d6ab2de` | M06 | Preserve routed deployment pricing, Messages metadata and selected identity |
| `a30c8335b6`, `ddcace7dba`, `c053cae99e`, `f26e443d58` | M06 | Preserve billing provider and authoritative passthrough/precomputed cost |
| `c33332df73` | M06/M12 | Separate billing from diagnostics |
| `c1b15a4297` | M07 | Preserve fallback bookkeeping isolation |
| `7cb70f4b5b` | M08 | Port endpoint and complete telemetry chain |
| `88b09400d8` | M09 | Port model-status and strengthen read-failure reporting |
| `ee94321912`, `f291dc745e` | M10 | Retire wrapper and validate upstream timer behavior |
| `5e55b8f119`, `a3e44743b8`, `c2fc622f0f`, `5227154aff` | M10 | Preserve no-Request, cancellation/buffer and exception/timer behavior as coverage |
| `238dc78548` | M10 | Do not replay historical formatting |
| `0208a36f3c` | M11 | Reimplement new-window spend preservation; review remains open |
| `7a7a2e3e49` | M11 | Replace old lock with upstream lease |
| `e096940e6c`, `8f1ab63740`, `5373f0a623`, `2c98707391` | M12 | Retain controlled shape/lifecycle diagnostics and adapt hook annotations |
| `35ec620f7f` | M13 | Regenerate schemas from target registered routes |
| `573c767a92` | M13 | Use upstream credentials form and retain placeholder correction |
| `f11a7c45a1` | M14 | Preserve independent identity deployment material |
| `50131ca2c6` | M01 | Adapt mirrors to upstream build |
| `c8caa3c67f` | M02/M03 | Format migrated semantics using target tooling |
| `381085a9f2` | Quality | Do not copy old lint/type budgets |

M04's ordinary endpoint remains a deliberate source difference: deployment api_base overrides it, but cannot configure SDK callers which omit that parameter. This followup was not fully replaced by deployment configuration. Before retiring it, inventory those callers and verify the official default endpoint with the required model family

## Validation And Independent Review

Evidence names refer to restricted local artifacts or recorded test runs. Database dumps, model exports, credentials, runtime hosts and provider tokens are excluded from this document and Git

| Evidence | Result and limit |
|---|---|
| migration-regressions.log/.xml | Initial broad run: 2686 passed, 17 failed, 12 skipped. Historical failures were investigated; this is not an all-green result |
| review-tag-regressions.log/.xml | Historical broad run: 3145 passed, 8 failed, 12 skipped. Credential and fixture failures were investigated; superseded by unified source acceptance below |
| review-unified-acceptance.log/.xml/manifest.json | 3392 passed, 3 skipped, 0 failed in 275.80 seconds. Three live OpenAI cases were explicitly excluded; three DB/Redis integration skips have separate budget-suite evidence. No skipped/excluded case is counted as passed |
| budget-review.log/.xml and focused budget followups | Final 307 passed, 91 warnings in 79.17 seconds. Includes actual PostgreSQL/Redis helper tests, delayed repair/admission and both cold-seed variants |
| Focused accounting, SSE/timing and diagnostics runs | 107, 70 and 302 passed at earlier checkpoints; later relevant fixes require revalidation |
| Observability/Redis regression run | 80 passed, including real unavailable Redis -> model-status 503, missing-key success and OpenAPI response fields |
| Static-review regressions | 605 passed, 1 warning across observability, DeepSeek Messages, Anthropic adapters, Responses adapters and shared prompt conversion |
| Protocol focused/upstream regressions | 198 focused passed, then 209 expanded upstream Router/protocol/SSE cases passed; latest source fixes are included in unified acceptance |
| basedpyright-review-head.json / basedpyright-review-fixed.json | Full scan then focused nine-file scan removed erroneous Final bindings, duplicate declaration and interface annotation errors. Existing development venv, not canonical type-budget environment |
| UI components | Eleven add-model form tests passed; saved credentials and actual model calls remain runtime checks |
| Live proxy/provider probes | First 14 probes passed. Additional direct-channel checks include four successes and a GROUP_DELETED 403 reproduced on both old and new images; the artifact aggregate remains false while that external baseline failure and remaining channels are being classified |
| Old/new schema compatibility | Both application versions read five critical tables and completed create/update/rollback checks against the upgraded clone. This establishes tested schema compatibility, not mixed-version budget-writer compatibility |
| migration-clone-report.json | Clone migration exit 0, history 146 -> 157, critical table row counts unchanged |
| RC3 candidate | Build succeeded; digest sha256:160a67a6cc0656c3596c74cd0600a8aa56bcd7f846797d9951062b2d6d173eac. Only 49/50 inspected modules match d50bbf85b4; budget source mismatch blocks this digest from release |

Independent reviewers cover protocol, accounting/budgets and plan/API/static correctness

| Finding | Disposition |
|---|---|
| Reused per-step message indices and wrong completed snapshots | Closed by protocol reviewer; focused and unified regressions passed |
| Sync reasoning not finalized, cross-step reasoning loss and tool index=0 reuse | Closed by protocol reviewer; focused/unified regressions and targeted static review completed |
| Merged reasoning+text+tool chunk losing text/tool output | Protocol reviewer fixed; included in focused protocol validation |
| Stale telemetry channel/attempt on deployment switch | Cleared from reused logger; targeted routing regression passed |
| Redis outage shown as empty successful model status | Closed: strict reads and 503 endpoint response, reproduced using actual RedisCache with unavailable socket |
| Observability schemas generated as unknown | Closed in source with frozen models and schema assertions; final generated artifact refresh remains |
| Optional tool instrumentation emitted with diagnostics disabled | Closed by respecting switch, with focused regressions |
| Incorrect automatic Final declarations and duplicate model-info type declaration | Closed in owned non-budget/non-streaming files; static checks and 605 regressions passed. Other owners handle their affected paths |
| Redis expiry/reseed and overlapping reset windows | Fixed and tested with DB conditional window updates, atomic snapshot-spend subtraction, generations and bounded reconciliation TTL; budget suite 200 passed |
| Delayed stale DB repair after successful reset | Closed: read-only generation snapshot precedes a fresh DB read; Lua rejects an obsolete generation before writes. Failed CAS cannot overwrite local cache, and admission returns the current Redis value. Two actual proxy-helper/DB/Redis tests passed |
| Missing counter and delayed old-window SET NX seed | Closed after actual failing-then-passing reproduction. Upstream ResetBudgetJob writes a zero placeholder after DB commit; skipping it introduced the race. Both initially missing and snapshot-then-expired counters now use SET NX zero, preserving any existing new-window counter. Nine actual integration cases passed |
| Five upstream test functions across four files disappeared or changed | Closed: two misindented text-delta tests restored, renamed Router assertions retained, intentional thinking-to-reasoning rewrite reviewed, and real completion_cost regression restored. Unified acceptance passed |

## Gate Classification

The original make check did not pass. Its log records standard lint/test-quality issues and strict/type-discipline budget breaches. No budget was increased, and the original full gate is not represented as green

For this migration, separate correctness evidence from aggregate coding-style debt. This is an explicit project-local adjustment to the initial unqualified full-gate requirement; preserve failed results for review rather than conceal them

| Class | Treatment |
|---|---|
| Syntax, undefined references, invalid calls/response fields, auth/trust violations and accounting/data races | Required fixes before source promotion; cannot be waived as style noise |
| Standard Ruff, diff integrity, deterministic affected regressions, image/source identity and isolated acceptance | Required checks on the frozen candidate |
| LIT001/LIT002 mutable type/construction counts; LIT010 missing Final counts | Advisory inventory. A count increase alone does not establish a runtime defect; actual aliasing and invalid Final declarations still need review |
| LIT006 casts, LIT011 parameter mutation, BLE001 broad catches | Review trust boundaries, caller mutation and swallowed business errors individually; only verified structural debt may be advisory |
| ANN204/ANN401, C901, PLW0603, SIM103/SIM401, B010 and legacy typing imports | Advisory where no behavior defect exists; bad shared state and incomplete API contracts remain substantive |
| B008 on FastAPI dependency declarations | Framework-required defaults are not inference-time mutable-default defects |
| LIT004/LIT009 suppressions | Remove ineffective suppressions and inspect actual errors; new suppressions need precise codes and reasons |
| Broad Any/Unknown totals | Not a correctness score alone. Inspect effective changed-code diagnostics; formal budget comparisons require canonical environment parity |
| Test-quality findings | Restore meaningful assertions and upstream coverage. Do not weaken or remove tests to reduce counts |

Record remaining debt without calling it an upstream-approved exemption. The focused static pass removed seven invalid Final uses, one duplicate declaration, FastAPI parameter mismatch and incorrect helper return/Literal declarations. Remaining reviewed diagnostics include list invariance and manually validated dict-to-TypedDict boundaries; these alone do not demonstrate runtime faults. Canonical type_check_gate --base v1.99.1 has not been recorded as passing

## Database Migration

The PostgreSQL 16 production backup was restored into an isolated database. Target SQL came from locked litellm-proxy-extras 0.4.89. The clone migration ran on 2026-09-05 from 10:27:13.961Z to 10:27:28.530Z and exited 0, applying exactly 11 published migrations

| Applied migration |
|---|
| `20260713010000_add_ptu_columns_to_daily_team_spend` |
| `20260730000000_add_api_key_and_request_tags_to_managed_object_table` |
| `20260810000000_add_verificationtoken_settings_updated_at` |
| `20260811172448_add_shadow_eval` |
| `20260813180408_add_shadow_eval_direction` |
| `20260814000000_add_proxy_worker_heartbeat` |
| `20260817000000_shadow_eval_multi_key` |
| `20260817143646_add_daily_guardrail_usage_units` |
| `20260818000000_add_spend_log_timestamps` |
| `20260818224500_add_shadow_eval_stopped_by` |
| `20260819000000_shadow_eval_max_budget` |

Migration history increased from 146 to 157. VerificationToken, Team, ProxyModel, Budget and SpendLogs row counts were unchanged; restricted evidence retains SQL SHA-256 values. Subsequent old/new application checks read five tables and passed create/update/rollback exercises against this upgraded clone. Background writer compatibility is a separate constraint. Production migration has not occurred

## Maintenance-Window Deployment

Use a controlled maintenance window. A negative mixed-version probe demonstrated that old writers do not maintain the new budget reconciliation markers; old reset jobs also lack conditional DB-window updates. Stopping only the old cron trigger is insufficient: old requests and in-flight jobs can still write incompatible state. Schema compatibility does not permit these writers to coexist

These fixes protect the tested custom reset and repair interleavings; they do not establish a distributed transaction across PostgreSQL, Redis, in-process caches and delayed spend-log flushes. Failed reconciliation uses bounded counter lifetimes and subsequent DB recovery. Record that bounded consistency model as an operational limitation rather than promising globally atomic accounting

1. Freeze reviewed source, regenerate OpenAPI/UI artifacts, commit and build a new candidate. Record digest and compare installed modules against that same commit, including import paths, UI assets, Prisma artifacts and dependency lock hashes. RC3 is not releasable because its budget module fails identity verification
2. Complete isolated readiness, management/authentication, model loading, protocol/cancellation, statistics and deterministic billing/budget checks against the exact candidate. Avoid production callbacks and unrelated background effects in isolation
3. After necessary code and isolated acceptance, replace mainline under the archived explicit lease. Record the actual commit, RC tag and candidate digest here
4. Use the existing deployment controller to stop new ingress traffic, drain long streams and stop all old application Pods and their running jobs. Confirm no old writer remains, then take the final recoverable database/configuration backup
5. Run one timed migration Job using the candidate's actual migration entrypoint. Application Pods retain DISABLE_SCHEMA_UPDATE=true; the Job must not inherit that disable flag. Verify the same migration set and critical database objects
6. Start the exact candidate with matching configuration, including sse_keepalive_ping_interval_seconds: 15. Verify readiness, admin/viewer versus ordinary-user access, loaded models and accounting/budget state before restoring traffic
7. Restore traffic in controlled scope. Validate real provider Chat/Messages/Responses, tool replay/long streams, deployment prices, cross-Pod telemetry, ingress behavior and enabled identity flow. Preserve sanitized request/response and cost evidence
8. Observe each expansion for at least 30 minutes and 100 representative requests, including 20 tool/long-stream requests where workload permits. After full rollout observe at least 24 hours and a budget boundary; use an independent test budget for long production periods

Do not clear shared Redis as an upgrade shortcut. Reverting the image also requires the matching old SSE configuration and demonstrated schema/state compatibility. Otherwise stop writes and follow the rehearsed recovery procedure. Database restore can lose writes after its restore point; application rollback does not undo migrations

## Remaining Checklist

- [x] Close protocol source findings and restore audited upstream coverage
- [x] Complete unified source regressions; explicitly record three live-provider exclusions and separately tested DB integration skips
- [x] Close final budget stale-repair and cold-seed interleavings; nine actual PostgreSQL/Redis helper integration cases passed
- [ ] Record the completed full budget rerun after final repair/placeholder changes
- [ ] Record final review commits, source diff, advisory debt and material limitations
- [ ] Regenerate final schemas and verify UI credential/model submission
- [ ] Rebuild frozen source and obtain complete candidate/module hash agreement; reject RC3 for release
- [ ] Pass isolated candidate startup and management/protocol/statistics/accounting acceptance
- [ ] Replace mainline with the archived lease and record new SHA, RC tag and digest
- [ ] Execute maintenance-window backup, single production migration and controlled deployment
- [ ] Complete real provider, ingress, cross-Pod statistics, accounting, identity and rollback checks
- [ ] Complete post-deployment observation and record production digest plus final release tag
