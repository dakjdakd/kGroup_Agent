from __future__ import annotations

import json
import uuid
import threading
from collections import defaultdict
from typing import Protocol

from .domain import ActionType, ConversationResult, IntentDecision, PolicyEngine, SessionStatus, is_abnormal
from .gateway import ActionGateway, SAFE_REPLY
from .storage import SQLiteStore


class LLMProvider(Protocol):
    def classify(self, message: str, history: list[dict[str, str]] | None = None) -> IntentDecision: ...
    def draft_reply(self, message: str, decision: IntentDecision, safe: bool = False) -> str: ...


class ConversationService:
    def __init__(self, store: SQLiteStore, llm: LLMProvider, gateway: ActionGateway | None = None, policy: PolicyEngine | None = None) -> None:
        self.store = store
        self.llm = llm
        self.gateway = gateway or ActionGateway(store)
        self.policy = policy or PolicyEngine()
        self._customer_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    def handle_message(self, customer_id: str, message_id: str, text: str) -> ConversationResult:
        if not customer_id.strip() or not message_id.strip() or not text.strip():
            raise ValueError("customer_id, message_id and text must be non-empty")
        with self._customer_locks[customer_id]:
            trace_id = str(uuid.uuid4())
            session = self.store.get_session(customer_id)
            if not self.store.add_message(message_id, customer_id, "inbound", text):
                event = self.store.find_event_for_message(message_id)
                if event:
                    payload = json.loads(event.get("payload") or "{}")
                    action = payload.get("final_action", event["action_type"])
                    return ConversationResult(
                        trace_id=trace_id,
                        customer_id=customer_id,
                        message_id=message_id,
                        action=action,
                        reply=payload.get("reply"),
                        intent=None,
                        unhappy=None,
                        abnormal_streak=payload.get("abnormal_streak", session.abnormal_streak),
                        session_status=session.status,
                        rate_limited=bool(payload.get("rate_limited", False)),
                        reason="idempotent_replay",
                    )
                # Another worker committed the inbound id before its decision/event.
                # Do not process the same message a second time; the first worker owns it.
                return ConversationResult(
                    trace_id=trace_id, customer_id=customer_id, message_id=message_id,
                    action="silent", reply=None, intent=None, unhappy=None,
                    abnormal_streak=session.abnormal_streak, session_status=session.status,
                    reason="idempotent_in_progress",
                )
            if session.status is not SessionStatus.ACTIVE:
                action_id = str(uuid.uuid4())
                self.store.add_event(
                    action_id, customer_id, message_id, "silent", "blocked", "terminal_session_silent", trace_id,
                    {"final_action": "silent", "session_status": session.status.value, "abnormal_streak": session.abnormal_streak},
                )
                return ConversationResult(trace_id=trace_id, customer_id=customer_id, message_id=message_id, action="silent", reply=None, intent=None, unhappy=None, abnormal_streak=session.abnormal_streak, session_status=session.status, reason="terminal_session_silent")

            decision = self.llm.classify(text, self.store.get_history(customer_id))
            session = self.store.apply_abnormal_signal(customer_id, is_abnormal(decision))
            if session.status is not SessionStatus.ACTIVE:
                self.store.add_event(
                    str(uuid.uuid4()), customer_id, message_id, "silent", "blocked", "terminal_session_silent", trace_id,
                    {"final_action": "silent", "session_status": session.status.value, "abnormal_streak": session.abnormal_streak},
                )
                return ConversationResult(trace_id, customer_id, message_id, "silent", None, decision.intent, decision.unhappy, session.abnormal_streak, session.status, reason="terminal_session_silent")
            policy = self.policy.decide(session, decision)
            if policy.action is None:
                return ConversationResult(trace_id, customer_id, message_id, "silent", None, decision.intent, decision.unhappy, session.abnormal_streak, session.status, reason=policy.reason)

            reply = None
            if policy.action is ActionType.REPLY:
                reply = SAFE_REPLY if policy.safe_reply else self.llm.draft_reply(text, decision)
            result_action, result_reply, rate_limited = self.gateway.execute(
                session,
                policy.action,
                message_id,
                trace_id,
                reply,
                policy.reason,
                {"intent": decision.intent.value, "unhappy": decision.unhappy, "risk_flags": [flag.value for flag in decision.risk_flags]},
            )
            return ConversationResult(trace_id, customer_id, message_id, result_action, result_reply, decision.intent, decision.unhappy, session.abnormal_streak, session.status, rate_limited, policy.reason)

    def reactivate(self, customer_id: str) -> SessionStatus:
        with self._customer_locks[customer_id]:
            session = self.store.get_session(customer_id)
            previous = session.status
            session.status = SessionStatus.ACTIVE
            session.abnormal_streak = 0
            session.version += 1
            self.store.save_session(session)
            trace_id = str(uuid.uuid4())
            self.store.add_event(
                str(uuid.uuid4()), customer_id, None, "human_reactivate", "executed", "controlled_human_reactivation", trace_id,
                {"from_status": previous.value, "to_status": session.status.value},
            )
            return session.status
