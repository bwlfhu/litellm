# Current Production Baseline

Verified on 2026-10-14. Use `mainline` as the single long-lived maintenance branch. Create short-lived `litellm_<task>` branches for changes and remove their worktrees after review

| Item | Value |
| --- | --- |
| Upstream release | `v1.99.1` |
| Deployed build commit | `01b32e5bad72b78b19a3fee4c2ca4151dccfab76` |
| Tested maintenance baseline | `c4415fe867c0ca2b3003552d6ed6cef2ad656799` |
| Image | `registry.allcam.org/litellm/litellm:v1.99.1-custom.01b32e5bad-deepseek-vision-recovery@sha256:1b5b1cadce89d0e71dcee0b453b3628061f9c363ad595002d247a9a46bea081f` |
| Release tag | `release-v1.99.1-deepseek-vision-20261012` |
| Installed LiteLLM / proxy extras | `1.99.1` / `0.4.89` |
| Development Python | `3.13.14` |

The maintenance baseline differs from the deployed build only in a test file. Documentation commits after the deployment do not change the deployed release identity

Provision the main worktree with `uv sync --frozen --python 3.13.14 --extra proxy --group proxy-dev --group e2e-dev`, then run `uv run --no-sync python scripts/prisma_generate_if_needed.py`. Before a build, verify the installed package versions, runtime source hashes, build commit and image digest. `docker/Dockerfile.deepseek-vision` pins the verified production image and checks the package versions

The DeepSeek user-image and user-tool-result-image fixes are present. Routing statistics, model status, the original tool-choice fix, dependency lock, database schema and UI remain from the verified 1.99.1 baseline. The 81 model configurations are unchanged. Official `deepseek-flash` remains order 1 and its backup remains order 2

Historical branches, stash entries and the independent WIP commit are preserved by archive refs and a verified private bundle. They are not part of the production baseline. The old DeepSeek branch also contains infrastructure files for Casdoor, FRP, Lucky and public-edge. Those files require a separate infrastructure review and were not deployed by this consolidation

See [UPGRADE_V1_99_1.md](UPGRADE_V1_99_1.md) for historical migration evidence and [deploy/INFRASTRUCTURE_ARCHIVE.md](deploy/INFRASTRUCTURE_ARCHIVE.md) for deferred infrastructure material
