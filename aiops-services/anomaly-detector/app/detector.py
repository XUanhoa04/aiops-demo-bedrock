"""
Hybrid anomaly detection engine (algorithmic layer).

Methods
-------
1. **EWMA residual Z-score** (numpy; statsmodels-compatible EW variance)
   Tracks exponentially weighted mean. Residual / √EW-var → z-score sensitive
   to *level shifts* without a seasonal model.
   Why: low-latency, explainable ("2.8σ above EWMA baseline"), works with
   small windows (demo poll = 30s × 30 samples ≈ 15 min).

2. **Rolling mean / std Z-score**
   Classic stationary baseline; good when traffic is roughly flat.

3. **STL decomposition** (statsmodels, when seasonality is detectable)
   Seasonal-Trend decomposition using LOESS. Anomaly = residual z-score after
   removing seasonal + trend. Why: diurnal/weekly patterns make raw z-scores
   fire every evening peak; STL residual is the right residual for SRE SLIs
   once you have ≥ 2 seasonal cycles of data.
   *Skipped automatically* when n < 2×period or variance of seasonal component
   is negligible (no false confidence from an under-determined fit).

4. **IsolationForest (sklearn)**
   Multivariate view over [request_rate, error_rate, latency]. Captures joint
   outliers (rate↓ + latency↑) that univariate rules miss.
   Why contamination≈0.08: demo traffic is mostly healthy; too high → alert
   fatigue, too low → miss chaos injections.

5. **Absolute thresholds** (cold-start safety net)
   error_rate / latency p95 hard caps so the first 8 samples still protect SLOs.

Why hybrid (not pure ML / pure threshold)?
------------------------------------------
* Stats methods are **explainable** in incident review ("p99 latency 2.8σ…").
* IsolationForest catches **combinatorial** shapes after warm-up.
* Thresholds cover **cold start** before any model has history.
* Vote policy (`any` | `majority` | `all`) trades FN vs FP for demos vs prod.

Each MethodResult carries:
  anomaly score, detection method name, human explanation string.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Optional

import numpy as np
from sklearn.ensemble import IsolationForest

from app.config import settings

logger = logging.getLogger(__name__)

# Optional STL — keep service bootable if statsmodels wheel fails on a platform
try:
    from statsmodels.tsa.seasonal import STL

    _HAS_STL = True
except Exception:  # pragma: no cover - import guard
    STL = None  # type: ignore
    _HAS_STL = False
    logger.warning("statsmodels STL unavailable — seasonal path disabled")


@dataclass
class MethodResult:
    method: str
    score: float
    is_anomaly: bool
    detail: dict = field(default_factory=dict)

    @property
    def explanation(self) -> str:
        return str((self.detail or {}).get("explanation") or "")


@dataclass
class HybridResult:
    service: str
    metric: str  # primary metric label for alerting
    value: float
    is_anomaly: bool
    anomaly_score: float
    methods: list[MethodResult]
    features: dict[str, float]
    winning_methods: list[str]

    def method_map(self) -> dict[str, MethodResult]:
        return {m.method: m for m in self.methods}

    @property
    def primary_method(self) -> str:
        if self.winning_methods:
            return self.winning_methods[0]
        if self.methods:
            return max(self.methods, key=lambda m: m.score).method
        return "none"

    @property
    def explanation(self) -> str:
        """Aggregate on-call sentence from winning (or strongest) methods."""
        parts: list[str] = []
        for m in self.methods:
            if m.is_anomaly and m.explanation:
                parts.append(m.explanation)
        if not parts:
            # Fall back to highest-score method's explanation
            ranked = sorted(self.methods, key=lambda m: m.score, reverse=True)
            for m in ranked:
                if m.explanation:
                    parts.append(m.explanation)
                    break
        if not parts:
            parts.append(
                f"{self.metric}={self.value:.4g} hybrid_score={self.anomaly_score:.3f} "
                f"on {self.service}"
            )
        winners = ", ".join(self.winning_methods) or self.primary_method
        return f"[{winners}] " + " | ".join(parts)


class SeriesState:
    """Per-series buffers + EWMA state."""

    def __init__(self, window: int, alpha: float) -> None:
        self.values: Deque[float] = deque(maxlen=window)
        self.alpha = alpha
        self.ewma: Optional[float] = None
        self.ewma_var: float = 0.0  # EW variance of residual for adaptive std

    def update(self, x: float) -> None:
        self.values.append(x)
        if self.ewma is None:
            self.ewma = x
            self.ewma_var = 0.0
            return
        residual = x - self.ewma
        self.ewma = self.alpha * x + (1.0 - self.alpha) * self.ewma
        # Exponentially weighted variance of residuals
        self.ewma_var = (
            self.alpha * (residual**2) + (1.0 - self.alpha) * self.ewma_var
        )

    def rolling_zscore(self, x: float) -> Optional[float]:
        if len(self.values) < settings.min_samples:
            return None
        arr = np.asarray(self.values, dtype=float)
        mu = float(arr.mean())
        sigma = float(arr.std(ddof=0))
        if sigma < 1e-12:
            return 0.0 if abs(x - mu) < 1e-12 else float("inf")
        return (x - mu) / sigma

    def ewma_zscore(self, x: float) -> Optional[float]:
        if self.ewma is None or len(self.values) < settings.min_samples:
            return None
        std = float(np.sqrt(max(self.ewma_var, 0.0)))
        if std < 1e-12:
            return 0.0 if abs(x - self.ewma) < 1e-12 else float("inf")
        return (x - self.ewma) / std


def _friendly_metric_name(metric: str) -> str:
    """Human label used inside explanation sentences."""
    mapping = {
        "http_latency_p95_seconds": "p95 latency",
        "http_latency_p99_seconds": "p99 latency",
        "http_error_rate": "error rate",
        "http_request_rate": "request rate",
    }
    return mapping.get(metric, metric)


class HybridDetector:
    def __init__(self) -> None:
        self._series: dict[str, SeriesState] = defaultdict(
            lambda: SeriesState(settings.window_size, settings.ewma_alpha)
        )
        # Multivariate feature history per service for IsolationForest
        self._feature_hist: dict[str, Deque[list[float]]] = defaultdict(
            lambda: deque(maxlen=settings.window_size)
        )
        self._iforest: dict[str, IsolationForest] = {}
        self._iforest_trained_n: dict[str, int] = {}

    def evaluate_service(
        self,
        service: str,
        features: dict[str, Optional[float]],
    ) -> list[HybridResult]:
        """
        Score each available univariate metric + one multivariate IsolationForest.

        `features` keys: http_request_rate, http_error_rate, http_latency_p95_seconds
        (optional http_latency_p99_seconds if scraped).
        """
        results: list[HybridResult] = []
        clean = {k: float(v) for k, v in features.items() if v is not None}

        # --- Univariate statistical paths ---
        for metric_name, value in clean.items():
            results.append(self._score_univariate(service, metric_name, value))

        # --- Multivariate IsolationForest on full feature vector ---
        if len(clean) >= 2:
            mv = self._score_multivariate(service, clean)
            if mv is not None:
                results.append(mv)

        # --- Absolute threshold safety net on error_rate / latency ---
        if "http_error_rate" in clean:
            thr = settings.error_rate_threshold
            val = clean["http_error_rate"]
            is_anom = val >= thr
            score = val / thr if thr > 0 else val
            thr_result = MethodResult(
                method="threshold",
                score=score,
                is_anomaly=is_anom,
                detail={
                    "threshold": thr,
                    "explanation": (
                        f"error rate={val:.4g} breached absolute threshold {thr} "
                        f"(cold-start / SLO safety net)"
                        if is_anom
                        else f"error rate={val:.4g} under threshold {thr}"
                    ),
                },
            )
            for r in results:
                if r.metric == "http_error_rate":
                    r.methods.append(thr_result)
                    self._recompute_vote(r)
                    break

        latency_key = (
            "http_latency_p99_seconds"
            if "http_latency_p99_seconds" in clean
            else "http_latency_p95_seconds"
        )
        if latency_key in clean:
            thr = settings.latency_p95_seconds_threshold
            val = clean[latency_key]
            is_anom = val >= thr
            score = val / thr if thr > 0 else val
            label = _friendly_metric_name(latency_key)
            thr_result = MethodResult(
                method="threshold",
                score=score,
                is_anomaly=is_anom,
                detail={
                    "threshold": thr,
                    "explanation": (
                        f"{label}={val:.4g}s breached absolute threshold {thr}s "
                        f"(cold-start / SLO safety net)"
                        if is_anom
                        else f"{label}={val:.4g}s under threshold {thr}s"
                    ),
                },
            )
            for r in results:
                if r.metric == latency_key or (
                    latency_key == "http_latency_p99_seconds"
                    and r.metric == "http_latency_p95_seconds"
                ):
                    r.methods.append(thr_result)
                    self._recompute_vote(r)
                    break
            # If only p99 was scored as its own series, attach there too
            for r in results:
                if r.metric == latency_key and thr_result not in r.methods:
                    r.methods.append(thr_result)
                    self._recompute_vote(r)

        return results

    def _score_univariate(
        self, service: str, metric: str, value: float
    ) -> HybridResult:
        key = f"{service}:{metric}"
        state = self._series[key]
        state.update(value)

        methods: list[MethodResult] = []
        friendly = _friendly_metric_name(metric)

        # Rolling baseline (explainable: mean/std of last N samples)
        arr = np.asarray(list(state.values), dtype=float) if state.values else None
        roll_mu = float(arr.mean()) if arr is not None and len(arr) else None
        roll_sigma = float(arr.std(ddof=0)) if arr is not None and len(arr) else None

        z_roll = state.rolling_zscore(value)
        if z_roll is not None:
            score = abs(z_roll) if np.isfinite(z_roll) else 10.0
            methods.append(
                MethodResult(
                    method="zscore",
                    score=float(score),
                    is_anomaly=score >= settings.zscore_threshold,
                    detail={
                        "z": float(z_roll) if np.isfinite(z_roll) else None,
                        "sigma": round(score, 3),
                        "baseline_mean": roll_mu,
                        "baseline_std": roll_sigma,
                        "window_samples": len(state.values),
                        "value": value,
                        "explanation": (
                            f"{friendly}={value:.4g} cao hơn {score:.2f} sigma so với "
                            f"rolling mean {roll_mu:.4g} "
                            f"(std={roll_sigma:.4g}, n={len(state.values)})"
                            if roll_mu is not None and roll_sigma is not None
                            else f"{friendly} z-score={score:.2f}"
                        ),
                    },
                )
            )

        z_ewma = state.ewma_zscore(value)
        if z_ewma is not None:
            score = abs(z_ewma) if np.isfinite(z_ewma) else 10.0
            ewma_std = float(np.sqrt(max(state.ewma_var, 0.0)))
            # Prefer the interview-grade narrative style requested in the brief
            methods.append(
                MethodResult(
                    method="ewma_zscore",
                    score=float(score),
                    is_anomaly=score >= settings.zscore_threshold,
                    detail={
                        "z": float(z_ewma) if np.isfinite(z_ewma) else None,
                        "sigma": round(score, 3),
                        "ewma_baseline": state.ewma,
                        "ewma_std": ewma_std,
                        "alpha": settings.ewma_alpha,
                        "window_samples": len(state.values),
                        "poll_interval_sec": settings.poll_interval_sec,
                        "value": value,
                        "explanation": (
                            f"{friendly} cao hơn {score:.2f} sigma so với EWMA baseline "
                            f"{float(state.ewma):.4g} "
                            f"(α={settings.ewma_alpha}, value={value:.4g}, "
                            f"n={len(state.values)} samples ≈ "
                            f"{len(state.values) * settings.poll_interval_sec // 60} min window)"
                            if state.ewma is not None
                            else f"{friendly} EWMA z={score:.2f}"
                        ),
                    },
                )
            )

        # STL residual z-score when seasonality is present and sample count allows
        stl_result = self._score_stl(metric, state, value, friendly)
        if stl_result is not None:
            methods.append(stl_result)

        result = HybridResult(
            service=service,
            metric=metric,
            value=value,
            is_anomaly=False,
            anomaly_score=0.0,
            methods=methods,
            features={metric: value},
            winning_methods=[],
        )
        self._recompute_vote(result)
        return result

    def _score_stl(
        self,
        metric: str,
        state: SeriesState,
        value: float,
        friendly: str,
    ) -> Optional[MethodResult]:
        """
        STL residual anomaly detector.

        Algorithm choice notes
        ----------------------
        * period defaults to `stl_period` (env). For 30s polls, period=10 ≈ 5 min
          micro-cycles; for true daily seasonality you need hours of history and
          a much larger window — production would run STL on a longer TSDB range.
        * We require residual seasonal strength (var_seasonal / var_total) above
          `stl_min_seasonal_strength` so we do not "detect seasonality" on noise.
        * Robust=True down-weights outliers so one chaos spike does not warp
          the seasonal component forever.
        """
        if not settings.enable_stl or not _HAS_STL or STL is None:
            return None
        period = max(2, int(settings.stl_period))
        n = len(state.values)
        # statsmodels STL needs n >= 2 * period
        if n < max(settings.min_samples, 2 * period):
            return None

        arr = np.asarray(list(state.values), dtype=float)
        total_var = float(np.var(arr))
        if total_var < 1e-18:
            return None

        try:
            stl = STL(arr, period=period, robust=True)
            res = stl.fit()
        except Exception as exc:
            logger.debug("STL fit failed metric=%s err=%s", metric, exc)
            return None

        seasonal = np.asarray(res.seasonal, dtype=float)
        residual = np.asarray(res.resid, dtype=float)
        seasonal_var = float(np.var(seasonal))
        strength = seasonal_var / total_var if total_var > 0 else 0.0

        if strength < settings.stl_min_seasonal_strength:
            # No meaningful seasonality — do not emit a weak STL vote
            logger.debug(
                "STL skipped metric=%s seasonal_strength=%.4f < %.4f",
                metric,
                strength,
                settings.stl_min_seasonal_strength,
            )
            return None

        resid_std = float(np.std(residual, ddof=0))
        last_resid = float(residual[-1])
        if resid_std < 1e-12:
            z = 0.0 if abs(last_resid) < 1e-12 else 10.0
        else:
            z = last_resid / resid_std
        score = abs(z) if np.isfinite(z) else 10.0
        is_anom = score >= settings.zscore_threshold

        return MethodResult(
            method="stl",
            score=float(score),
            is_anomaly=is_anom,
            detail={
                "z": float(z) if np.isfinite(z) else None,
                "sigma": round(score, 3),
                "period": period,
                "seasonal_strength": round(strength, 4),
                "residual": last_resid,
                "residual_std": resid_std,
                "n": n,
                "value": value,
                "explanation": (
                    f"{friendly} residual cao hơn {score:.2f} sigma sau STL "
                    f"(period={period}, seasonal_strength={strength:.2f}, "
                    f"value={value:.4g}) — seasonality đã được tách khỏi residual"
                ),
            },
        )

    def _score_multivariate(
        self,
        service: str,
        features: dict[str, float],
    ) -> Optional[HybridResult]:
        # Stable feature order
        keys = [
            "http_request_rate",
            "http_error_rate",
            "http_latency_p95_seconds",
            "http_latency_p99_seconds",
        ]
        present = [k for k in keys if k in features]
        if len(present) < 2:
            return None
        vec = [features[k] for k in present]

        hist = self._feature_hist[service]
        hist.append(vec)

        method: Optional[MethodResult] = None
        if len(hist) >= settings.min_samples:
            X = np.asarray(list(hist), dtype=float)
            model = IsolationForest(
                n_estimators=settings.iforest_n_estimators,
                contamination=settings.iforest_contamination,
                random_state=42,
                n_jobs=1,
            )
            try:
                model.fit(X)
                self._iforest[service] = model
                self._iforest_trained_n[service] = len(hist)
                # decision_function: higher = more normal; invert → anomaly score
                raw = float(model.decision_function([vec])[0])
                pred = int(model.predict([vec])[0])  # -1 anomaly, 1 normal
                score = max(0.0, -raw * 5.0)  # scale for Grafana readability
                feat_summary = ", ".join(f"{k}={features[k]:.4g}" for k in present)
                method = MethodResult(
                    method="isolation_forest",
                    score=score,
                    is_anomaly=(pred == -1),
                    detail={
                        "decision_function": raw,
                        "n_samples": len(hist),
                        "features": present,
                        "contamination": settings.iforest_contamination,
                        "explanation": (
                            f"IsolationForest flagged multivariate outlier on "
                            f"{service} (score={score:.3f}, n={len(hist)}, "
                            f"features=[{feat_summary}]) — joint shape khác "
                            f"baseline, không chỉ 1 metric đơn lẻ"
                        ),
                    },
                )
            except Exception as exc:
                logger.warning("isolation_forest failed service=%s err=%s", service, exc)
                method = None

        if method is None:
            return None

        primary = "http_error_rate" if "http_error_rate" in features else present[0]
        result = HybridResult(
            service=service,
            metric=f"multivariate:{primary}",
            value=features.get(primary, vec[0]),
            is_anomaly=method.is_anomaly,
            anomaly_score=method.score,
            methods=[method],
            features=dict(features),
            winning_methods=[method.method] if method.is_anomaly else [],
        )
        self._recompute_vote(result)
        return result

    def _recompute_vote(self, result: HybridResult) -> None:
        if not result.methods:
            result.is_anomaly = False
            result.anomaly_score = 0.0
            result.winning_methods = []
            return

        flags = [m.is_anomaly for m in result.methods]
        vote = settings.hybrid_vote.lower()
        if vote == "all":
            result.is_anomaly = all(flags)
        elif vote == "majority":
            result.is_anomaly = sum(flags) > len(flags) / 2
        else:  # "any" — sensitive, good for demos
            result.is_anomaly = any(flags)

        result.anomaly_score = max(m.score for m in result.methods)
        result.winning_methods = [m.method for m in result.methods if m.is_anomaly]

    def force_score(
        self,
        service: str,
        metric: str,
        value: float,
        threshold: float,
    ) -> HybridResult:
        """Manual / API path — still updates series state for realism."""
        uni = self._score_univariate(service, metric, value)
        thr = MethodResult(
            method="manual",
            score=value / threshold if threshold else value,
            is_anomaly=value >= threshold,
            detail={
                "threshold": threshold,
                "source": "api",
                "explanation": (
                    f"Manual inject: {_friendly_metric_name(metric)}={value:.4g} "
                    f"≥ threshold {threshold}"
                ),
            },
        )
        uni.methods.append(thr)
        self._recompute_vote(uni)
        # Manual always forces anomaly if above threshold for demo reliability
        if value >= threshold:
            uni.is_anomaly = True
            if "manual" not in uni.winning_methods:
                uni.winning_methods.append("manual")
        return uni
