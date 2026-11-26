"""CLI entry point for ghst."""

from __future__ import annotations

import argparse
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
    # The daemon writes its own pid file once it holds the single-instance
    # lock; a second daemon started at the same moment just exits
    for _ in range(40):
        if socket_path.exists() or proc.poll() is not None:
            break
        time.sleep(0.05)

    if not getattr(args, "quiet", False):
        print(f"ghst: daemon started (pid {proc.pid})")


def _terminate(pid: int) -> bool:
    """SIGTERM the daemon and wait for it to exit, so an immediate restart
    isn't refused by its lock. Returns False if it wasn't running."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        for _ in range(60):
            time.sleep(0.1)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)  # still alive after 6s
    except ProcessLookupError:
        pass
    return True


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
    except ValueError:
        print("ghst: invalid pid file")
        pid = 0
    if pid and _terminate(pid):
        print(f"ghst: daemon stopped (pid {pid})")
    elif pid:
        print("ghst: daemon not running (stale pid file)")

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
        health = _probe(config)
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


def _probe(config: GhstConfig) -> dict:
    """Check the LLM server from the CLI when the daemon can't."""
    from ghst.setup import probe

    return probe(config)


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

    # Step 2: Configure provider (keeps the rest of an existing config)
    from ghst.setup import setup_provider

    print()
    config = GhstConfig.load()
    if not setup_provider(config):
        print("  Provider not set up; run `ghst provider` later.")
        return
    provider = config.provider.name
    ac_model = config.provider.effective_autocomplete_model

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
    print("    → Autocomplete appears as ghost text (Tab accepts it, → one word at a time)")
    print("    → Press Cmd+I (or Ctrl+G) to describe a command in English")
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
    """Handle `ghst model`: pick models from the server's list, or show them."""
    config = GhstConfig.load()
    if not sys.stdin.isatty():
        print(f"  ghost text: {config.provider.effective_autocomplete_model}")
        print(f"  Cmd+I:      {config.provider.model}")
        return
    from ghst.setup import choose_models, report

    if choose_models(config):
        config.save_profile()
        config.write_toml()
        _send_reload(config)
        report(config)


def _cmd_model_list(args: argparse.Namespace) -> None:
    """Handle `ghst model list`."""
    config = GhstConfig.load()
    health = _probe(config)
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
    """Handle `ghst provider`: choose and connect a provider, or show it."""
    config = GhstConfig.load()
    if not sys.stdin.isatty():
        from ghst.config import profile_id
        from ghst.setup import PROVIDER_LABELS

        print(f"  provider: {PROVIDER_LABELS[profile_id(config.provider)]}")
        print(f"  endpoint: {config.provider.effective_api_base_url}")
        return
    _run_setup(config, None)


def _cmd_provider_set(args: argparse.Namespace) -> None:
    """Handle `ghst provider set <name>`."""
    from ghst.setup import PROVIDER_LABELS

    if args.name not in PROVIDER_LABELS:
        names = ", ".join(PROVIDER_LABELS)
        print(f"  ghst: unknown provider '{args.name}' (use one of: {names})", file=sys.stderr)
        sys.exit(1)
    _run_setup(GhstConfig.load(), args.name)


def _run_setup(config: GhstConfig, pid: str | None) -> None:
    from ghst.setup import setup_provider

    try:
        saved = setup_provider(config, pid)
    except KeyboardInterrupt:
        saved = False
        print()
    if saved:
        _send_reload(config)
    else:
        print("  Nothing changed.")
        sys.exit(1)


# ── ChatGPT sign-in ──────────────────────────────────────────────────────────

def _cmd_login(args: argparse.Namespace) -> None:
    """Handle `ghst login`: sign in with ChatGPT and use the plan for requests."""
    from ghst.setup import sign_in_chatgpt

    if not sign_in_chatgpt(new_account=args.new_account, open_browser=not args.no_browser):
        sys.exit(1)
    _run_setup(GhstConfig.load(), "chatgpt")


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

_B, _D, _C, _R = "\x1b[1m", "\x1b[2m", "\x1b[36m", "\x1b[0m"


