"""User state management for multi-step Telegram bot flows."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class UserState(str, Enum):
    """Possible user states in the bot flow."""
    IDLE = "idle"
    AWAITING_BILL_REVIEW = "awaiting_bill_review"
    EDITING_FIELD = "editing_field"
    AWAITING_CONFIG_VALUE = "awaiting_config_value"
    AWAITING_ALERT_TIME = "awaiting_alert_time"
    AWAITING_SNOOZE_DAYS = "awaiting_snooze_days"
    CONFIRMING_DELETE = "confirming_delete"


@dataclass
class UserStateData:
    """Data associated with a user's current state."""
    state: UserState = UserState.IDLE
    bill_id: int | None = None
    field_name: str | None = None
    original_message_id: int | None = None
    review_message_id: int | None = None
    temp_data: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def reset(self) -> None:
        """Reset to idle state."""
        self.state = UserState.IDLE
        self.bill_id = None
        self.field_name = None
        self.original_message_id = None
        self.review_message_id = None
        self.temp_data.clear()
        self.updated_at = datetime.utcnow()


class StateManager:
    """Manages user states across the bot."""

    def __init__(self) -> None:
        self._states: dict[int, UserStateData] = {}

    def get_state(self, user_id: int) -> UserStateData:
        """Get or create user state."""
        if user_id not in self._states:
            self._states[user_id] = UserStateData()
        return self._states[user_id]

    def set_state(
        self,
        user_id: int,
        state: UserState,
        **kwargs: Any,
    ) -> UserStateData:
        """Set user state with optional data."""
        user_state = self.get_state(user_id)
        user_state.state = state
        user_state.updated_at = datetime.utcnow()

        for key, value in kwargs.items():
            if hasattr(user_state, key):
                setattr(user_state, key, value)
            else:
                user_state.temp_data[key] = value

        return user_state

    def clear_state(self, user_id: int) -> None:
        """Clear user state to idle."""
        if user_id in self._states:
            self._states[user_id].reset()

    def get_temp_data(self, user_id: int, key: str, default: Any = None) -> Any:
        """Get temporary data for user."""
        return self.get_state(user_id).temp_data.get(key, default)

    def set_temp_data(self, user_id: int, key: str, value: Any) -> None:
        """Set temporary data for user."""
        self.get_state(user_id).temp_data[key] = value

    def is_in_state(self, user_id: int, state: UserState) -> bool:
        """Check if user is in specific state."""
        return self.get_state(user_id).state == state

    def cleanup_old_states(self, max_age_hours: int = 24) -> int:
        """Remove states older than max_age_hours. Returns count removed."""
        now = datetime.utcnow()
        to_remove = [
            uid for uid, state in self._states.items()
            if (now - state.updated_at).total_seconds() > max_age_hours * 3600
        ]
        for uid in to_remove:
            del self._states[uid]
        return len(to_remove)


# Global state manager instance
state_manager = StateManager()
