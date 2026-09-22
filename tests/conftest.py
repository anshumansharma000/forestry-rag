"""Keep rollout tests reproducible regardless of a developer's local .env."""
import pytest


@pytest.fixture(autouse=True)
def legacy_rollout_defaults(monkeypatch):
    from jev_settings import FEATURES

    for name in FEATURES:
        feature = name.upper()
        monkeypatch.setenv(f"JEV_{feature}_MODE", "off")
    # Existing contracts run under shipped defaults; feature tests opt in explicitly.
    for flag in ("RAG_LITE_EXTRACTION", "RAG_RISK_BASED_VERIFICATION", "RAG_LEGAL_BATCH_EMBEDDINGS"):
        monkeypatch.setenv(flag, "false")
