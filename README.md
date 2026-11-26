# ghst — AI-powered shell plugin

LLM-powered ghost-text autocomplete, natural language commands, and semantic history search for zsh. Works with any terminal emulator that supports ANSI escapes — no terminal modifications needed.

## Features

- **Autocomplete** — Ghost text as you type: your best matching past command at once (atuin or zsh history), then the model's suggestion (which may extend that command, not replace it). Tab accepts it all, → one piece at a time.
- **Natural Language Commands** — Press Cmd+I (or Ctrl+G), describe what you want in English, get a shell command.
- **Runs locally or in the cloud** — a local server such as oMLX or Ollama, your ChatGPT Plus/Pro plan, OpenAI, or Anthropic. Set up with `ghst provider`.
- **History Search** — Search your shell history with natural language instead of substring matching (bind it with `history_search_hotkey`, e.g. `"^R"`).

## Install

### uv (recommended)

```bash
uv tool install ghst
```

### pipx

```bash
pipx install ghst
```

### pip

```bash
pip install ghst
```

Then run the setup wizard:

```bash
ghst init
exec zsh
```

The `init` wizard configures your LLM provider, adds shell integration to `.zshrc`, starts the daemon, and verifies the connection. `exec zsh` reloads your shell to activate it.

## Development Setup

```bash
git clone https://github.com/insprd/ghst.git
cd ghst
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
ghst init        # configure provider + inject zshrc
exec zsh         # reload shell to activate
```

> **Note:** In dev mode, you must activate the venv (`source .venv/bin/activate`) in each new shell for `ghst` to resolve to your local checkout. Alternatively, use `uv run ghst` without activating. The `eval "$(ghst shell-init zsh)"` line in your `.zshrc` handles this automatically once the venv is active.

## Recommended setup: local models on oMLX

On Apple Silicon this is the fastest setup we've found, and nothing leaves your Mac:

| Job | Model | Size | Speed |
|---|---|---|---|
| Ghost text | `mlx-community/Qwen2.5-Coder-1.5B-4bit` (base model, fill-in-the-middle) | ~0.9 GB | ~45ms median |
| Cmd+I (English → command) | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-8bit` | ~30 GB | ~200–600ms |

1. Install [oMLX](https://github.com/jundot/omlx) and download both models from its model page.
2. Run `ghst provider`, choose **Local server**, keep `http://localhost:8000/v1`, and accept the offer to reuse oMLX's API key.
3. Pick the two models above. ghst detects the base coder model and uses fill-in-the-middle for it.

Use the **base** Qwen2.5-Coder model for ghost text, not `-Instruct`. Chat-tuned small models
mostly repeat what you typed when asked to continue it; the base model of the same size
continues it. Measured on 14 commands that weren't in history:

| Ghost-text model | Median | No suggestion |
|---|---|---|
| Qwen2.5-Coder-1.5B **base**, fill-in-the-middle | 44ms | 0 of 14 |
| Qwen2.5-Coder-1.5B-Instruct, chat | ~63ms | about half |
| Qwen2.5-Coder-3B base | 66ms | 2 of 14, and less accurate |
| Qwen2.5-Coder-7B base | 167ms | 0 of 14 |
| Qwen3-Coder-30B-A3B-Instruct, chat | 182ms | 0 of 14, the most accurate |

With less memory, use the 1.5B base model for both jobs. For the best suggestions at about 4× the
latency, use the 30B for ghost text too. Ollama and LM Studio work the same way at their own URLs.

## Usage

### Autocomplete

Just start typing. After a brief pause, ghost text appears with a suggestion:

```
$ git sta‹tus --short›
```

- **Tab** (or **Shift+→**) — Accept the full suggestion
- **→** — Accept the next word or path segment (`src/`, then `ghst/`, …)
- **Esc** — Dismiss

### Natural Language Commands (Cmd+I or Ctrl+G)

```
$ █                          # Press Cmd+I
ghst> find python files modified this week
$ find . -name "*.py" -mtime -7█
```

