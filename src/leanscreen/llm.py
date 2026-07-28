"""LLM client for autoformalization.

We call the Anthropic Messages API directly over httpx (already a dependency)
rather than pulling the SDK — fewer deps, fully typed, and trivially testable
with ``httpx.MockTransport``. The API key is read from the ``ANTHROPIC_API_KEY``
environment variable, exactly as the user sets it up; it is never logged.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from leanscreen.errors import ConfigError, CreditsExhaustedError, LLMError
from leanscreen.logging import get_logger, log_event

# USD per MILLION tokens for the summary's cost ESTIMATE. CALIBRATED against a
# real invoice (2026-07-04: est $52.48 vs billed $17.49 exposed the old Opus-4
# table as 3x high; these rates reproduce that bill to the cent). The billing
# console remains the authority.
_PRICE_PER_MTOK = {
    "input": 5.0,
    "output": 25.0,
    "cache_read": 0.5,
    "cache_write": 6.25,
}

# Non-Opus families price at an exact uniform ratio of the Opus table (all four
# components scale together), so a single multiplier per family keeps the
# estimate honest: Fable/Mythos bill 2x Opus ($10/$50), Sonnet 0.6x ($3/$15),
# Haiku 0.2x ($1/$5). Applied at usage-RECORDING time, so summed totals stay
# dollar-correct even when clients on different models are added together
# (the 2026-07-09 Fable runs printed half their real spend without this).
_PRICE_MULTIPLIER_PREFIXES = (
    ("claude-fable", 2.0),
    ("claude-mythos", 2.0),
    ("claude-sonnet", 0.6),
    ("claude-haiku", 0.2),
)


def _price_multiplier(model: str) -> float:
    for prefix, multiplier in _PRICE_MULTIPLIER_PREFIXES:
        if model.startswith(prefix):
            return multiplier
    return 1.0  # Opus rates — the calibrated baseline table


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Token usage accumulated across every successful API call of one client."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Dollar cost accumulated at recording time under the RECORDING client's
    # per-model prices; 0.0 for hand-built totals, which fall back to the flat
    # Opus table in estimated_cost_usd().
    est_cost_usd: float = 0.0

    def __add__(self, other: UsageTotals) -> UsageTotals:
        return UsageTotals(
            self.calls + other.calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.est_cost_usd + other.est_cost_usd,
        )

    def _flat_cost_usd(self) -> float:
        return (
            self.input_tokens * _PRICE_PER_MTOK["input"]
            + self.output_tokens * _PRICE_PER_MTOK["output"]
            + self.cache_read_tokens * _PRICE_PER_MTOK["cache_read"]
            + self.cache_write_tokens * _PRICE_PER_MTOK["cache_write"]
        ) / 1_000_000

    def estimated_cost_usd(self) -> float:
        return self.est_cost_usd if self.est_cost_usd > 0.0 else self._flat_cost_usd()

    def summary_line(self) -> str:
        return (
            f"tokens: calls={self.calls} in={self.input_tokens:,} out={self.output_tokens:,} "
            f"cache_read={self.cache_read_tokens:,} cache_write={self.cache_write_tokens:,} "
            f"(~${self.estimated_cost_usd():.2f} est.)"
        )


_log = get_logger(__name__)

# Transient API conditions worth retrying: rate limits, server errors, overload,
# and Cloudflare edge errors (520-524) — a 520 killed a 2h prescreen run on
# 2026-07-15; the edge returns these for momentary origin hiccups.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})

# Model families with a LOCKED generation surface (per the Anthropic API docs):
# thinking is ALWAYS ON and not configurable — {"type": "disabled"} and
# {"type": "enabled", "budget_tokens": N} both return 400 — and the sampling
# parameters (temperature/top_p/top_k) are removed (also 400). For these
# models the client must send neither field, from the FIRST request; thinking
# output counts against max_tokens, so callers must budget headroom for it.
_LOCKED_SURFACE_MODEL_PREFIXES = ("claude-fable", "claude-mythos")


class LLMClient(Protocol):
    """Minimal text-completion interface, so tests can inject a fake."""

    def complete(
        self,
        system: str,
        user: str,
        temperature: float | None = None,
        *,
        cache_prefix: str | Sequence[str] | None = None,
    ) -> str:
        """Return the model's text response to a system + user prompt.

        ``temperature`` overrides the client's configured default for THIS call
        (best-of-N drafting varies it per sample); ``None`` keeps the default.
        ``cache_prefix``, when set, is a leading substring of ``user`` marked for
        prompt caching (the stable head reused across calls); ``None`` disables it.
        """
        ...


class AnthropicClient:
    """Anthropic Messages API client over httpx."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        model: str,
        base_url: str = "https://api.anthropic.com/v1/messages",
        version: str = "2023-06-01",
        max_tokens: int = 1024,
        temperature: float = 0.0,
        api_key: str | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 2.0,
        cache_system: bool = False,
        thinking: dict[str, Any] | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ConfigError("ANTHROPIC_API_KEY is not set (export it in your shell).")
        self._http = http
        self._model = model
        self._base_url = base_url
        self._version = version
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._key = key
        # Locked-surface models (Fable/Mythos) reject `temperature` and every
        # `thinking` config with a 400 — suppress both up front rather than
        # discovering the rejections one paid request at a time.
        locked_surface = model.startswith(_LOCKED_SURFACE_MODEL_PREFIXES)
        # Cleared once if the model rejects `temperature`. Shared across the
        # pipeline's drafting threads WITHOUT a lock on purpose: the race is
        # benign (worst case one extra 400-retry per worker) and the flag
        # only ever flips one way.
        self._send_temperature = not locked_surface
        # Exact spend accounting, parsed from every 200's `usage` block. Guarded
        # by a lock: the pipeline's drafting threads share one client.
        self._usage = UsageTotals()
        self._usage_lock = threading.Lock()
        # Consecutive billing-400 count (any 200 resets it). At the threshold the
        # breaker trips: credit exhaustion is global, so continuing only burns
        # wall-clock and degrades multi-step levers (rescues, corrections).
        self._billing_failures = 0
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        # Mark the SYSTEM prompt as a prompt-cache block. For clients whose
        # calls share only the system prompt (the back-translation aid: the
        # user content is wholly different every call, so `cache_prefix` never
        # applies), this is the only cacheable segment — without it every call
        # re-pays the full system prompt.
        self._cache_system = cache_system
        # Optional `thinking` config forwarded verbatim (e.g. {"type":
        # "disabled"} to stop a Sonnet-family model's default thinking from
        # consuming the whole max_tokens budget and leaving no text block).
        # None = omit the field, keeping every model's default. On a
        # locked-surface model the config is NEVER sent (any explicit value is
        # a 400 there); on other models a 400-rejection drops it once for the
        # rest of the run — the same free degradation as `temperature`.
        self._thinking = thinking
        self._send_thinking = thinking is not None and not locked_surface
        self._cost_multiplier = _price_multiplier(model)

    @property
    def temperature_degraded(self) -> bool:
        """True when ``temperature`` is not being sent — either because the
        model rejected it mid-run and it was dropped, or because the model's
        surface is locked (Fable/Mythos) and it was never sent at all.

        The best-of-N planner reads this: with static seeds AND no temperature,
        every extra sample is a byte-identical greedy duplicate — paid, then
        discarded by dedup — so the pipeline collapses to one sample.
        """
        return not self._send_temperature

    def complete(
        self,
        system: str,
        user: str,
        temperature: float | None = None,
        *,
        cache_prefix: str | Sequence[str] | None = None,
    ) -> str:
        """Call the Messages API and return the first text block.

        ``temperature`` overrides the configured default for this single call
        (per-sample best-of-N variation); ``None`` falls back to
        ``self._temperature``. Either way it is only sent while
        ``self._send_temperature`` holds — if the model has rejected the
        parameter, none is sent and the call degrades to the model's greedy
        default, exactly as before.

        ``cache_prefix`` (a leading substring of ``user``, or an ascending
        chain of nested ones) is sent as separate prompt-cached content
        blocks — one breakpoint per chain element (the API allows 4). A chain
        lets a run-stable head (e.g. a static seed block) survive as a cache
        HIT when the per-paper tail changes: without it, every new paper
        re-WROTE the whole span at 1.25x. The bytes the model receives are
        exactly the full ``user`` — caching changes billing only. Below the
        model's cache floor a marker is silently ignored (no caching, no
        penalty); chain elements that are not prefixes of ``user`` (or break
        the strictly-ascending nesting) are dropped, falling back as far as
        the plain string.

        Transient failures (429 rate limit, 5xx, overload, transport errors) are
        retried with exponential backoff — this is the money path, and a single
        blip must not abort a long batch run.
        """
        effective_temperature = temperature if temperature is not None else self._temperature
        content: Any
        candidates = [cache_prefix] if isinstance(cache_prefix, str) else list(cache_prefix or ())
        # Keep the longest strictly-ascending chain of true prefixes of `user`.
        chain: list[str] = []
        for prefix in candidates:
            if not prefix or not user.startswith(prefix) or len(prefix) >= len(user):
                continue
            if chain and (not prefix.startswith(chain[-1]) or len(prefix) <= len(chain[-1])):
                continue
            chain.append(prefix)
        if chain:
            content = []
            consumed = ""
            for prefix in chain:
                content.append(
                    {
                        "type": "text",
                        "text": prefix[len(consumed) :],
                        "cache_control": {"type": "ephemeral"},
                    }
                )
                consumed = prefix
            content.append({"type": "text", "text": user[len(consumed) :]})
        else:
            content = user
        system_payload: Any = system
        if self._cache_system:
            system_payload = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system_payload,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "x-api-key": self._key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }
        last_error = "no attempt made"
        attempt = 0
        dropped_temperature = False  # the one-time, free temperature degradation
        dropped_thinking = False  # ditto for the optional `thinking` config
        while attempt <= self._max_retries:
            sent_temperature = self._send_temperature  # local, race-safe snapshot
            if sent_temperature:
                payload["temperature"] = effective_temperature
            else:
                payload.pop("temperature", None)
            sent_thinking = self._send_thinking
            if sent_thinking:
                payload["thinking"] = self._thinking
            else:
                payload.pop("thinking", None)
            try:
                response = self._post(payload, headers)
            except LLMError as exc:  # transport error — transient, retry
                last_error = str(exc)
                self._sleep(attempt)
                attempt += 1
                continue
            # Some newer models reject `temperature` (deprecated, e.g. Opus 4.8).
            # Drop it for the rest of the run and re-issue THIS request once for
            # FREE — without advancing `attempt`, so the transient-retry budget is
            # preserved. Guard on `sent_temperature` (the local snapshot, not the
            # shared flag) so a 400 that merely *mentions* temperature but wasn't
            # caused by us sending it falls through to the real error handling.
            if (
                sent_temperature
                and not dropped_temperature
                and response.status_code == 400
                and "temperature" in response.text.lower()
            ):
                self._send_temperature = False
                dropped_temperature = True
                continue  # free re-issue — do NOT advance `attempt`
            # Degradation for a rejected `thinking` config: drop it run-wide
            # and re-issue once for free, mirroring `temperature`. There is no
            # budget_tokens fallback stage — that config is itself rejected
            # with a 400 by every current model (removed on Fable/Opus 4.7+/
            # Sonnet 5, deprecated on 4.6), so re-issuing with it only burns a
            # guaranteed-failing request (the 2026-07-09/10 Fable runs).
            if (
                sent_thinking
                and not dropped_thinking
                and response.status_code == 400
                and "thinking" in response.text.lower()
            ):
                self._send_thinking = False
                dropped_thinking = True
                continue  # free re-issue — do NOT advance `attempt`
            if response.status_code in _RETRYABLE_STATUSES:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                self._sleep(attempt)
                attempt += 1
                continue
            if response.status_code != 200:
                if "credit balance is too low" in response.text:
                    with self._usage_lock:
                        self._billing_failures += 1
                        tripped = self._billing_failures >= 3
                    if tripped:
                        raise CreditsExhaustedError(
                            "Anthropic account has no credit (3+ consecutive billing "
                            "rejections) — aborting the run; drafts already stored are "
                            "safe. Top up (or fix the key) and rerun the same command."
                        )
                raise LLMError(
                    f"Anthropic API returned {response.status_code}: {response.text[:500]}"
                )
            body = response.json()
            with self._usage_lock:
                self._billing_failures = 0
            self._record_usage(body.get("usage") or {})
            if isinstance(content, list):  # caching active — surface hit/miss for debugging
                usage = body.get("usage") or {}
                log_event(
                    _log,
                    logging.DEBUG,
                    "llm.cache",
                    cache_read=usage.get("cache_read_input_tokens", 0),
                    cache_write=usage.get("cache_creation_input_tokens", 0),
                    uncached_input=usage.get("input_tokens", 0),
                )
            resp_blocks = body.get("content")
            if isinstance(resp_blocks, list) and resp_blocks and not _has_text_block(body):
                # A 200 whose content is ALL thinking blocks: thinking output
                # counts against max_tokens, and on an always-on-thinking model
                # (Fable) a hard prompt can spend the entire budget reasoning
                # and never write the answer. There is no request-side rescue —
                # thinking cannot be disabled or bounded on these models — so
                # surface the actual cause and the operator lever instead of
                # burning another paid call on an identical request.
                raise LLMError(
                    "Anthropic response contained no text blocks: thinking "
                    f"consumed the whole output budget (stop_reason="
                    f"{body.get('stop_reason')!r}, max_tokens={self._max_tokens}). "
                    "Raise the output cap (ARXIV_FORMALIZE_MAX_TOKENS=32000 "
                    "recommended for always-thinking models like Fable)."
                )
            return _extract_text(body)
        raise LLMError(
            f"Anthropic API failed after {self._max_retries + 1} attempts; last: {last_error}"
        )

    @property
    def usage(self) -> UsageTotals:
        """A snapshot of the tokens this client has spent so far."""
        with self._usage_lock:
            return self._usage

    def _record_usage(self, usage: dict[str, Any]) -> None:
        counted = UsageTotals(
            calls=1,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        )
        delta = UsageTotals(
            counted.calls,
            counted.input_tokens,
            counted.output_tokens,
            counted.cache_read_tokens,
            counted.cache_write_tokens,
            est_cost_usd=counted._flat_cost_usd() * self._cost_multiplier,
        )
        with self._usage_lock:
            self._usage = self._usage + delta

    def _sleep(self, attempt: int) -> None:
        """Exponential backoff between retries (skipped when base is 0, e.g. tests).

        No sleep after the FINAL attempt: the loop is about to exit and raise,
        and the terminal 16s backoff bought nothing (up to 16s per failed
        draft in a batch riding out a sustained 529).
        """
        if attempt >= self._max_retries:
            return
        delay = self._backoff_base * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def _post(self, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        try:
            return self._http.post(self._base_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"transport error calling Anthropic: {exc}") from exc


def _has_text_block(body: dict[str, Any]) -> bool:
    """True when at least one content block is a text block (vs all-thinking)."""
    content = body.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "text" for b in content)


def _extract_text(body: dict[str, Any]) -> str:
    """Pull the concatenated text blocks out of a Messages API response."""
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise LLMError(f"unexpected Anthropic response shape: {body!r}")
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    if not parts:
        raise LLMError("Anthropic response contained no text blocks")
    return "".join(parts)
