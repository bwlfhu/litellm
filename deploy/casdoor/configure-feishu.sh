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

for key in CASDOOR_FEISHU_APP_ID CASDOOR_FEISHU_APP_SECRET; do
  if [[ -z "${!key:-}" ]]; then
    printf '%s\n' "Missing $key in $env_file" >&2
    exit 1
  fi
done

app_id_base64=$(printf '%s' "$CASDOOR_FEISHU_APP_ID" | base64 -w 0)
app_secret_base64=$(printf '%s' "$CASDOOR_FEISHU_APP_SECRET" | base64 -w 0)
created_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)

kubectl -n identity exec statefulset/casdoor-postgres -- psql -U casdoor -d casdoor \
  -c "INSERT INTO provider (owner, name, created_time, display_name, category, type, method, client_id, client_secret) VALUES ('admin', 'provider-feishu-pdep', '$created_time', 'Feishu', 'OAuth', 'Lark', 'Normal', convert_from(decode('$app_id_base64', 'base64'), 'UTF8'), convert_from(decode('$app_secret_base64', 'base64'), 'UTF8')) ON CONFLICT (owner, name) DO UPDATE SET display_name = EXCLUDED.display_name, category = EXCLUDED.category, type = EXCLUDED.type, method = EXCLUDED.method, client_id = EXCLUDED.client_id, client_secret = EXCLUDED.client_secret;"

kubectl -n identity exec statefulset/casdoor-postgres -- psql -U casdoor -d casdoor \
  -c "UPDATE application SET providers = '[{\"owner\":\"admin\",\"name\":\"provider-feishu-pdep\",\"canSignUp\":true,\"canSignIn\":true,\"canUnlink\":false,\"bindingRule\":[],\"countryCodes\":[],\"prompted\":false,\"signupGroup\":\"\",\"rule\":\"None\",\"provider\":null}]' WHERE owner = 'admin' AND name = 'pdep';"

kubectl -n identity exec statefulset/casdoor-postgres -- psql -U casdoor -d casdoor \
  -Atc "select name || ':' || type || ':' || category from provider where owner = 'admin' and name = 'provider-feishu-pdep'; select name || ':' || providers from application where owner = 'admin' and name = 'pdep';"
