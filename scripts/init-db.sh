#!/usr/bin/env sh
# Creates tutor_scheduling DB alongside the default family_copilot DB.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    SELECT 'CREATE DATABASE tutor_scheduling'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'tutor_scheduling')\gexec
EOSQL
