"""Market session awareness and quote-staleness policy.

The premarket / market-open distinction is a first-class concept, not a
convenience: option quotes before the options market opens do not represent
prices anyone can transact at, so the pipeline is not permitted to finalise a
contract from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config.methodology import MarketScheduleConfig
from app.models.common import utcnow
from app.models.enums import WorkflowStage

# US market holidays are intentionally not hard-coded here: an incomplete
# holiday table is worse than none. `is_trading_day` covers weekends and the
# orchestrator surfaces a data-quality flag when a provider returns no bars
# for the requested day, which catches holidays without pretending to know
# every one of them.


@dataclass(frozen=True)
class SessionInfo:
    stage: WorkflowStage
    now_local: datetime
    trading_day: date
    options_quotes_actionable: bool
    max_quote_age_seconds: int
    note: str


class MarketCalendar:
    def __init__(self, schedule: MarketScheduleConfig) -> None:
        self.schedule = schedule
        self.tz = ZoneInfo(schedule.timezone)

    @staticmethod
    def _parse(hhmm: str) -> time:
        h, m = hhmm.split(":")
        return time(int(h), int(m))

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5

    def next_trading_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while not self.is_trading_day(nxt):
            nxt += timedelta(days=1)
        return nxt

    def session(self, now: datetime | None = None) -> SessionInfo:
        now_local = (now or utcnow()).astimezone(self.tz)
        today = now_local.date()
        t = now_local.time()

        closes = self._parse(self.schedule.regular_close)
        quotes_valid_from = self._parse(self.schedule.options_quote_valid_from)
        post_end = self._parse(self.schedule.postmarket_end)

        if not self.is_trading_day(today):
            return SessionInfo(
                stage=WorkflowStage.PREMARKET,
                now_local=now_local,
                trading_day=self.next_trading_day(today),
                options_quotes_actionable=False,
                max_quote_age_seconds=self.schedule.max_quote_age_seconds_premarket,
                note="Market closed (weekend). Option quotes are last-session values.",
            )

        if t < quotes_valid_from:
            return SessionInfo(
                stage=WorkflowStage.PREMARKET,
                now_local=now_local,
                trading_day=today,
                options_quotes_actionable=False,
                max_quote_age_seconds=self.schedule.max_quote_age_seconds_premarket,
                note=(
                    f"Before {self.schedule.options_quote_valid_from} ET: option quotes are "
                    "not actionable. Contract selection and scoring are deferred."
                ),
            )

        if quotes_valid_from <= t <= closes:
            return SessionInfo(
                stage=WorkflowStage.MARKET_OPEN,
                now_local=now_local,
                trading_day=today,
                options_quotes_actionable=True,
                max_quote_age_seconds=self.schedule.max_quote_age_seconds_live,
                note="Options market open; live quotes in use.",
            )

        if t <= post_end:
            return SessionInfo(
                stage=WorkflowStage.POSTMARKET,
                now_local=now_local,
                trading_day=today,
                options_quotes_actionable=False,
                max_quote_age_seconds=self.schedule.max_quote_age_seconds_premarket,
                note="After the close: quotes reflect the final print, not a live market.",
            )

        return SessionInfo(
            stage=WorkflowStage.POSTMARKET,
            now_local=now_local,
            trading_day=today,
            options_quotes_actionable=False,
            max_quote_age_seconds=self.schedule.max_quote_age_seconds_premarket,
            note="Outside all sessions.",
        )

    def is_quote_stale(self, as_of: datetime | None, session: SessionInfo) -> bool:
        """Stale means: too old to act on, given the current session."""
        if as_of is None:
            return True
        age = (utcnow() - as_of).total_seconds()
        return age > session.max_quote_age_seconds

    def opens_at(self, d: date) -> datetime:
        return datetime.combine(d, self._parse(self.schedule.regular_open), self.tz)


__all__ = ["MarketCalendar", "SessionInfo"]
