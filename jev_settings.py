"""Independent Jev ownership and rollout settings."""
import math
import os

ADDITIONAL_FEATURES = ("routing", "planning", "history", "classification", "extraction")
FEATURES = ("verification", "rerank", "rewrite", *ADDITIONAL_FEATURES)


def api_key() -> str:
    return (os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_KEY") or "").strip()


def mode(feature: str) -> str:
    value = os.getenv(f"JEV_{feature.upper()}_MODE", "off").strip().lower()
    if value not in {"off", "shadow", "active"}:
        raise ValueError(f"JEV_{feature.upper()}_MODE must be off, shadow or active")
    return value


def active_failure_policy() -> str:
    """Control technical-failure handling without weakening negative decisions.

    ``closed`` keeps an active Jev-owned task exclusive and fails safely. ``baseline``
    permits the former task owner to run only when Jev is unavailable, never when Jev
    returned a negative decision. Shadow mode always runs the baseline for comparison.
    """
    value = os.getenv("JEV_ACTIVE_FAILURE_POLICY", "closed").strip().lower()
    if value not in {"closed", "baseline"}:
        raise ValueError("JEV_ACTIVE_FAILURE_POLICY must be closed or baseline")
    return value


def rag_lab_extraction_policy() -> str:
    """Choose whether extraction-quality findings warn or block experimental builds."""
    value = os.getenv("JEV_RAG_LAB_EXTRACTION_POLICY", "warn").strip().lower()
    if value not in {"warn", "block"}:
        raise ValueError("JEV_RAG_LAB_EXTRACTION_POLICY must be warn or block")
    return value


def number(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside its permitted range")
    return value


def status() -> dict:
    return {
        "typesafe_api_key_configured": bool(api_key()),
        "jev_model": os.getenv("JEV_MODEL", "jev-latest").strip(),
        "jev_active_failure_policy": os.getenv("JEV_ACTIVE_FAILURE_POLICY", "closed").strip().lower(),
        "jev_rag_lab_extraction_policy": os.getenv("JEV_RAG_LAB_EXTRACTION_POLICY", "warn").strip().lower(),
        **{f"jev_{feature}_mode": os.getenv(f"JEV_{feature.upper()}_MODE", "off").strip().lower()
           for feature in FEATURES},
    }


def invalid_settings() -> list[str]:
    invalid = []
    for feature in FEATURES:
        try:
            mode(feature)
        except ValueError:
            invalid.append(f"JEV_{feature.upper()}_MODE")
    try:
        active_failure_policy()
    except ValueError:
        invalid.append("JEV_ACTIVE_FAILURE_POLICY")
    try:
        rag_lab_extraction_policy()
    except ValueError:
        invalid.append("JEV_RAG_LAB_EXTRACTION_POLICY")
    for name, default, low, high in (("JEV_TIMEOUT_SECONDS", 8, .1, 30),
                                    ("JEV_APPROVAL_THRESHOLD", .99, .95, 1),
                                    ("JEV_INPUT_USD_PER_MILLION", .042, .000001, 100)):
        try:
            number(name, default, low, high)
        except ValueError:
            invalid.append(name)
    return invalid
