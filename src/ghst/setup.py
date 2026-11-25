"""Interactive provider and model setup behind `ghst provider`, `ghst model` and `ghst login`."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from ghst.config import GhstConfig, ProviderConfig, profile_id
from ghst.picker import Option, pick

LOCAL_DEFAULT_URL = "http://localhost:8000/v1"  # oMLX's default; Ollama is :11434/v1
OMLX_SETTINGS = Path.home() / ".omlx" / "settings.json"

PROVIDERS: list[tuple[str, Option]] = [
    ("local", Option("Local server", "oMLX, Ollama, LM Studio: any OpenAI-compatible server")),
    ("chatgpt", Option("ChatGPT plan", "sign in with ChatGPT and use your Plus or Pro allowance")),
    ("openai", Option("OpenAI API", "API key from platform.openai.com, billed per token")),
    ("anthropic", Option("Anthropic API", "API key from console.anthropic.com, billed per token")),
]
PROVIDER_LABELS = {pid: option.label for pid, option in PROVIDERS}

# Words that mark small, fast models; preferred for ghost text
_FAST_WORDS = ("nano", "mini", "haiku", "flash", "spark", "small", "1.5b", "3b", "7b", "8b")
# OpenAI's /models also lists embedding, audio and image models that can't complete text
_OPENAI_CHAT = re.compile(r"^(gpt-|o\d|chatgpt-)")
_OPENAI_NOT_CHAT = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding")


# ── Prompts ──────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{shown}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_key(prompt: str, saved: str, optional: bool = False) -> str:
    """Ask for an API key without echoing it. Enter keeps the saved one."""
    if os.environ.get("GHST_API_KEY"):
        print("  Using the API key from GHST_API_KEY.")
        return os.environ["GHST_API_KEY"]
    if saved:
        hint = f" [saved …{saved[-4:]}, Enter keeps it]"
    elif optional:
        hint = " [Enter if the server has none]"
    else:
        hint = ""
    try:
        answer = getpass.getpass(f"  {prompt}{hint}: ").strip()
    except EOFError:
        answer = ""
    return re.sub(r"[\x00-\x1f\x7f]", "", answer) or saved


def confirm(prompt: str, default: bool = True) -> bool:
    answer = ask(f"{prompt} {'[Y/n]' if default else '[y/N]'}").lower()
    return default if not answer else answer.startswith("y")


def omlx_api_key() -> str:
    """oMLX's own API key, so connecting to it needs no copy and paste."""
    try:
        return json.loads(OMLX_SETTINGS.read_text())["auth"]["api_key"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""


# ── Server checks ────────────────────────────────────────────────────────────

def _with_client(config: GhstConfig, use: Any) -> Any:
    """Run `use(client)` on a fresh LLM client and close it afterwards."""
    from ghst.llm import LLMClient

    async def run() -> Any:
        client = LLMClient(config)
        try:
            return await use(client)
        finally:
            await client.close()

    return asyncio.run(run())


def probe(config: GhstConfig) -> dict[str, Any]:
    """Check the server from the CLI: reachable, key accepted, which models."""
    return _with_client(config, lambda client: client.probe())


def try_ghost_text(config: GhstConfig, buffer: str = "git pu") -> tuple[str, float, str]:
    """One real ghost-text request. Returns (suggestion, milliseconds, error)."""
    from ghst.prompts import autocomplete_system, autocomplete_user

    messages = [
        {"role": "system", "content": autocomplete_system()},
        {"role": "user", "content": autocomplete_user(
            buffer=buffer, cwd=os.getcwd(), history=["git status", "git commit -m 'wip'"],
        )},
    ]

    async def complete(client: Any) -> tuple[str, float, str]:
        start = time.monotonic()
        text = await client.complete(
            messages, model=config.provider.effective_autocomplete_model, single_line=True,
        )
        return text, (time.monotonic() - start) * 1000, client.last_error

    return _with_client(config, complete)


def usable_models(pid: str, models: list[str]) -> list[str]:
    if pid != "openai":
        return models
    return sorted(
        m for m in models
        if _OPENAI_CHAT.match(m) and not any(word in m for word in _OPENAI_NOT_CHAT)
    )


def _default_index(models: list[str], current: str, prefer_fast: bool) -> int:
    if current in models:
        return models.index(current)
    if prefer_fast:
        for i, model in enumerate(models):
            if any(word in model.lower() for word in _FAST_WORDS):
                return i
    return 0


# ── Flows ────────────────────────────────────────────────────────────────────

def choose_models(config: GhstConfig) -> bool:
    """Pick the ghost-text and Cmd+I models from the server's list."""
    provider = config.provider
    pid = profile_id(provider)
    print("  Checking the server…")
    health = probe(config)
    if not health.get("ok"):
        print(f"  ✗ {health.get('url', '')}: {health.get('error', 'not reachable')}")
        if not confirm("Type the model names yourself?", default=False):
            return False
        provider.autocomplete_model = ask("Ghost-text model", provider.effective_autocomplete_model)
        provider.model = ask("Cmd+I model", provider.model)
        return True

    models = usable_models(pid, health.get("models", []))
    if not models:
        print("  The server listed no models; type the names instead.")
        provider.autocomplete_model = ask("Ghost-text model", provider.effective_autocomplete_model)
        provider.model = ask("Cmd+I model", provider.model)
        return True

    def options(current: str) -> list[Option]:
        return [Option(m, "current" if m == current else "") for m in models]

    current_ac = provider.effective_autocomplete_model
    ac = pick(
        "Ghost-text model (fast matters most):", options(current_ac),
        _default_index(models, current_ac, prefer_fast=True),
    )
    if ac is None:
        return False
    nl = pick(
        "Cmd+I model (plain English to command):", options(provider.model),
        _default_index(models, provider.model, prefer_fast=False),
    )
    if nl is None:
        return False
    provider.autocomplete_model, provider.model = models[ac], models[nl]
    return True


def _provider_for(config: GhstConfig, pid: str) -> ProviderConfig:
    """Saved settings for a provider, else sensible defaults."""
    if pid in config.profiles:
        return replace(config.profiles[pid])
    if profile_id(config.provider) == pid:
        return replace(config.provider)
    defaults = {
        "local": ProviderConfig(name="openai", api_base_url=LOCAL_DEFAULT_URL, model=""),
        "openai": ProviderConfig(name="openai", model="gpt-4o", autocomplete_model="gpt-4o-mini"),
        "anthropic": ProviderConfig(
            name="anthropic", model="claude-sonnet-4-5", autocomplete_model="claude-haiku-4-5",
        ),
        "chatgpt": ProviderConfig(name="chatgpt", model=""),
    }
    return defaults[pid]


def _configure_access(provider: ProviderConfig, pid: str) -> bool:
    """Ask for what this provider needs to connect. False if the user gave up."""
    if pid == "local":
        provider.name = "openai"
        provider.api_base_url = ask("Server URL", provider.api_base_url or LOCAL_DEFAULT_URL)
        omlx_key = omlx_api_key()
        is_omlx = ":8000" in provider.api_base_url
        if not provider.api_key and omlx_key and is_omlx and confirm(
            "Use the API key from your oMLX settings?"
        ):
            provider.api_key = omlx_key
            return True
        provider.api_key = ask_key("API key", provider.api_key, optional=True)
    elif pid in ("openai", "anthropic"):
        provider.name = pid
        provider.api_base_url = ""
        provider.api_key = ask_key(f"{PROVIDER_LABELS[pid]} key", provider.api_key)
        if not provider.api_key:
            print("  ✗ An API key is needed.")
            return False
    elif pid == "chatgpt":
        from ghst import chatgpt

        provider.name = "chatgpt"
        record = chatgpt.active_record(chatgpt.load_store())
        if chatgpt.plan_enabled(record):
            print(f"  Signed in to ChatGPT as {record.get('email') if record else ''}")
        elif not sign_in_chatgpt():
            return False
    return True


def sign_in_chatgpt(new_account: bool = False, open_browser: bool = True) -> bool:
    from ghst import chatgpt

    try:
        record = chatgpt.login(new_account=new_account, open_browser=open_browser)
    except chatgpt.ChatGPTError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return False
    print(f"  ✓ Signed in to ChatGPT as {record.get('email') or record['subject']}")
    if not chatgpt.plan_enabled(record):
        print(
            "  ✗ ChatGPT plan usage isn't enabled for this sign-in, so ghst can't use it.\n"
            "    It needs an eligible plan (Plus or Pro) and permission to use it in ghst.",
            file=sys.stderr,
        )
        return False
    if chatgpt.mark_welcomed():
        print(
            "\n  You're using your ChatGPT plan\n"
            "  Eligible usage in ghst uses your ChatGPT plan. Manage usage in your\n"
            f"  ChatGPT settings: {chatgpt.USAGE_URL}\n"
        )
        if sys.stdin.isatty():
            input("  Press Enter to continue (Got it) ")
    return True


def setup_provider(config: GhstConfig, pid: str | None = None) -> bool:
    """Choose a provider, connect to it, pick models, and save. True when saved."""
    current = profile_id(config.provider)
    ids = [p for p, _ in PROVIDERS]
    if pid is None:
        options = [
            Option(o.label, f"{o.hint}  (current)" if p == current else o.hint)
            for p, o in PROVIDERS
        ]
        choice = pick("Provider:", options, ids.index(current))
        if choice is None:
            return False
        pid = ids[choice]

    provider = _provider_for(config, pid)
    if not _configure_access(provider, pid):
        return False
    previous = config.provider
    config.provider = provider
    if not choose_models(config):
        config.provider = previous
        return False
    config.save_profile()
    config.write_toml()
    report(config)
    return True


def report(config: GhstConfig) -> None:
    """Summarise the setup and time one real ghost-text request."""
    provider = config.provider
    pid = profile_id(provider)
    print(f"\n  ✓ {PROVIDER_LABELS[pid]}", end="")
    print(f" ({provider.api_base_url})" if pid == "local" else "")
    from ghst.config import resolve_autocomplete_mode

    mode = "fill-in-the-middle" if resolve_autocomplete_mode(config) == "fim" else "chat"
    print(f"    ghost text: {provider.effective_autocomplete_model}  ({mode})")
    print(f"    Cmd+I:      {provider.model}")
    if pid == "chatgpt":
        from ghst.chatgpt import USAGE_URL

        print(f"    billing:    Using ChatGPT plan (Manage usage: {USAGE_URL})")
    suggestion, ms, error = try_ghost_text(config)
    if not suggestion and "Timeout" in error:
        # The first request after switching models can be slow while it loads
        print("    test:       waiting for the model to load…")
        suggestion, ms, error = try_ghost_text(config)
    if suggestion:
        print(f"    test:       git pu → git pu{suggestion}  ({ms:.0f}ms)")
    elif "Timeout" in error:
        limit = config.ui.autocomplete_timeout_ms
        print(f"    test:       no reply within {limit}ms; the model may still be loading")
    else:
        print(f"    test:       no suggestion ({error or 'empty reply'})")
