"""Request-local generation accounting; never records prompts or answer text."""

import logging
import os
import threading
import time
from collections import Counter
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

from generation_cost import COST_POLICY_VERSION, embedding_cost, generation_cost, normalized_usage, positive_setting
from pipeline_trace import event, trace_request

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


def record_unmetered_attempt(operation="generation", model=None, attempt=None) -> None:
    usage = _usage.get()
    if usage is not None:
        with usage["lock"]:
            usage["unmetered_attempts"] += 1
            usage["unmetered_stages"].append({"operation": operation, "model": model, "attempt": attempt,
                                               "estimated_cost_usd": None, "estimated_cost_inr": None})


def record_generation(operation: str, model: str, metadata: dict, *, attempt=1, max_output_tokens=None,
                      thinking_level=None, elapsed_ms=None) -> str | None:
    usage = _usage.get()
    if usage is None:
        return None
    with usage["lock"]:
        usage["calls"].append({"operation": operation, "model": model, "raw_usage": dict(metadata),
                               "generation_attempt": attempt, "max_output_tokens": max_output_tokens,
                               "thinking_level": thinking_level, "elapsed_ms": elapsed_ms,
                               **normalized_usage(metadata), **generation_cost(model, metadata)})
    return usage["query_id"]


def record_verification_escalation() -> None:
    usage = _usage.get()
    if usage is not None:
        with usage["lock"]:
            usage["verification_escalations"] += 1


def record_decision(operation: str, model: str | None, metadata: dict | None, *, elapsed_ms: float, succeeded: bool) -> dict:
    metadata = metadata if isinstance(metadata, dict) else {}
    tokens = metadata.get("input_tokens")
    rate = positive_setting("JEV_INPUT_USD_PER_MILLION", 0.042)
    fx = positive_setting("COST_USD_TO_INR", 90)
    known = type(tokens) is int and tokens >= 0 and rate is not None and isinstance(model, str) and model.startswith("jev-")
    usd = tokens * rate / 1_000_000 if known else None
    cost = {"estimated_cost_usd": usd, "estimated_cost_inr": usd * fx if usd is not None and fx else None}
    usage = _usage.get()
    if usage is not None:
        with usage["lock"]:
            usage["decisions"].append(
                {"operation": operation, "model": model, "input_tokens": tokens, "raw_usage": dict(metadata),
                 "pricing_version": "configured-jev-input-rate", "input_usd_per_million": rate,
                 "elapsed_ms": elapsed_ms, "succeeded": succeeded, **cost}
            )
    return cost


def record_embedding(model: str, metadata: dict | None, *, items: int = 1, operation="embedding", attempt=1) -> None:
    usage = _usage.get()
    if usage is None:
        return
    metadata = metadata if isinstance(metadata, dict) else {}
    tokens = metadata.get("promptTokenCount")
    with usage["lock"]:
        usage["embeddings"].append(
            {
                "model": model,
                "operation": operation,
                "attempt": attempt,
                "items": items,
                "prompt_tokens": tokens,
                "raw_usage": dict(metadata),
                **embedding_cost(model, metadata),
            }
        )


