"""ghst daemon — asyncio socket server.

Listens on a Unix domain socket, routes JSON requests to the appropriate
handler (autocomplete, NL command, error correction, history search),
and returns responses.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ghst.config import GhstConfig, resolve_autocomplete_mode, resolve_history_source
from ghst.context import ContextInfo
from ghst.llm import TIMEOUT_HISTORY, TIMEOUT_NL, LLMClient
from ghst.prompts import (
    COMMAND_SYSTEM,
    autocomplete_fim,
    autocomplete_system,
    autocomplete_user,
    error_correction_user,
    history_search_user,
    nl_command_user,
    proactive_system,
    proactive_user,
)
from ghst.safety import check_dangerous, sanitize_history, sanitize_output

logger = logging.getLogger("ghst")

RATE_LIMIT_RPM = 240  # max requests per minute to LLM


class RateLimiter:
    """Simple RPM-based rate limiter."""

    def __init__(self, rpm: int = RATE_LIMIT_RPM) -> None:
        self.rpm = rpm
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > 60:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.rpm:
            return False
        self._timestamps.append(now)
        return True


@dataclass
class SessionEntry:
    """One entry in the rolling session buffer."""

    command: str
    output: str  # truncated to SESSION_OUTPUT_LINES


@dataclass
class SessionBuffer:
    """Rolling buffer of recent commands and their outputs.

    Used for proactive suggestions to give the LLM session-level awareness.
    """

    MAX_ENTRIES: int = 20
    MAX_OUTPUT_LINES: int = 20
    entries: deque[SessionEntry] = field(default_factory=lambda: deque(maxlen=20))

    def add(self, command: str, output: str) -> None:
        # Truncate output to last MAX_OUTPUT_LINES lines
        lines = output.splitlines()
        if len(lines) > self.MAX_OUTPUT_LINES:
            lines = lines[-self.MAX_OUTPUT_LINES:]
        self.entries.append(SessionEntry(command=command, output="\n".join(lines)))

    def format_for_prompt(self) -> str:
        if not self.entries:
            return ""
        parts = []
        for i, entry in enumerate(self.entries):
            idx = len(self.entries) - i
            parts.append(f"[{idx}] {entry.command}")
            if entry.output.strip():
                # Indent output
                indented = "\n".join(f"    {line}" for line in entry.output.splitlines())
                parts.append(indented)
        return "\n".join(parts)


def _ensure_leading_space(buffer: str, suggestion: str) -> str:
    """Fallback: add a space before unambiguous new-token characters."""
    if not suggestion or not buffer:
        return suggestion
    if (
        not buffer[-1].isspace()
        and not suggestion[0].isspace()
        and suggestion[0] in "-|>&;<()\"'"
    ):
        return " " + suggestion
    return suggestion



_FENCE_RE: re.Pattern[str] | None = None



def _strip_echo(buffer: str, suggestion: str) -> str:
    """Drop text the model repeated from the buffer (small models do this)."""
    typed = buffer.rstrip()
    if typed and suggestion.startswith(typed):
        rest = suggestion[len(typed):]
        return rest.lstrip() if buffer != typed else rest
    if buffer and not buffer[-1].isspace():
        token = buffer.split()[-1]
        if token != buffer and suggestion.startswith(token):
            return suggestion[len(token):]
    return suggestion


async def _atuin_matches(buffer: str, cwd: str, limit: int = 8) -> list[str]:
    """Past commands from atuin that start with the buffer, this directory first."""
    atuin = shutil.which("atuin")
    if not atuin or not buffer.strip():
        return []
    env = {**os.environ, "ATUIN_SESSION": os.environ.get("ATUIN_SESSION", "ghst")}

    async def search(*extra: str) -> list[str]:
        proc = await asyncio.create_subprocess_exec(
            atuin, "search", "--search-mode", "prefix", "--filter-mode", "global",
            "--limit", "20", "--cmd-only", "--print0", *extra, "--", buffer,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=0.5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        # atuin prints oldest first
        return out.decode(errors="replace").split("\0")[::-1]

    try:
        here, everywhere = await asyncio.gather(search("--cwd", cwd), search())
    except (asyncio.TimeoutError, OSError):
        return []
    matches: list[str] = []
    for cmd in here + everywhere:
        cmd = cmd.strip()
        if cmd and "\n" not in cmd and cmd != buffer.strip() and cmd not in matches:
            matches.append(cmd[:200])
    return matches[:limit]


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences that LLMs sometimes wrap responses in."""
    global _FENCE_RE
    if _FENCE_RE is None:
        _FENCE_RE = re.compile(r"^```(?:\w*)\n?(.*?)```$", re.DOTALL)
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text


