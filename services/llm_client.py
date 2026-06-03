"""Lightweight OpenAI-compatible chat client (no IntelliQ backend dependency)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import config


class LLMError(RuntimeError):
    pass


def _strip_json_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def chat_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 2000,
) -> str:
    if not config.OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY is not set")

    url = f"{config.OPENAI_API_BASE.rstrip('/')}/chat/completions"
    body = json.dumps(
        {
            "model": config.LLM_MODEL,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise LLMError(f"LLM HTTP {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"LLM request failed: {e}") from e

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected LLM response shape: {payload!r}") from e


def chat_json(system_prompt: str, user_prompt: str, **kwargs) -> dict:
    raw = chat_completion(system_prompt, user_prompt, **kwargs)
    cleaned = _strip_json_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM did not return valid JSON: {cleaned[:300]}") from e
    if not isinstance(parsed, dict):
        raise LLMError("LLM JSON root must be an object")
    return parsed
