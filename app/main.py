from __future__ import annotations

import argparse
import os
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .domain import SessionStatus
from .llm import DemoLLM, GeminiClient, LLMError
from .service import ConversationService
from .storage import SQLiteStore
from .workflow import build_graph


class MessageRequest(BaseModel):
    message_id: str | None = None
    text: str = Field(min_length=1, max_length=5000)


def create_app(service: ConversationService | None = None) -> FastAPI:
    store = service.store if service else SQLiteStore(os.getenv("DATABASE_PATH", "data/agent.db"))
    llm = service.llm if service else GeminiClient()
    service = service or ConversationService(store, llm)
    workflow = build_graph(service)
    app = FastAPI(title="Guarded Lead Agent", version="0.1.0")
    app.state.service = service

    @app.get("/health")
    def health():
        return {"ok": True, "llm_configured": getattr(llm, "configured", True)}

    @app.post("/sessions/{customer_id}/messages")
    def message(customer_id: str, request: MessageRequest):
        try:
            state = workflow.invoke({"customer_id": customer_id, "message_id": request.message_id or str(uuid.uuid4()), "text": request.text})
            return state["result"]
        except LLMError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/sessions/{customer_id}/reactivate")
    def reactivate(customer_id: str):
        return {"customer_id": customer_id, "status": service.reactivate(customer_id).value}

    @app.get("/sessions/{customer_id}")
    def session(customer_id: str):
        current = store.get_session(customer_id)
        return {"customer_id": customer_id, "status": current.status.value, "abnormal_streak": current.abnormal_streak, "version": current.version, "last_outbound_at": current.last_outbound_at}

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Guarded lead qualification agent")
    parser.add_argument("--demo", action="store_true", help="run without an API key")
    args = parser.parse_args()
    if args.demo:
        service = ConversationService(SQLiteStore(":memory:"), DemoLLM())
        current = "demo-customer"
        print("Guarded Lead Agent demo. Type /quit to exit.")
        while True:
            text = input("you> ").strip()
            if text == "/quit":
                break
            result = service.handle_message(current, str(uuid.uuid4()), text)
            print(f"agent [{result.action}]> {result.reply or '(silent)'}")
    else:
        import uvicorn
        uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")), reload=False)


if __name__ == "__main__":
    main()
