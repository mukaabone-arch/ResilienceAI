#!/bin/sh
# Runs once, when the database volume is first created.
# The API connects as app_user, which doesn't own the tables, so row-level security
# and the append-only audit grants are enforced. Migrations run as the owner.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE app_user LOGIN PASSWORD :'app_password';
GRANT CONNECT ON DATABASE resilience TO app_user;
SQL
