from __future__ import annotations

import time
import uuid

from .domain import ActionType, Session, SessionStatus, can_transition
from .rate_limit import RateLimiter, SQLiteRateLimiter
from .storage import SQLiteStore


SAFE_REPLY = "我可以继续介绍公开的产品信息。如果你有具体需求，我可以先帮你整理，再由工作人员确认。"


class ReplyValidator:
    def validate(self, text: str) -> bool:
        lowered = text.lower()
        forbidden = ("api_key", "system prompt", "系统提示词", "内部规则", "价格底线", "secret")
        return bool(text.strip()) and len(text) <= 2000 and not any(token in lowered for token in forbidden)


class ActionGateway:
    """Single side-effect boundary. No caller can bypass state or rate checks."""

    def __init__(self, store: SQLiteStore, validator: ReplyValidator | None = None, rate_limiter: RateLimiter | None = None) -> None:
        self.store = store
        self.validator = validator or ReplyValidator()
        self.rate_limiter = rate_limiter or SQLiteRateLimiter(store)

    def execute(self, session: Session, action: ActionType, message_id: str, trace_id: str, reply: str | None = None, reason: str = "") -> tuple[str, str | None, bool]:
        action_id = str(uuid.uuid4())
        if session.status is not SessionStatus.ACTIVE or not can_transition(session.status, action):
            self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "terminal_session_or_invalid_transition", trace_id)
            return "silent", None, False

        if action is ActionType.REPLY:
            if not reply or not self.validator.validate(reply):
                self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "reply_validation_failed", trace_id)
                return "schedule_followup", None, False
            if not self.rate_limiter.allow(session.customer_id, action_id, time.time()):
                self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "sliding_window_rate_limit", trace_id)
                return "schedule_followup", None, True
            session.last_outbound_at = time.time()
            session.version += 1
            self.store.save_session(session)
            self.store.add_event(action_id, session.customer_id, message_id, action.value, "executed", reason, trace_id, {"reply": reply})
            return action.value, reply, False

        if action is ActionType.ESCALATE_TO_HUMAN:
            session.status = SessionStatus.ESCALATED
        elif action is ActionType.MARK_NOT_INTERESTED:
            session.status = SessionStatus.CLOSED_NOT_INTERESTED
        session.version += 1
        self.store.save_session(session)
        self.store.add_event(action_id, session.customer_id, message_id, action.value, "executed", reason, trace_id)
        return action.value, None, False
