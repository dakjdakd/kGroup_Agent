"""Run a safe, single-request Gemini configuration and connectivity check.

The script intentionally prints only non-secret metadata and a short, classified
error message. It never prints the API key or the request URL containing it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python scripts/check_gemini.py` from the repository root without
# requiring an editable install first.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.llm import GeminiClient, LLMError
from app.main import _load_dotenv


def classify_failure(detail: str) -> str:
    text = detail.casefold()
    if "user location is not supported" in text:
        return "region_blocked"
    if "http 401" in text or "http 403" in text:
        return "invalid_or_unauthorized_key"
    if "http 404" in text:
        return "model_or_endpoint_not_found"
    if "http 429" in text:
        return "quota_or_rate_limited"
    if "timed out" in text or "timeout" in text:
        return "network_timeout"
    if "urlopen error" in text or "name or service not known" in text:
        return "network_or_dns_error"
    return "provider_request_failed"


def main() -> int:
    _load_dotenv(str(ROOT / ".env"))
    client = GeminiClient()
    print({
        "key_present": client.configured,
        "key_length": len(client.api_key),
        "model": client.model,
        "base_url": client.base_url,
        "provider": os.getenv("LLM_PROVIDER", "gemini"),
    })
    if not client.configured:
        print({"request": "not_run", "reason": "GEMINI_API_KEY is not configured"})
        return 2
    try:
        decision = client.classify("连接性检查：请返回结构化分类结果", [])
    except LLMError as exc:
        detail = str(exc)
        print({"request": "failed", "category": classify_failure(detail), "detail": detail[:300]})
        return 1
    print({
        "request": "ok",
        "intent": decision.intent.value,
        "confidence": decision.confidence,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
