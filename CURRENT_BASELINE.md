# Current Production Baseline

Verified on 2026-10-15. Use `mainline` as the single long-lived maintenance branch. Create short-lived `litellm_<task>` branches for changes and remove their worktrees after review

| Item | Value |
| --- | --- |
| Upstream release | `v1.99.1` |
| Deployed build commit | `cd7ebbe046028acae57a6e65cfe3f40cb00c0d79` |
| Tested patch source | `cd7ebbe046028acae57a6e65cfe3f40cb00c0d79` |
| Image | `registry.allcam.org/litellm/litellm:v1.99.1-custom.cd7ebbe046-messages-sse-fix@sha256:82a24a7e273899de8ed79f7371503c7d4f9a658439807b3230795fcd3b4d09d1` |
| Image tag | `v1.99.1-custom.cd7ebbe046-messages-sse-fix` |
| Installed LiteLLM / proxy extras | `1.99.1` / `0.4.89` |
| Development Python | `3.13.14` |

Four production replicas use this image. Their installed package versions and three changed runtime module hashes match the frozen patch source. Messages tool round trips passed on every replica, covering 8 real HTTP requests and 370 paired SSE frames with zero `event` and `data.type` mismatches. Documentation commits after the build do not change the deployed source identity

Provision the main worktree with `uv sync --frozen --python 3.13.14 --extra proxy --group proxy-dev --group e2e-dev`, then run `uv run --no-sync python scripts/prisma_generate_if_needed.py`. Before a build, verify the installed package versions, runtime source hashes, build commit and image digest. `docker/Dockerfile.deepseek-vision` pins the verified predecessor image and checks the package versions

The DeepSeek image, tool-history, reasoning-policy and Messages SSE fixes remain present. The affected Chat deployment now uses the trusted `deepseek_anthropic_missing_reasoning: omit` policy, which forwards absent historical reasoning without fabricating a single space. Messages SSE output now emits an `event` line that always matches the JSON `data.type`, including when the upstream event line is stale or wrong. Existing nonempty reasoning and tool records are preserved. Other deployment policies retain their previous behavior. Routing priorities, credentials, accounting, dependencies, database schema and UI were not changed by this patch

The DeepSeek Messages transformation test file passed all 255 tests, including regression coverage for mismatched `event` and `data.type` fields. The prior reasoning-policy patch had 344 targeted tests. Standard Ruff passed for the changed files. The aggregate repository gate still reports pre-existing branch budget overruns; it is not recorded as green. Temporary production traffic-capture mounts were removed during rollout

The original final incident request returned a normal tool call in the candidate replay, with all 31 manufactured placeholders absent. Another legacy request was rejected by the upstream for missing thinking, and an earlier long request did not complete within the verification window. These results do not establish recovery of every existing conversation

Protocol attribution corrected on 2026-10-16: the direct tests that ignored `thinking.disabled` used Chat Completions. Fresh direct native Messages probes, bypassing LiteLLM, returned zero thinking blocks for both disabled requests and one thinking block for the enabled control. The prior claim that upstream Messages required a thinking-disable fix is withdrawn. Fresh Chat probes returned reasoning for both `thinking.disabled` and `reasoning_effort=none`; the Chat adapter's parameter mapping must be assessed against that endpoint's contract. The original incident's mismatch frame was not captured, so synthetic mismatch coverage and passing production requests do not establish that incident's source

Historical branches, stash entries and the independent WIP commit are preserved by archive refs and a verified private bundle. They are not part of the production baseline. The old DeepSeek branch also contains infrastructure files for Casdoor, FRP, Lucky and public-edge. Those files require a separate infrastructure review and were not deployed by this consolidation

See [UPGRADE_V1_99_1.md](UPGRADE_V1_99_1.md) for historical migration evidence and [deploy/INFRASTRUCTURE_ARCHIVE.md](deploy/INFRASTRUCTURE_ARCHIVE.md) for deferred infrastructure material
