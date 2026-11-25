"""CLI entry point for ghst."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from ghst.config import GhstConfig, resolve_history_source


def _strip_control_chars(s: str) -> str:
    """Remove ANSI escape sequences and control characters from a string."""
    # Strip ANSI escape sequences (e.g. ^[[C from arrow keys)
    s = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', s)
    # Strip any remaining control characters (except common whitespace)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    return s.strip()


def _get_src_dir() -> str:
    """Return the ghst Python source directory for auto-reload detection."""
    return str(Path(__file__).resolve().parent)


def _color_esc(value: str) -> str:
    """Convert a color config value to a terminal escape sequence.

    Accepts a 256-color index (e.g. "243") or hex RGB (e.g. "#767676").
    Returns empty string for invalid/empty input (caller uses default).
    """
    value = value.strip()
    if not value:
        return ""
    if value.startswith("#") and len(value) == 7:
        try:
            r, g, b = int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
            return f"\\e[38;2;{r};{g};{b}m"
        except ValueError:
            return ""
    try:
        n = int(value)
        if 0 <= n <= 255:
            return f"\\e[38;5;{n}m"
    except ValueError:
        pass
    return ""


def _shell_init_zsh(config: GhstConfig) -> None:
    """Output zsh integration code for eval."""
    src_dir = _get_src_dir()
    shell_dir = Path(__file__).resolve().parent / "shell"
    socket_path = config.get_socket_path()
    # Find the bin directory containing the ghst entry point
    bin_dir = str(Path(shutil.which("ghst") or sys.argv[0]).resolve().parent)

    ghost_esc = _color_esc(config.ui.ghost_color)
    ghost_export = f'\nexport __GHST_GHOST_ESC=$\'{ghost_esc}\'' if ghost_esc else ""

    # UI color exports
    color_exports = ""
    for name, attr in (
        ("ACCENT", "accent_color"),
        ("SUCCESS", "success_color"),
        ("WARNING", "warning_color"),
        ("ERROR", "error_color"),
    ):
        esc = _color_esc(getattr(config.ui, attr))
        if esc:
            color_exports += f"\nexport __GHST_{name}_ESC=$'{esc}'"

    def q(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    ui = config.ui
    ui_settings = (
        f"typeset -gi __GHST_DELAY={int(ui.autocomplete_delay_ms)}"
        f" __GHST_DELAY_SHORT={int(ui.autocomplete_delay_short_ms)}"
        f" __GHST_DELAY_THRESHOLD={int(ui.autocomplete_delay_threshold)}"
        f" __GHST_MIN_CHARS={int(ui.autocomplete_min_chars)}\n"
        f"typeset -g __GHST_HISTORY_SOURCE={resolve_history_source(ui.history_source)}"
        f" __GHST_HISTORY_LIMIT={int(ui.history_match_limit)}\n"
        f"typeset -g __GHST_NL_HOTKEY={q(ui.nl_hotkey)}"
        f" __GHST_HISTORY_HOTKEY={q(ui.history_search_hotkey)}"
        f" __GHST_CHEAT_HOTKEY={q(ui.cheat_sheet_hotkey)}"
    )

    print(f'''# ghst — AI-powered shell plugin
# Add to .zshrc: eval "$(ghst shell-init zsh)"

export __GHST_SRC_DIR="{src_dir}"
export __GHST_SOCKET="{socket_path}"{ghost_export}{color_exports}
{ui_settings}

# Ensure ghst CLI is on PATH
[[ ":$PATH:" != *":{bin_dir}:"* ]] && export PATH="{bin_dir}:$PATH"

# Auto-start daemon if not running
if [[ ! -S "$__GHST_SOCKET" ]]; then
    ghst start --quiet &>/dev/null &!
else
    # Probe socket liveness — stale file from crashed daemon?
    zmodload zsh/net/socket 2>/dev/null
    if ! zsocket "$__GHST_SOCKET" 2>/dev/null; then
        rm -f "$__GHST_SOCKET" 2>/dev/null
        ghst start --quiet &>/dev/null &!
    else
        {{ exec {{REPLY}}<&- }} 2>/dev/null  # bare exec 2>... would redirect the shell
    fi
fi
''')

    # Inline shell integration files (avoids runtime source path issues)
    for name in ("ghst.zsh", "autocomplete.zsh", "nl-command.zsh"):
        source_file = shell_dir / name
        if source_file.exists():
            print(f"# ── {name} {'─' * (60 - len(name))}")
            print(source_file.read_text())
        else:
            print(f"# WARNING: {name} not found at {source_file}", file=sys.stderr)


def _cmd_shell_init(args: argparse.Namespace) -> None:
    """Handle `ghst shell-init <shell>`."""
    config = GhstConfig.load()
    shell = args.shell
    if shell != "zsh":
        print(f"ghst: shell '{shell}' is not yet supported (only zsh)", file=sys.stderr)
        sys.exit(1)
    _shell_init_zsh(config)


def _cmd_start(args: argparse.Namespace) -> None:
    """Handle `ghst start`."""
    config = GhstConfig.load()
    pid_path = config.get_pid_path()
    socket_path = config.get_socket_path()

    # Check if already running
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            if not getattr(args, "quiet", False):
                print(f"ghst: daemon already running (pid {pid})")
            return
        except (ProcessLookupError, ValueError):
            # Stale PID file
            pid_path.unlink(missing_ok=True)
            socket_path.unlink(missing_ok=True)

    # Start daemon as a background process
    daemon_cmd = [sys.executable, "-m", "ghst.daemon"]
    proc = subprocess.Popen(
        daemon_cmd,
        stdin=subprocess.DEVNULL,  # never hold on to the terminal
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    pid_path.write_text(str(proc.pid))

    if not getattr(args, "quiet", False):
        print(f"ghst: daemon started (pid {proc.pid})")


def _cmd_stop(args: argparse.Namespace) -> None:
    """Handle `ghst stop`."""
    config = GhstConfig.load()
    pid_path = config.get_pid_path()
    socket_path = config.get_socket_path()

    if not pid_path.exists():
        print("ghst: daemon not running")
        return

    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"ghst: daemon stopped (pid {pid})")
    except ProcessLookupError:
        print("ghst: daemon not running (stale pid file)")
    except ValueError:
        print("ghst: invalid pid file")

    pid_path.unlink(missing_ok=True)
    socket_path.unlink(missing_ok=True)


def _cmd_status(args: argparse.Namespace) -> None:
    """Handle `ghst status`."""
    config = GhstConfig.load()
    pid_path = config.get_pid_path()

    running = False
    pid = None
    uptime_str = ""
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            running = True
            # Calculate uptime from PID file mtime
            mtime = pid_path.stat().st_mtime
            uptime_secs = time.time() - mtime
            if uptime_secs < 60:
                uptime_str = f"{int(uptime_secs)}s"
            elif uptime_secs < 3600:
                uptime_str = f"{int(uptime_secs / 60)}m"
            else:
                uptime_str = f"{uptime_secs / 3600:.1f}h"
        except (ProcessLookupError, ValueError):
            pass

    if running:
        print(f"  daemon:     running (pid {pid}, uptime {uptime_str})")
    else:
        print("  daemon:     not running")

    print(
        f"  provider:   {config.provider.name} "
        f"({config.provider.effective_autocomplete_model})"
    )
    print(f"  config:     {config.config_path}")
    print(f"  socket:     {config.get_socket_path()}")

    health = _query_daemon_health(config) if running else None
    if health is None:
        health = _probe_directly(config)
    _print_health(health, config)


def _query_daemon_health(config: GhstConfig) -> dict | None:
    """Ask the running daemon to check the LLM server."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(config.get_socket_path()))
            sock.sendall(b'{"type":"health"}\n')
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        health = json.loads(data.decode())
    except (OSError, json.JSONDecodeError):
        return None
    return health if health.get("type") == "health" else None


