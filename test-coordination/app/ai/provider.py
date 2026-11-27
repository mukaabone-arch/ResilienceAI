"""Who writes the final wording of a suggestion.

RulesProvider (default): the composer's text, unchanged. Nothing leaves the platform.

AnthropicProvider (opt-in, AI_PROVIDER=anthropic): Claude rewrites the composer's
text into clearer prose. Safeguards:
- It receives only that text and the facts behind it, never the database, and
  never secrets; people's names are replaced with placeholders before sending
  and restored afterwards.
- It is told to add nothing that isn't in the facts.
- Its answer is checked: every record code it mentions must exist in the facts,
  and every placeholder must map back. Otherwise, or on any error or timeout,
  the composer's text is used and the reason is logged with the suggestion.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from ..config import settings

PROMPT_VERSION = "assistant-v1"
# Current models think before answering and that counts toward max_tokens, so leave room. They also reject
# non-default sampling settings (temperature, top_p, top_k) with a 400, so none are sent.
MAX_TOKENS = 4000
CODE = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z0-9]{1,6})*-\d{2,4}(?:-[A-Z]?\d{2,4})*\b|\b[A-Z]{2,4}-\d{2}\b")


@dataclass
class Result:
    text: str
    provider: str
    model: str | None = None
    fallback_reason: str | None = None


class Provider(Protocol):
    name: str
    def rewrite(self, feature: str, draft: str, names: list[str]) -> Result: ...


class RulesProvider:
    name = "rules"

    def rewrite(self, feature: str, draft: str, names: list[str]) -> Result:
        return Result(text=draft, provider="rules")


def pseudonymise(text: str, names: list[str]) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    out = text
    for i, n in enumerate(sorted({x for x in names if x}, key=len, reverse=True), start=1):
        token = f"Person {chr(64 + i) if i <= 26 else i}"
        if n in out:
            mapping[token] = n
            out = out.replace(n, token)
    return out, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    for token in sorted(mapping, key=len, reverse=True):
        text = text.replace(token, mapping[token])
    return text


SYSTEM = (
    "You rewrite status notes for a bank's resilience-test coordination tool. Rewrite the draft so it is clear, "
    "calm and brief for busy managers. Rules: use only facts in the draft; do not add numbers, dates, names, "
    "causes or advice that are not there; keep every record code (like DB-01 or BANK-DR-2026-001) exactly as "
    "written; keep placeholders like 'Person A' exactly as written; keep list structure where the draft has it; "
    "plain text only, no markdown headings. Reply with the rewritten text only."
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, transport=None):
        self.transport = transport            # tests inject an httpx transport

    def rewrite(self, feature: str, draft: str, names: list[str]) -> Result:
        import httpx
        safe, mapping = pseudonymise(draft, names)
        try:
            with httpx.Client(timeout=settings.ai_timeout_s, transport=self.transport) as c:
                r = c.post("https://api.anthropic.com/v1/messages", headers={
                    "x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    content=json.dumps({"model": settings.ai_model, "max_tokens": MAX_TOKENS, "system": SYSTEM,
                                        "messages": [{"role": "user", "content": f"Feature: {feature}\n\nDraft:\n{safe}"}]}))
            if r.status_code != 200:
                return Result(draft, "rules", settings.ai_model, api_error(r))
            out, why = reply_text(r)
            if why:
                return Result(draft, "rules", settings.ai_model, why)
        except Exception as e:  # noqa: BLE001 - any failure falls back to the composer's text
            return Result(draft, "rules", settings.ai_model, f"model call failed ({type(e).__name__})")
        problem = check(out, safe, mapping)
        if problem:
            return Result(draft, "rules", settings.ai_model, problem)
        return Result(restore(out, mapping), "anthropic", settings.ai_model)


def check(out: str, source: str, mapping: dict[str, str]) -> str | None:
    if not out:
        return "model returned no text"
    if len(out) > max(3 * len(source), 600):
        return "model answer was too long"
    unknown = set(CODE.findall(out)) - set(CODE.findall(source))
    if unknown:
        return f"model mentioned records not in the facts: {', '.join(sorted(unknown))}"
    stray = set(re.findall(r"Person [A-Z]\b", out)) - set(mapping)
    if stray:
        return "model introduced people not in the facts"
    return None


def api_error(r) -> str:
    """'model call failed (400: invalid_request_error: ...)' — the API's own reason, so it can be fixed; never the key."""
    detail = ""
    try:
        err = r.json().get("error") or {}
        detail = ": ".join(x for x in (err.get("type"), (err.get("message") or "")[:200]) if x)
    except Exception:  # noqa: BLE001 - the body may not be JSON
        pass
    return f"model call failed ({r.status_code}{': ' + detail if detail else ''})"


def reply_text(r) -> tuple[str, str | None]:
    """The text of a Messages API reply, or ("", why) when there is none."""
    body = r.json()
    out = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text").strip()
    if not out and body.get("stop_reason") == "max_tokens":
        return "", "model ran out of room before answering (max_tokens)"
    return out, None


def provider() -> Provider:
    if settings.ai_provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicProvider()
    return RulesProvider()
