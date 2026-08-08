"""Controlled vocabularies.

Agents are constrained to these enumerations so downstream deterministic code
never has to interpret free-form prose.
"""

from __future__ import annotations

from enum import Enum


class WorkflowStage(str, Enum):
    """Which half of the day the pipeline is running in.

    The distinction matters because option quotes before the options market
    opens are stale and must not be used to finalise a contract.
    """

    PREMARKET = "PREMARKET"
    MARKET_OPEN = "MARKET_OPEN"
    POSTMARKET = "POSTMARKET"


class MarketRegime(str, Enum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    ROTATIONAL = "ROTATIONAL"
    RANGE_BOUND = "RANGE_BOUND"
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    UNCERTAIN = "UNCERTAIN"


class VolatilityRegime(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class Bias(str, Enum):
    STRONG_BULLISH = "STRONG_BULLISH"
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    STRONG_BEARISH = "STRONG_BEARISH"

    @property
    def sign(self) -> int:
        return {
            Bias.STRONG_BULLISH: 1,
            Bias.BULLISH: 1,
            Bias.NEUTRAL: 0,
            Bias.BEARISH: -1,
            Bias.STRONG_BEARISH: -1,
        }[self]


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BULLISH else -1


class StrategyType(str, Enum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"

    @property
    def is_spread(self) -> bool:
        return self in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD)

    @property
    def direction(self) -> Direction:
        return (
            Direction.BULLISH
            if self in (StrategyType.LONG_CALL, StrategyType.BULL_CALL_SPREAD)
            else Direction.BEARISH
        )

    @property
    def option_right(self) -> OptionRight:
        return (
            OptionRight.CALL
            if self in (StrategyType.LONG_CALL, StrategyType.BULL_CALL_SPREAD)
            else OptionRight.PUT
        )


class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class CatalystType(str, Enum):
    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    EARNINGS_REVISION = "EARNINGS_REVISION"
    ANALYST_UPGRADE = "ANALYST_UPGRADE"
    ANALYST_DOWNGRADE = "ANALYST_DOWNGRADE"
    PRICE_TARGET_CHANGE = "PRICE_TARGET_CHANGE"
    MERGER_ACQUISITION = "MERGER_ACQUISITION"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    FDA_DECISION = "FDA_DECISION"
    LITIGATION = "LITIGATION"
    REGULATORY_ACTION = "REGULATORY_ACTION"
    SEC_FILING = "SEC_FILING"
    EXECUTIVE_CHANGE = "EXECUTIVE_CHANGE"
    INVESTOR_DAY = "INVESTOR_DAY"
    CONFERENCE = "CONFERENCE"
    MAJOR_CONTRACT = "MAJOR_CONTRACT"
    INDUSTRY_DEVELOPMENT = "INDUSTRY_DEVELOPMENT"
    MACRO_EVENT = "MACRO_EVENT"
    FED_EVENT = "FED_EVENT"
    SECTOR_ROTATION = "SECTOR_ROTATION"
    TECHNICAL_BREAKOUT = "TECHNICAL_BREAKOUT"
    OPTIONS_FLOW = "OPTIONS_FLOW"
    OTHER = "OTHER"


class CatalystScope(str, Enum):
    MARKET_WIDE = "MARKET_WIDE"
    SECTOR = "SECTOR"
    COMPANY = "COMPANY"


class EvidenceQuality(str, Enum):
    """How well-supported a claim is. Drives deterministic score credit."""

    CONFIRMED_FACT = "CONFIRMED_FACT"
    REPORTED = "REPORTED"
    INTERPRETATION = "INTERPRETATION"
    RUMOR = "RUMOR"
    UNVERIFIED = "UNVERIFIED"


class TimeHorizon(str, Enum):
    INTRADAY = "INTRADAY"
    DAYS_1_3 = "DAYS_1_3"
    WEEK_1 = "WEEK_1"
    WEEKS_2_4 = "WEEKS_2_4"
    MONTHS_1_3 = "MONTHS_1_3"

    @property
    def approx_days(self) -> int:
        return {
            TimeHorizon.INTRADAY: 1,
            TimeHorizon.DAYS_1_3: 3,
            TimeHorizon.WEEK_1: 7,
            TimeHorizon.WEEKS_2_4: 21,
            TimeHorizon.MONTHS_1_3: 60,
        }[self]


class EventImportance(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PreliminaryQuality(str, Enum):
    """Agent 2's coarse self-assessment.

    Deliberately NOT numeric: the agent must not anchor the deterministic
    scoring engine with a number it invented.
    """

    SPECULATIVE = "SPECULATIVE"
    PLAUSIBLE = "PLAUSIBLE"
    WELL_SUPPORTED = "WELL_SUPPORTED"


class ValidationVerdict(str, Enum):
    CONFIRMED = "CONFIRMED"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"
    CONTRADICTED = "CONTRADICTED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class Classification(str, Enum):
    EXCEPTIONAL = "EXCEPTIONAL"
    HIGH_CONVICTION = "HIGH_CONVICTION"
    GOOD_CANDIDATE = "GOOD_CANDIDATE"
    WATCHLIST = "WATCHLIST"
    REJECTED = "REJECTED"


class RejectionReasonCode(str, Enum):
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_VOLUME = "INSUFFICIENT_VOLUME"
    INSUFFICIENT_OPEN_INTEREST = "INSUFFICIENT_OPEN_INTEREST"
    MISSING_CRITICAL_DATA = "MISSING_CRITICAL_DATA"
    CATALYST_NOT_VALIDATED = "CATALYST_NOT_VALIDATED"
    EARNINGS_BLACKOUT = "EARNINGS_BLACKOUT"
    PROVIDER_DISAGREEMENT = "PROVIDER_DISAGREEMENT"
    REWARD_RISK_TOO_LOW = "REWARD_RISK_TOO_LOW"
    PREMIUM_EXCEEDS_LIMIT = "PREMIUM_EXCEEDS_LIMIT"
    EXCESSIVE_THETA = "EXCESSIVE_THETA"
    NO_TRADABLE_CONTRACT = "NO_TRADABLE_CONTRACT"
    STRATEGY_NOT_ALLOWED = "STRATEGY_NOT_ALLOWED"
    STALE_QUOTES = "STALE_QUOTES"
    BELOW_MIN_SCORE = "BELOW_MIN_SCORE"


class HumanDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WATCHED = "WATCHED"
    ENTERED = "ENTERED"
    SKIPPED = "SKIPPED"


class DataProvider(str, Enum):
    FMP = "fmp"
    UNUSUAL_WHALES = "unusual_whales"
    ROBINHOOD = "robinhood"
    NEWS = "news"
    INTERNAL = "internal"


class AgentName(str, Enum):
    MARKET_INTELLIGENCE = "market_intelligence"
    OPPORTUNITY_GENERATOR = "opportunity_generator"
    TRADE_VALIDATOR = "trade_validator"
    RISK_REVIEWER = "risk_reviewer"


class AgentStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DataQualitySeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


__all__ = [n for n in dir() if n[0].isupper()]
