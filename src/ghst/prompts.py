"""Prompt templates for all ghst request types.

All prompts instruct the LLM to return ONLY the completion/command/fix —
no explanation, no markdown, no commentary. Empty string if unsure.
"""

from __future__ import annotations

import platform


def _os_info() -> str:
    """Return a short OS identifier for the system prompt."""
    system = platform.system().lower()
    if system == "darwin":
        return "macOS"
    if system == "linux":
        try:
            import distro  # type: ignore[import-untyped]
            return f"Linux ({distro.name()})"
        except ImportError:
            return "Linux"
    return system


# Kept long and byte-stable on purpose: local servers cache the prompt prefix
# in fixed-size blocks, so a fixed system prompt is evaluated once, not per key.
AUTOCOMPLETE_SYSTEM = f"""\
You are a shell command autocomplete engine on {_os_info()}.
You receive some context and a partial command line. Reply with ONLY the text \
that should be appended to it, like inline ghost text in a terminal.
RULES:
- Output only the characters that come after the partial command
- Never repeat any part of the partial command, not even its last word
- Start with a space only when the next token is a new argument and the \
partial command does not already end with a space
- One line, one command. No explanation, no markdown, no surrounding quotes
- Prefer the user's own past commands, and the files, branches and scripts \
named in the context, over generic guesses
- Always use relative paths (never absolute like /Users/... or /home/...)
- If no useful continuation exists, output nothing

EXAMPLES (partial command => your reply)
git chec => kout main
git checkout  => main
git pu => sh
docker ps =>  -a
cd sr => c/
npm run  => dev
brew ins => tall
git log --on => eline
git commit -m "fix => : handle empty input"
tar -xzf archive.tar.gz =>  -C build/
ssh  => deploy@staging
kubectl get po => ds -n default"""


COMMAND_SYSTEM = f"""\
You are an expert shell assistant. The user is on {_os_info()}.
You help with shell commands — corrections, generation, and search.
RULES:
- Return ONLY the requested output (command, fix, etc.)
- NO explanations, NO markdown, NO commentary
- If unsure, return an empty string
- Never suggest commands that would be destructive without clear user intent
- Preserve the user's command style (quoting, flag style, etc.)"""


def autocomplete_system() -> str:
    return AUTOCOMPLETE_SYSTEM


def autocomplete_user(
    buffer: str,
    cwd: str,
    history: list[str],
    shell: str = "zsh",
    exit_status: int = 0,
    dir_listing: str = "",
    git_branch: str = "",
    git_dirty: bool = False,
    git_branches: list[str] | None = None,
    project_types: list[str] | None = None,
    active_env: str = "",
    history_matches: list[str] | None = None,
) -> str:
    recent = [h[:120] for h in history[-5:]]

    context_lines = [f"Context: {shell} shell in {cwd}"]
    if dir_listing:
        context_lines.append(f"Files here: {dir_listing}")
    if git_branch:
        status = " (dirty)" if git_dirty else ""
        context_lines.append(f"Git: on {git_branch}{status}")
        if git_branches:
            context_lines.append(f"Branches: {', '.join(git_branches[:8])}")
    if project_types:
        context_lines.append(f"Project: {', '.join(project_types)}")
    if active_env:
        context_lines.append(f"Env: {active_env}")
    if recent:
        context_lines.append("Recent commands:\n" + "\n".join(recent))
    if exit_status:
        context_lines.append(f"Last exit: {exit_status}")
    if history_matches:
        context_lines.append(
            "Past commands starting with the same text (most recent first; "
            "continue toward the most likely one):\n"
            + "\n".join(m[:120] for m in history_matches[:5])
        )

    context_block = "\n".join(context_lines)
    return f"""\
{context_block}

Complete: {buffer}"""


def proactive_system(session_buffer: str = "") -> str:
    base = COMMAND_SYSTEM
    if session_buffer:
        base += f"\n\nRecent session:\n{session_buffer}"
    return base


def proactive_user(
    cwd: str,
    history: list[str],
    last_command: str,
    last_output: str,
    shell: str = "zsh",
) -> str:
    hist_text = "\n".join(history[-5:]) if history else "(none)"
    return f"""\
Shell: {shell}
Working directory: {cwd}
Recent commands:
{hist_text}

Last command: {last_command}
Its output (last 50 lines):
{last_output}

The user's prompt is empty. Suggest the single most likely next command they would want to run.
Return ONLY the command. Return an empty string if nothing is clearly suggested."""


def nl_command_user(
    prompt: str,
    cwd: str,
    buffer: str = "",
    history: list[str] | None = None,
    shell: str = "zsh",
) -> str:
    hist_text = "\n".join((history or [])[-10:]) if history else "(none)"
    context = ""
    if buffer:
        context = f"\nPartial command already typed: {buffer!r}"
    return f"""\
Shell: {shell}
Working directory: {cwd}
Recent commands:
{hist_text}
{context}
User request: {prompt}

Generate ONLY the shell command. No explanation."""


def error_correction_user(
    failed_command: str,
    exit_status: int,
    stderr: str,
    cwd: str,
    shell: str = "zsh",
) -> str:
    return f"""\
Shell: {shell}
Working directory: {cwd}

Failed command: {failed_command}
Exit status: {exit_status}
Error output:
{stderr}

Return ONLY the corrected command. If you can't determine the fix, return an empty string."""


def history_search_user(
    query: str,
    history: list[str],
    shell: str = "zsh",
) -> str:
    hist_text = "\n".join(history)
    return f"""\
Shell: {shell}

User is searching their history for: {query}

Shell history (most recent last):
{hist_text}

Return a JSON array of the most relevant commands, ranked by relevance.
Format: [{{"command": "...", "score": 0.95}}, ...]
Return at most 10 results. Only include commands that match the user's intent.
If nothing matches, return an empty array: []"""