def _probe_directly(config: GhstConfig) -> dict:
    """Check the LLM server from the CLI when the daemon can't."""
    import asyncio

    from ghst.llm import LLMClient

    async def run() -> dict:
        client = LLMClient(config)
        try:
            return await client.probe()
        finally:
            await client.close()

    return asyncio.run(run())


def _print_health(health: dict, config: GhstConfig) -> None:
    """Print whether the LLM server is usable."""
    url = health.get("url", config.provider.effective_api_base_url)
    if config.provider.name == "chatgpt":
        from ghst.chatgpt import USAGE_URL

        account = health.get("account") or "not signed in"
        print(f"  account:    {account}")
        if health.get("plan"):
            print(f"  billing:    Using ChatGPT plan (Manage usage: {USAGE_URL})")
    if health.get("ok"):
        print(f"  server:     ok ({url}, {health.get('latency_ms', '?')}ms)")
    else:
        print(f"  server:     DOWN ({url}: {health.get('error', 'unknown error')})")
    for model in health.get("unlisted_models") or []:
        print(f"  warning:    model '{model}' is not listed by the server")
    circuit = health.get("circuit") or {}
    if circuit.get("state", "closed") != "closed":
        print(
            f"  paused:     after {circuit.get('consecutive_failures', 0)} failed requests;"
            f" retrying in {circuit.get('probe_in_seconds', 0)}s"
        )
    if health.get("last_error"):
        print(f"  last error: {health['last_error']}")


