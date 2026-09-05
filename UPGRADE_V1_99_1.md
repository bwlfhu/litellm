# Stable Fork Migration

Upstream: `v1.99.1`, `10f4033437df30b91b5dbf2b64711d0a8683fc52`

Production source: `1e102bc6e9759711cf313dd05bb0eb2e9d137d86`

Followup source: `573c767a926884f0a88217dc7d66ce71d3c784fc`

The migration uses the final behavior of the production fork and its two unshipped followups. Historical intermediate thinking policies are not replayed. Shared upstream implementations remain the baseline

The previous mainline is preserved by `archive-mainline-v197-f11a7c45a1`. Production and followups are also preserved by `archive-v1.97.0-custom-1e102bc6e9` and `archive-deepseek-followups-573c767a92`. After implementation, independent review and validation, the upgraded branch replaces `mainline` as requested. Promotion must use an explicit lease against the archived remote mainline commit

## Work Packages

| Package | Scope | State | Decision |
|---|---|---|---|
| M01 | Build and configuration | In progress | Keep upstream dependency pins and Python 3.13; port configurable mirrors |
| M02 | Deployment protocol boundaries | Pending | Port trusted deployment context |
| M03 | DeepSeek reasoning and tool history | Pending | Port final observable behavior |
| M04 | Provider compatibility and model metadata | Pending | Compare individual backports; configure endpoint explicitly where possible |
| M05 | Responses and stream lifecycle | Pending | Validate upstream then port missing behavior |
| M06 | Deployment pricing and accounting | Pending | Preserve authoritative cost and routed model identity |
| M07 | Router order, cooldown and fallback | Pending | Preserve upstream architecture and port missing contracts |
| M08 | Routing statistics | Pending | Preserve endpoint and deployment-attempt accounting |
| M09 | Model status | Pending | Preserve endpoint; validate current Redis formats |
| M10 | SSE and timing diagnostics | Pending | Use upstream keepalive; migrate configuration and necessary timing |
| M11 | Budget reset concurrency | Pending | Use upstream lease; verify preservation of new-window spend |
| M12 | Tool diagnostics | Pending | Separate optional diagnostics from protocol and accounting |
| M13 | UI and generated schemas | Pending | Validate upstream form; regenerate schemas |
| M14 | Identity deployment material | Pending | Preserve independent deployment material |

## Release Gates

Source-to-image checks matched the production DeepSeek Chat/Messages, routing statistics and budget reset modules. The old deployment and configuration snapshots are stored outside the repository with restricted permissions

Application tests, real-provider proxy checks, database restore and migration, mixed-version Redis behavior, candidate image verification, independent reviews and production observation remain release gates. No pending gate is represented as completed by a source migration commit

The old SSE setting `sse_keepalive_interval_seconds` must become `sse_keepalive_ping_interval_seconds`. Application Pods keep schema updates disabled; a single verified migration job owns database changes
