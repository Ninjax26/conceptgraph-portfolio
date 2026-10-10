"""Start a local ConceptGraph process without editing .env or printing secrets."""

import argparse
import asyncio
import json
import os
import subprocess


def use_local_neo4j():
    result = subprocess.run(["docker", "inspect", "conceptgraph-neo4j"], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Start the existing ConceptGraph Neo4j container before using --local-neo4j.")
    info = json.loads(result.stdout)[0]
    auth = next((item.split("=", 1)[1] for item in info["Config"]["Env"] if item.startswith("NEO4J_AUTH=")), "")
    if not auth or auth == "none" or "/" not in auth:
        raise RuntimeError("The existing local Neo4j container has no usable scoped credential.")
    username, password = auth.split("/", 1)
    os.environ.update(NEO4J_URI="bolt://127.0.0.1:7687", NEO4J_USERNAME=username, NEO4J_PASSWORD=password)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("api", "worker", "combined"), default="combined")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--local-neo4j", action="store_true", help="Use the existing container credential in this process only")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Use a valid local port.")
    if args.local_neo4j:
        use_local_neo4j()
    os.environ.update(PROCESSING_ROLE=args.role, SHARED_PROVIDER_LIMITS_ENABLED="true",
                      MAX_PDFS_PER_INSTALLATION="0", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      LOCAL_MODEL_DEVICE="cpu", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", TOKENIZERS_PARALLELISM="false")
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.config import settings
    from scripts.evaluate_ingestion_scaling import require_local_database
    require_local_database(settings.postgres_dsn)
    if args.role == "worker":
        import logging
        from app.worker import run_worker
        logging.basicConfig(level=logging.INFO)
        asyncio.run(run_worker())
    else:
        import uvicorn
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
