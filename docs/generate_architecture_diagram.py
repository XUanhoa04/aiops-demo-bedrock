#!/usr/bin/env python3
"""
Generate SentinelLoop architecture diagram with diagrams.mingrammer.com
(https://diagrams.mingrammer.com/).

Requires:
  pip install diagrams
  Graphviz — `dot` on PATH (https://graphviz.org)

Usage (repo root):
  python docs/generate_architecture_diagram.py

Output:
  docs/architecture-sentinel-loop.png
"""

from __future__ import annotations

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.onprem.client import User
from diagrams.onprem.compute import Server
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.monitoring import Grafana, Prometheus
from diagrams.programming.framework import FastAPI
from diagrams.programming.language import Python
from diagrams.generic.database import SQL
from diagrams.generic.storage import Storage
from diagrams.generic.compute import Rack

OUT_DIR = Path(__file__).resolve().parent
OUT_NAME = "architecture-sentinel-loop"


def main() -> None:
    graph_attr = {
        "fontsize": "13",
        "bgcolor": "white",
        "pad": "0.45",
        "splines": "spline",
        "nodesep": "0.55",
        "ranksep": "0.8",
        "dpi": "150",
    }

    with Diagram(
        "SentinelLoop — Explainable AIOps Closed Loop",
        filename=str(OUT_DIR / OUT_NAME),
        outformat="png",
        show=False,
        direction="TB",
        graph_attr=graph_attr,
    ):
        operator = User("Operator / On-call")

        with Cluster("1 · Demo apps (4-service topology + OTel)"):
            checkout = FastAPI("checkout\n:8080")
            inventory = FastAPI("inventory\n:8082")
            payment = FastAPI("payment\n:8081")
            fraud = FastAPI("fraud\n:8083")
            checkout >> Edge(label="POST /reserve") >> inventory
            checkout >> Edge(label="POST /pay") >> payment
            payment >> Edge(label="POST /score") >> fraud

        with Cluster("2 · Observability backbone (LGTM)"):
            prom = Prometheus("Prometheus\nRED / OTel metrics")
            loki = Storage("Loki\nlogs")
            tempo = Rack("Tempo\ntraces")
            grafana = Grafana("Grafana\nExplore deep-links")
            [prom, loki, tempo] >> grafana

        with Cluster("3 · Detect & score"):
            detector = Python(
                "anomaly-detector\nEWMA · Z · STL · IForest\n+ multi-signal confidence"
            )
            redis = Redis("Redis\naiops:anomalies")

        with Cluster("4 · Single control plane"):
            im = FastAPI("incident-manager\ntickets · topology UI · Trace links")
            decision = Python(
                "decision-engine\n≥85 gated rem · 60–85 RCA · <60 escalate"
            )
            rca = Python(
                "rca-engine\ntopology + config patterns\nBedrock | rule fallback"
            )
            rem = Server("remediation\nrisk-gated + optional API key")
            topo = SQL("topology YAML\n+ rca_patterns.yaml")

        with Cluster("5 · Learn (meta-SLOs)"):
            feedback = Python("feedback-collector")
            engqa = Python("engine-qa\nprecision · FP · hallucination")
            console = Python("aiops-console\n:8500")

        # Operator
        operator >> Edge(label="load / chaos") >> checkout
        operator >> console
        operator >> grafana
        operator >> rem

        # Telemetry (OTLP into LGTM)
        for app in (checkout, inventory, payment, fraud):
            app >> Edge(label="OTLP", style="dashed", color="#666666") >> prom
            app >> Edge(style="dashed", color="#666666") >> loki
            app >> Edge(style="dashed", color="#666666") >> tempo

        # Detect
        prom >> Edge(label="PromQL pull") >> detector
        detector >> Edge(label="AnomalyEvent + confidence") >> redis
        redis >> im
        detector >> Edge(label="webhook") >> im

        # Decide (single control plane — IM does not always call RCA)
        im >> Edge(label="policy") >> decision
        decision >> Edge(label="medium / unknown") >> rca
        decision >> Edge(label="high + pattern") >> rem
        decision >> Edge(label="low conf", style="dotted") >> im
        topo >> Edge(label="neighbors + patterns") >> rca

        # Evidence for RCA
        prom >> Edge(style="dashed", color="#2E86AB") >> rca
        loki >> Edge(style="dashed", color="#2E86AB") >> rca
        tempo >> Edge(style="dashed", color="#2E86AB") >> rca

        rca >> Edge(label="root_cause + trace_id") >> im
        rca >> rem
        rem >> Edge(label="chaos reset / propose", style="dotted") >> checkout

        # Learn + deep-link
        im >> feedback
        im >> engqa
        console >> im
        im >> Edge(label="View Trace / topology") >> grafana
        feedback >> Edge(style="dotted") >> prom
        engqa >> Edge(style="dotted") >> prom

    out = OUT_DIR / f"{OUT_NAME}.png"
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
