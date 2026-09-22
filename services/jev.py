"""Bounded TypeSafe API calls. Never log state, answers, keys or upstream bodies."""
import logging
import math
import os
import time

import requests

from jev_settings import api_key, number
from token_usage import current_query_id, record_decision

logger = logging.getLogger(__name__)
ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def evaluate(state: dict, questions: dict, *, operation: str) -> dict | None:
    key = api_key()
    if not key:
        logger.info("jev_unavailable", extra={"operation": operation, "reason": "missing_key"})
        return None
    import json
    payload = {"model": os.getenv("JEV_MODEL", "jev-latest").strip(), "state": state, "questions": questions}
    # Skip oversized inputs intact, rather than silently losing legal evidence.
    if len(json.dumps(payload, ensure_ascii=False)) > 60000:
        logger.info("jev_unavailable", extra={"operation": operation, "reason": "input_limit"})
        return None
    try:
        timeout = number("JEV_TIMEOUT_SECONDS", 8, 0.1, 30)
    except ValueError:
        return None
    started = time.perf_counter()
    data = None
    success = False
    try:
        # No retries or redirects: an optional decision must not amplify an outage
        # or forward the bearer token to an unexpected host.
        with requests.post(ENDPOINT, json=payload, headers={"Authorization": f"Bearer {key}"},
                           timeout=(min(3, timeout), timeout), allow_redirects=False) as response:
            if response.status_code != 200:
                return None
            data = response.json()
        if not isinstance(data, dict):
            return None
        answers = data.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(questions):
            return None
        for answer in answers.values():
            if not isinstance(answer, dict) or answer.get("type") != "noul":
                return None
            value = answer.get("noul")
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
                return None
        success = True
        return {name: answer["noul"] for name, answer in answers.items()}
    except (requests.RequestException, ValueError):
        return None
    finally:
        usage = data.get("usage") if isinstance(data, dict) else None
        model = data.get("model") if isinstance(data, dict) else payload["model"]
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        cost = record_decision(operation, model, usage, elapsed_ms=elapsed, succeeded=success)
        logger.info("jev_evaluation", extra={"query_id": current_query_id(), "operation": operation,
                                            "succeeded": success, "duration_ms": elapsed, **cost})
