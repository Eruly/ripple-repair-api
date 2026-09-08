"""OpenAI 호환 /chat/completions 를 표준 라이브러리(urllib)로 호출해 JSON 객체를 받는다.

- 인증: OPENAI_API_KEY (또는 REPORT_AUDIT_API_KEY). 127.0.0.1/localhost 서버는 키 없이 호출한다.
- 사고(thinking) 모델: REPORT_AUDIT_LLM_THINKING=1 이면 사고를 켠 채 큰 출력 한도를 준다. 기본은 끔 (JSON 안정성).
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from typing import Any


def _api_key(base_url: str) -> str:
    key = os.getenv("REPORT_AUDIT_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        return key
    if "127.0.0.1" in base_url or "localhost" in base_url:
        return "dummy"
    raise RuntimeError("OPENAI_API_KEY 필요")


LLM_THINKING = os.getenv("REPORT_AUDIT_LLM_THINKING", "0") == "1"
_LAST_USAGE = threading.local()  # 호출 스레드별 마지막 응답의 토큰 사용량


def last_usage() -> dict | None:
    return getattr(_LAST_USAGE, "usage", None)


def chat_json(base_url: str, model: str, messages: list[dict[str, str]], timeout: int = 900,
              max_tokens: int | None = None) -> dict[str, Any]:
    """JSON 객체 한 개를 돌려주는 채팅 호출. <think> 블록은 제거하고 첫 {…} 를 파싱한다."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    if max_tokens is None:
        max_tokens = 16000 if LLM_THINKING else 4000
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": LLM_THINKING}}
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {_api_key(base_url)}",
                                          "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    _LAST_USAGE.usage = payload.get("usage")
    choice = payload["choices"][0]
    content = choice["message"].get("content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    if not content:
        raise RuntimeError(f"empty content (finish_reason={choice.get('finish_reason')}, usage={payload.get('usage')})")
    m = re.search(r"\{.*\}", content, re.S)
    return json.loads(m.group(0) if m else content)
