from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Intent(str, Enum):
    INTERESTED = "interested"
    NEEDS_MORE_INFO = "needs_more_info"
    EXPLICITLY_REJECTED = "explicitly_rejected"
    OFF_TOPIC = "off_topic"
    OTHER = "other"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ESCALATED = "escalated"
    CLOSED_NOT_INTERESTED = "closed_not_interested"


class ActionType(str, Enum):
    REPLY = "reply"
    SCHEDULE_FOLLOWUP = "schedule_followup"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    MARK_NOT_INTERESTED = "mark_not_interested"


class RiskFlag(str, Enum):
    NONE = "none"
    PROMPT_EXTRACTION = "prompt_extraction"
    INTERNAL_POLICY_REQUEST = "internal_policy_request"
    PRICE_FLOOR_REQUEST = "price_floor_request"
    UNAUTHORIZED_ACTION_REQUEST = "unauthorized_action_request"


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    unhappy: bool
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=80)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    action_candidate: ActionType | None = None
    reply_draft: str | None = Field(default=None, max_length=2000)


@dataclass
class Session:
    customer_id: str
    status: SessionStatus = SessionStatus.ACTIVE
    abnormal_streak: int = 0
    version: int = 0
    last_outbound_at: float | None = None
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


@dataclass
class ConversationResult:
    trace_id: str
    customer_id: str
    message_id: str
    action: str
    reply: str | None
    intent: Intent | None
    unhappy: bool | None
    abnormal_streak: int
    session_status: SessionStatus
    rate_limited: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "customer_id": self.customer_id,
            "message_id": self.message_id,
            "action": self.action,
            "reply": self.reply,
            "intent": self.intent.value if self.intent else None,
            "unhappy": self.unhappy,
            "abnormal_streak": self.abnormal_streak,
            "session_status": self.session_status.value,
            "rate_limited": self.rate_limited,
            "reason": self.reason,
        }


ALLOWED_ACTIONS = frozenset(ActionType)


def is_abnormal(decision: IntentDecision) -> bool:
    return decision.intent is Intent.OFF_TOPIC or decision.unhappy


def advance_streak(session: Session, decision: IntentDecision) -> None:
    session.abnormal_streak = session.abnormal_streak + 1 if is_abnormal(decision) else 0
    session.updated_at = datetime.now(timezone.utc).timestamp()


def can_transition(status: SessionStatus, action: ActionType) -> bool:
    return status is SessionStatus.ACTIVE and action in ALLOWED_ACTIONS


class PolicyDecision(BaseModel):
    action: ActionType | None
    reason: str
    safe_reply: bool = False


class PolicyEngine:
    """Deterministic authority for business actions; LLM output is only a signal."""

    def decide(self, session: Session, decision: IntentDecision) -> PolicyDecision:
        if session.status is not SessionStatus.ACTIVE:
            return PolicyDecision(action=None, reason="terminal_session_silent")
        if session.abnormal_streak >= 2:
            return PolicyDecision(action=ActionType.ESCALATE_TO_HUMAN, reason="two_consecutive_abnormal_messages")
        if any(flag is not RiskFlag.NONE for flag in decision.risk_flags):
            return PolicyDecision(action=ActionType.REPLY, reason="sensitive_request_safe_template", safe_reply=True)
        if decision.intent is Intent.EXPLICITLY_REJECTED:
            if decision.confidence < 0.6:
                return PolicyDecision(action=ActionType.SCHEDULE_FOLLOWUP, reason="low_confidence_rejection")
            return PolicyDecision(action=ActionType.MARK_NOT_INTERESTED, reason="explicit_rejection")
        if decision.intent in (Intent.INTERESTED, Intent.NEEDS_MORE_INFO, Intent.OFF_TOPIC):
            return PolicyDecision(action=ActionType.REPLY, reason=f"intent_{decision.intent.value}")
        return PolicyDecision(action=ActionType.SCHEDULE_FOLLOWUP, reason="uncertain_intent")
