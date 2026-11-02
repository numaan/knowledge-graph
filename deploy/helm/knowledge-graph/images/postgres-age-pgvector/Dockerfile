# PostgreSQL 17 with Apache AGE and pgvector preinstalled.
# Use this image in production in place of the apt-get install at start:
#   postgres.image=<registry>/postgres-age-pgvector:17.11
#   postgres.extensions.install=false
FROM postgres:17.11-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-17-age=1.7.0~rc0-1.pgdg12+1 \
      postgresql-17-pgvector=0.8.7-1.pgdg12+1 \
 && rm -rf /var/lib/apt/lists/*
