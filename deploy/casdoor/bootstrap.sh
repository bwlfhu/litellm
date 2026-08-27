#!/usr/bin/env bash
set -euo pipefail

env_file="${HOME}/.env"
umask 077
touch "$env_file"
chmod 600 "$env_file"

append_if_missing() {
  local key="$1"
  local value="$2"
  if ! grep -q "^${key}=" "$env_file"; then
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}

append_if_missing CASDOOR_POSTGRES_DB casdoor
append_if_missing CASDOOR_POSTGRES_USER casdoor
append_if_missing CASDOOR_POSTGRES_PASSWORD "$(openssl rand -hex 32)"
append_if_missing CASDOOR_ADMIN_USERNAME admin
append_if_missing CASDOOR_ADMIN_PASSWORD "$(openssl rand -hex 32)"
append_if_missing CASDOOR_PDEP_CLIENT_ID "pdep-$(openssl rand -hex 16)"
append_if_missing CASDOOR_PDEP_CLIENT_SECRET "$(openssl rand -hex 32)"
append_if_missing CASDOOR_RADIUS_SECRET "$(openssl rand -hex 32)"
append_if_missing CASDOOR_FEISHU_APP_ID ""
append_if_missing CASDOOR_FEISHU_APP_SECRET ""

set -a
. "$env_file"
set +a

app_conf=$(printf '%s\n' \
  'appname = casdoor' \
  'httpport = 8000' \
  'runmode = prod' \
  'SessionOn = true' \
  'copyrequestbody = true' \
  'driverName = postgres' \
  "dataSourceName = \"user=${CASDOOR_POSTGRES_USER} password=${CASDOOR_POSTGRES_PASSWORD} host=casdoor-postgres port=5432 dbname=${CASDOOR_POSTGRES_DB} sslmode=disable\"" \
  "dbName = ${CASDOOR_POSTGRES_DB}" \
  'tableNamePrefix =' \
  'showSql = false' \
  'redisEndpoint =' \
  'defaultStorageProvider =' \
  'isCloudIntranet = false' \
  'authState = "casdoor"' \
  'socks5Proxy = ""' \
  'verificationCodeTimeout = 10' \
  'initScore = 0' \
  'logPostOnly = true' \
  'isUsernameLowered = false' \
  'origin = "https://sso.allcam.org"' \
  'originFrontend = "https://sso.allcam.org"' \
  'staticBaseUrl = "https://cdn.casbin.org"' \
  'isDemoMode = false' \
  'batchSize = 100' \
  'showGithubCorner = false' \
  'forceLanguage = ""' \
  'defaultLanguage = "zh"' \
  'aiAssistantUrl = "https://ai.casbin.com"' \
  'defaultApplication = "app-built-in"' \
  'maxItemsForFlatMenu = 7' \
  'enableErrorMask = false' \
  'enableGzip = true' \
  'radiusSecret = "'"$CASDOOR_RADIUS_SECRET"'"' \
  'quota = {"organization": -1, "user": -1, "application": -1, "provider": -1}' \
  'logConfig = {"adapter":"console"}' \
  'initDataNewOnly = false' \
  'initDataFile = "./init_data.json"' \
  'frontendBaseDir = "./web/build"')

kubectl apply -f "$(dirname "$0")/namespace.yaml"
kubectl -n identity create secret generic casdoor-postgres \
  --from-literal=POSTGRES_DB="$CASDOOR_POSTGRES_DB" \
  --from-literal=POSTGRES_USER="$CASDOOR_POSTGRES_USER" \
  --from-literal=POSTGRES_PASSWORD="$CASDOOR_POSTGRES_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n identity create secret generic casdoor-app-conf \
  --from-literal=app.conf="$app_conf" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl get secret -n litellm autocode-tls -o json \
  | jq 'del(.metadata.uid, .metadata.resourceVersion, .metadata.creationTimestamp, .metadata.managedFields, .metadata.namespace) | .metadata.name = "casdoor-tls"' \
  | kubectl -n identity apply -f -
kubectl apply -f "$(dirname "$0")/postgres.yaml"
kubectl apply -f "$(dirname "$0")/casdoor.yaml"
kubectl -n identity rollout status statefulset/casdoor-postgres --timeout=5m
kubectl -n identity rollout status deployment/casdoor --timeout=5m
