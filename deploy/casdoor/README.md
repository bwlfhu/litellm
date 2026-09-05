# Casdoor Production Deployment

Run `./bootstrap.sh` from this directory to generate missing Casdoor credentials in `~/.env`, create the Kubernetes Secrets, copy the existing production wildcard TLS Secret, and deploy PostgreSQL and Casdoor without public ingress.

Run `./provision-pdep.sh` before applying `ingress.yaml`. It replaces the Casdoor built-in administrator password with the generated value and creates the PDEP OIDC client. The PDEP redirect URI is `https://pdep.allcam.org/api/v1/auth/sso/callback`.

The deployed Casdoor image is `sha256:fcc4b7bf301539da57b10c58d7f77be0397cd0f4f6f61da8111471636a647653`; PostgreSQL is `sha256:60b180625695e90b6e450405d5f0bcb74bc8c52c65d1cd11aeb3da46cac06dd8`. Mirror both to Harbor before adding a second node or relying on node image garbage collection.

After adding the production Feishu application credentials to `~/.env`, run `./configure-feishu.sh`. PDEP integration requirements are documented in `PDEP_OIDC.md`.