# ── Init wizard ──────────────────────────────────────────────────────────────

def _cmd_init(args: argparse.Namespace) -> None:
    """Handle `ghst init` — interactive setup wizard."""
    print("  ghst — AI-powered shell plugin\n")

    # Step 1: Detect shell and add integration
    shell = os.environ.get("SHELL", "").rsplit("/", 1)[-1] or "zsh"
    print(f"  Detected shell: {shell}")
    if shell != "zsh":
        print("  ⚠ Only zsh is supported currently. Proceeding with zsh.")
        shell = "zsh"

    ghst_bin = shutil.which("ghst") or str(Path(sys.executable).resolve().parent / "ghst")
    init_line = f'eval "$({ghst_bin} shell-init {shell})"'
    rc_path = Path.home() / f".{shell}rc"
    needs_install = True
    needs_update = False
    rc_content = ""
    if rc_path.exists():
        rc_content = rc_path.read_text()
        if init_line in rc_content:
            # Exact line already present — nothing to do
            needs_install = False
        elif "ghst shell-init" in rc_content or "shai shell-init" in rc_content \
                or "aish shell-init" in rc_content:
            # Stale eval line from old path or project name — replace it
            needs_install = False
            needs_update = True

    if needs_update:
        # Replace the stale eval line with the correct one
        lines = rc_content.splitlines(keepends=True)
        new_lines = []
        for line in lines:
            if "shell-init" in line and ("ghst" in line or "shai" in line or "aish" in line):
                new_lines.append(f"{init_line}\n")
            else:
                new_lines.append(line)
        rc_path.write_text("".join(new_lines))
        print(f"\n  ✓ Updated shell integration path in ~/{rc_path.name}")
    elif not needs_install:
        print(f"\n  ✓ Shell integration already in ~/{rc_path.name}")
    else:
        print(f"\n  Add shell integration to ~/{rc_path.name}? [Y/n] ", end="", flush=True)
        add = input().strip()
        if add.lower() != "n":
            with open(rc_path, "a") as f:
                f.write(f"\n# Added by ghst\n{init_line}\n")
            print(f"  ✓ Added to ~/{rc_path.name}")
        else:
            print(f'\n  Add this to your ~/{rc_path.name} manually:\n')
            print(f"    {init_line}")
            print(f"\n  Then restart your shell or run: exec {shell}\n")

    # Step 2: Configure provider
    print("  Choose your LLM provider:")
    print("    1) OpenAI (default)")
    print("    2) Anthropic")
    print("\n  > ", end="", flush=True)
    choice = input().strip()
    provider = "anthropic" if choice == "2" else "openai"

    # API key
    default_models = {
        "openai": ("gpt-4o", "gpt-4o-mini"),
        "anthropic": ("claude-sonnet-4-5", "claude-haiku-4-5"),
    }

    api_key = ""
    env_key = os.environ.get("GHST_API_KEY", "")
    if env_key:
        print("\n  API key found in GHST_API_KEY environment variable ✓")
        api_key = ""  # Don't store in config if env var is set
    else:
        api_key = getpass.getpass("\n  API key: ")
        # Strip any terminal escape sequences or control characters
        api_key = _strip_control_chars(api_key)

    # Models
    default_model, default_autocomplete = default_models[provider]
    print(f"  Model for commands [{default_model}]: ", end="", flush=True)
    model = input().strip() or default_model
    print(f"  Model for autocomplete [{default_autocomplete}]: ", end="", flush=True)
    ac_model = input().strip() or default_autocomplete

    # Write config
    config = GhstConfig()
    config.provider.name = provider
    config.provider.api_key = api_key
    config.provider.model = model
    config.provider.autocomplete_model = ac_model
    config.write_toml()

    print(f"\n  ✓ Config written to {config.config_path}")

    # Step 3: Verify connection
    print("\n  Verifying connection...")

    # Start daemon
    _cmd_start(argparse.Namespace(quiet=True))
    time.sleep(1)  # Give daemon time to start

    # Test connection
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(str(config.get_socket_path()))
        sock.sendall(b'{"type":"reload_config"}\n')
        data = sock.recv(4096)
        sock.close()
        result = json.loads(data.decode())
        if result.get("ok"):
            print("  ✓ Daemon started")
            print(f"  ✓ Connected to {provider} ({ac_model})")
        else:
            print("  ⚠ Daemon started but config reload failed")
    except OSError:
        print("  ⚠ Could not connect to daemon — run 'ghst start' manually")

    print("\n  You're all set! Open a new shell and start typing.\n")
    print("    → Autocomplete appears as ghost text (accept with Tab or →)")
    print("    → Press Ctrl+G for natural language command mode")
    print("    → Press Ctrl+R to search history in plain English")
    print("    → Run `ghst status` to check daemon health")
    print("    → Run `ghst help` for all commands\n")


