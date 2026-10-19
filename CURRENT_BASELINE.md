# Current Production Baseline

Verified on 2026-10-19. Use `mainline` as the single long-lived maintenance branch. Create short-lived `litellm_<task>` branches for changes and remove their worktrees after review

| Item | Value |
| --- | --- |
| Upstream release | `v1.99.1` |
| Deployed build commit | `982f2b9b7a28bec184d0289fbc6c5fb9705c017b` |
| Tested patch source | `982f2b9b7a28bec184d0289fbc6c5fb9705c017b` |
| Image | `registry.allcam.org/litellm/litellm:v1.99.1-custom.982f2b9b7a-anthropic-errors@sha256:810d7b77bcd434897d069d0fd4c32eded541672ebc2595b25a2f3a661b417439` |
| Image tag | `v1.99.1-custom.982f2b9b7a-anthropic-errors` |
| Installed LiteLLM / proxy extras | `1.99.1` / `0.4.89` |
| Development Python | `3.13.14` |

Four production replicas use this image. Package versions and runtime hashes for the two updated proxy modules and retained Messages transformation match the frozen patch source. October 19 live probes completed normally on two replicas; two other replicas returned upstream timeouts as valid Anthropic error events. All four responses had zero event-type mismatches, but the two timeouts remain failed model requests. The earlier October 15 image passed Messages tool round trips on every replica, covering 8 real HTTP requests and 370 paired SSE frames. Documentation commits after a build do not change its deployed source identity

Provision the main worktree with `uv sync --frozen --python 3.13.14 --extra proxy --group proxy-dev --group e2e-dev`, then run `uv run --no-sync python scripts/prisma_generate_if_needed.py`. Before a build, verify the installed package versions, runtime source hashes, build commit and image digest. `docker/Dockerfile.deepseek-vision` pins the verified predecessor image and checks the package versions

The DeepSeek image, tool-history, reasoning-policy and Messages SSE fixes remain present. The affected Chat deployment uses the trusted `deepseek_anthropic_missing_reasoning: omit` policy, which forwards absent historical reasoning without fabricating a single space. Messages SSE output emits an `event` line matching the JSON `data.type`. Proxy-generated failures after keepalives and during streaming now use Anthropic `event: error` with top-level `type: error`, preserving the underlying error instead of producing a missing-type mismatch. Anthropic failures do not append OpenAI's `[DONE]` marker. Existing nonempty reasoning and tool records are preserved. Routing priorities, credentials, accounting, dependencies, database schema and UI were not changed by this patch

The October 19 proxy and Anthropic endpoint tests passed all 311 cases, including 31 tests covering streaming, keepalives and error exits. Isolated HTTP tests reproduced the missing-type errors with the prior production image and verified the candidate's typed error events for delayed HTTP 503 and stream read failures; the Chat control retained its OpenAI envelope. The prior Messages transformation patch passed 255 tests, and the reasoning-policy patch had 344 targeted tests. Ruff, formatting, import checks and the strict Ruff, type-discipline, test-quality and basedpyright gates against the prior production source passed. The aggregate repository gate is not recorded as green. Production traffic capture remains disabled

The original final incident request returned a normal tool call in the candidate replay, with all 31 manufactured placeholders absent. Another legacy request was rejected by the upstream for missing thinking, and an earlier long request did not complete within the verification window. These results do not establish recovery of every existing conversation

Protocol attribution corrected on 2026-10-16: the direct tests that ignored `thinking.disabled` used Chat Completions. Fresh direct native Messages probes, bypassing LiteLLM, returned zero thinking blocks for both disabled requests and one thinking block for the enabled control. The prior claim that upstream Messages required a thinking-disable fix is withdrawn. Fresh Chat probes returned reasoning for both `thinking.disabled` and `reasoning_effort=none`; the Chat adapter's parameter mapping must be assessed against that endpoint's contract. The original incident's mismatch frame was not captured, so synthetic mismatch coverage and passing production requests do not establish that incident's source

The October 19 recurrence exposed a separate proxy error-serialization defect beyond the provider frame sanitizer. Three recorded connection failures entered `keepalive_then_relay`, whose old error frame had no top-level `type`. Isolated full-proxy HTTP tests confirmed that this path triggers the Harness parser's mismatch condition. The original client frames were not captured; the evidence links recorded failure stacks to the reproduced code path. The underlying connectivity failures and timeouts are not resolved by correcting their SSE representation and do not establish an upstream protocol defect

Historical branches, stash entries and the independent WIP commit are preserved by archive refs and a verified private bundle. They are not part of the production baseline. The old DeepSeek branch also contains infrastructure files for Casdoor, FRP, Lucky and public-edge. Those files require a separate infrastructure review and were not deployed by this consolidation

See [UPGRADE_V1_99_1.md](UPGRADE_V1_99_1.md) for historical migration evidence and [deploy/INFRASTRUCTURE_ARCHIVE.md](deploy/INFRASTRUCTURE_ARCHIVE.md) for deferred infrastructure material
