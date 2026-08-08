"""LLM access for the reasoning agents.

Two backends:

``anthropic``
    Real Claude calls. The agent supplies a system prompt, a user prompt built
    from provider data, and a Pydantic schema; the response is parsed and
    validated against that schema, with one repair round-trip on failure.

``scripted``
    No LLM at all. Agents fall back to a documented heuristic path so the whole
    pipeline runs offline with zero credentials. Every run records which backend
    produced it (``agent_runs.llm_backend``), so an offline scan can never be
    mistaken for a reasoned one.

The system prompt used for every call forbids fabrication explicitly; the
schemas do the rest of the work by leaving no field in which a fabricated
number would be accepted without a source.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.config.settings import LLMBackend, Settings, get_settings

T = TypeVar("T", bound=BaseModel)

#: Ceiling for the single truncation retry. Well inside what the current models
#: allow, and far past what any schema here legitimately needs.
MAX_OUTPUT_TOKENS = 32000

ANTI_HALLUCINATION_CLAUSE = """
Hard rules that override every other instruction:
- Never invent a price, volume, greek, implied volatility, date, or headline.
  If a value was not given to you in the input data, omit the field or state
  that it is unavailable.
- Never state a numeric market measurement that is not present in the input.
- Distinguish confirmed fact from interpretation using the evidence_quality
  enum. Anything you inferred is INTERPRETATION, not CONFIRMED_FACT.
- Do not assign a numeric conviction or confidence score to a trade. Scoring is
  performed downstream by deterministic application code, and any number you
  supply would be discarded.
- Preferring "no opportunity" over a weak one is a correct answer.
""".strip()


class LLMUnavailable(RuntimeError):
    pass


class LLMTruncated(RuntimeError):
    """The model ran out of output budget mid-response.

    Worth its own type: the symptom is a JSON decode error deep in parsing,
    which reads like a malformed model response rather than a budget problem.
    """


class LLMClient(Protocol):
    backend: str

    def structured(
        self, *, system: str, user: str, schema: type[T], max_tokens: int | None = None
    ) -> T: ...


class AnthropicLLMClient:
    """Claude-backed structured output with a single repair attempt."""

    backend = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        api_key = s.secret("anthropic_api_key")
        if not api_key:
            raise LLMUnavailable("LLM_BACKEND=anthropic requires ANTHROPIC_API_KEY")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMUnavailable(
                "The 'anthropic' package is not installed. Install the 'llm' extra."
            ) from exc
        self._client = anthropic.Anthropic(api_key=api_key, timeout=s.llm_timeout_seconds)
        self._model = s.llm_model
        self._max_tokens = s.llm_max_tokens

    def _call(self, system: str, user: str, max_tokens: int) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        if resp.stop_reason == "max_tokens":
            # Truncated JSON surfaces as an inscrutable decode error several
            # frames away. Name the real cause here instead.
            raise LLMTruncated(
                f"The response hit the {max_tokens}-token output limit and was cut off "
                f"({len(text)} characters returned). Either raise LLM_MAX_TOKENS or "
                f"narrow the response schema."
            )
        return text

    def structured(
        self, *, system: str, user: str, schema: type[T], max_tokens: int | None = None
    ) -> T:
        budget = max_tokens or self._max_tokens
        full_system = (
            f"{system}\n\n{ANTI_HALLUCINATION_CLAUSE}\n\n"
            "Respond with a single JSON object matching this schema. No prose, "
            "no markdown fences.\n"
            f"{json.dumps(schema.model_json_schema())}"
        )

        try:
            raw = self._call(full_system, user, budget)
        except LLMTruncated:
            # Output length varies run to run, so the same prompt can fit one
            # day and overflow the next. Retry once with more room before
            # giving up on the reasoning path entirely.
            retry_budget = min(budget * 2, MAX_OUTPUT_TOKENS)
            if retry_budget <= budget:
                raise
            raw = self._call(full_system, _be_briefer(user), retry_budget)

        try:
            return schema.model_validate(_extract_json(raw))
        except (ValidationError, ValueError) as first_error:
            repair = (
                f"{user}\n\nYour previous response did not validate against the schema.\n"
                f"Error:\n{first_error}\n\nReturn corrected JSON only."
            )
            raw2 = self._call(full_system, repair, budget)
            return schema.model_validate(_extract_json(raw2))


class ScriptedLLMClient:
    """Placeholder used when no LLM is configured.

    Calling it is an error: agents check ``settings.llm_backend`` and take their
    documented heuristic path instead of pretending a model reasoned.
    """

    backend = "scripted"

    def structured(
        self, *, system: str, user: str, schema: type[T], max_tokens: int | None = None
    ) -> T:
        raise LLMUnavailable(
            "No LLM is configured (LLM_BACKEND=scripted). Agents must use their "
            "heuristic fallback path rather than calling the model."
        )


def build_llm(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    if s.llm_backend is LLMBackend.ANTHROPIC:
        return AnthropicLLMClient(s)
    return ScriptedLLMClient()


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first complete JSON object out of a model response.

    Slicing from the first ``{`` to the last ``}`` looks equivalent but is not:
    a model that appends a sentence of explanation, or emits a second object,
    produces a span that fails to parse with "Extra data". ``raw_decode`` reads
    exactly one value and ignores whatever follows.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in the model response")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not decode JSON from the model response: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object, got {type(value).__name__}")
    return value


def _be_briefer(user: str) -> str:
    return (
        f"{user}\n\nYour previous attempt was cut off by the output limit. "
        "Return the same structure but be more selective: keep only the items "
        "that genuinely matter, and keep every free-text field to one or two "
        "sentences."
    )


__all__ = [
    "ANTI_HALLUCINATION_CLAUSE",
    "MAX_OUTPUT_TOKENS",
    "AnthropicLLMClient",
    "LLMClient",
    "LLMTruncated",
    "LLMUnavailable",
    "ScriptedLLMClient",
    "build_llm",
]
