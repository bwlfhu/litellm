# PDEP OIDC Integration

PDEP must use the authorization-code flow with PKCE. Its OIDC discovery URL is `https://sso.allcam.org/.well-known/openid-configuration`.

Use the following values from `/home/allcam/.env` in the PDEP runtime Secret: `CASDOOR_PDEP_CLIENT_ID` and `CASDOOR_PDEP_CLIENT_SECRET`. The callback URL must remain `https://pdep.allcam.org/api/v1/auth/sso/callback` and the requested scopes should be `openid profile email`.

PDEP must generate and validate `state` and `nonce`, use PKCE `S256`, exchange the callback code at the discovery document token endpoint, validate the RS256 signature against the discovery document JWKS URI, and require matching `iss`, `aud`, `exp`, and nonce claims. Persist Casdoor `sub` as the external identity key; email is an attribute, not an identity key.

The current PDEP production image has no Casdoor/OIDC configuration keys. Its upcoming release must add the configuration and callback flow before the two client values are copied into `pdep-app-secret`.

# Feishu Provider

Create a separate production Feishu/Lark OAuth application. Its redirect URI must be `https://sso.allcam.org/callback`. Store its App ID and App Secret as `CASDOOR_FEISHU_APP_ID` and `CASDOOR_FEISHU_APP_SECRET` in `/home/allcam/.env`, then run `./configure-feishu.sh`.

The script makes Feishu the PDEP application's sign-in provider and allows Casdoor to create a local user from a successful Feishu authorization. Complete the authorization test only with a production Feishu user that is permitted by the Feishu application configuration.
