"""Profile the actual graph query against an isolated synthetic Neo4j container."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.rag_service import RetrievalService


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def db_hits(plan):
    return plan["dbHits"] + sum(db_hits(child) for child in plan.get("children", []))


def profile(driver, query):
    with driver.session() as session:
        result = session.run("PROFILE " + query, terms=["fixture"], course_ids=["course"],
                             document_ids=["ready"])
        anchors = [row["concept"]["id"] for row in result]
        return {"anchor_ids": anchors, "db_hits": db_hits(result.consume().profile)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-query", type=Path, help="Optional previously saved query to compare.")
    args = parser.parse_args()
    name = "conceptgraph-profile-" + uuid4().hex
    docker("run", "--pull=never", "--rm", "-d", "--name", name, "-p", "127.0.0.1::7687",
           "-e", "NEO4J_AUTH=none", "neo4j:5-community")
    try:
        port = docker("port", name, "7687").rsplit(":", 1)[1]
        with GraphDatabase.driver(f"bolt://127.0.0.1:{port}", auth=None) as driver:
            for attempt in range(90):
                try:
                    driver.verify_connectivity()
                    break
                except ServiceUnavailable:
                    if attempt == 89:
                        raise
                    time.sleep(1)
            driver.execute_query("""CREATE (c:Course {id:'course'})
                WITH c UNWIND range(1,300) AS i
                CREATE (c)-[:CONTAINS]->(:Concept {name:'fixture '+toString(i),
                        id:toString(i),upload_id:'ready'})""")
            driver.execute_query("""MATCH (a:Concept),(b:Concept)
                WHERE toInteger(b.id)-toInteger(a.id) IN [1,2,3]
                CREATE (a)-[:PREREQUISITE_OF]->(b)""")
            results = {}
            if args.baseline_query:
                results["baseline"] = profile(driver, args.baseline_query.read_text())
            query = RetrievalService._fallback_cypher("fixture", max_hops=2).cypher
            results["current"] = profile(driver, query)
            print(json.dumps(results, indent=2))
    finally:
        docker("stop", "-t", "1", name)


if __name__ == "__main__":
    main()
