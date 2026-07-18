"""
Execute (or simulate) remediation actions.

Low-risk demo path
------------------
Call checkout/payment POST /chaos to reset error injection — reversible,
safe for laptop demos.

High-risk path
--------------
Restart / scale: attempt Docker SDK if available, otherwise print the
exact docker/kubectl command and mark status=simulated.
Production would go through SSM / Ansible / Argo / change ticket.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from app.config import settings
from app.models import ActionRecord, ActionStatus, ActionType, RiskLevel, utc_now

logger = logging.getLogger(__name__)


class ActionExecutor:
    def __init__(self) -> None:
        self._http = httpx.Client(timeout=httpx.Timeout(8.0, connect=2.0))
        self._docker = None
        self._container_map: dict[str, str] = {}
        try:
            self._container_map = json.loads(settings.container_map_json or "{}")
        except json.JSONDecodeError:
            self._container_map = {}
        self._init_docker()

    def _init_docker(self) -> None:
        if not settings.docker_host and not self._socket_hint():
            logger.info("docker client disabled (no DOCKER_HOST / socket)")
            return
        try:
            import docker  # type: ignore

            if settings.docker_host:
                self._docker = docker.DockerClient(base_url=settings.docker_host)
            else:
                self._docker = docker.from_env()
            self._docker.ping()
            logger.info("docker client ready")
        except Exception as exc:
            self._docker = None
            logger.warning("docker unavailable — will simulate: %s", exc)

    @staticmethod
    def _socket_hint() -> bool:
        from pathlib import Path

        return Path("/var/run/docker.sock").exists()

    def close(self) -> None:
        self._http.close()
        if self._docker is not None:
            try:
                self._docker.close()
            except Exception:
                pass

    def service_base_url(self, service: str) -> str:
        s = (service or "").lower()
        if "payment" in s:
            return settings.payment_url.rstrip("/")
        return settings.checkout_url.rstrip("/")

    def container_name(self, service: str) -> str:
        return self._container_map.get(service) or self._container_map.get(
            service.replace("_", "-"), service
        )

    def execute(self, rec: ActionRecord, *, executed_by: str) -> ActionRecord:
        """Run the action; mutates and returns the record (caller persists)."""
        rec.executed_by = executed_by
        if settings.simulate_only:
            return self._simulate(rec, note="SIMULATE_ONLY=true")

        atype = rec.action_type
        try:
            if atype == ActionType.RESET_ERROR_RATE.value:
                return self._reset_chaos(rec, error_rate=0.01, extra_latency_ms=0)
            if atype == ActionType.RESET_LATENCY.value:
                return self._reset_chaos(rec, extra_latency_ms=0)
            if atype == ActionType.LOG_ONLY.value:
                rec.command = f"# log-only: {rec.action_text}"
                rec.status = ActionStatus.EXECUTED
                rec.result = "logged suggestion (no side effects)"
                rec.executed_at = utc_now()
                return rec
            if atype == ActionType.MARK_FALSE_POSITIVE.value:
                rec.status = ActionStatus.EXECUTED
                rec.result = "false-positive handled by incident PATCH (caller)"
                rec.executed_at = utc_now()
                return rec
            if atype == ActionType.RESTART_SERVICE.value:
                return self._restart_service(rec)
            if atype == ActionType.SCALE_DEPLOYMENT.value:
                return self._scale_deployment(rec)
            # custom high-risk: never auto-mutate infra
            return self._simulate(rec, note="custom/high-risk — command only")
        except Exception as exc:
            logger.exception("execute failed action=%s", rec.id)
            rec.status = ActionStatus.FAILED
            rec.result = str(exc)
            rec.executed_at = utc_now()
            return rec

    def _reset_chaos(
        self,
        rec: ActionRecord,
        *,
        error_rate: Optional[float] = None,
        extra_latency_ms: Optional[float] = None,
        base_latency_ms: Optional[float] = None,
    ) -> ActionRecord:
        base = self.service_base_url(rec.target_service)
        payload: dict[str, Any] = {}
        if error_rate is not None:
            payload["error_rate"] = error_rate
        if extra_latency_ms is not None:
            payload["extra_latency_ms"] = extra_latency_ms
        if base_latency_ms is not None:
            payload["base_latency_ms"] = base_latency_ms
        if not payload:
            payload = {"error_rate": 0.01, "extra_latency_ms": 0}

        rec.payload = payload
        rec.command = f"POST {base}/chaos {json.dumps(payload)}"
        if settings.simulate_only:
            return self._simulate(rec, note="chaos reset simulated")

        resp = self._http.post(f"{base}/chaos", json=payload)
        rec.executed_at = utc_now()
        if resp.is_success:
            rec.status = ActionStatus.EXECUTED
            rec.result = resp.text[:1000]
        else:
            rec.status = ActionStatus.FAILED
            rec.result = f"HTTP {resp.status_code}: {resp.text[:500]}"
        return rec

    def _restart_service(self, rec: ActionRecord) -> ActionRecord:
        cname = self.container_name(rec.target_service)
        rec.command = f"docker restart {cname}"
        rec.payload = {"container": cname, "service": rec.target_service}

        if self._docker is None:
            return self._simulate(
                rec,
                note="docker SDK unavailable — would run: " + rec.command,
            )

        try:
            container = self._docker.containers.get(cname)
            container.restart(timeout=10)
            rec.status = ActionStatus.EXECUTED
            rec.result = f"restarted container {cname}"
            rec.executed_at = utc_now()
            return rec
        except Exception as exc:
            # Fall back to simulate rather than hard-fail demos without compose apps
            logger.warning("docker restart failed: %s", exc)
            return self._simulate(rec, note=f"docker restart error: {exc}")

    def _scale_deployment(self, rec: ActionRecord) -> ActionRecord:
        cname = self.container_name(rec.target_service)
        replicas = int(rec.payload.get("replicas") or 2)
        # Compose doesn't scale named single containers the same way as k8s;
        # log a realistic kubectl + docker compose command pair.
        kubectl = (
            f"kubectl scale deployment/{rec.target_service} "
            f"--replicas={replicas}"
        )
        compose_cmd = f"docker compose up -d --scale {rec.target_service}={replicas}"
        rec.command = f"{kubectl}  # or: {compose_cmd}"
        rec.payload = {
            **rec.payload,
            "replicas": replicas,
            "container": cname,
        }

        if self._docker is None:
            return self._simulate(rec, note="scale requires orchestration — simulated")

        # Docker Engine alone cannot "scale a deployment"; simulate with annotation.
        return self._simulate(
            rec,
            note=(
                f"docker SDK present but scale is k8s/compose-level; "
                f"logged command only (replicas={replicas})"
            ),
        )

    def _simulate(self, rec: ActionRecord, *, note: str) -> ActionRecord:
        rec.status = ActionStatus.SIMULATED
        rec.result = note
        if not rec.command:
            rec.command = f"# simulate {rec.action_type} on {rec.target_service}: {rec.action_text}"
        rec.executed_at = utc_now()
        logger.warning(
            "SIMULATED action id=%s type=%s cmd=%s note=%s",
            rec.id,
            rec.action_type,
            rec.command,
            note,
        )
        return rec


def risk_allows_auto(rec: ActionRecord) -> bool:
    return rec.risk_level == RiskLevel.LOW
