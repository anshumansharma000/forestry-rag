"""Standard Gemini text-generation estimates, excluding tax and infrastructure.

Rates verified at https://ai.google.dev/gemini-api/docs/pricing on 2026-09-17.
Unknown models, tiers, or incomplete usage produce unknown costs, not zero.
"""

import math
import os
from datetime import UTC, date, datetime

PRICING_VERSION = "google-standard-2026-09-17"
COST_POLICY_VERSION = "selective-evidence-audit-2026-09-18"


def positive_setting(name: str, default: float) -> float | None:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def generation_cost(model: str, usage: dict, *, as_of: date | None = None) -> dict:
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
    # Current app sends text without external tools. Do not guess costs for tool-use additions.
    if usage.get("toolUsePromptTokenCount", 0):
        return result
    if thoughts is None and type(total) is int:
        thoughts = total - prompt - output
    if type(thoughts) is not int or thoughts < 0:
        return result
    usd = ((prompt - cached) * input_rate + cached * cached_rate + (output + thoughts) * output_rate) / 1_000_000
    result.update({"estimated_cost_usd": usd, "estimated_cost_inr": usd * fx if fx is not None else None})
    return result
