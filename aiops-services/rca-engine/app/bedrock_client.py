"""
Amazon Bedrock Converse client with retry, JSON parse, and cost telemetry.

Logs (every successful / failed call)
-------------------------------------
  - model_id
  - latency_ms
  - input_tokens / output_tokens / total_tokens (from Converse usage)
  - stop_reason
  - incident_id / service_name

Why Converse API?
  Unified request shape across Claude/Nova; avoids per-model invoke_model
  body formats. Production: also emit these metrics as Prometheus histograms
  (llm_latency_seconds, llm_tokens_total) for cost SLOs.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.models import EvidencePack, LLMUsage, RCAResult
from app.prompts import SYSTEM_PROMPT, build_messages

logger = logging.getLogger(__name__)


class BedrockError(Exception):
    """Raised when Bedrock call or parse fails after retries."""


class BedrockRCAClient:
    def __init__(self) -> None:
        self.model_id = settings.bedrock_model_id
        self.region = settings.aws_default_region
        self._client = None
        self.last_error: Optional[str] = None
        self.last_usage: Optional[LLMUsage] = None
        self.invocations = 0
        self.failures = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_latency_ms = 0.0

    @property
    def configured(self) -> bool:
        return bool(settings.aws_access_key_id and settings.aws_secret_access_key)

    def _get_client(self):
        if self._client is not None:
            return self._client
        kwargs: dict[str, Any] = {
            "service_name": "bedrock-runtime",
            "region_name": self.region,
            "config": BotoConfig(
                connect_timeout=5,
                read_timeout=int(settings.bedrock_timeout_sec),
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        }
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            kwargs["aws_access_key_id"] = settings.aws_access_key_id
            kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
            if settings.aws_session_token:
                kwargs["aws_session_token"] = settings.aws_session_token
        self._client = boto3.client(**kwargs)
        return self._client

    def analyze(self, pack: EvidencePack) -> tuple[RCAResult, LLMUsage]:
        """Call Bedrock Converse; return (RCAResult, LLMUsage)."""
        if not self.configured:
            raise BedrockError("AWS credentials missing (AWS_ACCESS_KEY_ID / SECRET)")

        try:
            raw_text, usage = self._converse_with_retry(pack)
            result = parse_rca_json(raw_text)
            # Prefer model-chosen trace if present in pack; else keep pack hint
            if not result.primary_trace_id and pack.primary_trace_id:
                result.primary_trace_id = pack.primary_trace_id
            if not result.why_root_cause and result.evidence:
                result.why_root_cause = (
                    "Derived from cited evidence: " + "; ".join(result.evidence[:3])
                )
            self.invocations += 1
            self.last_error = None
            self.last_usage = usage
            if usage.input_tokens:
                self.total_input_tokens += usage.input_tokens
            if usage.output_tokens:
                self.total_output_tokens += usage.output_tokens
            self.total_latency_ms += usage.latency_ms
            return result, usage
        except Exception as exc:
            self.failures += 1
            self.last_error = str(exc)
            logger.exception(
                "bedrock analyze failed incident=%s err=%s",
                pack.incident_id,
                exc,
            )
            raise BedrockError(str(exc)) from exc

    def _converse_with_retry(self, pack: EvidencePack) -> tuple[str, LLMUsage]:
        attempts = max(1, settings.bedrock_max_retries)
        attempt_box = {"n": 0}

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(
                (ClientError, BotoCoreError, TimeoutError, ConnectionError)
            ),
        )
        def _call() -> tuple[str, LLMUsage]:
            attempt_box["n"] += 1
            return self._converse_once(pack, attempt=attempt_box["n"])

        return _call()

    def _converse_once(self, pack: EvidencePack, *, attempt: int = 1) -> tuple[str, LLMUsage]:
        client = self._get_client()
        # Spec: temperature 0.1–0.2 for grounded JSON stability.
        temperature = max(0.1, min(0.2, float(settings.bedrock_temperature)))
        max_tokens = int(settings.bedrock_max_tokens)

        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": build_messages(pack),
            "system": [{"text": SYSTEM_PROMPT}],
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
                "topP": 0.9,
            },
        }

        logger.info(
            "bedrock converse start model=%s incident=%s service=%s temp=%.2f "
            "max_tokens=%s attempt=%s",
            self.model_id,
            pack.incident_id,
            pack.service_name,
            temperature,
            max_tokens,
            attempt,
        )
        t0 = time.perf_counter()
        resp = client.converse(**kwargs)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        usage = _parse_usage(resp, self.model_id, temperature, latency_ms, attempt)
        logger.info(
            "bedrock converse done incident=%s model=%s latency_ms=%.1f "
            "input_tokens=%s output_tokens=%s total_tokens=%s stop_reason=%s",
            pack.incident_id,
            self.model_id,
            usage.latency_ms,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            usage.stop_reason,
        )

        text = _extract_text(resp)
        if not text:
            raise BedrockError("empty model response")
        return text, usage

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "model_id": self.model_id,
            "region": self.region,
            "temperature": settings.bedrock_temperature,
            "max_tokens": settings.bedrock_max_tokens,
            "invocations": self.invocations,
            "failures": self.failures,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_latency_ms": round(self.total_latency_ms, 1),
            "avg_latency_ms": (
                round(self.total_latency_ms / self.invocations, 1)
                if self.invocations
                else None
            ),
            "last_error": self.last_error,
            "last_usage": self.last_usage.model_dump() if self.last_usage else None,
        }


def _parse_usage(
    resp: dict[str, Any],
    model_id: str,
    temperature: float,
    latency_ms: float,
    attempt: int,
) -> LLMUsage:
    raw = resp.get("usage") or {}
    # Converse field names (AWS): inputTokens, outputTokens, totalTokens
    input_t = raw.get("inputTokens")
    output_t = raw.get("outputTokens")
    total_t = raw.get("totalTokens")
    if total_t is None and input_t is not None and output_t is not None:
        total_t = int(input_t) + int(output_t)
    return LLMUsage(
        model_id=model_id,
        latency_ms=round(latency_ms, 1),
        input_tokens=int(input_t) if input_t is not None else None,
        output_tokens=int(output_t) if output_t is not None else None,
        total_tokens=int(total_t) if total_t is not None else None,
        cache_read_tokens=_opt_int(raw.get("cacheReadInputTokens")),
        cache_write_tokens=_opt_int(raw.get("cacheWriteInputTokens")),
        stop_reason=(resp.get("stopReason") or None),
        temperature=temperature,
        attempt=attempt,
    )


def _opt_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _extract_text(resp: dict[str, Any]) -> str:
    parts: list[str] = []
    output = resp.get("output") or {}
    message = output.get("message") or {}
    for block in message.get("content") or []:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    return "\n".join(parts).strip()


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def parse_rca_json(raw: str) -> RCAResult:
    """Parse model text into RCAResult (strict schema validation)."""
    text = (raw or "").strip()
    if not text:
        raise BedrockError("empty JSON payload")

    candidates = [text]
    m = _JSON_FENCE.search(text)
    if m:
        candidates.insert(0, m.group(1).strip())
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])

    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, list) and data:
                data = data[0]
            if not isinstance(data, dict):
                continue
            # Normalize alternate keys
            aliases = {
                "rootCause": "root_cause",
                "whyRootCause": "why_root_cause",
                "why": "why_root_cause",
                "reasoning": "why_root_cause",
                "runbookSuggestion": "runbook_suggestion",
                "affectedComponents": "affected_components",
                "suggestedActions": "suggested_actions",
                "primaryTraceId": "primary_trace_id",
                "trace_id": "primary_trace_id",
            }
            for src, dst in aliases.items():
                if src in data and dst not in data:
                    data[dst] = data.pop(src)
            return RCAResult.model_validate(data)
        except Exception as exc:
            last_err = exc
            continue
    raise BedrockError(f"failed to parse RCA JSON: {last_err}; raw={text[:300]}")
