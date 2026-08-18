from fastapi.testclient import TestClient

from app.llm import DemoLLM
from app.llm import GeminiClient
from app.main import create_app
from app.service import ConversationService
from app.storage import SQLiteStore


def test_reactivate_requires_human_credentials(monkeypatch):
    monkeypatch.setenv("HUMAN_REACTIVATE_TOKEN", "test-token")
    service = ConversationService(SQLiteStore(":memory:"), DemoLLM())
    client = TestClient(create_app(service))
    assert client.post("/sessions/c1/reactivate").status_code == 401
    assert client.post("/sessions/c1/reactivate", headers={
        "X-Actor-Id": "agent-1", "X-Actor-Role": "human_agent", "X-Human-Token": "wrong"
    }).status_code == 403
    response = client.post("/sessions/c1/reactivate", headers={
        "X-Actor-Id": "agent-1", "X-Actor-Role": "human_agent", "X-Human-Token": "test-token"
    })
    assert response.status_code == 200
    assert response.json()["actor_id"] == "agent-1"


def test_gemini_client_reads_key_from_environment_without_logging_it(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-key")
    client = GeminiClient()
    assert client.configured is True
    assert client.api_key == "test-only-key"


def test_message_id_is_required_for_http_idempotency():
    service = ConversationService(SQLiteStore(":memory:"), DemoLLM())
    client = TestClient(create_app(service))
    response = client.post("/sessions/c1/messages", json={"text": "hello"})
    assert response.status_code == 422


def test_session_read_requires_human_credentials(monkeypatch):
    monkeypatch.setenv("HUMAN_REACTIVATE_TOKEN", "test-token")
    service = ConversationService(SQLiteStore(":memory:"), DemoLLM())
    client = TestClient(create_app(service))
    assert client.get("/sessions/c1").status_code == 401
    response = client.get("/sessions/c1", headers={"X-Actor-Role": "human_agent", "X-Human-Token": "test-token"})
    assert response.status_code == 200
