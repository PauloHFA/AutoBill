"""Sistema de alertas proativos, política escalonada e agendamento."""

from typing import TYPE_CHECKING, Any

from autobill.alerts.formatter import (
    format_monthly_summary,
    format_proactive_alert,
    format_weekly_summary,
    get_alert_action_keyboard,
)
from autobill.alerts.policy import (
    AlertDecision,
    check_anti_golpe_bank_mismatch,
    decide_bill_alert,
    local_today,
    parse_lead_days,
)

if TYPE_CHECKING:
    from autobill.alerts.scheduler import AlertScheduler

__all__ = [
    "AlertDecision",
    "AlertScheduler",
    "check_anti_golpe_bank_mismatch",
    "decide_bill_alert",
    "format_monthly_summary",
    "format_proactive_alert",
    "format_weekly_summary",
    "get_alert_action_keyboard",
    "local_today",
    "parse_lead_days",
]


def __getattr__(name: str) -> Any:
    if name == "AlertScheduler":
        from autobill.alerts.scheduler import AlertScheduler

        return AlertScheduler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
