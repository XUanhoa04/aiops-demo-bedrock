#!/usr/bin/env python3
"""
Generate service topology diagram (4 demo apps).

Requires: pip install diagrams + Graphviz on PATH.

Usage:
  python docs/generate_topology_diagram.py

Output:
  docs/topology-demo-apps.png
"""

from __future__ import annotations

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.programming.framework import FastAPI
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.inmemory import Redis

OUT_DIR = Path(__file__).resolve().parent
OUT_NAME = "topology-demo-apps"


def main() -> None:
    graph_attr = {
        "fontsize": "14",
        "bgcolor": "white",
        "pad": "0.5",
        "dpi": "150",
        "ranksep": "0.7",
    }
    with Diagram(
        "SentinelLoop demo topology (default compose)",
        filename=str(OUT_DIR / OUT_NAME),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=graph_attr,
    ):
        with Cluster("Request path"):
            checkout = FastAPI("checkout-service\n:8080")
            inventory = FastAPI("inventory-service\n:8082")
            payment = FastAPI("payment-service\n:8081")
            fraud = FastAPI("fraud-service\n:8083")

        with Cluster("Shared infra (logical)"):
            redis = Redis("redis-cache")
            pg = PostgreSQL("postgres\norders / payments / inventory")

        checkout >> Edge(label="POST /reserve") >> inventory
        checkout >> Edge(label="POST /pay") >> payment
        payment >> Edge(label="POST /score") >> fraud

        inventory >> Edge(style="dashed", color="#888") >> redis
        payment >> Edge(style="dashed", color="#888") >> redis
        fraud >> Edge(style="dashed", color="#888") >> redis
        inventory >> Edge(style="dashed", color="#888") >> pg
        payment >> Edge(style="dashed", color="#888") >> pg
        checkout >> Edge(style="dashed", color="#888") >> pg

    print(f"Wrote {OUT_DIR / (OUT_NAME + '.png')}")


if __name__ == "__main__":
    main()
