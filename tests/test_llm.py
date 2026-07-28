"""Unit tests for the Anthropic LLM client (mocked transport, no network)."""

from __future__ import annotations

import httpx
import pytest

from leanscreen.errors import ConfigError, CreditsExhaustedError, LLMError
from leanscreen.llm import AnthropicClient, UsageTotals


def _client(
    handler: object,
    *,
    api_key: str | None = "sk-test",
    model: str = "claude-sonnet-4-6",
    **kwargs: object,
) -> AnthropicClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http = httpx.Client(transport=transport)
    # backoff 0 -> retries are instant in tests
    return AnthropicClient(
        http,
        model=model,
        api_key=api_key,
        backoff_base_seconds=0.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_complete_returns_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hello world"}]})

    assert _client(handler).complete("sys", "user") == "hello world"


def test_persistent_429_raises_after_retries() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="rate limited")

    with pytest.raises(LLMError, match="failed after"):
        _client(handler).complete("sys", "user")
    assert calls["n"] == 4  # initial try + 3 retries


def test_transient_429_recovers() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert _client(handler).complete("sys", "user") == "ok"
    assert calls["n"] == 3


def test_overloaded_529_recovers() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(529, text="overloaded")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert _client(handler).complete("sys", "user") == "ok"


def test_non_retryable_4xx_raises_immediately() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    with pytest.raises(LLMError, match="403"):
        _client(handler).complete("sys", "user")
    assert calls["n"] == 1  # no retry on a permanent error


def test_no_text_blocks_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": []})

    with pytest.raises(LLMError):
        _client(handler).complete("sys", "user")


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        _client(lambda _r: httpx.Response(200), api_key=None)


def test_retries_without_temperature_on_400() -> None:
    seen_temps: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen_temps.append("temperature" in body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "`temperature` is deprecated for this model."}},
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert _client(handler).complete("sys", "user") == "ok"
    # First call had temperature (rejected), retry dropped it (succeeded).
    assert seen_temps == [True, False]


def test_per_call_temperature_is_sent() -> None:
    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content).get("temperature"))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _client(handler).complete("sys", "user", temperature=0.8)
    assert seen == [0.8]  # the per-call value, not the client default


def test_temperature_none_falls_back_to_client_default() -> None:
    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content).get("temperature"))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = AnthropicClient(
        http,
        model="claude-sonnet-4-6",
        api_key="sk-test",
        temperature=0.3,
        backoff_base_seconds=0.0,
    )
    client.complete("sys", "user")  # no per-call temperature
    assert seen == [0.3]  # the configured client default


def test_different_per_call_temperatures_are_distinct() -> None:
    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content).get("temperature"))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client(handler)
    client.complete("sys", "user", temperature=0.0)
    client.complete("sys", "user", temperature=0.9)
    assert seen == [0.0, 0.9]  # best-of-N samples genuinely vary the temperature


def test_per_call_temperature_dropped_on_400() -> None:
    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen.append(body.get("temperature"))
        if "temperature" in body:
            return httpx.Response(
                400, json={"error": {"message": "`temperature` is deprecated for this model."}}
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert _client(handler).complete("sys", "user", temperature=0.7) == "ok"
    # Per-call temp tried then dropped -> greedy fallback (current behavior).
    assert seen == [0.7, None]


def test_temperature_400_does_not_consume_retry_budget() -> None:
    # The one-time temperature drop is a FREE re-issue: it must not eat a
    # transient-retry slot. With the default budget (3), a temperature-400
    # followed by 3 transient 503s then a 200 must still succeed.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls["n"] += 1
        if "temperature" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "`temperature` deprecated"}})
        if calls["n"] <= 4:  # calls 2,3,4 are the 3 transient failures
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert _client(handler).complete("sys", "user", temperature=0.7) == "ok"
    assert calls["n"] == 5  # 1 free temp-400 + 3 transient + 1 success


def test_non_temperature_400_mentioning_temperature_raises_real_error() -> None:
    # A genuine 400 that merely *mentions* temperature (e.g. a validation error)
    # must NOT be treated as the unsupported-param case: after one free re-issue
    # without temperature it still 400s, so we raise the REAL error rather than
    # spinning the loop and reporting "no attempt made".
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "temperature: must be <= 1"}})

    with pytest.raises(LLMError, match="400"):
        _client(handler).complete("sys", "user", temperature=0.5)
    assert calls["n"] == 2  # initial (with temp) + one free re-issue (no temp), then raise


