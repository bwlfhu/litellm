# Current Production Baseline

Verified on 2026-10-14. Use `mainline` as the single long-lived maintenance branch. Create short-lived `litellm_<task>` branches for changes and remove their worktrees after review

| Item | Value |
| --- | --- |
| Upstream release | `v1.99.1` |
| Deployed build commit | `715f26b9008b6532db5fa3a1920c23bc8f982ff2` |
| Tested patch source | `715f26b9008b6532db5fa3a1920c23bc8f982ff2` |
| Image | `registry.allcam.org/litellm/litellm:v1.99.1-custom.715f26b900-reasoning-policy@sha256:7d98f5658d91a6a5be90e810321da98e8f6d76453762672d189c6e5c9bc48d9c` |
| Image tag | `v1.99.1-custom.715f26b900-reasoning-policy` |
| Installed LiteLLM / proxy extras | `1.99.1` / `0.4.89` |
| Development Python | `3.13.14` |

Four production replicas use this image. Their installed package versions and three changed runtime module hashes match the frozen patch source. Chat and Messages tool round trips passed on every replica, covering 16 real HTTP requests. Documentation and history reconciliation commits after the build do not change the deployed source identity

Provision the main worktree with `uv sync --frozen --python 3.13.14 --extra proxy --group proxy-dev --group e2e-dev`, then run `uv run --no-sync python scripts/prisma_generate_if_needed.py`. Before a build, verify the installed package versions, runtime source hashes, build commit and image digest. `docker/Dockerfile.deepseek-vision` pins the verified predecessor image and checks the package versions

The DeepSeek image, tool-history and SSE fixes remain present. The affected Chat deployment now uses the trusted `deepseek_anthropic_missing_reasoning: omit` policy, which forwards absent historical reasoning without fabricating a single space. Existing nonempty reasoning and tool records are preserved. Other deployment policies retain their previous behavior. Routing priorities, credentials, accounting, dependencies, database schema and UI were not changed by this patch

Targeted regressions passed 344 tests. Standard Ruff and strict/type-discipline/test-quality comparisons against the prior deployed source passed. The aggregate repository gate still reports pre-existing branch budget overruns; it is not recorded as green. Temporary production traffic-capture mounts were removed during rollout

The original final incident request returned a normal tool call in the candidate replay, with all 31 manufactured placeholders absent. Another legacy request was rejected by the upstream for missing thinking, and an earlier long request did not complete within the verification window. These results do not establish recovery of every existing conversation. Direct upstream tests still show `thinking.disabled` being ignored, so that separate compatibility defect remains open

Historical branches, stash entries and the independent WIP commit are preserved by archive refs and a verified private bundle. They are not part of the production baseline. The old DeepSeek branch also contains infrastructure files for Casdoor, FRP, Lucky and public-edge. Those files require a separate infrastructure review and were not deployed by this consolidation

See [UPGRADE_V1_99_1.md](UPGRADE_V1_99_1.md) for historical migration evidence and [deploy/INFRASTRUCTURE_ARCHIVE.md](deploy/INFRASTRUCTURE_ARCHIVE.md) for deferred infrastructure material
