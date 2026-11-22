#!/usr/bin/env bash
set -Eeuo pipefail
for name in partner hatchet phoenix; do
  case "$name" in
    partner) password="$APP_DB_PASSWORD";;
    hatchet) password="$HATCHET_DB_PASSWORD";;
    phoenix) password="$PHOENIX_DB_PASSWORD";;
  esac
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v role="$name" -v password="$password" <<'SQL'
CREATE ROLE :"role" LOGIN PASSWORD :'password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE :"role" OWNER :"role";
REVOKE ALL ON DATABASE :"role" FROM PUBLIC;
SQL
done
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname partner <<'SQL'
CREATE DATABASE partner_test OWNER partner;
REVOKE ALL ON DATABASE partner_test FROM PUBLIC;
CREATE SCHEMA langgraph AUTHORIZATION partner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