def test_sends_auth_headers() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x-api-key"] = request.headers.get("x-api-key", "")
        seen["anthropic-version"] = request.headers.get("anthropic-version", "")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _client(handler).complete("sys", "user")
    assert seen["x-api-key"] == "sk-test"
    assert seen["anthropic-version"] == "2023-06-01"


def test_cache_prefix_splits_into_cached_block() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen["content"] = body["messages"][0]["content"]
        seen["system"] = body["system"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    assert _client(handler).complete("sys", "PREFIXtail", cache_prefix="PREFIX") == "ok"
    content = seen["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "PREFIX", "cache_control": {"type": "ephemeral"}}
    assert content[1] == {"type": "text", "text": "tail"}
    # byte-identical to the full user prompt: caching changes billing, not content
    assert content[0]["text"] + content[1]["text"] == "PREFIXtail"
    assert seen["system"] == "sys"


def test_no_cache_prefix_sends_plain_string() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["content"] = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _client(handler).complete("sys", "hello")
    assert seen["content"] == "hello"  # back-compat: plain string, no caching


def test_cache_prefix_not_a_prefix_falls_back_to_plain() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["content"] = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    # cache_prefix that is NOT a leading substring of user -> safe plain-string fallback
    _client(handler).complete("sys", "hello world", cache_prefix="XYZ")
    assert seen["content"] == "hello world"


def test_cache_system_marks_the_system_prompt_block() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    transport = httpx.MockTransport(handler)
    client = AnthropicClient(
        httpx.Client(transport=transport),
        model="claude-opus-4-8",
        api_key="sk-test",
        backoff_base_seconds=0.0,
        cache_system=True,
    )
    client.complete("the aid system prompt", "wholly different user content")
    assert captured["system"] == [
        {
            "type": "text",
            "text": "the aid system prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_system_stays_plain_string_by_default() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _client(handler).complete("sys", "user")
    assert captured["system"] == "sys"  # byte-identical payload for every other caller


# ---- spend accounting: every 200's `usage` block is tallied exactly ----------


def _usage_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 7,
            },
        },
    )


def test_usage_accumulates_across_calls() -> None:
    client = _client(_usage_handler)
    client.complete("s", "u")
    client.complete("s", "u")
    totals = client.usage
    assert (totals.calls, totals.input_tokens, totals.output_tokens) == (2, 200, 80)
    assert (totals.cache_read_tokens, totals.cache_write_tokens) == (1800, 14)


def test_usage_accumulates_under_threads() -> None:
    import concurrent.futures

    client = _client(_usage_handler)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: client.complete("s", "u"), range(40)))
    assert client.usage.calls == 40
    assert client.usage.input_tokens == 4000


def test_usage_missing_block_counts_call_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client(handler)
    client.complete("s", "u")
    assert client.usage == UsageTotals(calls=1)


def test_usage_cost_estimate_and_summary_line() -> None:
    totals = UsageTotals(
        calls=3,
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=2_000_000,
        cache_write_tokens=0,
    )
    # 1M in @$5 + 0.1M out @$25 + 2M cache-read @$0.5 = 5 + 2.5 + 1
    assert totals.estimated_cost_usd() == pytest.approx(8.5)
    line = totals.summary_line()
    assert line.startswith("tokens: calls=3 in=1,000,000 out=100,000")
    assert "$8.50 est." in line


def test_usage_totals_add() -> None:
    a = UsageTotals(1, 10, 20, 30, 40)
    b = UsageTotals(2, 1, 2, 3, 4)
    assert a + b == UsageTotals(3, 11, 22, 33, 44)


def _flat_usage_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1000, "output_tokens": 1000},
        },
    )


def test_fable_usage_records_double_cost() -> None:
    # Fable bills 2x the Opus table; the multiplier applies at RECORDING time.
    client = _client(_flat_usage_handler, model="claude-fable-5")
    client.complete("s", "u")
    flat = (1000 * 5 + 1000 * 25) / 1_000_000  # Opus-table cost for the same tokens
    assert client.usage.estimated_cost_usd() == pytest.approx(2 * flat)  # 0.06


