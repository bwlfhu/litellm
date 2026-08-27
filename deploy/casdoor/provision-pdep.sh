#!/usr/bin/env bash
set -euo pipefail

env_file="${HOME}/.env"
if [[ ! -f "$env_file" ]]; then
  printf '%s\n' "Missing $env_file" >&2
  exit 1
fi

set -a
. "$env_file"
set +a

for key in CASDOOR_ADMIN_PASSWORD CASDOOR_PDEP_CLIENT_ID CASDOOR_PDEP_CLIENT_SECRET; do
  if [[ -z "${!key:-}" ]]; then
    printf '%s\n' "Missing $key in $env_file" >&2
    exit 1
  fi
done

admin_password_hash=$(CASDOOR_ADMIN_PASSWORD="$CASDOOR_ADMIN_PASSWORD" python3 -c 'import bcrypt, os; print(bcrypt.hashpw(os.environ["CASDOOR_ADMIN_PASSWORD"].encode(), bcrypt.gensalt()).decode())')
created_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)

kubectl -n identity exec statefulset/casdoor-postgres -- psql -U casdoor -d casdoor \
  -c "UPDATE \"user\" SET password = '$admin_password_hash', password_type = 'bcrypt', password_salt = '', updated_time = '$created_time' WHERE owner = 'built-in' AND name = 'admin';"

kubectl -n identity exec statefulset/casdoor-postgres -- psql -U casdoor -d casdoor \
  -c "INSERT INTO application (owner, name, created_time, display_name, organization, grant_types, token_format, token_signing_method, expire_in_hours, refresh_expire_in_hours, enable_password, enable_sign_up, enable_guest_signin, disable_signin, enable_signin_session, client_id, client_secret, redirect_uris, providers) VALUES ('admin', 'pdep', '$created_time', 'PDEP', 'built-in', '[\"authorization_code\",\"refresh_token\"]', 'JWT', 'RS256', 24, 168, false, true, false, false, true, '$CASDOOR_PDEP_CLIENT_ID', '$CASDOOR_PDEP_CLIENT_SECRET', '[\"https://pdep.allcam.org/api/v1/auth/sso/callback\"]', '[]') ON CONFLICT (owner, name) DO UPDATE SET display_name = EXCLUDED.display_name, organization = EXCLUDED.organization, grant_types = EXCLUDED.grant_types, token_format = EXCLUDED.token_format, token_signing_method = EXCLUDED.token_signing_method, expire_in_hours = EXCLUDED.expire_in_hours, refresh_expire_in_hours = EXCLUDED.refresh_expire_in_hours, enable_password = EXCLUDED.enable_password, enable_sign_up = EXCLUDED.enable_sign_up, enable_guest_signin = EXCLUDED.enable_guest_signin, disable_signin = EXCLUDED.disable_signin, enable_signin_session = EXCLUDED.enable_signin_session, client_id = EXCLUDED.client_id, client_secret = EXCLUDED.client_secret, redirect_uris = EXCLUDED.redirect_uris, providers = EXCLUDED.providers;"

kubectl -n identity exec statefulset/casdoor-postgres -- psql -U casdoor -d casdoor \
  -Atc "select name || ':' || client_id || ':' || redirect_uris from application where owner = 'admin' and name = 'pdep'" \
  | sed 's/:.*:/:<stored>:/'
