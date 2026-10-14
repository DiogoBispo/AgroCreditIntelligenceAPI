#!/bin/sh
# Ambiente local: cria os papéis (sem login) e um usuário de login por papel.
# Em produção, usuários e senhas vêm do gestor de segredos; as permissões vêm das migrations.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v api_pw="$API_DB_PASSWORD" -v etl_pw="$ETL_DB_PASSWORD" -v maint_pw="$MAINT_DB_PASSWORD" <<'SQL'
CREATE ROLE role_api NOLOGIN;
CREATE ROLE role_etl NOLOGIN;
CREATE ROLE role_maint NOLOGIN;
CREATE ROLE agrocredit_api LOGIN PASSWORD :'api_pw' IN ROLE role_api;
CREATE ROLE agrocredit_etl LOGIN PASSWORD :'etl_pw' IN ROLE role_etl;
CREATE ROLE agrocredit_maint LOGIN PASSWORD :'maint_pw' IN ROLE role_maint;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