The generated command is placed in your buffer for review — **never auto-executed**. Press **Ctrl+Z** to undo and restore your original buffer.

Terminals don't pass Cmd+key to the shell by default, so map Cmd+I to the key code ghst listens for (`ESC [105;9u`):

- **Ghostty** (`~/.config/ghostty/config`): `keybind = cmd+i=text:\x1b[105;9u`
- **Rex** (`~/.config/rex/init.lua`): `rex.bind("cmd+i", "com.superlogical.terminal.write", { data = "\27[105;9u" })`

Ctrl+G works without any mapping unless your terminal uses it, as zellij-style Rex configs do for "locked" mode.

### History Search (`history_search_hotkey`, unbound by default)

```
ghst history> that docker command for postgres
  → docker exec -it postgres-dev psql -U admin -d myapp
    docker run -d --name postgres-dev -e POSTGRES_PASSWORD=secret postgres:15
```

### Cheat Sheet (Ctrl+/)

Press Ctrl+/ at any time to see a quick reference of all shortcuts.

## Configuration

Config file: `~/.config/ghst/config.toml`

```toml
[provider]                              # easiest to set with `ghst provider`
name = "openai"                         # "openai" (OpenAI or a local server), "anthropic", "chatgpt"
api_base_url = "http://localhost:8000/v1"   # oMLX; leave out for OpenAI itself
api_key = "..."                         # Or set GHST_API_KEY env var
model = "Qwen3-Coder-30B-A3B-Instruct-8bit"       # Cmd+I: English → command
autocomplete_model = "Qwen2.5-Coder-1.5B-4bit"    # ghost text: fast base model

[ui]
autocomplete_delay_ms = 100             # Debounce delay (ms)
autocomplete_min_chars = 2              # Min chars before autocomplete fires
nl_hotkey = "^G ^[[105;9u"              # Cmd+I / Ctrl+G (space-separated keys)
history_search_hotkey = ""              # History search hotkey ("" = unbound, "^R" to take Ctrl+R)
history_source = "auto"                 # Past commands: "auto" (atuin, else zsh), "atuin", "zsh", "off"
autocomplete_mode = "auto"              # "chat", or "fim" (fill-in-the-middle) for base coder models
history_match_limit = 5                 # How many past commands to use
instant_history = true                  # Show the best past command before the model answers
autocomplete_max_tokens = 32            # Longest ghost-text reply
autocomplete_timeout_ms = 3000          # Give up on a ghost-text request after this long
rate_limit_rpm = 240                    # Most model requests per minute
ghost_color = "#6e7681"                 # Ghost text color (hex or 256-color index)
accent_color = "#79c0ff"                # Prompt accent color (ghst>)
success_color = "#56d364"               # Success indicator (✓)
warning_color = "#e3b341"               # Warning indicator (⚠)
error_color = "#f85149"                 # Error indicator (✗)
```

See `config/default.toml` for all available settings.

### Use your ChatGPT plan (Sign in with ChatGPT)

Instead of an API key, ghst can run on your ChatGPT Plus or Pro plan:

```bash
ghst login            # Continue with ChatGPT in the browser, then pick models
ghst status           # account, "Using ChatGPT plan", server check
ghst model list       # models your plan offers
ghst logout           # revoke and remove the tokens
```

