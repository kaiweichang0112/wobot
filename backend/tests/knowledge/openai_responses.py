"""A stand-in for the OpenAI Responses endpoint: records each request, answers with one
message, and so lets tests check what a reader sends without paying for a call. Given a
list of contents, it answers the first request with the first, the next with the next."""

import json

import httpx2
from openai import AsyncOpenAI


def openai_answering(content, requests, **response):
    contents = content if isinstance(content, list) else [content]
    answered = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        answered.append(request)
        reply = contents[min(len(answered), len(contents)) - 1]
        return httpx2.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 0,
                "model": "gpt-test-2026-01-01",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [reply],
                    }
                ],
                "usage": {
                    "input_tokens": 420,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 31,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 451,
                },
                **response,
            },
        )

    return AsyncOpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