class GhstDaemon:
    """Main daemon process."""

    IDLE_TIMEOUT_SECONDS: float = 30 * 60  # 30 minutes

    def __init__(self, config: GhstConfig | None = None) -> None:
        self.config = config or GhstConfig.load()
        self.llm = LLMClient(self.config)
        self.context = ContextInfo()
        self.session = SessionBuffer()
        self._server: asyncio.AbstractServer | None = None
        self._last_activity: float = 0.0
        self._rate_limiter = RateLimiter(self.config.ui.rate_limit_rpm)
        self._inflight_complete: asyncio.Task[dict[str, Any]] | None = None
        self._stopping = asyncio.Event()
        self._lock_file: Any = None

    async def handle_request(self, data: dict[str, Any]) -> dict[str, Any]:
        """Route a request to the appropriate handler."""
        self._last_activity = asyncio.get_event_loop().time()
        req_type = data.get("type", "")
        try:
            if req_type == "complete":
                # Cancel stale in-flight autocomplete to free up resources
                if self._inflight_complete and not self._inflight_complete.done():
                    self._inflight_complete.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._inflight_complete
                self._inflight_complete = asyncio.create_task(
                    self._handle_complete(data)
                )
                try:
                    return await self._inflight_complete
                except asyncio.CancelledError:
                    logger.debug("Autocomplete cancelled by newer request")
                    return {
                        "type": "complete",
                        "request_id": data.get("request_id", ""),
                        "suggestion": "",
                    }
            elif req_type == "nl":
                return await self._handle_nl(data)
            elif req_type == "error_correct":
                return await self._handle_error_correct(data)
            elif req_type == "history_search":
                return await self._handle_history_search(data)
            elif req_type == "health":
                return await self._handle_health()
            elif req_type == "reload_config":
                return self._handle_reload_config()
            else:
                return {"type": "error", "message": f"Unknown request type: {req_type}"}
        except Exception as e:
            logger.exception("Error handling %s request", req_type)
            return {"type": "error", "message": str(e)}

    async def _handle_complete(self, data: dict[str, Any]) -> dict[str, Any]:
        """Handle autocomplete and proactive suggestion requests."""
        buffer = data.get("buffer", "")
        cwd = data.get("cwd", os.getcwd())
        shell = data.get("shell", "zsh")
        history = sanitize_history(data.get("history", []))
        last_command = data.get("last_command", "")
        last_output = data.get("last_output", "")
        request_id = data.get("request_id", "")

        # Rate limit check
        if not self._rate_limiter.allow():
            logger.debug("Rate limit exceeded — dropping request")
            return {"type": "complete", "request_id": request_id, "suggestion": ""}

        is_proactive = not buffer and last_output

        if is_proactive:
            # Add to session buffer
            sanitized_output = sanitize_output(last_output)
            self.session.add(last_command, sanitized_output)

            # Skip proactive on high latency
            if self.llm.health.is_high_latency:
                return {"type": "complete", "request_id": request_id, "suggestion": ""}

            session_text = self.session.format_for_prompt()
            messages = [
                {"role": "system", "content": proactive_system(session_text)},
                {"role": "user", "content": proactive_user(
                    cwd=cwd, history=history, last_command=last_command,
                    last_output=sanitized_output, shell=shell,
                )},
            ]
            cache_key = ("proactive", last_command, cwd, str(hash(last_output)))
            model = self.config.provider.effective_autocomplete_model
            suggestion = await self.llm.complete(
                messages, model=model, timeout=self._autocomplete_timeout,
                use_cache_key=cache_key,
            )
        else:
            # Regular autocomplete
            exit_status = data.get("exit_status", 0)
            ctx = self.context.gather(cwd, shell)
            raw_matches = data.get("history_matches")
            if not isinstance(raw_matches, list):
                raw_matches = await self._history_matches(buffer, cwd)
            history_matches = sanitize_history([m for m in raw_matches if isinstance(m, str)])
            messages: list[dict[str, Any]] = []
            fim_prompt = ""
            if resolve_autocomplete_mode(self.config) == "fim":
                fim_prompt = autocomplete_fim(
                    buffer=buffer, cwd=cwd, history=history,
                    dir_listing=ctx.dir_listing(cwd, max_entries=20),
                    git_branch=ctx.git_branch, history_matches=history_matches,
                )
            else:
                messages = [
                    {"role": "system", "content": autocomplete_system()},
                    {"role": "user", "content": autocomplete_user(
                        buffer=buffer, cwd=cwd, history=history, shell=shell,
                        exit_status=exit_status,
                        dir_listing=ctx.dir_listing(cwd, max_entries=20),
                        git_branch=ctx.git_branch,
                        git_dirty=ctx.git_dirty,
                        git_branches=ctx.git_branches,
                        project_types=ctx.project_types,
                        active_env=ContextInfo.active_env(),
                        history_matches=history_matches,
                    )},
                ]
            cache_key = ("autocomplete", buffer, cwd)
            model = self.config.provider.effective_autocomplete_model
            suggestion = await self.llm.complete(
                messages, model=model, timeout=self._autocomplete_timeout,
                use_cache_key=cache_key, single_line=True, fim_prompt=fim_prompt,
            )
            suggestion = _strip_echo(buffer, suggestion)
            # FIM post-process: ensure leading space for special chars
            suggestion = _ensure_leading_space(buffer, suggestion)

        # Strip trailing whitespace but preserve leading
        suggestion = suggestion.rstrip()

        # Strip markdown code fences
        suggestion = _strip_code_fences(suggestion)

        # Reject multiline suggestions for autocomplete
        if "\n" in suggestion:
            suggestion = suggestion.split("\n")[0].rstrip()

        # Safety check
        full_command = buffer + suggestion if buffer else suggestion
        warning = check_dangerous(full_command)

        # "suggestion" stays the last field: the shell parses it from the end
        response: dict[str, Any] = {"type": "complete", "request_id": request_id}
        if warning:
            response["warning"] = warning
        response["suggestion"] = suggestion

        return response

    async def _handle_nl(self, data: dict[str, Any]) -> dict[str, Any]:
        """Handle natural language command construction."""
        prompt = data.get("prompt", "")
        cwd = data.get("cwd", os.getcwd())
        shell = data.get("shell", "zsh")
        buffer = data.get("buffer", "")
        history = sanitize_history(data.get("history", []))

        if not prompt:
            return {"type": "nl", "command": ""}

        messages = [
            {"role": "system", "content": COMMAND_SYSTEM},
            {"role": "user", "content": nl_command_user(
                prompt=prompt, cwd=cwd, buffer=buffer,
                history=history, shell=shell,
            )},
        ]
        model = self.config.provider.model
        command = await self.llm.complete_with_retry(
            messages, model=model, timeout=TIMEOUT_NL, retries=1,
        )
        # Models often wrap the command in a markdown fence or a "$ " prompt
        command = _strip_code_fences(command).removeprefix("$ ").strip()

        response: dict[str, Any] = {"type": "nl", "command": command}

        if command:
            warning = check_dangerous(command)
            if warning:
                response["warning"] = warning

        return response

    async def _handle_error_correct(self, data: dict[str, Any]) -> dict[str, Any]:
        """Handle error correction requests."""
        failed_command = data.get("failed_command", "")
        exit_status = data.get("exit_status", 1)
        stderr = sanitize_output(data.get("stderr", ""))
        cwd = data.get("cwd", os.getcwd())
        shell = data.get("shell", "zsh")

        if not failed_command:
            return {"type": "error_correct", "suggestion": ""}

        messages = [
            {"role": "system", "content": COMMAND_SYSTEM},
            {"role": "user", "content": error_correction_user(
                failed_command=failed_command, exit_status=exit_status,
                stderr=stderr, cwd=cwd, shell=shell,
            )},
        ]
        model = self.config.provider.effective_autocomplete_model
        suggestion = await self.llm.complete(
            messages, model=model, timeout=self._autocomplete_timeout,
        )

        return {"type": "error_correct", "suggestion": suggestion.rstrip()}

    async def _handle_history_search(self, data: dict[str, Any]) -> dict[str, Any]:
        """Handle natural language history search."""
        query = data.get("query", "")
        history = sanitize_history(data.get("history", []))
        shell = data.get("shell", "zsh")

        if not query or not history:
            return {"type": "history_search", "results": []}

        messages = [
            {"role": "system", "content": COMMAND_SYSTEM},
            {"role": "user", "content": history_search_user(
                query=query, history=history, shell=shell,
            )},
        ]
        model = self.config.provider.model
        response_text = await self.llm.complete_with_retry(
            messages, model=model, timeout=TIMEOUT_HISTORY, retries=1,
        )

        # Parse JSON results
        try:
            results = json.loads(response_text)
            if not isinstance(results, list):
                results = []
        except (json.JSONDecodeError, TypeError):
            results = []

        return {"type": "history_search", "results": results}

    @property
    def _autocomplete_timeout(self) -> httpx.Timeout:
        read = self.config.ui.autocomplete_timeout_ms / 1000
        return httpx.Timeout(connect=1.0, read=read, write=1.0, pool=1.0)

    async def _history_matches(self, buffer: str, cwd: str) -> list[str]:
        """Past commands from atuin. The shell sends zsh matches itself."""
        ui = self.config.ui
        if resolve_history_source(ui.history_source) != "atuin" or ui.history_match_limit <= 0:
            return []
        return await _atuin_matches(buffer, cwd, limit=ui.history_match_limit)

    async def _handle_health(self) -> dict[str, Any]:
        probe = await self.llm.probe()
        return {
            "type": "health", **probe,
            "circuit": self.llm.health.status_display(),
            "last_error": self.llm.last_error,
        }

    def _handle_reload_config(self) -> dict[str, Any]:
        """Reload configuration from disk."""
        try:
            self.config = GhstConfig.load()
            self.llm.config = self.config
            self._rate_limiter.rpm = self.config.ui.rate_limit_rpm
            logger.info("Configuration reloaded")
            return {"type": "reload_config", "ok": True}
        except Exception as e:
            logger.error("Failed to reload config: %s", e)
            return {"type": "reload_config", "ok": False, "message": str(e)}

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single client connection."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    data = json.loads(line.decode())
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON received: %r", line[:200])
                    continue

                if data.get("type") == "complete" and data.get("stream_history"):
                    # Show the best history match at once while the model works
                    buffer = data.get("buffer", "")
                    matches = data.get("history_matches")
                    if not isinstance(matches, list):
                        matches = await self._history_matches(buffer, data.get("cwd", ""))
                    matches = [m for m in matches if isinstance(m, str)]
                    data["history_matches"] = matches
                    if (
                        self.config.ui.instant_history
                        and matches
                        and matches[0].startswith(buffer)
                    ):
                        partial = {
                            "type": "complete",
                            "request_id": data.get("request_id", ""),
                            "partial": True,
                            "suggestion": matches[0][len(buffer):],
                        }
                        writer.write(json.dumps(partial, ensure_ascii=False).encode() + b"\n")
                        await writer.drain()

                response = await self.handle_request(data)
                logger.debug("Request: %s → %s", data.get("type"), response)
                writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _acquire_lock(self) -> bool:
        """Hold an exclusive lock for our lifetime so only one daemon runs."""
        lock_path = self.config.get_socket_path().with_suffix(".lock")
        self._lock_file = open(lock_path, "w")  # noqa: SIM115 — held until exit
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            return False
        return True

    async def start(self) -> None:
        """Start the daemon socket server."""
        socket_path = self.config.get_socket_path()
        pid_path = self.config.get_pid_path()

        if not self._acquire_lock():
            logger.info("another ghst daemon is already running; exiting")
            return

        # We hold the lock, so any socket file left behind is stale
        socket_path.unlink(missing_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(socket_path)
        )

        # Set socket permissions (owner only)
        os.chmod(str(socket_path), 0o600)

        # Write PID file
        pid_path.write_text(str(os.getpid()))

        self._last_activity = asyncio.get_event_loop().time()
        logger.info("ghst daemon started (pid %d, socket %s)", os.getpid(), socket_path)

        async with self._server:
            while not self._stopping.is_set():
                # Wake every minute to check for idleness, or at once on stop()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=60)
                idle = asyncio.get_event_loop().time() - self._last_activity
                if idle >= self.IDLE_TIMEOUT_SECONDS:
                    logger.info("Idle timeout (%.0fs) — shutting down", idle)
                    break
                if not socket_path.exists():
                    # Unreachable without it; exit so the shell can start a new one
                    logger.warning("socket file was removed — shutting down")
                    break
            await self.stop()

    async def stop(self) -> None:
        """Stop serving and let start() return so the process exits."""
        lock, self._lock_file = self._lock_file, None
        if lock is None:
            return  # never started, or another stop() is already cleaning up
        self._stopping.set()
        if self._server:
            self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.llm.close(), timeout=2)

        # Only the lock holder owns these files
        self.config.get_socket_path().unlink(missing_ok=True)
        self.config.get_pid_path().unlink(missing_ok=True)
        lock.close()

        logger.info("ghst daemon stopped")


async def _run() -> None:
    """Run the daemon."""
    log_path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "ghst"
    log_path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path / "daemon.log"),
            logging.StreamHandler(),
        ],
    )
    # Silence noisy third-party loggers
    for name in ("hpack", "httpcore", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
    daemon = GhstDaemon()

    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        # Wake start() so it runs stop() and returns; if cleanup hangs, exit
        # anyway so the single-instance lock is released
        daemon._stopping.set()
        loop.call_later(5, os._exit, 0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)

    await daemon.start()


def main() -> None:
    """Entry point for `python -m ghst.daemon`."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
