"""Tests for fill-in-the-middle ghost text on base coder models."""

from __future__ import annotations

import json

import httpx
import pytest

from ghst.config import GhstConfig, ProviderConfig, resolve_autocomplete_mode
from ghst.llm import LLMClient
from ghst.prompts import autocomplete_fim

LOCAL = "http://localhost:8000/v1"


@pytest.mark.parametrize(("model", "base_url", "mode", "expected"), [
    ("Qwen2.5-Coder-3B-4bit", LOCAL, "auto", "fim"),
    ("Qwen2.5-Coder-1.5B-Instruct-4bit", LOCAL, "auto", "chat"),
    ("Qwen3-Coder-30B-A3B-Instruct-8bit", LOCAL, "auto", "chat"),
    ("Qwen2.5-Coder-3B-4bit", "", "auto", "chat"),  # not a local server
    ("anything", LOCAL, "fim", "fim"),
    ("Qwen2.5-Coder-3B-4bit", LOCAL, "chat", "chat"),
])
def test_mode(model: str, base_url: str, mode: str, expected: str) -> None:
    config = GhstConfig(provider=ProviderConfig(
        name="openai", api_base_url=base_url, model=model, autocomplete_model=model,
    ))
    config.ui.autocomplete_mode = mode
    assert resolve_autocomplete_mode(config) == expected


def test_prompt_is_a_transcript_ending_in_the_typed_line() -> None:
    prompt = autocomplete_fim(
        buffer="brew install po", cwd="/tmp", history=["ls", "git status"],
        git_branch="main", history_matches=["brew install poetry", "brew install pnpm"],
    )
    assert prompt.startswith("<|fim_prefix|># zsh on")
    assert prompt.endswith("$ brew install po<|fim_suffix|>\n<|fim_middle|>")
    # older matches first, then recent commands, so the newest are nearest the cursor
    order = [prompt.index(c) for c in ("brew install pnpm", "brew install poetry", "$ ls")]
    assert order == sorted(order)


async def test_fim_request_goes_to_completions() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"text": "etry  "}]})

    config = GhstConfig(provider=ProviderConfig(
        name="openai", api_base_url=LOCAL, api_key="k", model="Qwen2.5-Coder-3B-4bit",
    ))
    llm = LLMClient(config)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = await llm.complete([], single_line=True, fim_prompt="<|fim_prefix|>$ brew install po")
    await llm.close()
    assert text == "etry"
    assert seen["path"] == "/v1/completions"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["prompt"] == "<|fim_prefix|>$ brew install po"
    assert "\n" in body["stop"] and body["temperature"] == 0