# ── Config commands ──────────────────────────────────────────────────────────

def _cmd_config(args: argparse.Namespace) -> None:
    """Handle `ghst config` — open config in $EDITOR."""
    config = GhstConfig.load()
    config_path = config.config_path

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # Copy default config
        default = Path(__file__).resolve().parent.parent.parent / "config" / "default.toml"
        if default.exists():
            config_path.write_text(default.read_text())
        else:
            config.write_toml()

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(config_path)])


def _send_reload(config: GhstConfig) -> None:
    """Send reload_config to daemon after config changes."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(str(config.get_socket_path()))
        sock.sendall(b'{"type":"reload_config"}\n')
        sock.recv(4096)
        sock.close()
    except OSError:
        pass  # Daemon not running; changes take effect on next start


def _cmd_set(args: argparse.Namespace) -> None:
    """Handle `ghst set <key> <value>`."""
    config = GhstConfig.load()
    key = args.key
    value = args.value

    if config.set_value(key, value):
        print(f"  ✓ {key} → {value}")
        _send_reload(config)
        if "hotkey" in key:
            print("  ⚠ Restart your shell for hotkey changes to take effect")
    else:
        print(f"  ghst: unknown config key '{key}'", file=sys.stderr)
        print("  Run `ghst defaults` to see available keys", file=sys.stderr)
        sys.exit(1)


def _cmd_get(args: argparse.Namespace) -> None:
    """Handle `ghst get <key>`."""
    config = GhstConfig.load()
    key = args.key
    value = config.get_flat(key)
    default = config.get_default(key)

    if value is None and key not in GhstConfig.FLAT_KEYS:
        print(f"  ghst: unknown config key '{key}'", file=sys.stderr)
        sys.exit(1)

    if value == default:
        print(f"  {key} = {value} (default)")
    else:
        print(f"  {key} = {value} (default: {default})")


def _cmd_reset(args: argparse.Namespace) -> None:
    """Handle `ghst reset <key>`."""
    config = GhstConfig.load()
    key = args.key

    if config.reset_value(key):
        default = config.get_default(key)
        print(f"  ✓ {key} → {default} (default)")
        _send_reload(config)
    else:
        print(f"  ghst: unknown config key '{key}'", file=sys.stderr)
        sys.exit(1)


def _cmd_defaults(args: argparse.Namespace) -> None:
    """Handle `ghst defaults`."""
    config = GhstConfig.load()
    for key in GhstConfig.FLAT_KEYS:
        value = config.get_flat(key)
        default = config.get_default(key)
        # Mask API key
        display = value
        if key == "api_key" and value:
            display = value[:4] + "..." + value[-4:] if len(str(value)) > 8 else "****"
        pad = " " * (28 - len(key))
        if value == default:
            print(f"  {key}{pad}= {display}")
        else:
            print(f"  {key}{pad}= {display} (default: {default})")


# ── Model commands ───────────────────────────────────────────────────────────

def _cmd_model(args: argparse.Namespace) -> None:
    """Handle `ghst model`."""
    config = GhstConfig.load()
    ac = config.provider.effective_autocomplete_model
    nl = config.provider.model
    name = config.provider.name
    print(f"  autocomplete: {ac} ({name})")
    print(f"  nl-commands:  {nl} ({name})")


def _cmd_model_list(args: argparse.Namespace) -> None:
    """Handle `ghst model list`."""
    config = GhstConfig.load()
    health = _probe_directly(config)
    if not health.get("ok"):
        print(f"  ghst: {health.get('error', 'could not reach the server')}", file=sys.stderr)
        sys.exit(1)
    current = {config.provider.effective_autocomplete_model, config.provider.model}
    for model in health.get("models", []):
        print(f"  {'*' if model in current else ' '} {model}")


def _cmd_model_set(args: argparse.Namespace) -> None:
    """Handle `ghst model set <model>`."""
    config = GhstConfig.load()
    model = args.model

    if args.autocomplete:
        config.provider.autocomplete_model = model
        config.write_toml()
        print(f"  ✓ autocomplete model → {model}")
    elif args.nl:
        config.provider.model = model
        config.write_toml()
        print(f"  ✓ nl-commands model → {model}")
    else:
        # Set both
        config.provider.model = model
        config.provider.autocomplete_model = model
        config.write_toml()
        print(f"  ✓ model → {model} (autocomplete + nl-commands)")

    _send_reload(config)


# ── Provider commands ────────────────────────────────────────────────────────

def _cmd_provider(args: argparse.Namespace) -> None:
    """Handle `ghst provider`."""
    config = GhstConfig.load()
    print(f"  provider: {config.provider.name}")
    print(f"  endpoint: {config.provider.effective_api_base_url}")


def _cmd_provider_set(args: argparse.Namespace) -> None:
    """Handle `ghst provider set <name>`."""
    config = GhstConfig.load()
    name = args.name

    if name not in ("openai", "anthropic", "chatgpt"):
        print(f"  ghst: unknown provider '{name}' (use 'openai', 'anthropic' or 'chatgpt')")
        sys.exit(1)

    if name == "chatgpt":
        print("  Run `ghst login` to sign in with ChatGPT; it switches the provider for you.")
        return

    config.provider.name = name

    # Prompt for API key if switching providers
    env_key = os.environ.get("GHST_API_KEY", "")
    if not env_key:
        api_key = getpass.getpass("  API key: ")
        config.provider.api_key = api_key

    # Suggest default models
    defaults = {
        "openai": ("gpt-4o", "gpt-4o-mini"),
        "anthropic": ("claude-sonnet-4-5", "claude-haiku-4-5"),
    }
    default_model, default_ac = defaults[name]
    print(f"  Model for commands [{default_model}]: ", end="", flush=True)
    model = input().strip()
    config.provider.model = model or default_model
    print(f"  Model for autocomplete [{default_ac}]: ", end="", flush=True)
    ac_model = input().strip()
    config.provider.autocomplete_model = ac_model or default_ac

    config.write_toml()
    _send_reload(config)
    print(f"  ✓ provider → {name}")


# ── ChatGPT sign-in ──────────────────────────────────────────────────────────

def _choose_model(models: list[str], current: str, label: str, prefer_small: bool) -> str:
    """Pick a model from the list, asking when the terminal is interactive."""
    if current in models:
        default = current
    else:
        small = [m for m in models if any(t in m for t in ("nano", "mini", "spark", "flash"))]
        default = (small or models)[0] if prefer_small else models[0]
    if not sys.stdin.isatty():
        return default
    print(f"\n  Model for {label}:")
    for i, model in enumerate(models, 1):
        print(f"    {i}. {model}{'  (default)' if model == default else ''}")
    answer = input("  Number or name [default]: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(models):
        return models[int(answer) - 1]
    return answer or default


def _cmd_login(args: argparse.Namespace) -> None:
    """Handle `ghst login`: sign in with ChatGPT and use the plan for requests."""
    from ghst import chatgpt

    try:
        record = chatgpt.login(new_account=args.new_account, open_browser=not args.no_browser)
    except chatgpt.ChatGPTError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Signed in to ChatGPT as {record.get('email') or record['subject']}")
    if not chatgpt.plan_enabled(record):
        print(
            "  ⚠ ChatGPT plan usage isn't enabled for this sign-in, so ghst can't use it.\n"
            "    It needs an eligible plan (Plus or Pro) and permission to use it in ghst.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = GhstConfig.load()
    config.provider.name = "chatgpt"  # api_base_url is kept for switching back
    models = _probe_directly(config).get("models", [])
    if models:
        config.provider.autocomplete_model = _choose_model(
            models, config.provider.autocomplete_model, "ghost text (pick the fastest)", True
        )
        config.provider.model = _choose_model(
            models, config.provider.model, "Ctrl+G commands", False
        )
    else:
        print("  ⚠ Could not list ChatGPT models; set them with `ghst model set`.")
    config.write_toml()
    _send_reload(config)

    if chatgpt.mark_welcomed():
        print(
            "\n  You're using your ChatGPT plan\n"
            "  Eligible usage in ghst uses your ChatGPT plan. Manage usage in your\n"
            f"  ChatGPT settings: {chatgpt.USAGE_URL}\n"
        )
        if sys.stdin.isatty():
            input("  Press Enter to continue (Got it) ")
    print(f"  ✓ Using ChatGPT plan (Manage usage: {chatgpt.USAGE_URL})")
    print(f"    ghost text: {config.provider.effective_autocomplete_model}")
    print(f"    Ctrl+G:     {config.provider.model}")


def _cmd_logout(args: argparse.Namespace) -> None:
    """Handle `ghst logout`."""
    from ghst import chatgpt

    record, revoked = chatgpt.logout()
    if not record:
        print("  Not signed in to ChatGPT.")
        return
    _send_reload(GhstConfig.load())
    if revoked:
        print("  ✓ Signed out of ChatGPT")
    else:
        print(
            "  ⚠ Removed the ChatGPT tokens from this machine, but OpenAI didn't confirm\n"
            "    the sign-out. Disconnect ghst in ChatGPT settings to be sure."
        )


# ── Help command ─────────────────────────────────────────────────────────────

HELP_TEXT = """\
  ghst — AI-powered shell plugin

  Features:
    Autocomplete       Ghost text suggestions as you type (Tab/→ to accept)
    NL Commands        Ctrl+G → describe in English → get a command
    Error Correction   Automatic fix suggestions after failed commands
    History Search     Ctrl+R → search history in plain English
    Cheat Sheet        Ctrl+/ → show shortcuts at the prompt

  Commands:
    ghst init                 Setup wizard (re-run to reconfigure)
    ghst status               Daemon health, provider, models, uptime
    ghst model [set]          Show or change models
    ghst provider [set]       Show or change LLM provider
    ghst login | logout       Sign in with ChatGPT to use your ChatGPT plan
    ghst set <key> <value>    Change a config value
    ghst get <key>            Show a config value
    ghst reset <key>          Reset a config key to default
    ghst defaults             Show all settings with defaults
    ghst config               Edit config file in $EDITOR
    ghst start | stop         Manage daemon
    ghst help                 This screen

  Run `ghst help <command>` for details on any command.
