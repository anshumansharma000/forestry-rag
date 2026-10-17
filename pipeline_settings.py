"""Validated, independently reversible answer-pipeline configuration."""

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass

from settings import env_bool


def integer(name: str, default: int, low: int, high: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def choice(name: str, default: str, values: tuple[str, ...]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in values:
        raise ValueError(f"{name} must be one of {values}")
    return value


@dataclass(frozen=True)
class PipelineSettings:
    context_v2: bool
    resolver: str
    prune_decisions: bool
    shadow_sample_rate: float
    planning: str
    route: str
    lite_first: bool
    evidence: str
    adaptive: bool
    verification: str
    embedding_cache: bool
    retrieval_cache: bool
    answer_cache: bool
    concurrency: int
    batch_neighbors: bool
    optional_followup: bool
    extraction_diagnostics: bool
    http_pooling: bool
    routing_policy: str
    audit_policy: str
    compaction: str

    @property
    def shared_context(self):
        return self.context_v2 or self.routing_policy == "jev-led-v1" or self.audit_policy == "independent-v1"

    @classmethod
    def read(cls):
        rate = float(os.getenv("JEV_SHADOW_SAMPLE_RATE", "1"))
        if not math.isfinite(rate) or not 0 <= rate <= 1:
            raise ValueError("JEV_SHADOW_SAMPLE_RATE must be between 0 and 1")
        return cls(
            env_bool("RAG_QUERY_CONTEXT_V2"),
            choice("RAG_CONTEXT_RESOLVER", "legacy", ("legacy", "combined")),
            env_bool("RAG_PRUNE_INEFFECTIVE_DECISIONS"),
            rate,
            choice("RAG_PLANNING_POLICY", "legacy", ("legacy", "v2")),
            choice("RAG_ROUTE_POLICY", "legacy", ("legacy", "v2")),
            env_bool("RAG_LITE_FIRST", True),
            choice("RAG_EVIDENCE_POLICY", "legacy", ("legacy", "v2")),
            env_bool("RAG_ADAPTIVE_RETRIEVAL"),
            choice("RAG_VERIFICATION_POLICY", "legacy", ("legacy", "v2")),
            env_bool("RAG_EMBEDDING_CACHE"),
            env_bool("RAG_RETRIEVAL_CACHE"),
            env_bool("RAG_ANSWER_CACHE"),
            integer("RAG_RETRIEVAL_CONCURRENCY", 1, 1, 2),
            env_bool("RAG_BATCH_NEIGHBORS"),
            env_bool("RAG_OPTIONAL_FOLLOWUP"),
            env_bool("RAG_EXTRACTION_DIAGNOSTICS"),
            env_bool("RAG_HTTP_POOLING"),
            choice("RAG_ROUTING_POLICY", "legacy", ("legacy", "jev-led-v1")),
            choice("RAG_AUDIT_POLICY", "legacy", ("legacy", "independent-v1")),
            choice("RAG_EVIDENCE_COMPACTION", "legacy", ("legacy", "exact-v1")),
        )

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:20]


def policy_digest() -> str:
    """Hash effective inputs, including legacy overrides; never emit their values."""
    # Cache cannot survive an unversioned prompt/model/configuration deployment.
    config = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("RAG_", "RETRIEVAL_", "GEMINI_", "JEV_", "EMBEDDING_", "LEGAL_REGISTRY_"))
        and not k.endswith(("_KEY", "_TOKEN", "_SECRET"))
        and not any(secret in k for secret in ("SECRET", "URL", "PASSWORD", "CREDENTIAL"))
    }
    return hashlib.sha256(
        json.dumps({"recipe": "pipeline-v3.0", "config": config, "settings": asdict(PipelineSettings.read())}, sort_keys=True).encode()
    ).hexdigest()


def safe_status():
    try:
        policy = PipelineSettings.read()
        for name, default, low, high in (
            ("RAG_CACHE_WAIT_MS", 100, 0, 3000),
            ("RAG_CACHE_LOCK_SECONDS", 60, 1, 300),
            ("RAG_CACHE_MAX_ENTRIES", 512, 1, 10000),
            ("RAG_CACHE_MAX_VALUE_BYTES", 524288, 1024, 4194304),
            ("RAG_RETRIEVAL_CACHE_TTL_SECONDS", 300, 1, 300),
            ("RAG_ANSWER_CACHE_TTL_SECONDS", 300, 1, 300),
        ):
            integer(name, default, low, high)
        return {
            "valid": True,
            "settings": asdict(policy),
            "config_digest": policy_digest(),
            "cache_configured": bool(os.getenv("RAG_CACHE_REDIS_URL")) and len(os.getenv("RAG_CACHE_HMAC_KEY", "").encode()) >= 32,
        }
    except (ValueError, TypeError):
        return {"valid": False, "invalid": "valid answer_pipeline settings"}
