"""Safety invariants.

These tests exist to fail loudly if someone later wires execution into the
system, leaks a credential into a log, or lets an agent invent a price. They
are the guardrails the rest of the design leans on.
"""

from __future__ import annotations

import inspect
import pkgutil
from datetime import date
from pathlib import Path

import pytest

import app
from app.agents.llm import ANTI_HALLUCINATION_CLAUSE, ScriptedLLMClient, build_llm
from app.config.settings import Settings
from app.models.enums import StrategyType
from app.models.trade_candidate import CandidateSet, TradeCandidate
from app.providers.base import FORBIDDEN_METHOD_TOKENS, assert_no_execution_surface
from app.providers.registry import build_providers
from app.providers.robinhood.mcp import READ_ONLY_TOOLS

SCAN_DAY = date(2024, 6, 3)


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        llm_backend="scripted",
        fmp_backend="mock",
        robinhood_backend="mock",
        unusual_whales_backend="mock",
        news_backend="mock",
    )


# ------------------------------------------------------------- no execution
def test_no_provider_exposes_an_order_method(settings):
    bundle = build_providers(SCAN_DAY, settings)
    for provider in (bundle.market_data, bundle.options_market, bundle.options_flow, bundle.news):
        if provider is not None:
            assert_no_execution_surface(provider)


def test_a_provider_with_an_order_method_is_refused():
    class RogueProvider:
        def place_order(self):  # pragma: no cover - never called
            ...

    with pytest.raises(RuntimeError, match="research-only"):
        assert_no_execution_surface(RogueProvider())  # type: ignore[arg-type]


def test_the_robinhood_tool_allowlist_is_read_only():
    for tool in READ_ONLY_TOOLS:
        assert tool.startswith("get_"), f"{tool} is not obviously read-only"
    for token in ("place_option_order", "place_equity_order", "cancel_option_order", "exercise_option"):
        assert token not in READ_ONLY_TOOLS


def test_an_mcp_tool_outside_the_allowlist_is_refused():
    from app.providers.robinhood.mcp import RobinhoodMCPProvider

    provider = RobinhoodMCPProvider(lambda name, params: {})
    with pytest.raises(Exception, match="allowlist"):
        provider._tool("place_option_order", symbol="NVDA")


def test_enabling_execution_is_refused_at_startup():
    with pytest.raises(ValueError, match="research-only"):
        Settings(enable_order_execution=True)


def test_no_module_in_the_package_defines_an_order_submitting_function():
    """A structural check across the whole codebase, not just providers."""
    offenders: list[str] = []
    package_root = Path(app.__file__).parent
    for module_info in pkgutil.walk_packages([str(package_root)], prefix="app."):
        try:
            module = __import__(module_info.name, fromlist=["_"])
        except Exception:  # pragma: no cover - optional deps
            continue
        for name, obj in vars(module).items():
            if not inspect.isfunction(obj) or obj.__module__ != module_info.name:
                continue
            lowered = name.lower()
            if any(tok in lowered for tok in ("place_order", "submit_order", "execute_trade")):
                offenders.append(f"{module_info.name}.{name}")
    assert not offenders, f"order-submitting functions found: {offenders}"


def test_forbidden_tokens_cover_the_obvious_cases():
    for token in ("place_order", "submit_order", "exercise", "cancel_order"):
        assert token in FORBIDDEN_METHOD_TOKENS


# ------------------------------------------------------------------ secrets
def test_secrets_are_redacted_in_the_log_safe_view():
    s = Settings(
        anthropic_api_key="sk-ant-secret-value",
        fmp_api_key="fmp-secret-value",
        unusual_whales_api_key="uw-secret-value",
    )
    dumped = str(s.safe_dict())
    assert "sk-ant-secret-value" not in dumped
    assert "fmp-secret-value" not in dumped
    assert "uw-secret-value" not in dumped
    assert s.safe_dict()["anthropic_api_key"] == "***set***"
    assert s.safe_dict()["news_api_key"] == "***unset***"


def test_secret_values_are_not_in_the_default_repr():
    s = Settings(anthropic_api_key="sk-ant-secret-value")
    assert "sk-ant-secret-value" not in repr(s)


def test_provider_request_records_strip_key_parameters(settings):
    bundle = build_providers(SCAN_DAY, settings)
    bundle.market_data.get_quote("NVDA")
    assert bundle.requests
    for req in bundle.requests:
        assert not any("key" in k.lower() for k in req.params)


def test_logging_redacts_secret_shaped_fields():
    from app.logging_config import _redact

    event = {"event": "call", "api_key": "abc123", "authorization": "Bearer xyz", "symbol": "NVDA"}
    cleaned = _redact(None, None, dict(event))
    assert cleaned["api_key"] == "***redacted***"
    assert cleaned["authorization"] == "***redacted***"
    assert cleaned["symbol"] == "NVDA"


def test_env_example_contains_no_real_looking_credentials():
    text = Path(".env.example").read_text()
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            key = key.strip().upper()
            # LLM_MAX_TOKENS is a size, not a credential.
            is_credential = key.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
            if is_credential and "MAX" not in key:
                assert value.strip() == "", f"{key} has a value committed to .env.example"


# ------------------------------------------------------- no fabricated data
def test_the_llm_contract_forbids_inventing_market_data():
    for phrase in ("Never invent a price", "not present in the input", "unavailable"):
        assert phrase in ANTI_HALLUCINATION_CLAUSE


def test_the_llm_contract_forbids_the_model_scoring_trades():
    assert "numeric conviction" in ANTI_HALLUCINATION_CLAUSE
    assert "deterministic" in ANTI_HALLUCINATION_CLAUSE


def test_trade_candidate_has_no_numeric_confidence_field():
    """Structural guarantee that an agent cannot anchor the score."""
    fields = set(TradeCandidate.model_fields)
    for banned in ("confidence", "confidence_score", "score", "conviction", "rating"):
        assert banned not in fields


def test_the_scripted_backend_refuses_to_pretend_it_reasoned(settings):
    llm = build_llm(settings)
    assert isinstance(llm, ScriptedLLMClient)
    with pytest.raises(Exception, match="heuristic fallback"):
        llm.structured(system="s", user="u", schema=CandidateSet)


def test_mock_providers_refuse_to_invent_data_for_unknown_tickers(settings):
    bundle = build_providers(SCAN_DAY, settings)
    with pytest.raises(KeyError, match="not in the synthetic universe"):
        bundle.market_data.get_quote("NOTAREALTICKER")


def test_no_trade_requires_an_explicit_rationale():
    with pytest.raises(ValueError, match="no_trade_rationale"):
        CandidateSet(run_id="r", candidates=[])
    CandidateSet(run_id="r", candidates=[], no_trade_rationale="nothing qualified")


# ------------------------------------------------------- strategy restriction
def test_only_defined_risk_strategies_exist_in_the_enum():
    for strategy in StrategyType:
        assert strategy.value in {
            "LONG_CALL",
            "LONG_PUT",
            "BULL_CALL_SPREAD",
            "BEAR_PUT_SPREAD",
        }


def test_every_allowed_strategy_is_a_net_debit():
    """A defined-risk debit position can never lose more than it cost."""
    for strategy in StrategyType:
        assert strategy.value.startswith(("LONG_", "BULL_CALL", "BEAR_PUT"))
