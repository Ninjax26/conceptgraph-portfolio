#!/usr/bin/env bash
set -euo pipefail

mkdir -p \
  app/api/v1 \
  app/core \
  app/db \
  app/models \
  app/schemas \
  app/services \
  app/tasks \
  app/utils \
  data/neo4j/data \
  data/neo4j/logs \
  data/neo4j/import \
  data/neo4j/plugins \
  data/qdrant \
  data/postgres

touch \
  app/__init__.py \
  app/api/__init__.py \
  app/api/v1/__init__.py \
  app/core/__init__.py \
  app/db/__init__.py \
  app/models/__init__.py \
  app/schemas/__init__.py \
  app/services/__init__.py \
  app/tasks/__init__.py \
  app/utils/__init__.py \
  app/services/graph_service.py \
  app/services/rag_service.py

echo "ConceptGraph project scaffold created."
