"""
aiops_shared — common models, OTEL bootstrap, Redis helpers for AIOps Demo.

Production choice: a tiny internal package copied into each image (see Dockerfiles)
avoids a private PyPI / multi-repo monorepo for a single-repo CV demo, while still
keeping DRY contracts between detector, incident-manager, RCA topology, and remediation.
"""

from aiops_shared.models import (
    AnomalyEvent,
    AnomalySeverity,
    Incident,
    IncidentStatus,
    RemediationAction,
)
from aiops_shared.otel import setup_otel
from aiops_shared.logging_config import setup_logging

__all__ = [
    "AnomalyEvent",
    "AnomalySeverity",
    "Incident",
    "IncidentStatus",
    "RemediationAction",
    "setup_otel",
    "setup_logging",
]

__version__ = "0.1.0"
