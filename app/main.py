from __future__ import annotations

import argparse
import hmac
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .domain import SessionStatus
from .llm import DemoLLM, GeminiClient, LLMError
from .service import ConversationService
from .storage import SQLiteStore
from .workflow import build_graph


def _load_dotenv(path: str = ".env") -> None:
    """Load the tiny local config file without adding a runtime dependency."""
    file = Path(path)
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


class MessageRequest(BaseModel):
    # The upstream channel must provide a stable id; generating one server-side
    # would turn a client retry into a second business message.
    message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=5000)


def create_app(service: ConversationService | None = None) -> FastAPI:
    store = service.store if service else SQLiteStore(os.getenv("DATABASE_PATH", "data/agent.db"))
    llm = service.llm if service else GeminiClient()
    service = service or ConversationService(store, llm)
    workflow = build_graph(service)
    app = FastAPI(title="Guarded Lead Agent", version="0.1.0")
    app.state.service = service
    static_dir = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    def frontend():
        return FileResponse(static_dir / "index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "llm_configured": getattr(llm, "configured", True)}

    @app.post("/sessions/{customer_id}/messages")
    def message(customer_id: str, request: MessageRequest):
        try:
            state = workflow.invoke({"customer_id": customer_id, "message_id": request.message_id, "text": request.text})
            return state["result"]
        except LLMError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/sessions/{customer_id}/reactivate")
    def reactivate(
        customer_id: str,
        x_actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
        x_actor_role: str | None = Header(default=None, alias="X-Actor-Role"),
        x_human_token: str | None = Header(default=None, alias="X-Human-Token"),
    ):
        configured_token = os.getenv("HUMAN_REACTIVATE_TOKEN", "")
        if not configured_token or not x_actor_id or not x_actor_role or not x_human_token:
            raise HTTPException(status_code=401, detail="human actor credentials are required")
        if not hmac.compare_digest(x_human_token, configured_token):
            raise HTTPException(status_code=403, detail="invalid human actor token")
        try:
            status = service.reactivate(customer_id, x_actor_id, x_actor_role)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"customer_id": customer_id, "status": status.value, "actor_id": x_actor_id}

    @app.get("/sessions/{customer_id}")
    def session(
        customer_id: str,
        x_actor_role: str | None = Header(default=None, alias="X-Actor-Role"),
        x_human_token: str | None = Header(default=None, alias="X-Human-Token"),
    ):
        configured_token = os.getenv("HUMAN_REACTIVATE_TOKEN", "")
        if not configured_token or x_actor_role not in {"human_agent", "admin"} or not x_human_token:
            raise HTTPException(status_code=401, detail="human actor credentials are required")
        if not hmac.compare_digest(x_human_token, configured_token):
            raise HTTPException(status_code=403, detail="invalid human actor token")
        current = store.get_session(customer_id)
        return {"customer_id": customer_id, "status": current.status.value, "abnormal_streak": current.abnormal_streak, "version": current.version, "last_outbound_at": current.last_outbound_at}

    @app.get("/sessions/{customer_id}/history")
    def history(
        customer_id: str,
        x_actor_role: str | None = Header(default=None, alias="X-Actor-Role"),
        x_human_token: str | None = Header(default=None, alias="X-Human-Token"),
    ):
        configured_token = os.getenv("HUMAN_REACTIVATE_TOKEN", "")
        if not configured_token or x_actor_role not in {"human_agent", "admin"} or not x_human_token:
            raise HTTPException(status_code=401, detail="human actor credentials are required")
        if not hmac.compare_digest(x_human_token, configured_token):
            raise HTTPException(status_code=403, detail="invalid human actor token")
        service._validate_identifier(customer_id, "customer_id")
        return {"customer_id": customer_id, "messages": store.get_history_records(customer_id)}

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
