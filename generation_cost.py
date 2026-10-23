"""Standard Gemini text-generation estimates, excluding tax and infrastructure.

Rates verified at https://ai.google.dev/gemini-api/docs/pricing on 2026-09-17.
Unknown models, tiers, or incomplete usage produce unknown costs, not zero.
"""

import math
import os
from datetime import UTC, date, datetime

PRICING_VERSION = "google-standard-2026-09-17"
COST_POLICY_VERSION = "query-api-accounting-2026-10-22"
EMBEDDING_PRICING_VERSION = "google-text-embedding-standard-2026-10-17"


def normalized_usage(usage: dict) -> dict:
    """Preserve raw fields at the caller; infer only from compatible complete counts."""
    result = dict(usage)
    prompt, output, total = (usage.get(k) for k in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"))
    thoughts = usage.get("thoughtsTokenCount")
    if thoughts is None and all(type(v) is int and v >= 0 for v in (prompt, output, total)):
        tool = usage.get("toolUsePromptTokenCount", 0)
        if type(tool) is int and tool >= 0 and total >= prompt + output + tool:
            result["thoughtsTokenCount"] = total - prompt - output - tool
    # An omitted optional cache count denotes no reported cached content.
    result.setdefault("cachedContentTokenCount", 0)
    return result


def embedding_cost(model: str, usage: dict, *, as_of: date | None = None) -> dict:
    """Text-only synchronous embed/batchEmbedContents; not asynchronous Batch API pricing.

    $0.20/M text tokens verified at Google pricing on 2026-10-17. Never infer tokens
    from dimensions, characters, or vector counts. Explicit overrides bind one model.
    """
    as_of = as_of or datetime.now(UTC).date()
    fx = positive_setting("COST_USD_TO_INR", 90)
    result = {"estimated_cost_usd": None, "estimated_cost_inr": None, "cost_usd_to_inr": fx,
              "pricing_version": EMBEDDING_PRICING_VERSION, "pricing_date": as_of.isoformat()}
    rate = .20 if model == "gemini-embedding-2" and as_of >= date(2026, 10, 17) else None
    if os.getenv("COST_EMBEDDING_RATE_MODEL") == model:
        rate = positive_setting("COST_EMBEDDING_USD_PER_MILLION_TOKENS", 0)
        result["pricing_version"] = "configured-text-embedding-rate"
    tokens = usage.get("promptTokenCount")
    if usage.get("serviceTier", "standard") != "standard" or type(tokens) is not int or tokens < 0 or rate is None:
        return result
    usd = tokens * rate / 1_000_000
    result.update(estimated_cost_usd=usd, estimated_cost_inr=usd * fx if fx is not None else None)
    return result


def positive_setting(name: str, default: float) -> float | None:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def generation_cost(model: str, usage: dict, *, as_of: date | None = None) -> dict:
    usage = normalized_usage(usage)
    as_of = as_of or datetime.now(UTC).date()
    fx = positive_setting("COST_USD_TO_INR", 90.0)
    result = {"estimated_cost_usd": None, "estimated_cost_inr": None, "cost_usd_to_inr": fx,
              "pricing_version": PRICING_VERSION, "pricing_date": as_of.isoformat()}
    if usage.get("serviceTier", "standard") != "standard":
        return result
    if model == "gemini-3.5-flash-lite":
        input_rate, output_rate, cached_rate = 0.30, 2.50, 0.03
    elif model == "gemini-3.8-flash":
        multiplier = 1 if as_of < date(2027, 1, 1) else 2
        input_rate, output_rate, cached_rate = 0.75 * multiplier, 3.75 * multiplier, 0.075 * multiplier
    else:
        return result
    prompt = usage.get("promptTokenCount")
    output = usage.get("candidatesTokenCount")
    cached = usage.get("cachedContentTokenCount", 0)
    thoughts = usage.get("thoughtsTokenCount")
    total = usage.get("totalTokenCount")
    if any(type(v) is not int or v < 0 for v in (prompt, output, cached)) or cached > prompt:
        return result
    tool = usage.get("toolUsePromptTokenCount", 0)
    if type(tool) is not int or tool < 0:
        return result
    if thoughts is None and type(total) is int:
        thoughts = total - prompt - output - tool
    if type(thoughts) is not int or thoughts < 0:
        return result
    if total is not None and (type(total) is not int or total != prompt + tool + output + thoughts):
        return result
    usd = ((prompt - cached + tool) * input_rate + cached * cached_rate + (output + thoughts) * output_rate) / 1_000_000
    result.update({"estimated_cost_usd": usd, "estimated_cost_inr": usd * fx if fx is not None else None})
    return result


def search_cost(model: str, metadata: dict | None) -> dict:
    """Gross search estimate; never assume this app owns the provider's free allowance."""
    queries = metadata.get("webSearchQueries") if isinstance(metadata, dict) else None
    valid = isinstance(queries, list) and all(isinstance(q, str) for q in queries)
    count = len({q.strip() for q in queries if q.strip()}) if valid else None
    known = count is not None and model.startswith(("gemini-3", "gemini-4"))
    usd = count * .014 if known else None
    fx = positive_setting("COST_USD_TO_INR", 90)
    return {"search_queries": count, "estimated_cost_usd": usd,
            "estimated_cost_inr": usd * fx if usd is not None and fx else None,
            "pricing_version": "google-search-gross-2026-10-22", "free_allowance_applied": False}
