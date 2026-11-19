"""The one place the application talks to Google's Gemini models.

Configuration comes from environment variables. In GitHub Codespaces, add them
as Codespaces secrets (GitHub > Settings > Codespaces > Secrets) and they are
set automatically in every Codespace for this repository:

    GEMINI_API_KEY             API key from your GCP project or Google AI Studio (required)
    GEMINI_MODEL               model ID to use (optional, defaults to DEFAULT_MODEL)
    GOOGLE_GENAI_USE_VERTEXAI  "true" sends requests to Vertex AI (aiplatform.googleapis.com),
                               which is what GCP project keys need; the Codespace sets this.
                               Unset or "false" sends them to the AI Studio endpoint
                               (generativelanguage.googleapis.com), for AI Studio keys.
    LANGSMITH_API_KEY          LangSmith API key (optional). When set, every Gemini call is
                               traced to LangSmith (see config/settings.py).
    LANGSMITH_PROJECT          LangSmith project for the traces (optional, default "alderwick-health")

Example, from any view or service:

    from clinic import gemini

    try:
        summary = gemini.generate_text(
            f"Summarize this policy for a patient in two sentences:\\n\\n{policy.body}",
            system_instruction="You are a helpful assistant for Alderwick Family Health.",
        )
    except gemini.GeminiError as exc:
        summary = None  # show a friendly message instead of crashing the page

Prompts and replies are never written to the application logs, only the model name and
timing. When LangSmith tracing is on, prompts and replies ARE sent to LangSmith, which is
another reason to use synthetic text only.
"""

import logging
import os
import time

from google import genai
from google.genai import types
from langsmith import wrappers
from langsmith.utils import tracing_is_enabled

DEFAULT_MODEL = "gemini-3.5-flash-lite"

logger = logging.getLogger("alderwick.ai")


class GeminiError(Exception):
    """Gemini is not configured or the request failed."""


def api_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def model_name():
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


def uses_vertex_ai():
    return os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true")


def langsmith_enabled():
    """True when LangSmith tracing is configured (LANGSMITH_API_KEY set and tracing on)."""
    return bool(os.environ.get("LANGSMITH_API_KEY")) and bool(tracing_is_enabled())


def get_client():
    key = api_key()
    if not key:
        raise GeminiError(
            "GEMINI_API_KEY is not set. Add it as a GitHub Codespaces secret, then rebuild or restart the Codespace."
        )
    client = genai.Client(api_key=key, vertexai=uses_vertex_ai())
    if langsmith_enabled():
        # Every generate_content call on this client becomes an LLM run in LangSmith,
        # with the prompt, reply, model, and token usage.
        client = wrappers.wrap_gemini(client, chat_name="Gemini")
    return client


def generate_content(contents, *, system_instruction=None, tools=None, model=None):
    """Send `contents` to Gemini and return the full response object.

    Use this when you need more than the text, for example function calls when
    passing `tools` (see public/chatbot.py). Most features should use generate_text().
    """
    model = model or model_name()
    config = None
    if system_instruction or tools:
        config = types.GenerateContentConfig(system_instruction=system_instruction, tools=tools)
    client = get_client()
    started = time.monotonic()
    try:
        response = client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as exc:
        logger.warning(
            "gemini_failed model=%s duration_ms=%.1f error=%s",
            model,
            (time.monotonic() - started) * 1000,
            type(exc).__name__,
        )
        message = f"{type(exc).__name__}: {exc}"
        if "API_KEY_SERVICE_BLOCKED" in message:
            message = (
                "This key is not allowed to call the endpoint the request went to. GCP keys need "
                "GOOGLE_GENAI_USE_VERTEXAI=true; AI Studio keys need it unset.\n\n" + message
            )
        raise GeminiError(message) from exc
    logger.info("gemini_call model=%s duration_ms=%.1f", model, (time.monotonic() - started) * 1000)
    return response


def generate_text(prompt, *, system_instruction=None, model=None):
    """Send a prompt to Gemini and return the reply as a string.

    `prompt` is usually a string. For a multi-turn conversation, pass a list of
    google.genai.types.Content objects instead.
    """
    response = generate_content(prompt, system_instruction=system_instruction, model=model)
    return response.text or ""
