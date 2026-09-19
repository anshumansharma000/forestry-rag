"""Request-local generation accounting; never records prompts or answer text."""

import logging
import os
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

from generation_cost import COST_POLICY_VERSION, generation_cost, positive_setting

logger = logging.getLogger(__name__)
_usage: ContextVar[dict | None] = ContextVar("generation_usage", default=None)
FIELDS = {
    "prompt_tokens": "promptTokenCount",
    "output_tokens": "candidatesTokenCount",
    "thinking_tokens": "thoughtsTokenCount",
    "cached_tokens": "cachedContentTokenCount",
    "total_tokens": "totalTokenCount",
}


def current_query_id() -> str | None:
    usage = _usage.get()
    return usage["query_id"] if usage is not None else None


def record_unmetered_attempt() -> None:
    usage = _usage.get()
    if usage is not None:
        usage["unmetered_attempts"] += 1


def record_generation(operation: str, model: str, metadata: dict) -> str | None:
    usage = _usage.get()
    if usage is None:
        return None
    usage["calls"].append({"operation": operation, "model": model, **metadata, **generation_cost(model, metadata)})
    return usage["query_id"]


def track_query_usage(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _usage.get() is not None:
            return function(*args, **kwargs)
        usage = {"query_id": uuid4().hex, "calls": [], "unmetered_attempts": 0}
        token = _usage.set(usage)
        succeeded = False
        try:
            result = function(*args, **kwargs)
            succeeded = True
            return result
        finally:
            _usage.reset(token)
            calls = usage["calls"]
            costs = {}
            for currency in ("usd", "inr"):
                key = f"estimated_cost_{currency}"
                costs[key] = (
                    sum(call[key] for call in calls)
                    if not usage["unmetered_attempts"] and all(call[key] is not None for call in calls) else None
                )
            max_cost = positive_setting("COST_QUERY_MAX_INR", 1.5)
            inr = costs["estimated_cost_inr"]
            logger.info(
                "query_generation_usage",
                extra={
                    "query_id": usage["query_id"],
                    "operation": function.__name__,
                    "succeeded": succeeded,
                    "generation_calls": len(calls),
                    "generation_operations": [call["operation"] for call in calls],
                    "generation_models": [call["model"] for call in calls],
                    "cost_policy_version": COST_POLICY_VERSION,
                    "cost_optimizations_enabled": os.getenv("RAG_COST_OPTIMIZATIONS", "true").strip().lower() in {"1", "true", "yes"},
                    **costs,
                    "cost_complete": costs["estimated_cost_usd"] is not None,
                    "unmetered_generation_attempts": usage["unmetered_attempts"],
                    "cost_max_inr": max_cost,
                    "cost_exceeded": inr > max_cost if inr is not None and max_cost is not None else None,
                    "verification_escalations": sum(call["operation"] == "verify_complex" for call in calls),
                    "stage_costs": [
                        {key: call[key] for key in ("operation", "model", "estimated_cost_usd", "estimated_cost_inr")}
                        for call in calls
                    ],
                    # Missing upstream counts remain visible rather than being treated as zero.
                    **{
                        name: sum(call[field] for call in calls)
                        if all(isinstance(call.get(field), int) for call in calls) else None
                        for name, field in FIELDS.items()
                    },
                },
            )

    return wrapped
