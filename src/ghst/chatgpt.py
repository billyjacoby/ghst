"""Sign in with ChatGPT: run ghst's model requests on the user's ChatGPT plan.

Implements the open-source "ChatGPT plan usage" flow documented at
https://developers.openai.com/siwc/token-sharing-open-source — a loopback OAuth
sign-in with PKCE, rotating refresh tokens, and inference on the Responses API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ghst.config import _config_dir

AUTH_ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{AUTH_ISSUER}/api/accounts/oauth/token"
REVOKE_URL = f"{AUTH_ISSUER}/api/accounts/oauth/revoke"
JWKS_URL = f"{AUTH_ISSUER}/.well-known/jwks.json"
API_BASE = "https://api.openai.com/v1"
USAGE_URL = "https://chatgpt.com/settings/usage"

APP_NAME = "ghst"
REGISTER_CLIENT_ID = "dynamic_agent_client"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
REDIRECT_HOST = "127.0.0.1"  # the docs require 127.0.0.1, not localhost
REDIRECT_PORT = 1455
REDIRECT_PATH = "/auth/callback"
LOGIN_TIMEOUT_SECONDS = 300
REFRESH_MARGIN_SECONDS = 120

# Refresh errors after which only a new sign-in helps
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
}

# DER prefix of an RS256 (SHA-256) DigestInfo, for PKCS#1 v1.5 verification
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class ChatGPTError(Exception):
    """A sign-in or plan-usage failure with a message fit for the user."""

    def __init__(self, message: str, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ── Credential file ──────────────────────────────────────────────────────────

def credentials_path() -> Path:
    return _config_dir() / "chatgpt.json"


def load_store(path: Path | None = None) -> dict[str, Any]:
    path = path or credentials_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_store(store: dict[str, Any], path: Path | None = None) -> None:
    """Write the credential file atomically, readable only by the user."""
    path = path or credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, path)


def active_record(store: dict[str, Any]) -> dict[str, Any] | None:
    client_id = store.get("active_client_id")
    for record in store.get("registrations", []):
        if record.get("client_id") == client_id:
            return record
    return None


def _upsert_record(store: dict[str, Any], record: dict[str, Any]) -> None:
    records = [r for r in store.get("registrations", []) if r["client_id"] != record["client_id"]]
    store["registrations"] = [*records, record]


def host_id(store: dict[str, Any]) -> str:
    """Stable, opaque per-install ID that OpenAI requires on every sign-in."""
    if not store.get("ext_agent_host_id"):
        store["ext_agent_host_id"] = f"urn:uuid:{uuid.uuid4()}"
    return store["ext_agent_host_id"]


def plan_enabled(record: dict[str, Any] | None) -> bool:
    return bool(record and record.get("access_token") and PLAN_SCOPE in record.get("scopes", []))


def _apply_token_response(record: dict[str, Any], tokens: dict[str, Any]) -> None:
    """Replace access token, refresh token, expiry and scopes together."""
    record["access_token"] = tokens["access_token"]
    record["refresh_token"] = tokens.get("refresh_token", record.get("refresh_token", ""))
    record["token_type"] = tokens.get("token_type", "Bearer")
    record["expires_in"] = int(tokens.get("expires_in", 3600))
    if "scope" in tokens:
        record["scopes"] = tokens["scope"].split()
    if tokens.get("id_token"):
        record["id_token"] = tokens["id_token"]
    record["saved_at"] = datetime.now(timezone.utc).isoformat()


def _expires_at(record: dict[str, Any]) -> float:
    try:
        saved = datetime.fromisoformat(record["saved_at"]).timestamp()
    except (KeyError, ValueError):
        return 0.0
    return saved + int(record.get("expires_in", 0))


# ── ID token verification (RS256 against OpenAI's JWKS) ──────────────────────

def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _b64url_int(data: str) -> int:
    return int.from_bytes(_b64url_decode(data), "big")


def rs256_verify(signing_input: bytes, signature: bytes, n: int, e: int) -> bool:
    """PKCS#1 v1.5 RS256 signature check with integer math (no crypto library)."""
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    digest = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    return secrets.compare_digest(decoded, expected)


