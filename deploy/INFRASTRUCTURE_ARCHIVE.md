# Deferred Infrastructure Material

The archived `litellm_deepseek_compat_followups` branch contains eight later infrastructure commits after the production migration. They were not part of the deployed LiteLLM image and were not merged into `mainline` during workspace consolidation

The affected areas are Casdoor documentation and provisioning, FRP server and client configuration, Lucky reverse proxy deployment, certificate renewal, and the public LiteLLM API gateway. The source files remain recoverable from `archive-litellm_deepseek_compat_followups-c7d5839f6e`

The archived paths are:

- `deploy/casdoor/PDEP_OIDC.md`
- `deploy/casdoor/README.md`
- `deploy/casdoor/bootstrap.sh`
- `deploy/casdoor/casdoor.yaml`
- `deploy/casdoor/provision-pdep.sh`
- `deploy/frp/frps.service`
- `deploy/frp/frps.toml`
- `deploy/frp/lucky-edge-cert-renew`
- `deploy/frp/lucky-edge-cert-renew.service`
- `deploy/frp/lucky-edge-cert-renew.timer`
- `deploy/frp/lucky-edge.conf`
- `deploy/lucky/deployment.yaml`
- `deploy/lucky/frpc-configmap.yaml`
- `deploy/lucky/frpc-deployment.yaml`
- `deploy/lucky/kustomization.yaml`
- `deploy/lucky/namespace.yaml`
- `deploy/lucky/pvc.yaml`
- `deploy/lucky/service.yaml`
- `deploy/public-edge/kustomization.yaml`
- `deploy/public-edge/litellm-api-gateway.yaml`

Before any future migration, compare these files with the live Kubernetes resources and edge hosts, identify the owning repository, review credentials and certificates, and validate rollback. Do not merge the whole archived branch to recover one infrastructure feature. Extract the smallest reviewed change from `mainline`, then deploy it independently with its own evidence
