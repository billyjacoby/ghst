"""Tests for Sign in with ChatGPT (ghst.chatgpt)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ghst import chatgpt


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def rsa_key(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    key = tmp_path_factory.mktemp("key") / "key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    modulus = subprocess.run(
        ["openssl", "rsa", "-in", str(key), "-noout", "-modulus"],
        check=True, capture_output=True, text=True,
    ).stdout.strip().split("=", 1)[1]
    n = bytes.fromhex(modulus)
    return {"path": key, "jwk": {"kty": "RSA", "kid": "k1", "n": b64url(n), "e": b64url(b"\1\0\1")}}


def make_id_token(rsa_key: dict[str, Any], claims: dict[str, Any], kid: str = "k1") -> str:
    header = b64url(json.dumps({"alg": "RS256", "kid": kid}).encode())
    payload = b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(rsa_key["path"])],
        input=signing_input, check=True, capture_output=True,
    ).stdout
    return f"{header}.{payload}.{b64url(signature)}"


def claims(**overrides: Any) -> dict[str, Any]:
    base = {
        "iss": chatgpt.AUTH_ISSUER, "aud": "oaiapp_123", "sub": "user-1",
        "email": "me@example.com", "nonce": "n0", "exp": time.time() + 600,
    }
    return {**base, **overrides}


class TestPkceAndUrl:
    def test_challenge_is_s256_of_verifier(self) -> None:
        verifier, challenge = chatgpt.pkce_pair()
        assert 43 <= len(verifier) <= 128
        assert challenge == b64url(hashlib.sha256(verifier.encode()).digest())

    def test_first_sign_in_registers(self) -> None:
        url = chatgpt.authorize_url(
            client_id=chatgpt.REGISTER_CLIENT_ID, host="urn:uuid:x", redirect_uri="http://r",
            state="s", nonce="n", code_challenge="c",
        )
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert url.startswith(chatgpt.AUTHORIZE_URL)
        assert params["client_id"] == "dynamic_agent_client"
        assert params["agent_name_hint"] == "ghst"
        assert params["resource"] == "https://api.openai.com/v1"
        assert "chatgpt.tokens.use.direct" in params["scope"].split()
        assert params["code_challenge_method"] == "S256"
        assert "id_token_hint" not in params

    def test_reauthorization_omits_name_hint(self) -> None:
        url = chatgpt.authorize_url(
            client_id="oaiapp_123", host="urn:uuid:x", redirect_uri="http://r",
            state="s", nonce="n", code_challenge="c", id_token_hint="tok", login_hint="a@b",
        )
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert params["client_id"] == "oaiapp_123"
        assert "agent_name_hint" not in params
        assert params["id_token_hint"] == "tok"


class TestIdToken:
    def test_valid_token(self, rsa_key: dict[str, Any]) -> None:
        token = make_id_token(rsa_key, claims())
        result = chatgpt.verify_id_token(token, "oaiapp_123", "n0", {"keys": [rsa_key["jwk"]]})
        assert result["sub"] == "user-1"

    @pytest.mark.parametrize(("change", "message"), [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"aud": "oaiapp_other"}, "different client"),
        ({"exp": time.time() - 1}, "expired"),
        ({"nonce": "other"}, "nonce"),
    ])
    def test_rejected_claims(
        self, rsa_key: dict[str, Any], change: dict[str, Any], message: str,
    ) -> None:
        token = make_id_token(rsa_key, claims(**change))
        with pytest.raises(chatgpt.ChatGPTError, match=message):
            chatgpt.verify_id_token(token, "oaiapp_123", "n0", {"keys": [rsa_key["jwk"]]})

    def test_tampered_payload(self, rsa_key: dict[str, Any]) -> None:
        header, _, sig = make_id_token(rsa_key, claims()).split(".")
        forged = b64url(json.dumps(claims(sub="someone-else")).encode())
        with pytest.raises(chatgpt.ChatGPTError, match="signature"):
            chatgpt.verify_id_token(
                f"{header}.{forged}.{sig}", "oaiapp_123", "n0", {"keys": [rsa_key["jwk"]]}
            )

    def test_unknown_key(self, rsa_key: dict[str, Any]) -> None:
        token = make_id_token(rsa_key, claims(), kid="other")
        with pytest.raises(chatgpt.ChatGPTError, match="unknown key"):
            chatgpt.verify_id_token(token, "oaiapp_123", "n0", {"keys": [rsa_key["jwk"]]})


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events
    )


def delta(text: str) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "delta": text}


async def run_stream(handler: Any, **kwargs: Any) -> str:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await chatgpt.stream_response(
            client, "tok", {"model": "m"}, httpx.Timeout(5.0), **kwargs
        )


class TestStreaming:
    async def test_collects_text_until_completed(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            seen["url"] = str(request.url)
            body = sse(delta("sh orig"), delta("in main"), {"type": "response.completed"})
            return httpx.Response(200, content=body)

        assert await run_stream(handler) == "sh origin main"
        assert seen == {"auth": "Bearer tok", "url": "https://api.openai.com/v1/responses"}

    async def test_single_line_stops_at_newline(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=sse(delta("tall"), delta(" jq\nmore"), delta("x")))

        assert await run_stream(handler, single_line=True) == "tall jq"

    async def test_single_line_stops_at_max_chars(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=sse(delta("abcdefgh"), delta("ijk")))

        assert await run_stream(handler, single_line=True, max_chars=5) == "abcde"

    async def test_usage_limit_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}
            return httpx.Response(429, json=body)

        with pytest.raises(chatgpt.ChatGPTError, match="usage limit") as info:
            await run_stream(handler)
        assert info.value.status == 429
        assert chatgpt.USAGE_URL in str(info.value)

    async def test_failed_and_interrupted_streams(self) -> None:
        def failed(request: httpx.Request) -> httpx.Response:
            event = {"type": "response.failed", "response": {"error": {"message": "boom"}}}
            return httpx.Response(200, content=sse(delta("x"), event))

        def interrupted(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=sse(delta("x")))

        with pytest.raises(chatgpt.ChatGPTError, match="boom"):
            await run_stream(failed)
        with pytest.raises(chatgpt.ChatGPTError, match="ended before"):
            await run_stream(interrupted)


class TestRequestShape:
    def test_system_becomes_instructions(self) -> None:
        body = chatgpt.responses_body(
            [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}], "m",
        )
        assert body == {
            "model": "m", "instructions": "be terse",
            "input": [{"role": "user", "content": "hi"}], "store": False, "stream": True,
        }

    def test_model_list(self) -> None:
        payload = {"models": [
            {"slug": "a", "visibility": "list"}, {"slug": "hidden", "visibility": "hide"},
            {"slug": "b"}, "junk",
        ]}
        assert chatgpt.list_models_from(payload) == ["a", "b"]


def saved_record(path: Path, **overrides: Any) -> dict[str, Any]:
    record = {
        "client_id": "oaiapp_123", "subject": "user-1", "email": "me@example.com",
        "access_token": "old-access", "refresh_token": "old-refresh",
        "scopes": chatgpt.SCOPES.split(), "expires_in": 3600,
        "saved_at": datetime.now(timezone.utc).isoformat(), **overrides,
    }
    chatgpt.save_store({"active_client_id": "oaiapp_123", "registrations": [record]}, path)
    return record


class TestSession:
    async def test_fresh_token_is_reused(self, tmp_path: Path) -> None:
        path = tmp_path / "chatgpt.json"
        saved_record(path)
        session = chatgpt.ChatGPTSession(path)

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not refresh")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await session.access_token(client) == "old-access"

    async def test_expired_token_refreshes_and_rotates(self, tmp_path: Path) -> None:
        path = tmp_path / "chatgpt.json"
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        saved_record(path, saved_at=stale)
        sent: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200, json={
                "access_token": "new-access", "refresh_token": "new-refresh",
                "expires_in": 3600, "scope": chatgpt.SCOPES,
            })

        session = chatgpt.ChatGPTSession(path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await session.access_token(client) == "new-access"
        assert sent == {
            "grant_type": "refresh_token", "client_id": "oaiapp_123",
            "refresh_token": "old-refresh", "resource": "https://api.openai.com/v1",
        }
        stored = chatgpt.active_record(chatgpt.load_store(path))
        assert stored is not None
        assert stored["refresh_token"] == "new-refresh"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    async def test_terminal_refresh_error_asks_for_login(self, tmp_path: Path) -> None:
        path = tmp_path / "chatgpt.json"
        saved_record(path, saved_at="2000-01-01T00:00:00+00:00")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "refresh_token_reused"})

        session = chatgpt.ChatGPTSession(path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(chatgpt.ChatGPTError, match="ghst login"):
                await session.access_token(client)

    async def test_no_plan_scope(self, tmp_path: Path) -> None:
        path = tmp_path / "chatgpt.json"
        saved_record(path, scopes=["openid", "email"])
        session = chatgpt.ChatGPTSession(path)
        async with httpx.AsyncClient() as client:
            with pytest.raises(chatgpt.ChatGPTError, match="isn't enabled"):
                await session.access_token(client)


class TestLoginFlow:
    """Runs `login()` end to end against a fake OpenAI and a fake browser."""

    def test_register_then_sign_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rsa_key: dict[str, Any],
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        token_requests: list[dict[str, str]] = []

        def browser(url: str) -> bool:
            params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            browser.params = params  # type: ignore[attr-defined]
            callback = (
                f"{params['redirect_uri']}?code=abc&state={params['state']}"
                f"&client_id=oaiapp_123&scope=openid"
            )
            threading.Timer(0.2, lambda: httpx.get(callback)).start()
            return True

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/jwks.json"):
                return httpx.Response(200, json={"keys": [rsa_key["jwk"]]})
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            token_requests.append(form)
            if request.url.path.endswith("/revoke"):
                return httpx.Response(200)
            nonce = browser.params["nonce"]  # type: ignore[attr-defined]
            return httpx.Response(200, json={
                "access_token": "acc", "refresh_token": "ref", "expires_in": 3600,
                "scope": chatgpt.SCOPES, "token_type": "Bearer",
                "id_token": make_id_token(rsa_key, claims(nonce=nonce)),
            })

        real_client = httpx.Client
        monkeypatch.setattr(chatgpt.webbrowser, "open", browser)
        monkeypatch.setattr(
            chatgpt.httpx, "Client",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        )

        record = chatgpt.login()
        assert record["email"] == "me@example.com"
        assert chatgpt.plan_enabled(record)
        assert browser.params["client_id"] == "dynamic_agent_client"  # type: ignore[attr-defined]
        exchange = token_requests[0]
        assert exchange["grant_type"] == "authorization_code"
        assert exchange["client_id"] == "oaiapp_123"
        assert exchange["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
        challenge = browser.params["code_challenge"]  # type: ignore[attr-defined]
        assert challenge == b64url(hashlib.sha256(exchange["code_verifier"].encode()).digest())

        path = chatgpt.credentials_path()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        store = chatgpt.load_store(path)
        assert store["ext_agent_host_id"].startswith("urn:uuid:")
        assert store["active_client_id"] == "oaiapp_123"

        record, revoked = chatgpt.logout()
        assert revoked
        assert token_requests[-1] == {
            "token": "ref", "token_type_hint": "refresh_token", "client_id": "oaiapp_123",
        }
        kept = chatgpt.active_record(chatgpt.load_store(path))
        assert kept is not None
        assert kept["client_id"] == "oaiapp_123"  # registration kept for next sign-in
        assert "refresh_token" not in kept


async def test_llm_client_refreshes_once_on_401(tmp_path: Path) -> None:
    from ghst.config import GhstConfig, ProviderConfig
    from ghst.llm import LLMClient

    path = tmp_path / "chatgpt.json"
    saved_record(path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            calls.append("refresh")
            return httpx.Response(200, json={
                "access_token": "new-access", "refresh_token": "r2", "expires_in": 3600,
            })
        calls.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old-access":
            error = {"code": "subscription_sharing_invalid_user"}
            return httpx.Response(401, json={"error": error})
        body = json.loads(request.content)
        assert body["instructions"] == "sys" and body["stream"] is True
        return httpx.Response(200, content=sse(delta("sh"), {"type": "response.completed"}))

    config = GhstConfig(provider=ProviderConfig(name="chatgpt", model="m"))
    llm = LLMClient(config)
    llm.chatgpt = chatgpt.ChatGPTSession(path)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "git pu"}]
    assert await llm.complete(messages, single_line=True) == "sh"
    assert calls == ["Bearer old-access", "refresh", "Bearer new-access"]
    await llm.close()