def test_mixed_model_totals_sum_recorded_costs() -> None:
    # Adding a Fable client's totals to an Opus client's keeps each side's
    # recorded per-model dollars — no re-pricing at the flat table.
    fable = _client(_flat_usage_handler, model="claude-fable-5")
    opus = _client(_flat_usage_handler, model="claude-opus-4-8")
    fable.complete("s", "u")
    opus.complete("s", "u")
    combined = fable.usage + opus.usage
    flat = (1000 * 5 + 1000 * 25) / 1_000_000
    assert combined.est_cost_usd == pytest.approx(2 * flat + flat)
    assert combined.estimated_cost_usd() == pytest.approx(0.09)


# ---- billing circuit breaker: credit exhaustion is GLOBAL, so it must abort ----

_BILLING_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"Your credit balance is too low to access the Anthropic API."}}'
)


def _billing_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(400, text=_BILLING_BODY)


def test_breaker_trips_after_three_consecutive_billing_400s() -> None:
    client = _client(_billing_handler)
    for _ in range(2):
        with pytest.raises(LLMError):
            client.complete("s", "u")
    with pytest.raises(CreditsExhaustedError):
        client.complete("s", "u")


def test_breaker_resets_on_any_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 3:  # two billing failures, then a success, then two more
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        return httpx.Response(400, text=_BILLING_BODY)

    client = _client(handler)
    for _ in range(2):
        with pytest.raises(LLMError):
            client.complete("s", "u")
    client.complete("s", "u")  # success resets the count
    for _ in range(2):
        with pytest.raises(LLMError):  # NOT CreditsExhaustedError — count restarted
            client.complete("s", "u")


def test_non_billing_400_never_trips_the_breaker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"error":{"message":"temperature is not supported"}}')

    client = _client(handler)
    for _ in range(5):
        with pytest.raises(LLMError):
            client.complete("s", "u")


def test_credits_exhausted_is_not_an_llm_error() -> None:
    # Per-draft handlers catch LLMError and degrade gracefully; the breaker must
    # NEVER be swallowed by them — it aborts the run.
    assert not issubclass(CreditsExhaustedError, LLMError)


def test_empty_content_200_raises_shape_error_with_usage_still_counted() -> None:
    # Live 2026-07-04 failure: Opus 4.8 at max_tokens=1 returned 200 with
    # content=[] (stop_reason max_tokens). The client raises a SHAPE LLMError
    # (callers like the preflight treat a 200-shape-error as proof of credit),
    # and the spend is still tallied.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [], "usage": {"input_tokens": 16, "output_tokens": 1}},
        )

    client = _client(handler)
    with pytest.raises(LLMError, match="unexpected Anthropic response shape"):
        client.complete("s", "u")
    assert client.usage.calls == 1 and client.usage.input_tokens == 16