def verify_id_token(
    id_token: str, client_id: str, nonce: str, jwks: dict[str, Any], now: float | None = None,
) -> dict[str, Any]:
    """Verify signature, issuer, audience, expiry and nonce; return the claims."""
    try:
        header_b64, payload_b64, sig_b64 = id_token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except ValueError as e:
        raise ChatGPTError("OpenAI returned a malformed ID token") from e

    if header.get("alg") != "RS256":
        raise ChatGPTError(f"unexpected ID token algorithm {header.get('alg')!r}")
    keys = [k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")]
    if not keys or keys[0].get("kty") != "RSA":
        raise ChatGPTError("ID token is signed with an unknown key")
    key = keys[0]
    signing_input = f"{header_b64}.{payload_b64}".encode()
    if not rs256_verify(signing_input, signature, _b64url_int(key["n"]), _b64url_int(key["e"])):
        raise ChatGPTError("ID token signature is invalid")

    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") != AUTH_ISSUER:
        raise ChatGPTError("ID token has the wrong issuer")
    if client_id not in audiences:
        raise ChatGPTError("ID token is for a different client")
    if float(claims.get("exp", 0)) <= (now or time.time()):
        raise ChatGPTError("ID token has expired")
    if claims.get("nonce") != nonce:
        raise ChatGPTError("ID token nonce does not match this sign-in")
    if not claims.get("sub"):
        raise ChatGPTError("ID token has no subject")
    return claims


# ── Sign-in (CLI, synchronous) ───────────────────────────────────────────────

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def authorize_url(
    *, client_id: str, host: str, redirect_uri: str, state: str, nonce: str,
    code_challenge: str, id_token_hint: str = "", login_hint: str = "",
) -> str:
    params = {
        "client_id": client_id,
        "ext_agent_host_id": host,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": API_BASE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    if client_id == REGISTER_CLIENT_ID:
        params["agent_name_hint"] = APP_NAME
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _wait_for_callback(server: HTTPServer, timeout: float) -> dict[str, str]:
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server API
            parsed = urlparse(self.path)
            if parsed.path != REDIRECT_PATH:
                self.send_error(404)
                return
            result.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
            body = (
                b"<!doctype html><meta charset=utf-8><title>ghst</title>"
                b"<p style='font:16px system-ui;margin:3em'>"
                b"Signed in. You can close this tab and go back to your terminal.</p>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # the default logs the callback URL, code included

    server.RequestHandlerClass = Handler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise ChatGPTError("sign-in timed out; run `ghst login` again")
    finally:
        server.shutdown()
        server.server_close()
    return result


def _token_request(client: httpx.Client, data: dict[str, str]) -> dict[str, Any]:
    response = client.post(TOKEN_URL, data=data, timeout=20)
    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        code = str(payload.get("error", f"HTTP {response.status_code}"))
        detail = payload.get("error_description") or code
        raise ChatGPTError(f"OpenAI rejected the token request: {detail}", code=code)
    return payload


def login(*, new_account: bool = False, open_browser: bool = True) -> dict[str, Any]:
    """Run the browser sign-in and save the credentials. Returns the record."""
    store = load_store()
    host = host_id(store)
    previous = None if new_account else active_record(store)
    client_id = previous["client_id"] if previous else REGISTER_CLIENT_ID

    try:
        server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), BaseHTTPRequestHandler)
    except OSError as e:
        raise ChatGPTError(
            f"port {REDIRECT_PORT} is busy (another sign-in may be running): {e.strerror}"
        ) from e
    redirect_uri = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"

    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    url = authorize_url(
        client_id=client_id, host=host, redirect_uri=redirect_uri, state=state, nonce=nonce,
        code_challenge=challenge,
        id_token_hint=previous.get("id_token", "") if previous else "",
        login_hint=previous.get("email", "") if previous else "",
    )

    print("  Continue with ChatGPT in your browser…")
    if not (open_browser and webbrowser.open(url)):
        print(f"\n  Open this URL to sign in:\n\n  {url}\n")
    callback = _wait_for_callback(server, LOGIN_TIMEOUT_SECONDS)

    if not secrets.compare_digest(callback.get("state", ""), state):
        raise ChatGPTError("sign-in response did not match this attempt (state mismatch)")
    if callback.get("error"):
        if callback["error"] == "access_denied":
            raise ChatGPTError("sign-in was cancelled")
        detail = callback.get("error_description", callback["error"])
        raise ChatGPTError(f"sign-in failed: {detail}")
    code = callback.get("code")
    if not code:
        raise ChatGPTError("sign-in response had no authorization code")

    returned_id = callback.get("client_id", "")
    if previous:
        if returned_id and returned_id != client_id:
            raise ChatGPTError(
                "you picked a different ChatGPT account; run `ghst login --new-account` for that"
            )
        issued_id = client_id
    else:
        if not returned_id:
            raise ChatGPTError("OpenAI did not return a client ID; registration is incomplete")
        issued_id = returned_id
        # Keep the registration even if the exchange below fails, so a retry
        # doesn't register ghst a second time
        _upsert_record(store, {"client_id": issued_id, "ext_agent_host_id": host})
        save_store(store)

    with httpx.Client() as client:
        tokens = _token_request(client, {
            "grant_type": "authorization_code",
            "client_id": issued_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": API_BASE,
        })
        jwks = client.get(JWKS_URL, timeout=20).json()
    claims = verify_id_token(tokens.get("id_token", ""), issued_id, nonce, jwks)

    if previous and previous.get("subject") and previous["subject"] != claims["sub"]:
        raise ChatGPTError(
            "you picked a different ChatGPT account; run `ghst login --new-account` for that"
        )
    record = previous or next(r for r in store["registrations"] if r["client_id"] == issued_id)
    record.update({
        "email": claims.get("email", ""),
        "issuer": claims["iss"],
        "subject": claims["sub"],
        "client_id": issued_id,
        "ext_agent_host_id": host,
    })
    _apply_token_response(record, tokens)
    _upsert_record(store, record)
    store["active_client_id"] = issued_id
    save_store(store)
    return record


def logout() -> tuple[dict[str, Any] | None, bool]:
    """Revoke and forget the tokens, keeping the registration for next time.

    Returns the record and whether OpenAI confirmed the revocation.
    """
    store = load_store()
    record = active_record(store)
    if not record or not record.get("refresh_token"):
        return record, True
    revoked = False
    try:
        with httpx.Client() as client:
            response = client.post(REVOKE_URL, timeout=20, data={
                "token": record["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": record["client_id"],
            })
        revoked = response.status_code == 200
    except httpx.HTTPError:
        pass
    for key in ("access_token", "refresh_token", "scopes", "saved_at", "expires_in"):
        record.pop(key, None)
    save_store(store)
    return record, revoked


def mark_welcomed() -> bool:
    """Record that the first-sign-in notice was shown. True if it is new."""
    store = load_store()
    record = active_record(store)
    if not record or record.get("welcomed"):
        return False
    record["welcomed"] = True
    save_store(store)
    return True


# ── Daemon side: tokens and inference ────────────────────────────────────────

class ChatGPTSession:
    """Keeps a fresh access token, re-reading the file when `ghst login` changes it."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or credentials_path()
        self._lock = asyncio.Lock()
        self._store: dict[str, Any] = {}
        self._mtime = -1.0

    def _reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._store, self._mtime = {}, -1.0
            return
        if mtime != self._mtime:
            self._store, self._mtime = load_store(self.path), mtime

    def record(self) -> dict[str, Any] | None:
        self._reload()
        return active_record(self._store)

    async def access_token(self, client: httpx.AsyncClient, force_refresh: bool = False) -> str:
        async with self._lock:  # refresh tokens rotate: one refresh at a time
            record = self.record()
            if not record or not record.get("refresh_token"):
                raise ChatGPTError("not signed in to ChatGPT; run `ghst login`", "not_signed_in")
            if not plan_enabled(record):
                raise ChatGPTError(
                    "ChatGPT plan usage isn't enabled for this sign-in", "plan_not_enabled"
                )
            if force_refresh or time.time() >= _expires_at(record) - REFRESH_MARGIN_SECONDS:
                await self._refresh(client, record)
            return record["access_token"]

    async def _refresh(self, client: httpx.AsyncClient, record: dict[str, Any]) -> None:
        response = await client.post(TOKEN_URL, timeout=20, data={
            "grant_type": "refresh_token",
            "client_id": record["client_id"],
            "refresh_token": record["refresh_token"],
            "resource": API_BASE,
        })
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            code = str(payload.get("error", f"HTTP {response.status_code}"))
            if code in TERMINAL_REFRESH_ERRORS:
                raise ChatGPTError("ChatGPT sign-in expired; run `ghst login`", code)
            raise ChatGPTError(f"could not refresh the ChatGPT sign-in ({code})", code)
        _apply_token_response(record, payload)
        save_store(self._store, self.path)
        self._mtime = self.path.stat().st_mtime


def plan_error(status: int, body: bytes) -> ChatGPTError:
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code", "") if isinstance(error, dict) else ""
    if code == "subscription_sharing_usage_limit_exceeded":
        return ChatGPTError(
            f"ChatGPT usage limit reached; manage usage at {USAGE_URL}", code, status
        )
    if code == "subscription_sharing_user_not_eligible":
        return ChatGPTError("this ChatGPT account can't use its plan in other apps", code, status)
    message = (error.get("message") if isinstance(error, dict) else None) or (
        payload.get("detail") if isinstance(payload, dict) else None
    )
    return ChatGPTError(
        f"ChatGPT request failed (HTTP {status}): {message or code or 'error'}", code, status
    )


async def stream_response(
    client: httpx.AsyncClient,
    token: str,
    body: dict[str, Any],
    timeout: httpx.Timeout,
    single_line: bool = False,
    max_chars: int = 0,
) -> str:
    """POST to the Responses API and collect the text.

    With single_line, stop reading at the first newline (or max_chars): ghost
    text only uses one line, and closing the stream early saves plan usage.
    """
    text = ""
    async with client.stream(
        "POST", f"{API_BASE}/responses", json=body, timeout=timeout,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    ) as response:
        if response.status_code >= 400:
            raise plan_error(response.status_code, await response.aread())
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            kind = event.get("type", "")
            if kind == "response.output_text.delta":
                text += event.get("delta", "")
                line = text.lstrip("\n")
                if single_line and ("\n" in line or (max_chars and len(line) >= max_chars)):
                    return line.split("\n", 1)[0][: max_chars or None]
            elif kind == "response.completed":
                return text
            elif kind in ("response.failed", "error"):
                error = event.get("response", {}).get("error") or event.get("error") or {}
                raise ChatGPTError(f"ChatGPT request failed: {error.get('message', kind)}",
                                   error.get("code", ""))
            elif kind == "response.incomplete":
                raise ChatGPTError("ChatGPT response was cut off", "incomplete")
    raise ChatGPTError("ChatGPT stream ended before the response completed", "interrupted")


def responses_body(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """Map chat messages to a Responses request (system text becomes instructions)."""
    instructions = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": m["role"], "content": m["content"]}
            for m in messages if m["role"] != "system"
        ],
        "store": False,
        "stream": True,
    }
    if instructions:
        body["instructions"] = instructions
    return body


def list_models_from(payload: Any) -> list[str]:
    """Model slugs meant for display, in the server's order."""
    models = payload.get("models", []) if isinstance(payload, dict) else []
    return [
        m["slug"] for m in models
        if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"
    ]
