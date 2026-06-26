#!/usr/bin/env bash
# Runs once on first Postgres boot. Creates the second database used for
# Django's system tables (auth, sessions, api keys). The primary "grader"
# database is created by POSTGRES_DB.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    SELECT 'CREATE DATABASE grader_system'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'grader_system')\gexec
EOSQL
