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
        "fontsize": "14",
        "bgcolor": "white",
        "pad": "0.5",
        "splines": "spline",
        "nodesep": "0.6",
        "ranksep": "0.85",
        "dpi": "140",
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

        with Cluster("1 · Demo traffic (OpenTelemetry)"):
            checkout = FastAPI("checkout-service\n:8080")
            payment = FastAPI("payment-service\n:8081")
            checkout >> Edge(label="POST /pay") >> payment

        with Cluster("2 · Observability backbone (LGTM)"):
            prom = Prometheus("Prometheus\nRED metrics")
            loki = Storage("Loki\nlogs")
            tempo = Rack("Tempo\ntraces")
            grafana = Grafana("Grafana\nExplore / dashboards")
            [prom, loki, tempo] >> grafana

        with Cluster("3 · Detect & score"):
            detector = Python(
                "anomaly-detector\nEWMA · Z · STL · IForest\n+ multi-signal confidence"
            )
            redis = Redis("Redis queues\nanomalies · decisions")

        with Cluster("4 · Decide & act"):
            im = FastAPI("incident-manager\ncorrelate · tickets · deep-links")
            decision = Python("decision-engine\n≥85 gated auto · 60–85 RCA · <60 escalate")
            rca = Python("rca-engine\ntopology-aware RCA\nBedrock | rule fallback")
            rem = Server("remediation\nrisk-gated propose/approve")
            topo = SQL("service topology\nconfig/*.yaml")

        with Cluster("5 · Learn (meta-SLOs)"):
            feedback = Python("feedback-collector")
            engqa = Python("engine-qa\nprecision · FP · hallucination")
            console = Python("aiops-console\n:8500")

        # Operator drives apps + UIs
        operator >> Edge(label="load / chaos") >> checkout
        operator >> console
        operator >> grafana
        operator >> rem

        # Telemetry
        checkout >> Edge(label="OTLP", style="dashed", color="#666666") >> prom
        payment >> Edge(style="dashed", color="#666666") >> prom
        checkout >> Edge(style="dashed", color="#666666") >> loki
        payment >> Edge(style="dashed", color="#666666") >> tempo

        # Detect
        prom >> Edge(label="PromQL pull") >> detector
        detector >> Edge(label="AnomalyEvent + confidence") >> redis
        redis >> im
        detector >> Edge(label="webhook") >> im

        # Decide
        im >> decision
        decision >> Edge(label="medium band") >> rca
        decision >> Edge(label="high + known pattern") >> rem
        im >> Edge(label="async analyze") >> rca
        topo >> Edge(label="upstream / downstream") >> rca

        # Evidence for RCA
        prom >> Edge(style="dashed", color="#2E86AB") >> rca
        loki >> Edge(style="dashed", color="#2E86AB") >> rca
        tempo >> Edge(style="dashed", color="#2E86AB") >> rca

        rca >> Edge(label="root_cause + trace_id") >> im
        rca >> rem
        rem >> Edge(label="reset chaos / propose", style="dotted") >> checkout

        # Learn + operator deep-link
        im >> feedback
        im >> engqa
        console >> im
        im >> Edge(label="🔍 Tempo Explore") >> grafana
        feedback >> Edge(style="dotted") >> prom
        engqa >> Edge(style="dotted") >> prom

    out = OUT_DIR / f"{OUT_NAME}.png"
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