ghst uses OpenAI's [open-source plan usage flow](https://developers.openai.com/siwc/token-sharing-open-source):
requests count against your plan's shared allowance, and you can see or cap ghst's
usage at [ChatGPT settings → Usage](https://chatgpt.com/settings/usage). Tokens are
stored in `~/.config/ghst/chatgpt.json` (owner-only). When the allowance runs out,
requests stop; ghst never switches to paid API billing.

## CLI Commands

| Command | Description |
|---|---|
| `ghst provider` | Pick a provider with the arrow keys and connect: local server (oMLX, Ollama, LM Studio), ChatGPT plan, OpenAI or Anthropic. Ends with a timed test suggestion |
| `ghst provider set <local\|chatgpt\|openai\|anthropic>` | Same, skipping the menu |
| `ghst model` | Pick the ghost-text and Cmd+I models from the server's list (type to filter) |
| `ghst model list` / `ghst model set <name>` | List models, or set one directly |
| `ghst login` / `ghst logout` | Sign in with ChatGPT to use your plan / sign out |
| `ghst status` | Daemon, provider, server check, ChatGPT account, last error |
| `ghst help settings` | Every setting with what it does and its current value |
| `ghst set` / `get` / `reset <key>` | Change, show or reset one setting |
| `ghst init` | First-time setup: shell line, provider, daemon |
| `ghst start` / `ghst stop` | Start or stop the daemon (normally automatic) |
| `ghst help [command]` | Overview, or details for one command |

ghst remembers each provider's settings under `[profiles.<name>]` in the config,
so switching providers and back keeps your keys, URLs and models.

## Architecture

```
zsh (ZLE widgets)  ←── Unix domain socket ──→  ghstd (Python daemon)
  autocomplete.zsh                               daemon.py (asyncio)
  nl-command.zsh                                 llm.py (httpx)
  history-search.zsh                             safety.py, config.py
```

The shell side sends JSON requests over a Unix socket; the daemon routes them to the LLM and returns suggestions. The daemon runs in the background, auto-starts on first use, and auto-restarts when Python source files change (for seamless development).

## Context Awareness

Autocomplete suggestions are informed by your full working environment — not just what you've typed. Every request includes:

| Context | Example | What it helps with |
|---|---|---|
| **Directory listing** | `src/  tests/  README.md` | `cd`, `cat`, `vim` suggest real file/folder names |
| **Git branch & status** | `on main (dirty)` | `git commit`, `git push`, `git stash` awareness |
| **Git branches** | `feature/auth, develop, next` | `git checkout`, `git merge` suggest real branch names |
| **Project type** | `python, docker` | Suggests `uv run pytest` instead of `npm test` |
| **Active environment** | `venv:.venv` | Knows `python` resolves to the venv, not system |
| **Recent commands** | last 5 from history | Learns your patterns within the session |
| **Exit status** | `0` or `1` | Knows if the last command failed |

All context is gathered locally and cached (5s TTL) to avoid redundant work during rapid typing. Project type is detected from marker files in the current directory:

`package.json` · `pyproject.toml` · `Cargo.toml` · `go.mod` · `Gemfile` · `Makefile` · `Dockerfile` · `docker-compose.yml` · `CMakeLists.txt` · `pom.xml` · `build.gradle` · `justfile` · `Taskfile.yml`

## Privacy

ghst sends the following data to your configured LLM provider:

- **Current buffer** (what you've typed so far)
- **Current working directory** and **directory listing** (non-hidden files/folders)
- **Recent shell history** (last 5-10 commands)
- **Git context** — current branch, dirty status, local branch names
- **Project type** — detected from marker files (e.g. `package.json`, `pyproject.toml`, `Cargo.toml`)
- **Active environment** — virtualenv name, conda env, `NODE_ENV`

ghst does **NOT** send:

- File contents (unless they appear in terminal output)
- Hidden/dotfiles or environment variables
- SSH keys, passwords, or other credentials

All sensitive data (API keys, passwords, tokens) is automatically stripped from history and terminal output before sending to the LLM.

## Roadmap

Planned features for future releases:

- **Error Correction** — Auto-suggest fixes as ghost text when a command fails
- **Proactive Suggestions** — Read the last command's output and suggest the next command on an empty prompt
- **Bash & Fish Support** — Extend autocomplete and NL commands beyond zsh
- **Homebrew Installation** — `brew install ghst` via a Homebrew tap

## Development

```bash
uv run pytest              # Run tests
uv run pytest -v           # Verbose
uv run ruff check src/     # Lint
uv run basedpyright src/ghst/  # Type check
```

The daemon auto-reloads during development: every 30 commands, the shell checks if any `.py` source file is newer than the running daemon and restarts it if so. No manual `ghst stop && ghst start` needed after editing Python code.

## License

MIT