def track_query_usage(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _usage.get() is not None:
            return function(*args, **kwargs)
        usage = {
            "query_id": uuid4().hex,
            "calls": [],
            "embeddings": [],
            "decisions": [],
            "unmetered_attempts": 0,
            "unmetered_stages": [],
            "verification_escalations": 0,
        }
        usage["lock"] = threading.Lock()
        started = time.perf_counter()
        trace_scope = trace_request()
        trace = trace_scope.__enter__()
        token = _usage.set(usage)
        succeeded = False
        query_outcome = "failed"
        try:
            result = function(*args, **kwargs)
            succeeded = True
            query_outcome = result.get("outcome", "completed") if isinstance(result, dict) else "completed"
            return result
        finally:
            _usage.reset(token)
            calls = usage["calls"]
            costs = {}
            for currency in ("usd", "inr"):
                key = f"estimated_cost_{currency}"
                costs[key] = (
                    sum(call[key] for call in calls)
                    if not usage["unmetered_attempts"] and all(call[key] is not None for call in calls)
                    else None
                )
            embeddings = usage["embeddings"]
            api_costs = {}
            for currency in ("usd", "inr"):
                key = f"estimated_cost_{currency}"
                embedding_total = sum(e[key] for e in embeddings) if all(e[key] is not None for e in embeddings) else None
                decisions = usage["decisions"]
                decision_total = sum(d[key] for d in decisions) if all(d[key] is not None for d in decisions) else None
                api_costs[f"embedding_cost_{currency}"] = embedding_total
                api_costs[f"decision_cost_{currency}"] = decision_total
                api_costs[f"total_api_cost_{currency}"] = (
                    costs[key] + embedding_total + decision_total
                    if costs[key] is not None and embedding_total is not None and decision_total is not None
                    else None
                )
            max_cost = positive_setting("COST_QUERY_MAX_INR", 1.5)
            inr = costs["estimated_cost_inr"]
            complex_query = any(call["operation"] in {"answer_complex", "answer_complex_high", "verify_complex"} for call in calls)
            target = positive_setting("COST_COMPLEX_TARGET_INR" if complex_query else "COST_ORDINARY_TARGET_INR",
                                      1.5 if complex_query else 1.2)
            total_inr = api_costs["total_api_cost_inr"]
            target_status = "unknown" if total_inr is None or target is None else ("exceeded" if total_inr > target else "within_target")
            target_details = {"api_cost_target_inr": target, "api_cost_target_status": target_status,
                              "api_cost_target_class": "complex" if complex_query else "ordinary"}
            logger.info(
                "query_generation_usage",
                extra={
                    "query_id": usage["query_id"],
                    "pipeline_events": trace.events,
                    "pipeline_duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "pipeline_audit_owner": trace.final_audit,
                    "query_outcome": query_outcome,
                    "provider_attempt_count": sum(e["code"] == "provider_attempt" for e in trace.events),
                    "operation_attempts": dict(Counter(e["operation"] for e in trace.events if e["code"] == "provider_attempt")),
                    "operation": function.__name__,
                    "succeeded": succeeded,
                    "generation_calls": len(calls),
                    "generation_operations": [call["operation"] for call in calls],
                    "generation_models": [call["model"] for call in calls],
                    "cost_policy_version": COST_POLICY_VERSION,
                    "cost_optimizations_enabled": os.getenv("RAG_COST_OPTIMIZATIONS", "true").strip().lower() in {"1", "true", "yes"},
                    **costs,
                    **api_costs,
                    **target_details,
                    "embedding_calls": len(embeddings),
                    "embedding_items": sum(e["items"] for e in embeddings),
                    "embedding_stages": embeddings,
                    "decision_calls": len(usage["decisions"]),
                    "decision_stages": usage["decisions"],
                    "total_api_cost_complete": api_costs["total_api_cost_usd"] is not None,
                    "cost_complete": costs["estimated_cost_usd"] is not None,
                    "unmetered_generation_attempts": usage["unmetered_attempts"],
                    "cost_max_inr": max_cost,
                    "cost_exceeded": inr > max_cost if inr is not None and max_cost is not None else None,
                    "verification_escalations": usage["verification_escalations"],
                    "strong_verification_calls": sum(call["operation"] == "verify_complex" for call in calls),
                    "stage_costs": calls,
                    "unmetered_stages": usage["unmetered_stages"],
                    # Missing upstream counts remain visible rather than being treated as zero.
                    **{
                        name: sum(call[field] for call in calls) if all(isinstance(call.get(field), int) for call in calls) else None
                        for name, field in FIELDS.items()
                    },
                },
            )

            # One compact completion record for operators; keep the detailed
            # accounting above compatible with existing consumers.
            all_calls = [*calls, *embeddings, *usage["decisions"], *usage["unmetered_stages"]]
            known_costs = {
                currency: sum(call[f"estimated_cost_{currency}"] for call in all_calls if call[f"estimated_cost_{currency}"] is not None)
                for currency in ("usd", "inr")
            }
            total_usd = api_costs["total_api_cost_usd"]
            total_inr = api_costs["total_api_cost_inr"]
            summary = (
                f"Estimated API cost: USD {total_usd:.6f}"
                if total_usd is not None
                else f"Estimated API cost incomplete; known subtotal: USD {known_costs['usd']:.6f}"
            )
            if total_inr is not None:
                summary += f" (INR {total_inr:.6f})"
            logger.info(
                "query_answer_cost",
                extra={
                    "query_id": usage["query_id"],
                    "operation": function.__name__,
                    "succeeded": succeeded,
                    "query_outcome": query_outcome,
                    "cost_summary": summary,
                    "total_api_cost_usd": total_usd,
                    "total_api_cost_inr": total_inr,
                    "known_api_cost_usd": known_costs["usd"],
                    "known_api_cost_inr": known_costs["inr"],
                    "total_api_cost_complete": total_usd is not None,
                    "unpriced_calls": sum(call["estimated_cost_usd"] is None for call in all_calls),
                    "unmetered_generation_attempts": usage["unmetered_attempts"],
                    "generation_models": list(dict.fromkeys(call["model"] for call in calls)),
                    "generation_calls": len(calls),
                    "embedding_calls": len(embeddings),
                    "decision_calls": len(usage["decisions"]),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "cost_policy_version": COST_POLICY_VERSION,
                    **target_details,
                },
            )

            trace_scope.__exit__(None, None, None)

    return wrapped


def record_attempt(operation: str, action: str, attempt: int):
    event("provider_attempt", operation=operation, action=action, attempt=attempt)
