from __future__ import annotations

import json
import uuid
import threading
from typing import Protocol
from weakref import WeakValueDictionary

from .domain import ActionType, ConversationResult, IntentDecision, PolicyEngine, SessionStatus, is_abnormal
from .gateway import ActionGateway, SAFE_REPLY
from .llm import LLMError
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
        # Weak values prevent attacker-controlled customer IDs from growing this map forever.
        self._customer_locks: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
        self._customer_locks_guard = threading.Lock()
        self.processing_timeout_seconds = 120.0

    def _customer_lock(self, customer_id: str) -> threading.RLock:
        with self._customer_locks_guard:
            lock = self._customer_locks.get(customer_id)
            if lock is None:
                lock = threading.RLock()
                self._customer_locks[customer_id] = lock
            return lock

    @staticmethod
    def _validate_identifier(value: str, field: str) -> None:
        if not value or not value.strip():
            raise ValueError(f"{field} must be non-empty")
        if len(value) > 128 or any(ord(char) < 32 for char in value):
            raise ValueError(f"{field} must be at most 128 characters and contain no control characters")

    def handle_message(self, customer_id: str, message_id: str, text: str) -> ConversationResult:
        self._validate_identifier(customer_id, "customer_id")
        self._validate_identifier(message_id, "message_id")
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        with self._customer_lock(customer_id):
            trace_id = str(uuid.uuid4())
            session = self.store.get_session(customer_id)
            claim = self.store.claim_message(message_id, customer_id, text, self.processing_timeout_seconds)
            if claim.state == "conflict":
                raise ValueError("message_id already exists for this customer with different text")
            if claim.state != "claimed":
                event = self.store.find_event_for_message(customer_id, message_id)
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
                return ConversationResult(
                    trace_id=trace_id, customer_id=customer_id, message_id=message_id,
                    action="silent", reply=None, intent=None, unhappy=None,
                    abnormal_streak=session.abnormal_streak, session_status=session.status,
                    reason="idempotent_in_progress" if claim.state == "processing" else "idempotent_replay",
                )
            try:
                if session.status is not SessionStatus.ACTIVE:
                    action_id = str(uuid.uuid4())
                    self.store.add_event(
                        action_id, customer_id, message_id, "silent", "blocked", "terminal_session_silent", trace_id,
                        {"final_action": "silent", "session_status": session.status.value, "abnormal_streak": session.abnormal_streak},
                    )
                    self.store.mark_message_completed(customer_id, message_id, claim.claim_token)
                    return ConversationResult(trace_id=trace_id, customer_id=customer_id, message_id=message_id, action="silent", reply=None, intent=None, unhappy=None, abnormal_streak=session.abnormal_streak, session_status=session.status, reason="terminal_session_silent")

                if not claim.claim_token:
                    raise LLMError("message claim token missing")
                if claim.decision_json:
                    from .domain import IntentDecision
                    decision = IntentDecision.model_validate(json.loads(claim.decision_json))
                else:
                    decision = self.llm.classify(text, self.store.get_history(customer_id))
                session = self.store.save_decision_once(
                    customer_id, message_id, claim.claim_token,
                    decision.model_dump_json(), is_abnormal(decision),
                )
                if session.status is not SessionStatus.ACTIVE:
                    self.store.add_event(
                        str(uuid.uuid4()), customer_id, message_id, "silent", "blocked", "terminal_session_silent", trace_id,
                        {"final_action": "silent", "session_status": session.status.value, "abnormal_streak": session.abnormal_streak, "intent": decision.intent.value, "unhappy": decision.unhappy, "confidence": decision.confidence},
                    )
                    self.store.mark_message_completed(customer_id, message_id, claim.claim_token)
                    return ConversationResult(trace_id, customer_id, message_id, "silent", None, decision.intent, decision.unhappy, session.abnormal_streak, session.status, reason="terminal_session_silent")
                policy = self.policy.decide(session, decision)
                if policy.action is None:
                    self.store.add_event(str(uuid.uuid4()), customer_id, message_id, "silent", "executed", policy.reason, trace_id, {"final_action": "silent", "intent": decision.intent.value, "confidence": decision.confidence})
                    self.store.mark_message_completed(customer_id, message_id, claim.claim_token)
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
                    {"intent": decision.intent.value, "unhappy": decision.unhappy, "confidence": decision.confidence, "risk_flags": [flag.value for flag in decision.risk_flags]},
                    claim_token=claim.claim_token,
                )
                if not self.store.mark_message_completed(customer_id, message_id, claim.claim_token):
                    raise LLMError("message claim lost before completion")
                return ConversationResult(trace_id, customer_id, message_id, result_action, result_reply, decision.intent, decision.unhappy, session.abnormal_streak, session.status, rate_limited, policy.reason)
            except Exception as exc:
                self.store.mark_message_failed(customer_id, message_id, str(exc), claim.claim_token)
                if isinstance(exc, LLMError):
                    raise
                raise

    def reactivate(self, customer_id: str, actor_id: str, actor_role: str) -> SessionStatus:
        self._validate_identifier(customer_id, "customer_id")
        self._validate_identifier(actor_id, "actor_id")
        if actor_role not in {"human_agent", "admin"}:
            raise PermissionError("human_agent or admin role required")
        with self._customer_lock(customer_id):
            session = self.store.get_session(customer_id)
            previous = session.status
            updated = self.store.reactivate_session(customer_id, session.version)
            if updated is None:
                raise RuntimeError("session changed during reactivation; retry")
            session = updated
            trace_id = str(uuid.uuid4())
            self.store.add_event(
                str(uuid.uuid4()), customer_id, None, "human_reactivate", "executed", "controlled_human_reactivation", trace_id,
                {"from_status": previous.value, "to_status": session.status.value, "actor_id": actor_id, "actor_role": actor_role},
            )
            return session.status