"""

HELP_COMMANDS: dict[str, str] = {
    "init": (
        "  ghst init\n\n  Interactive setup wizard. Detects your shell,\n"
        "  prompts for LLM provider and API key, writes config,\n"
        "  and verifies the connection. Safe to re-run."
    ),
    "start": (
        "  ghst start [--quiet]\n\n  Start the background daemon.\n"
        "  Auto-starts on first use via shell-init."
    ),
    "stop": "  ghst stop\n\n  Stop the background daemon.",
    "status": (
        "  ghst status\n\n  Show daemon health: running state, PID,\n"
        "  uptime, provider, models, connection quality."
    ),
    "model": (
        "  ghst model\n  ghst model set <model>\n"
        "  ghst model set --autocomplete <model>\n"
        "  ghst model set --nl <model>\n\n"
        "  Show or change models. Without flags, sets both."
    ),
    "provider": (
        "  ghst provider\n  ghst provider set <name>\n\n"
        "  Show or switch provider (openai, anthropic).\n"
        "  Prompts for API key and models when switching."
    ),
    "set": (
        "  ghst set <key> <value>\n\n  Set any config value.\n"
        "  Example: ghst set autocomplete_delay_ms 300\n\n"
        "  Run `ghst defaults` to see available keys."
    ),
    "get": "  ghst get <key>\n\n  Show current and default value of a config key.",
    "reset": "  ghst reset <key>\n\n  Reset a config key to its default value.",
    "defaults": "  ghst defaults\n\n  Show all config keys with current and default values.",
    "config": "  ghst config\n\n  Open ~/.config/ghst/config.toml in $EDITOR.",
}


def _cmd_help(args: argparse.Namespace) -> None:
    """Handle `ghst help [command]`."""
    command = getattr(args, "help_command", None)
    if command and command in HELP_COMMANDS:
        print(HELP_COMMANDS[command])
    elif command:
        print(f"  ghst: unknown command '{command}'")
        print("  Run `ghst help` to see all commands.")
    else:
        print(HELP_TEXT)


def main(argv: list[str] | None = None) -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="ghst",
        description="AI-powered shell plugin",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    sp = subparsers.add_parser("init", help="Interactive setup wizard")
    sp.set_defaults(func=_cmd_init)

    # shell-init
    sp = subparsers.add_parser("shell-init", help="Output shell integration code")
    sp.add_argument("shell", choices=["zsh"], help="Shell type")
    sp.set_defaults(func=_cmd_shell_init)

    # start
    sp = subparsers.add_parser("start", help="Start the daemon")
    sp.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    sp.set_defaults(func=_cmd_start)

    # stop
    sp = subparsers.add_parser("stop", help="Stop the daemon")
    sp.set_defaults(func=_cmd_stop)

    # status
    sp = subparsers.add_parser("status", help="Show daemon status and health")
    sp.set_defaults(func=_cmd_status)

    # config
    sp = subparsers.add_parser("config", help="Open config file in $EDITOR")
    sp.set_defaults(func=_cmd_config)

    # model
    sp_model = subparsers.add_parser("model", help="Show or change models")
    sp_model.set_defaults(func=_cmd_model)
    model_sub = sp_model.add_subparsers(dest="model_command")
    sp_model_set = model_sub.add_parser("set", help="Set model")
    sp_model_set.add_argument("model", help="Model name")
    sp_model_set.add_argument(
        "--autocomplete", action="store_true", help="Set autocomplete model only"
    )
    sp_model_set.add_argument(
        "--nl", action="store_true", help="Set NL command model only"
    )
    sp_model_set.set_defaults(func=_cmd_model_set)
    sp_model_list = model_sub.add_parser("list", help="List the server's models")
    sp_model_list.set_defaults(func=_cmd_model_list)

    # provider
    sp_provider = subparsers.add_parser("provider", help="Show or change provider")
    sp_provider.set_defaults(func=_cmd_provider)
    provider_sub = sp_provider.add_subparsers(dest="provider_command")
    sp_provider_set = provider_sub.add_parser("set", help="Set provider")
    sp_provider_set.add_argument("name", help="Provider name (openai, anthropic, chatgpt)")
    sp_provider_set.set_defaults(func=_cmd_provider_set)

    # login / logout
    sp = subparsers.add_parser("login", help="Sign in with ChatGPT")
    sp.add_argument(
        "--new-account", action="store_true", help="Sign in to a different ChatGPT account"
    )
    sp.add_argument("--no-browser", action="store_true", help="Print the sign-in URL instead")
    sp.set_defaults(func=_cmd_login)
    sp = subparsers.add_parser("logout", help="Sign out of ChatGPT")
    sp.set_defaults(func=_cmd_logout)

    # set
    sp = subparsers.add_parser("set", help="Set a config value")
    sp.add_argument("key", help="Config key")
    sp.add_argument("value", help="Config value")
    sp.set_defaults(func=_cmd_set)

    # get
    sp = subparsers.add_parser("get", help="Get a config value")
    sp.add_argument("key", help="Config key")
    sp.set_defaults(func=_cmd_get)

    # reset
    sp = subparsers.add_parser("reset", help="Reset a config value to default")
    sp.add_argument("key", help="Config key")
    sp.set_defaults(func=_cmd_reset)

    # defaults
    sp = subparsers.add_parser("defaults", help="Show all settings with defaults")
    sp.set_defaults(func=_cmd_defaults)

    # help
    sp = subparsers.add_parser("help", help="Show help")
    sp.add_argument("help_command", nargs="?", default=None, help="Command to get help for")
    sp.set_defaults(func=_cmd_help)

    args = parser.parse_args(argv)

    if not args.command:
        _cmd_help(argparse.Namespace(help_command=None))
        sys.exit(0)

    args.func(args)
