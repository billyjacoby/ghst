"""Tests for the daemon request handling."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ghst.config import GhstConfig
from ghst.daemon import (
    GhstDaemon,
    RateLimiter,
    SessionBuffer,
    _ensure_leading_space,
    _strip_code_fences,
)


class TestEnsureLeadingSpace:
    def test_adds_space_between_word_and_flag(self) -> None:
        assert _ensure_leading_space("ffmpeg", "-i input.mp4") == " -i input.mp4"

    def test_no_space_when_suggestion_starts_with_space(self) -> None:
        assert _ensure_leading_space("git", " status") == " status"

    def test_no_space_when_buffer_ends_with_space(self) -> None:
        assert _ensure_leading_space("git ", "status") == "status"

    def test_empty_suggestion(self) -> None:
        assert _ensure_leading_space("git", "") == ""

    def test_empty_buffer(self) -> None:
        assert _ensure_leading_space("", "ls") == "ls"

    def test_pipe_after_space(self) -> None:
        # Buffer ends with non-word char (space after 'foo' would be in buffer)
        assert _ensure_leading_space("grep foo ", "| wc -l") == "| wc -l"

    def test_pipe_no_space(self) -> None:
        # `|` is in the trigger list, so space added
        assert _ensure_leading_space("grep foo", "| wc -l") == " | wc -l"


class TestStripCodeFences:
    def test_plain_text_unchanged(self) -> None:
        assert _strip_code_fences("hello world") == "hello world"

    def test_strips_fenced_block(self) -> None:
        assert _strip_code_fences("```\nls -la\n```") == "ls -la"

    def test_strips_fenced_with_language(self) -> None:
        assert _strip_code_fences("```bash\nls -la\n```") == "ls -la"

    def test_strips_surrounding_whitespace(self) -> None:
        assert _strip_code_fences("  ```\nls -la\n```  ") == "ls -la"


class TestRateLimiter:
    def test_allows_under_limit(self) -> None:
        rl = RateLimiter(rpm=5)
        for _ in range(5):
            assert rl.allow() is True

    def test_blocks_over_limit(self) -> None:
        rl = RateLimiter(rpm=3)
        for _ in range(3):
            assert rl.allow() is True
        assert rl.allow() is False


class TestSessionBuffer:
    def test_add_and_format(self) -> None:
        buf = SessionBuffer()
        buf.add("git status", "M src/main.py\nM src/test.py")
        result = buf.format_for_prompt()
        assert "git status" in result
        assert "src/main.py" in result

    def test_max_entries(self) -> None:
        buf = SessionBuffer()
        for i in range(25):
            buf.add(f"cmd{i}", f"output{i}")
        assert len(buf.entries) == 20

    def test_output_truncation(self) -> None:
        buf = SessionBuffer()
        long_output = "\n".join(f"line{i}" for i in range(50))
        buf.add("long-cmd", long_output)
        entry = buf.entries[0]
        assert entry.output.count("\n") < 50

    def test_empty_buffer(self) -> None:
        buf = SessionBuffer()
        assert buf.format_for_prompt() == ""


class TestDaemonRequestHandling:
    """Test daemon request routing with mocked LLM."""

    @pytest.fixture
    def daemon(self) -> GhstDaemon:
        config = GhstConfig()
        return GhstDaemon(config)

    @pytest.mark.asyncio
    async def test_reload_config(self, daemon: GhstDaemon) -> None:
        result = await daemon.handle_request({"type": "reload_config"})
        assert result["type"] == "reload_config"

    @pytest.mark.asyncio
    async def test_unknown_type(self, daemon: GhstDaemon) -> None:
        result = await daemon.handle_request({"type": "invalid"})
        assert result["type"] == "error"

    @pytest.mark.asyncio
    async def test_empty_nl_prompt(self, daemon: GhstDaemon) -> None:
        result = await daemon.handle_request({
            "type": "nl",
            "prompt": "",
            "cwd": "/tmp",
        })
        assert result["command"] == ""

    @pytest.mark.asyncio
    async def test_empty_error_correct(self, daemon: GhstDaemon) -> None:
        result = await daemon.handle_request({
            "type": "error_correct",
            "failed_command": "",
            "exit_status": 1,
            "stderr": "",
        })
        assert result["suggestion"] == ""

    @pytest.mark.asyncio
    async def test_empty_history_search(self, daemon: GhstDaemon) -> None:
        result = await daemon.handle_request({
            "type": "history_search",
            "query": "",
            "history": [],
        })
        assert result["results"] == []


async def test_health_reports_unreachable_server(tmp_path) -> None:  # noqa: ANN001
    from ghst.config import ProviderConfig

    config = GhstConfig(
        provider=ProviderConfig(api_base_url="http://127.0.0.1:9/v1", api_key="x"),
        config_path=tmp_path / "config.toml",
    )
    result = await GhstDaemon(config).handle_request({"type": "health"})
    assert result["type"] == "health"
    assert result["ok"] is False
    assert result["error"] == "cannot connect"


async def test_single_instance_and_clean_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import shutil
    import tempfile

    # Unix socket paths are limited to ~104 bytes, so pytest's tmp_path is too long
    runtime = tempfile.mkdtemp(prefix="ghst", dir="/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    config = GhstConfig(config_path=tmp_path / "config.toml")
    first = GhstDaemon(config)
    running = asyncio.create_task(first.start())
    for _ in range(100):
        if config.get_pid_path().exists() or running.done():
            break
        await asyncio.sleep(0.01)
    assert not running.done(), running.exception()
    assert config.get_pid_path().read_text() == str(os.getpid())

    # A second daemon gives up and leaves the first one's files alone
    await asyncio.wait_for(GhstDaemon(config).start(), timeout=2)
    assert config.get_socket_path().exists()

    # stop() makes start() return, so the process can exit
    await first.stop()
    await asyncio.wait_for(running, timeout=2)
    assert not config.get_socket_path().exists()
    assert not config.get_pid_path().exists()
    shutil.rmtree(runtime)


async def test_nl_command_strips_markdown_fence(tmp_path: Path) -> None:
    daemon = GhstDaemon(GhstConfig(config_path=tmp_path / "config.toml"))

    async def fenced(*_args: object, **_kwargs: object) -> str:
        return "```bash\ndu -sh .\n```"

    daemon.llm.complete_with_retry = fenced  # type: ignore[method-assign]
    result = await daemon.handle_request({"type": "nl", "prompt": "disk usage"})
    assert result["command"] == "du -sh ."
