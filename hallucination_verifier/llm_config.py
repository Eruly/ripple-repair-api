"""Shared LLM endpoint configuration for OpenAI-compatible chat calls."""

from __future__ import annotations

import os
from typing import Any

DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:30000/v1"
DEFAULT_LOCAL_MODEL = "muse-glimmer-30b"
DEFAULT_FACTREASONER_MODEL = "muse-glimmer-30b"


def chat_base_url() -> str:
    """Return the OpenAI-compatible chat base URL."""
    return os.getenv("OPENAI_BASE_URL", DEFAULT_LOCAL_BASE_URL)


def chat_api_key() -> str:
    """Return the chat API key for OpenAI-compatible clients.

    Local OpenAI-compatible servers such as vLLM often do not require auth, but
    OpenAI client libraries still require a non-empty api_key value.
    """
    explicit = os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("LOCAL_LLM_API_KEY", "").strip()
    if explicit:
        return explicit

    base_url = chat_base_url()
    if "127.0.0.1" in base_url or "localhost" in base_url:
        return "EMPTY"

    return os.getenv("HF_TOKEN", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()


def verifier_model() -> str:
    """Model for verifier orchestration and helper LLM calls."""
    return (
        os.getenv("QWEN_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or DEFAULT_LOCAL_MODEL
    )


def factreasoner_base_url() -> str:
    """Return the dedicated FactReasoner endpoint when configured.

    FactReasoner is a separate service in this workspace. Falling back to the
    shared OpenAI-compatible endpoint keeps local installs that do not define
    ``FACTREASONER_*`` variables working as before.
    """
    return os.getenv("FACTREASONER_BASE_URL", "").strip() or chat_base_url()


def factreasoner_model() -> str:
    """Return the model used by FactReasoner graph/judge/cascade calls."""
    return os.getenv("FACTREASONER_MODEL", "").strip() or verifier_model() or DEFAULT_FACTREASONER_MODEL


def _factreasoner_role_model(variable: str) -> str:
    """Resolve an optional role-specific model without changing old installs."""
    return os.getenv(variable, "").strip() or factreasoner_model()


def factreasoner_graph_model() -> str:
    """Model used to extract the Fact Atom Graph."""
    return _factreasoner_role_model("FACTREASONER_GRAPH_MODEL")


def factreasoner_judge_model() -> str:
    """Model used for atom and fact-consistency judgments."""
    return _factreasoner_role_model("FACTREASONER_JUDGE_MODEL")


def factreasoner_correction_model() -> str:
    """Model used for target and cascade rewrite proposals."""
    return _factreasoner_role_model("FACTREASONER_CORRECTION_MODEL")


def factreasoner_review_model() -> str:
    """Model used for approval-only review of proposed edits."""
    return _factreasoner_role_model("FACTREASONER_REVIEW_MODEL")


def factreasoner_stop_sequences() -> list[str]:
    """Optional comma-separated stop strings for local chat-template servers."""
    raw = os.getenv("FACTREASONER_STOP_SEQUENCES", "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def local_chat_template_kwargs() -> dict[str, Any]:
    """Chat-template flags for Qwen3.x / GLM local servers.

    Thinking is on by default for those models and often leaves ``content``
    empty on structured JSON calls. Opt back in with ``FACTREASONER_ENABLE_THINKING=1``.
    """
    enabled = os.getenv("FACTREASONER_ENABLE_THINKING", "").strip().lower()
    if enabled in {"1", "true", "yes", "on"}:
        return {}
    return {"enable_thinking": False}


def with_local_chat_template(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach local chat-template kwargs to a raw Chat Completions payload."""
    kwargs = local_chat_template_kwargs()
    if kwargs:
        existing = dict(payload.get("chat_template_kwargs") or {})
        existing.update(kwargs)
        payload["chat_template_kwargs"] = existing
    return payload


def openai_extra_body() -> dict[str, Any] | None:
    """``extra_body`` for the OpenAI SDK; None when thinking is left on."""
    kwargs = local_chat_template_kwargs()
    if not kwargs:
        return None
    return {"chat_template_kwargs": kwargs}


def factreasoner_api_key() -> str:
    """Return FactReasoner's explicit key, with the shared key as fallback."""
    return (
        os.getenv("FACTREASONER_API_KEY", "").strip()
        or chat_api_key()
    )


def ontology_model() -> str:
    """Model for ontology verifier LLM judgment."""
    return os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_LOCAL_MODEL


def final_rewrite_model() -> str:
    """Model for final sentence rewrite."""
    return os.getenv("FINAL_REWRITE_MODEL", verifier_model())
