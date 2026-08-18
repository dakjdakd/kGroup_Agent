from __future__ import annotations

import time
import uuid
import re
import unicodedata
from typing import Any, Protocol

from .domain import ActionType, Session, SessionStatus, can_transition
from .rate_limit import RateLimiter, SQLiteRateLimiter
from .storage import SQLiteStore


SAFE_REPLY = "我可以继续介绍公开的产品信息。如果你有具体需求，我可以先帮你整理，再由工作人员确认。"


class OutboundProvider(Protocol):
    """Provider boundary for a real IM/CRM connector."""

    def send(self, customer_id: str, text: str, idempotency_key: str) -> bool: ...


class SQLiteOutboxProvider:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def send(self, customer_id: str, text: str, idempotency_key: str) -> bool:
        return self.store.record_outbound(idempotency_key, customer_id, text)


class ReplyValidator:
    def validate(self, text: str) -> bool:
        lowered = unicodedata.normalize("NFKC", text).casefold()
        compact = re.sub(r"\s+", "", lowered)
        forbidden = (
            "api_key", "system prompt", "developer message", "internal policy", "price floor",
            "系统提示词", "内部规则", "价格底线", "secret", "密钥",
        )
        return (
            bool(text.strip())
            and len(text) <= 2000
            and not any(token in lowered or token.replace(" ", "") in compact for token in forbidden)
            and not re.search(r"(?:system|developer)\s*(?:prompt|message)|内部\s*(?:规则|提示)", lowered)
        )


class ActionGateway:
    """Single side-effect boundary. No caller can bypass state or rate checks."""

    def __init__(self, store: SQLiteStore, validator: ReplyValidator | None = None, rate_limiter: RateLimiter | None = None, outbound_provider: OutboundProvider | None = None) -> None:
        self.store = store
        self.validator = validator or ReplyValidator()
        self.rate_limiter = rate_limiter or SQLiteRateLimiter(store)
        self.outbound_provider = outbound_provider or SQLiteOutboxProvider(store)

    def execute(
        self,
        session: Session,
        action: ActionType,
        message_id: str,
        trace_id: str,
        reply: str | None = None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        claim_token: str | None = None,
    ) -> tuple[str, str | None, bool]:
        action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"guarded-lead:{session.customer_id}:{message_id}:action"))
        metadata = metadata or {}
        if claim_token is not None and not self.store.claim_is_valid(session.customer_id, message_id, claim_token):
            return "silent", None, False
        try:
            action = ActionType(action)
        except (TypeError, ValueError):
            self.store.add_event(action_id, session.customer_id, message_id, str(action), "blocked", "unknown_action", trace_id)
            return "silent", None, False
        if session.status is not SessionStatus.ACTIVE or not can_transition(session.status, action):
            self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "terminal_session_or_invalid_transition", trace_id)
            return "silent", None, False

        if action is ActionType.REPLY:
            if not reply or not self.validator.validate(reply):
                self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "reply_validation_failed", trace_id, {**metadata, "final_action": ActionType.SCHEDULE_FOLLOWUP.value, "rate_limited": False})
                return "schedule_followup", None, False
            if not self.rate_limiter.allow(session.customer_id, action_id, time.time()):
                self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "sliding_window_rate_limit", trace_id, {**metadata, "final_action": ActionType.SCHEDULE_FOLLOWUP.value, "rate_limited": True})
                return "schedule_followup", None, True
            sent_at = time.time()
            try:
                accepted = self.outbound_provider.send(session.customer_id, reply, action_id)
            except Exception as exc:
                accepted = False
                self.store.record_outbound_failure(action_id, session.customer_id, reply, str(exc))
            if not accepted:
                release = getattr(self.rate_limiter, "release", None)
                if release is not None:
                    release(session.customer_id, action_id)
                self.store.record_outbound_failure(action_id, session.customer_id, reply, "provider_rejected")
                self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "outbound_provider_rejected", trace_id, {**metadata, "final_action": ActionType.SCHEDULE_FOLLOWUP.value, "rate_limited": False})
                return "schedule_followup", None, False
            # External providers do not necessarily write our conversation history;
            # record the accepted reply idempotently for future LLM context.
            self.store.add_message(action_id, session.customer_id, "outbound", reply)
            if not self.store.commit_reply(session.customer_id, sent_at):
                self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "session_changed_after_send", trace_id, {**metadata, "final_action": "silent"})
                return "silent", None, False
            session.last_outbound_at = sent_at
            session.version += 1
            self.store.add_event(
                action_id, session.customer_id, message_id, action.value, "executed", reason, trace_id,
                {**metadata, "reply": reply, "final_action": action.value, "abnormal_streak": session.abnormal_streak},
            )
            return action.value, reply, False

        if action is ActionType.ESCALATE_TO_HUMAN:
            target_status = SessionStatus.ESCALATED
        elif action is ActionType.MARK_NOT_INTERESTED:
            target_status = SessionStatus.CLOSED_NOT_INTERESTED
        else:
            target_status = None
        if target_status is not None:
            if not self.store.transition_session(session.customer_id, target_status):
                self.store.add_event(action_id, session.customer_id, message_id, action.value, "blocked", "session_changed_before_transition", trace_id, {**metadata, "final_action": "silent"})
                return "silent", None, False
            session.status = target_status
            session.version += 1
        self.store.add_event(
            action_id, session.customer_id, message_id, action.value, "executed", reason, trace_id,
            {**metadata, "final_action": action.value, "abnormal_streak": session.abnormal_streak},
        )
        return action.value, None, False