def test_nested_cache_prefixes_become_a_breakpoint_chain() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen["content"] = body["messages"][0]["content"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    # SEEDS is run-stable; SEEDSctx changes per paper. Two breakpoints let the
    # seed head survive as a cross-paper cache HIT when the context changes.
    out = _client(handler).complete("sys", "SEEDSctxTAIL", cache_prefix=("SEEDS", "SEEDSctx"))
    assert out == "ok"
    content = seen["content"]
    assert isinstance(content, list) and len(content) == 3
    assert content[0] == {"type": "text", "text": "SEEDS", "cache_control": {"type": "ephemeral"}}
    assert content[1] == {"type": "text", "text": "ctx", "cache_control": {"type": "ephemeral"}}
    assert content[2] == {"type": "text", "text": "TAIL"}
    assert "".join(block["text"] for block in content) == "SEEDSctxTAIL"


def test_duplicate_and_non_nested_prefixes_degrade_gracefully() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen["content"] = body["messages"][0]["content"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client(handler)
    # Identical elements (empty context: seed block == full prefix) -> ONE block.
    client.complete("sys", "SEEDStail", cache_prefix=("SEEDS", "SEEDS"))
    content = seen["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["text"] == "SEEDS" and "cache_control" in content[0]

    # A second element that is not nested is dropped; the first still caches.
    client.complete("sys", "SEEDStail", cache_prefix=("SEEDS", "OTHER"))
    content = seen["content"]
    assert isinstance(content, list) and len(content) == 2

    # Nothing valid -> plain string exactly as before.
    client.complete("sys", "SEEDStail", cache_prefix=("XYZ",))
    assert seen["content"] == "SEEDStail"


def test_temperature_degraded_property_flips_on_rejection() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        import json

        body = json.loads(request.content)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "temperature is not supported"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client(handler)
    assert client.temperature_degraded is False
    assert client.complete("sys", "user", 0.7) == "ok"  # free re-issue after the 400
    assert client.temperature_degraded is True
    assert calls["n"] == 2


def test_thinking_rejection_drops_config_once() -> None:
    """A model that 400s an explicit `thinking` config: drop it run-wide and
    re-issue once for free — the same single-stage degradation as temperature.
    There is no budget_tokens fallback stage: that config is itself a 400 on
    every current model (removed on Fable/Opus 4.7+/Sonnet 5)."""
    import json as _json

    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        payloads.append(body)
        if "thinking" in body:
            return httpx.Response(
                400, json={"error": {"message": "thinking.type: disabled is not supported"}}
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client(handler, max_tokens=8000, thinking={"type": "disabled"})
    assert client.complete("sys", "user") == "ok"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert "thinking" not in payloads[1]
    assert len(payloads) == 2


def test_locked_surface_model_sends_neither_thinking_nor_temperature() -> None:
    """Fable/Mythos have a locked surface: thinking is always on (every explicit
    config is a 400) and temperature is removed (also a 400). The client must
    send NEITHER field on the very first request — no discovery-by-400, no
    burned calls (the root cause of the 2026-07-09/10 Fable failures)."""
    import json as _json

    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(_json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client(
        handler,
        model="claude-fable-5",
        max_tokens=32000,
        thinking={"type": "disabled"},
        temperature=0.0,
    )
    assert client.complete("sys", "user", 0.4) == "ok"
    assert len(payloads) == 1
    assert "thinking" not in payloads[0]
    assert "temperature" not in payloads[0]
    # Best-of-N must know sampling diversity is unavailable from the start.
    assert client.temperature_degraded is True


def test_non_locked_model_still_sends_thinking_and_temperature() -> None:
    import json as _json

    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(_json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = _client(handler, thinking={"type": "disabled"}, temperature=0.0)
    assert client.complete("sys", "user") == "ok"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["temperature"] == 0.0
    assert client.temperature_degraded is False


def test_all_thinking_200_raises_diagnostic_without_retry() -> None:
    """A 200 whose content is all thinking blocks means thinking consumed the
    whole max_tokens budget. No request-side rescue exists on always-thinking
    models, so the client must fail fast — ONE paid call — with a message that
    names the cause (stop_reason, max_tokens) and the operator lever."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "content": [{"type": "thinking", "thinking": ""}],
                "stop_reason": "max_tokens",
            },
        )

    client = _client(handler, model="claude-fable-5", max_tokens=8000)
    with pytest.raises(LLMError, match=r"max_tokens.*8000|8000.*max_tokens"):
        client.complete("sys", "user")
    assert calls["n"] == 1


def test_no_backoff_sleep_after_final_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The terminal retry raised immediately after a 16s sleep that bought
    nothing — a 500-draft batch riding a sustained 529 wasted up to 16s per
    failed draft. Sleeps happen BETWEEN attempts only."""
    import time as time_mod

    sleeps: list[float] = []
    monkeypatch.setattr(time_mod, "sleep", lambda s: sleeps.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": "overloaded"})

    transport = httpx.MockTransport(handler)
    client = AnthropicClient(
        httpx.Client(transport=transport),
        model="claude-sonnet-4-6",
        api_key="sk-test",
        backoff_base_seconds=1.0,
        max_retries=3,
    )
    with pytest.raises(LLMError):
        client.complete("sys", "user")
    # 4 attempts (initial + 3 retries) -> exactly 3 sleeps, none terminal
    assert len(sleeps) == 3