def _style(text: str) -> str:
    """Bold headings and cyan commands when printing to a terminal."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return re.sub(r"\{[bcd/]\}", "", text)
    return (
        text.replace("{b}", _B).replace("{c}", _C).replace("{d}", _D).replace("{/}", _R)
    )


HELP_TEXT = """\
  {b}ghst{/} — AI ghost-text autocomplete for zsh

  {b}Get started{/}
    {c}ghst provider{/}             Choose a provider and connect: a local server
                              (oMLX, Ollama), your ChatGPT plan, OpenAI or Anthropic
    {c}ghst login{/}                Sign in with ChatGPT and use your plan
    {c}ghst status{/}               Check the daemon, the server and your account
    Then add {c}eval "$(ghst shell-init zsh)"{/} to ~/.zshrc and open a new tab.

  {b}Models{/}
    {c}ghst model{/}                Pick the ghost-text and Cmd+I models from a list
    {c}ghst model list{/}           Show the models the server offers
    {c}ghst model set <name>{/}     Set both models ({c}--autocomplete{/} or {c}--nl{/} for one)

  {b}Settings{/}
    {c}ghst help settings{/}        Every setting, what it does, and its value
    {c}ghst set <key> <value>{/}    Change one   {d}e.g. ghst set autocomplete_delay_ms 80{/}
    {c}ghst get <key>{/} · {c}ghst reset <key>{/} · {c}ghst config{/} (open the file)

  {b}Account{/}
    {c}ghst login{/} · {c}ghst logout{/}  ChatGPT sign-in ({c}--new-account{/}, {c}--no-browser{/})

  {b}Daemon{/}
    {c}ghst start{/} · {c}ghst stop{/}    Usually automatic; the shell starts it

  {b}At the prompt{/}
    Tab       accept the suggestion       →         accept the next word or dir/
    Esc       dismiss                     Cmd+I     describe a command in English
                                          {d}(Ctrl+G too, where the terminal doesn't take it){/}
    Ctrl+Z    undo a generated command    Ctrl+/    cheat sheet

  {d}ghst help <command> for details. Config: ~/.config/ghst/config.toml{/}
"""

HELP_COMMANDS: dict[str, str] = {
    "provider": """\
  {c}ghst provider{/}
  {c}ghst provider set <local|chatgpt|openai|anthropic>{/}

  Choose where ghost text comes from, connect to it, then pick models from
  the server's list. ghst remembers each provider's settings, so switching
  back later doesn't ask for the key again.

    {b}local{/}      Any OpenAI-compatible server. Defaults to oMLX on
               http://localhost:8000/v1 and can reuse oMLX's own API key.
               For Ollama use http://localhost:11434/v1.
               Recommended models: ghost text Qwen2.5-Coder-1.5B-4bit (the
               base model, not -Instruct; ghst uses fill-in-the-middle for it),
               Cmd+I Qwen3-Coder-30B-A3B-Instruct-8bit.
    {b}chatgpt{/}    Sign in with ChatGPT; requests use your Plus or Pro plan.
    {b}openai{/}     OpenAI API key, billed per token.
    {b}anthropic{/}  Anthropic API key, billed per token.

  At the end it sends one real ghost-text request and shows how long it took.""",
    "model": """\
  {c}ghst model{/}                       pick both models from a list
  {c}ghst model list{/}                  show what the server offers (* = in use)
  {c}ghst model set <name>{/}            set both
  {c}ghst model set --autocomplete <name>{/}
  {c}ghst model set --nl <name>{/}

  The ghost-text model runs on every pause in typing, so pick the fastest one
  that gives good suggestions. On a local server a base coder model such as
  Qwen2.5-Coder-1.5B-4bit is best: ghst sends it fill-in-the-middle prompts.
  The Cmd+I model turns English into commands and can be larger.""",
    "login": """\
  {c}ghst login{/} [{c}--new-account{/}] [{c}--no-browser{/}]

  Opens "Continue with ChatGPT" in your browser, then switches ghst to your
  ChatGPT plan and lets you pick models. Requests count against your plan's
  shared allowance; see or cap ghst's share at chatgpt.com/settings/usage.
  {c}--new-account{/} signs in to a different ChatGPT account.
  {c}--no-browser{/} prints the sign-in URL instead of opening it.""",
    "logout": """\
  {c}ghst logout{/}

  Revokes the ChatGPT sign-in and deletes its tokens from this machine.""",
    "status": """\
  {c}ghst status{/}

  Shows whether the daemon runs, which provider and model are in use, whether
  the server answers and accepts the key, the ChatGPT account and plan, and
  the last request error.""",
    "init": """\
  {c}ghst init{/}

  First-time setup: adds the shell line to ~/.zshrc, runs {c}ghst provider{/}
  and starts the daemon. Safe to re-run; other settings are kept.""",
    "set": """\
  {c}ghst set <key> <value>{/}

  Change one setting; the daemon picks it up at once. New shells pick up
  delay, hotkey and history settings. See {c}ghst help settings{/}.""",
    "get": "  {c}ghst get <key>{/}\n\n  Show a setting's value and default.",
    "reset": "  {c}ghst reset <key>{/}\n\n  Put a setting back to its default.",
    "defaults": "  {c}ghst defaults{/}\n\n  List every setting with its value and default.",
    "config": "  {c}ghst config{/}\n\n  Open ~/.config/ghst/config.toml in $EDITOR.",
    "start": "  {c}ghst start{/} [{c}--quiet{/}]\n\n  Start the background daemon.",
    "stop": "  {c}ghst stop{/}\n\n  Stop the background daemon.",
}

SETTINGS_HELP: list[tuple[str, list[tuple[str, str]]]] = [
    ("Provider (use ghst provider / ghst model)", [
        ("provider", "openai (OpenAI API or a local server), anthropic, or chatgpt"),
        ("api_base_url", "server URL, e.g. http://localhost:8000/v1 for oMLX"),
        ("api_key", "API key (or set GHST_API_KEY)"),
        ("autocomplete_model", "model for ghost text"),
        ("model", "model for Cmd+I (plain English to command)"),
    ]),
    ("Ghost text", [
        ("autocomplete_delay_ms", "pause in typing before asking for a suggestion"),
        ("autocomplete_delay_short_ms", "shorter pause once the line is long"),
        ("autocomplete_delay_threshold", "line length that switches to the shorter pause"),
        ("autocomplete_min_chars", "characters typed before suggestions start"),
        ("autocomplete_mode", "auto, chat, or fim (fill-in-the-middle for base coder models)"),
        ("autocomplete_max_tokens", "longest suggestion, in tokens"),
        ("autocomplete_timeout_ms", "give up on a suggestion after this long"),
        ("ghost_color", "ghost text colour: 256-colour index or #rrggbb"),
    ]),
    ("History", [
        ("history_source", "past commands from: auto, atuin, zsh, or off"),
        ("history_match_limit", "how many past commands to use"),
        ("instant_history", "show the best past command before the model answers"),
    ]),
    ("Hotkeys (zsh notation, e.g. ^G; empty = unbound)", [
        ("nl_hotkey", "describe a command in English (space-separated keys)"),
        ("history_search_hotkey", "search history in English"),
        ("cheat_sheet_hotkey", "show the cheat sheet"),
    ]),
    ("Limits and colours", [
        ("rate_limit_rpm", "most model requests per minute"),
        ("accent_color", "colour of ghst prompts"),
        ("success_color", "colour of ✓"),
        ("warning_color", "colour of ⚠"),
        ("error_color", "colour of ✗"),
    ]),
]


def _print_settings_help() -> None:
    config = GhstConfig.load()
    out = ["  {b}Settings{/}  {d}change with: ghst set <key> <value>{/}"]
    for heading, keys in SETTINGS_HELP:
        out.append(f"\n  {{b}}{heading}{{/}}")
        for key, text in keys:
            value = config.get_flat(key)
            if key == "api_key" and value:
                value = f"…{str(value)[-4:]}"
            shown = "" if value in ("", None) else f"  {{d}}= {value}{{/}}"
            out.append(f"    {{c}}{key:<30}{{/}}{text}{shown}")
    print(_style("\n".join(out)))


def _cmd_help(args: argparse.Namespace) -> None:
    """Handle `ghst help [command]`."""
    command = getattr(args, "help_command", None)
    if command == "settings":
        _print_settings_help()
    elif command in HELP_COMMANDS:
        print(_style(HELP_COMMANDS[command]))
    elif command:
        print(f"  ghst: unknown command '{command}'. Run `ghst help` to see them all.")
        sys.exit(1)
    else:
        print(_style(HELP_TEXT))


def main(argv: list[str] | None = None) -> None:
    """Main CLI entry point."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        argv = ["help"]
    elif len(argv) >= 2 and argv[1] in ("-h", "--help") and argv[0] in HELP_COMMANDS:
        argv = ["help", argv[0]]
    parser = argparse.ArgumentParser(
        prog="ghst",
        description="AI ghost-text autocomplete for zsh",
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
