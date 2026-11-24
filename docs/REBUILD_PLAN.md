# ghst rebuild plan

Status: superseded by `docs/PRD.md` (2026-11-24). Kept for its wire protocol design (section 6) and history.
Date: 2026-10-02
Scope: a first-principles redesign of ghst, delivered in phases so that every phase ships something usable and the current Python version keeps working until the new one replaces it.

---

## 1. Summary

ghst puts LLM features inside the shell's line editor: ghost-text autocomplete, natural language to command (Ctrl+G), natural language history search (Ctrl+R), and error correction. The current design (Python asyncio daemon + zsh ZLE widgets over a Unix socket) is the right *shape*. Most of the pain is in the details of the shell side: rendering behind ZLE's back, a subprocess spawned on every keystroke, and request lifecycle bookkeeping done in zsh.

The rebuild keeps the two-process shape and changes five things:

1. **Render through the line editor's own API** (`POSTDISPLAY` + `region_highlight` in zsh) instead of writing escape codes to `/dev/tty`.
2. **A shell-native wire protocol** over one persistent socket per shell session, so the shell never forks to build or parse a message.
3. **The daemon owns request lifecycle** (IDs, supersession, cancellation, timeouts). The shell just drops responses that are not current.
4. **A local tier before the LLM**: a history and filesystem ranker answers most keystrokes in under 10 ms, and the model is consulted only when that tier is not confident.
5. **A single native binary** (Rust) for CLI and daemon, with thin per-shell adapters embedded in it. This makes zsh, fish, and bash support realistic.

### Not a Starship plugin

Starship renders the prompt once, before input. It never sees keystrokes and cannot draw after the cursor, so it cannot host ghost text, Ctrl+G, or Ctrl+R. ghst belongs in the line editor layer, next to zsh-autosuggestions and Atuin. Starship gets an optional status segment (Appendix A) and nothing more.

---

## 2. Goals and non-goals

### Goals

- Keystroke handling on the shell side stays under **2 ms** of work, with no forks.
- Local suggestions appear in **under 10 ms** after the debounce fires. LLM suggestions p50 under **400 ms** on a fast-tier model.
- Zero rendering corruption under line wrap, multi-line buffers, terminal resize, and `zle reset-prompt`.
- Works offline and with the API down (local tier only, no error spam).
- One-command install with no runtime dependency: no Python, no `socat`, no venv.
- zsh first, then fish, then bash (via ble.sh), all served by the same daemon.
- Never auto-execute. Never send secrets to a provider.

### Non-goals

- Owning the PTY or replacing the terminal (see section 13, "Rejected alternatives").
- A chat UI or an agent that runs commands.
- Syncing history across machines (Atuin already does this; we integrate with it instead).
- Windows or PowerShell in this plan.

---

## 3. What the current code tells us

These findings drive the design. Line references are to the current tree.

| Finding | Evidence | Consequence |
|---|---|---|
| Ghost text is drawn with save/restore cursor escapes to `/dev/tty`, invisible to ZLE | `src/ghst/shell/autocomplete.zsh:35` | Any ZLE redraw, wrap, or resize can corrupt or orphan it. Many fix commits (`4590161`, `7a5237a`, `3d17dac`) are consequences of this. |
| `python3` is spawned per debounced keystroke to build JSON | `autocomplete.zsh:56`, and five more spawns in `nl-command.zsh` | 30 to 50 ms per request plus a hard dependency on a system Python. Undercuts the reason for using `zsocket`. |
| Responses are parsed with string slicing on `"suggestion": "` | `autocomplete.zsh` `__ghst_on_response` | Breaks on escaped quotes, unicode escapes, or key reordering. |
| A new socket connection per request, and stale `zle -F` FDs are cleaned up by hand in several places | `autocomplete.zsh`, `nl-command.zsh`, commits `f5d9ba9`, `27710d2` | Request lifecycle lives in the shell, which is the hardest place to get it right. |
| The daemon implements `error_correct` and proactive suggestions, but nothing in the shell sends them | `daemon.py:166`, `daemon.py:185`; no caller in `src/ghst/shell/` | Two advertised features are effectively unwired. Output capture is an unsolved problem (section 8.4). |
| Context gathering shells out to `git` per request, with a cache | `context.py:72` | Fine, but should be keyed on cwd and invalidated on change, with the shell telling the daemon when cwd changes. |
| Auto-reload compares `.py` mtimes to the PID file | `ghst.zsh` | Only needed because the daemon is source code. A versioned binary with a handshake replaces it. |
| History search ships up to 500 history lines to the LLM per query | `config.py:80` | Expensive and slow. A local index plus model reranking is cheaper and better. |

Things that are right and carry over unchanged: never auto-execute, secret sanitization, circuit breaker, idle exit, rate limiting, fill-in-the-middle style autocomplete prompt, inlined shell init (`eval "$(ghst init zsh)"`), configurable colors, Ctrl+Z undo of NL results.

---

## 4. Target architecture

```
┌──────────────────────── shell process (zsh / fish / bash) ─────────────────────────┐
│  adapter (~200 lines, embedded in the binary, printed by `ghst init <shell>`)      │
│   • keystroke hook → debounce → write request frame                                │
│   • one persistent socket fd, watched with zle -F (or fish/bash equivalent)        │
│   • renders via POSTDISPLAY + region_highlight                                     │
│   • precmd/preexec → session events (cwd, exit, duration, command)                 │
└───────────────────────────────┬────────────────────────────────────────────────────┘
                                │  Unix socket, framed protocol (section 6)
┌───────────────────────────────▼────────────────────────────────────────────────────┐
│  ghst daemon (Rust, tokio)                                                         │
│                                                                                    │
│  ┌────────────┐   ┌────────────────────┐   ┌───────────────────────────────────┐   │
│  │ sessions   │──▶│ request scheduler  │──▶│ completion pipeline               │   │
│  │ (per shell)│   │ ids, supersede,    │   │  tier 0: history + fs + git ranker│   │
│  └─────┬──────┘   │ cancel, deadlines  │   │  tier 1: local model (optional)   │   │
│        │          └────────────────────┘   │  tier 2: remote LLM               │   │
│        ▼                                   └──────────────┬────────────────────┘   │
│  ┌────────────┐   ┌────────────────────┐                  │                        │
│  │ context    │   │ history store      │◀─────────────────┘                        │
│  │ cache      │   │ (SQLite, FTS5)     │   ┌───────────────────────────────────┐   │
│  │ (per cwd)  │   └────────────────────┘   │ providers: anthropic, openai,     │   │
│  └────────────┘                            │ openai-compatible, ollama         │   │
│  ┌────────────┐   ┌────────────────────┐   │ circuit breaker, rate limit, cache│   │
│  │ safety     │   │ config + reload    │   └───────────────────────────────────┘   │
│  └────────────┘   └────────────────────┘                                           │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### Process model

- One daemon per user, socket at `$XDG_RUNTIME_DIR/ghst/ghst.sock`, falling back to `$TMPDIR/ghst-$UID/ghst.sock`. The directory is mode `0700`, and the daemon checks the peer UID with `getpeereid`/`SO_PEERCRED`.
- Started on demand by the adapter if the socket is dead (spawn `ghst daemon --detach`, which returns immediately). Optional `ghst service install` writes a launchd agent or systemd user unit for people who want it always on.
- Idle exit after 30 minutes with no connected sessions (not merely no requests).
- One persistent connection per shell. When the connection closes, the session ends.

### Repository layout (Cargo workspace)

```
crates/
  ghst-proto/     # frame codec, message types, shared by daemon and tests
  ghst-core/      # sessions, scheduler, pipeline, ranker, context, safety, history store
  ghst-llm/       # provider clients, prompts, circuit breaker, response cache
  ghst/           # the binary: CLI (clap) + daemon entry point
shell/
  zsh/ghst.zsh    # embedded with include_str!
  fish/ghst.fish
  bash/ghst.bash  # requires ble.sh
tests/
  shell/          # zpty / expect-driven end-to-end tests
  eval/           # completion quality eval set
legacy/           # current Python implementation until cutover
```

Key crates: `tokio`, `serde`, `reqwest` (rustls, HTTP/2), `rusqlite` (bundled, FTS5), `clap`, `toml`, `tracing`, `notify` (config reload), `nucleo` or a hand-rolled scorer for fuzzy ranking.

---

## 5. Key decisions

### D1. Rust for the daemon and CLI

Why: startup is instant, so `ghst init zsh` in `.zshrc` costs nothing; there is one static binary with no Python or venv; memory stays low for a process that may run all day; strong typing for the protocol and scheduler; and easy cross-compilation for distribution.

Cost: a rewrite of about 2,000 lines of Python and the loss of Python's quick iteration on prompts. Mitigation: prompts live in data files (embedded TOML templates) that can be overridden from config without rebuilding.

Alternative kept open: if Phase 0 shows the Python daemon is good enough once the shell side is fixed, we can stop after Phase 0 and package the Python daemon with a single-file bundler. Decide at the Phase 0 exit review.

### D2. The line editor is the integration layer

Rendering, keybindings, and buffer edits all go through the editor's API. No escape codes are written to `/dev/tty` for anything ZLE has drawn or will redraw. The one exception is the transient spinner during a blocking NL request, which is drawn in `POSTDISPLAY` too (plain text, colored via `region_highlight`).

### D3. A protocol the shell can produce without forking

A framed, length-prefixed text format (section 6). zsh can build frames with parameter expansion alone. The daemon does all parsing, escaping, and validation.

### D4. The daemon owns the request lifecycle

Every request carries an ID and a session ID. A newer `complete` request in the same session supersedes the older one, and the daemon cancels its upstream HTTP call. The shell keeps exactly one variable, the current request ID, and ignores any response that does not match.

### D5. Tiered completion

Most good completions are already in history, the filesystem, or git. The LLM is a fallback and an enricher, not the first resort. This is the single biggest win for latency, cost, and offline behavior.

### D6. Own history store, with Atuin as an optional source

The adapter reports each command with cwd, exit status, duration, and timestamp. The daemon stores it in SQLite with FTS5. If Atuin is installed, `ghst` can read its database instead of (or in addition to) its own. On first run, `HISTFILE` is imported.

---

## 6. Wire protocol

### Framing

Every message is one frame:

```
<kind> <id> <nfields>\n
<key>:<bytelen>:<bytes>\n      (repeated nfields times)
```

- `kind` is an ASCII word (`hello`, `complete`, `nl`, ...).
- `id` is an ASCII token chosen by the sender (`$RANDOM$RANDOM` in zsh is fine; the daemon only needs per-session uniqueness).
- `bytelen` is the length in bytes. zsh gets it with `setopt localoptions nomultibyte; ${#value}`. Values can contain anything, including newlines and NULs, with no escaping.

Example from zsh:

```zsh
__ghst_field() {  # key value
    setopt localoptions nomultibyte
    REPLY+="$1:${#2}:$2"$'\n'
}

__ghst_send_complete() {
    local REPLY="complete $__GHST_REQ 2"$'\n'
    __ghst_field buffer "$BUFFER"
    __ghst_field cursor "$CURSOR"
    print -rn -u $__GHST_FD -- "$REPLY"
}
```

Responses use the same framing. The shell parses them with a small `read -k` loop (or `sysread` from `zsh/system`), reading the header line and then exactly `bytelen` bytes per field. That is a pure-zsh routine of about 25 lines.

### Messages

Shell to daemon:

| kind | fields | notes |
|---|---|---|
| `hello` | `shell`, `shell_version`, `adapter_version`, `pid`, `tty`, `term` | First frame on connect. Creates a session. |
| `session` | any of `cwd`, `exit`, `duration_ms`, `last_command`, `virtual_env`, `columns` | Sent from precmd/chpwd. Only changed fields are sent. |
| `command_start` | `command`, `cwd`, `ts` | From preexec. |
| `complete` | `buffer`, `cursor` | Per debounced keystroke. Everything else comes from the session. |
| `accept` | `suggestion_id`, `accepted` (`full`, `word`, `none`) | Feedback for ranking and evals. |
| `nl` | `prompt`, `buffer` | Ctrl+G. |
| `history_search` | `query` | Ctrl+R. |
| `fix` | (none) | Explicit "fix last command" hotkey; uses session state. |
| `cancel` | `target_id` | ESC during NL or history search. |
| `ping` | (none) | Liveness check. |

Daemon to shell:

| kind | fields | notes |
|---|---|---|
| `welcome` | `daemon_version`, `session_id`, `config_epoch`, plus UI settings (colors, keys) | If `adapter_version` does not match, includes `action:restart` and the adapter re-runs `ghst init`. |
| `suggestion` | `for_id`, `suggestion_id`, `text`, `source` (`history`, `fs`, `local`, `llm`) | `text` is only the part to append after the cursor. |
| `progress` | `for_id`, `text` | Streaming partial NL output, optional. |
| `command` | `for_id`, `text`, `warning`, `explanation` | NL result. |
| `results` | `for_id`, `count`, `item.0` ... `item.N` | History search results. |
| `config` | changed UI settings | Pushed to every session on config reload. |
| `error` | `for_id`, `code`, `message` | `code` is machine-readable (`rate_limited`, `provider_down`, `bad_request`). |

### Versioning

`adapter_version` and `daemon_version` are both the crate version. The protocol carries a separate integer `proto` in `hello`/`welcome`. A mismatch is never fatal: the daemon answers in the older protocol when it can, and otherwise asks for a restart.

---

## 7. Daemon internals

### 7.1 Sessions

`Session { id, shell, cwd, last_exit, last_command, last_duration, env_hints, columns, recent: VecDeque<CommandRecord> }`. Updated by `session` and `command_start` frames. The per-keystroke `complete` frame is tiny because the session already holds the context.

### 7.2 Scheduler

- Per session, at most one in-flight `complete`. A new one cancels the old (`tokio::select!` with a cancellation token; dropping the `reqwest` future aborts the HTTP/2 stream).
- Deadlines: `complete` 1.5 s, `nl` 15 s, `history_search` 8 s, `fix` 8 s. A deadline miss sends an `error` frame with `code:timeout`.
- Global concurrency and rate limits come from config (default 60 LLM calls per minute, shared across sessions).

### 7.3 Context cache

Keyed by cwd. Holds git branch, dirty state, recent branches, project type markers, and a truncated directory listing. Invalidated by a filesystem watcher on the directory and `.git/HEAD`, with a 5 s TTL as backstop. Gathered off the request path when a `session` frame reports a new cwd, so it is usually warm by the first keystroke.

### 7.4 Safety

Ported from `safety.py` and extended:

- Secret redaction before any provider call: known token formats (AWS, GitHub, Anthropic, OpenAI, Slack, JWT, private key blocks), `KEY=value` style assignments where the key looks sensitive, and URLs with credentials.
- A configurable blocklist of commands whose text never leaves the machine (`pass`, `op`, `gpg`, `ssh-keygen`, ...). Those commands are still stored locally for the history tier.
- Dangerous-command classification (`rm -rf /`, `dd of=/dev/`, `mkfs`, fork bombs, `chmod -R 777 /`, `curl | sh`) attached as `warning` on NL and fix results.
- A `privacy = "local-only"` mode that disables tier 2 entirely.

### 7.5 Providers

A trait `Provider { complete(prompt, opts, cancel) -> Stream<Chunk> }` with implementations for Anthropic (prompt caching on the system block), OpenAI, any OpenAI-compatible base URL, and Ollama. Model names use aliases, not dated snapshots. Separate model settings per feature: `autocomplete_model` (fast), `model` (NL, fix, history rerank).

The circuit breaker (3 failures, 30 s cooldown, probe), response cache (keyed on normalized prompt), and latency tracking are ported from `llm.py`.

---

## 8. Features

### 8.1 Autocomplete pipeline

On `complete(buffer, cursor)`:

1. **Gate.** Skip if the cursor is not at the end of the buffer, the buffer is shorter than `min_chars`, or the buffer is in a blocklisted command.
2. **Tier 0, local ranker (target under 10 ms).**
   - Prefix matches from history, scored by frecency, with a boost for the same cwd, the same git repo, success (exit 0), and recency in this session.
   - Path completion for the token under the cursor, from the cached directory listing.
   - Git-aware completion for `git checkout`/`switch`/`branch`/`push` arguments from cached branches.
   - Output: the best candidate and a confidence score.
3. **Emit early.** If the confidence is at or above `local_threshold`, send the `suggestion` now with `source:history` (or `fs`).
4. **Tier 1 or 2 (optional).** If confidence is below threshold, or the buffer is long and novel, call the local model (if configured) or the remote fast model using the fill-in-the-middle prompt with session context. If the model's answer differs from what was shown, send a replacement `suggestion` for the same `for_id`. The shell swaps it in only if the user has not typed since.
5. **Post-process.** Ported from `daemon.py`: strip code fences, fix leading spaces (`_ensure_leading_space`), turn absolute paths inside cwd into relative ones, and drop suggestions that only repeat the buffer or exceed 200 characters.
6. **Learn.** `accept` frames update a small per-prefix acceptance table that feeds the tier 0 score.

### 8.2 Natural language command (Ctrl+G)

- The adapter enters a mode using recursive-edit (kept from the current design) with autocomplete disabled, shows a `POSTDISPLAY` label, and sends `nl` on Enter.
- The daemon builds a prompt with session context plus the five most relevant history entries for the request (from FTS5), and streams the answer. The shell shows `progress` text dimmed in `POSTDISPLAY` until `command` arrives.
- The result replaces `BUFFER`. The original is saved for Ctrl+Z undo. If there is a `warning`, it is shown in `POSTDISPLAY` with the warning color.
- ESC sends `cancel` and restores the buffer.

### 8.3 History search (Ctrl+R)

- Local first: FTS5 search plus fuzzy match over the history store, top 50 candidates.
- If the query looks like natural language (several words, no shell metacharacters) and an LLM is available, rerank the top 50 with the main model and return the top 10. Only 50 commands are sent, after redaction, instead of 500.
- Results are shown as a numbered list in `POSTDISPLAY`. Up/down or a number picks one; Enter places it in `BUFFER`.

### 8.4 Error correction

Currently unwired. The hard part is getting stderr without breaking interactive programs.

Plan, in order of preference:

1. **v1, no output capture.** After a non-zero exit, the daemon gets `last_command`, `exit`, cwd, and context from the session. For exit 127 (`command not found`), use tier 0 first (a typo of a known command, or a binary from history) and the LLM second. For other failures, nothing is shown automatically. A hotkey (default Ctrl+X Ctrl+F) sends `fix` and puts the suggestion in the buffer for review.
2. **v1.1, terminal-assisted capture, opt-in.** When the terminal can report screen contents, fetch the last N lines on `fix`: `tmux capture-pane -p -S -50` inside tmux, `kitty @ get-text` in kitty, `wezterm cli get-text` in WezTerm. The daemon picks the method from `TERM_PROGRAM`/`TMUX` in the `hello` frame. Redaction runs before anything leaves the machine.
3. **Explicitly not doing:** redirecting stderr through `tee` in preexec. It breaks tty detection, colors, and progress bars in too many tools.

Proactive suggestions (a ghost suggestion on an empty prompt after a command) are limited to tier 0 in v1, for example suggesting `git push` after a successful `git commit` based on history sequences. The LLM version stays behind a flag until output capture is solved.

---

## 9. Shell adapters

### 9.1 zsh (primary)

Responsibilities, in about 200 to 250 lines:

- `zmodload zsh/net/socket zsh/system zsh/zle`, connect once with `zsocket`, send `hello`, watch the fd with `zle -F __ghst_on_frame`. On EOF or error, mark disconnected and retry connect at the next precmd (at most once per 5 s).
- Wrap `self-insert`, `backward-delete-char`, and friends with a single widget that updates state and arms the debounce. The debounce timer uses `zle -F` on a `sleep` fd (as today), or `sched` if testing shows it is cheaper.
- Rendering:

  ```zsh
  __ghst_show() {  # $1 = text to show after the cursor
      __ghst_hide
      [[ -z $1 ]] && return
      POSTDISPLAY=$1
      local start=$(( ${#BUFFER} ))
      region_highlight+=("P$start $(( start + ${#POSTDISPLAY} )) fg=$__GHST_GHOST_FG memo=ghst")
  }
  __ghst_hide() {
      POSTDISPLAY=
      region_highlight=( ${region_highlight:#*memo=ghst} )
  }
  ```

  `memo=` needs zsh 5.9. On older zsh, fall back to removing entries by recorded index.
- Keybindings: Right arrow or End accepts all, Shift+Right or Alt+F accepts one word, Tab accepts if a suggestion is visible and otherwise falls through to normal completion. Each sends an `accept` frame.
- Hooks: `precmd` sends `session` (exit, duration, cwd if changed), `preexec` sends `command_start`, `chpwd` sends `session(cwd)`.
- Compatibility: detect zsh-autosuggestions and syntax-highlighting plugins. If zsh-autosuggestions is loaded, either disable it with a notice or run in "LLM-only" mode where ghst fills in only when zsh-autosuggestions has nothing. Order our `region_highlight` entries after zsh-syntax-highlighting's.

### 9.2 fish (phase 6)

fish has no public API for custom autosuggestions. Options, to be evaluated in a spike:
- Use `commandline -f` plus a custom `fish_postexec`/keypress handler and render via `commandline --insert` with selection highlighting. Probably not good enough for ghost text.
- Ship NL, history search, and fix for fish first (they do not need ghost text), and do autocomplete only if a clean method exists.

### 9.3 bash (phase 6)

Ghost text needs ble.sh (`ble/complete/auto-complete` hooks). Without ble.sh, bash gets NL (`bind -x` on Ctrl+G editing `READLINE_LINE`), history search, and fix only.

---

## 10. CLI

```
ghst init <shell>            # print adapter code (eval in rc file)
ghst setup                   # interactive wizard: provider, key, rc line, test call
ghst daemon [--detach]       # run the daemon
ghst status                  # daemon pid, version, sessions, provider health, latency p50/p95
ghst doctor                  # checks: socket, zsh version, region_highlight memo, plugin conflicts,
                             #   provider reachability, key validity, terminal capture method
ghst stop | restart
ghst config [edit|get|set|reset|defaults|path]
ghst model [set <alias>] | ghst provider [set <name>]
ghst history import [--atuin|--histfile PATH] | ghst history stats
ghst service install|uninstall   # launchd / systemd user unit
ghst eval [--suite PATH]     # run the completion eval set against current config
```

The current command names (`start`, `stop`, `status`, `model`, `provider`, `set`, `get`, `reset`, `defaults`) keep working as aliases during the transition. `shell-init` stays an alias of `init`.

---

## 11. Configuration

Keep `~/.config/ghst/config.toml` and its existing keys so current configs load unchanged. New keys:

```toml
[provider]
name = "anthropic"
model = "claude-sonnet-4-5"
autocomplete_model = "claude-haiku-4-5"
api_key_cmd = "op read op://dev/anthropic/key"   # alternative to api_key in plain text

[local]
enabled = true
local_threshold = 0.75     # tier 0 confidence needed to skip the LLM
model = ""                 # e.g. "ollama:qwen2.5-coder:1.5b"; empty disables tier 1

[privacy]
mode = "default"           # "default" | "local-only"
never_send = ["pass", "op", "gpg", "vault"]

[history]
source = "ghst"            # "ghst" | "atuin" | "both"

[error_correction]
auto_on_127 = true
capture = "auto"           # "auto" | "off"
```

The API key can come from `api_key`, `api_key_cmd`, or `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`, in that order of precedence (environment last, so it never silently overrides config). The daemon watches the file and pushes `config` frames on change. `reload_config` goes away.

---

## 12. Distribution

- Release builds with `cargo-dist`: macOS arm64/x86_64 and Linux x86_64/arm64 (musl). Artifacts: tarballs, a shell installer, and a Homebrew tap formula.
- A Nix flake exposing `packages.default` and a Home Manager module (`programs.ghst.enable`, settings rendered to TOML, zsh integration line added automatically).
- PyPI: the final Python release (0.6.x) prints a deprecation notice that points to the new install. Optionally publish the Rust binary as a PyPI wheel with `maturin` (bin bindings) so `uv tool install ghst` keeps working.
- CI: `cargo nextest run`, `clippy -D warnings`, `rustfmt --check`, shell e2e tests on macOS and Linux runners against zsh 5.8 and 5.9, `actionlint`, and `zsh -n` syntax checks on the zsh adapter (shellcheck does not support zsh).

---

## 13. Testing strategy

| Layer | What | How |
|---|---|---|
| Protocol | Frame encode/decode, arbitrary bytes, truncation, oversize | Unit tests plus `proptest` round trips in `ghst-proto` |
| Ranker | Frecency, cwd boost, typo correction | Unit tests with fixed history fixtures |
| Scheduler | Supersession, cancel, deadlines | `tokio::test` with a fake provider that sleeps and records cancellation |
| Providers | Request shape, streaming parse, error mapping | `wiremock` fixtures recorded from real responses |
| Safety | Redaction and danger detection | Table tests ported from `tests/test_safety.py` and extended |
| zsh adapter | Ghost text appears, accept works, no corruption on wrap/resize, ESC cancels, daemon down does not hang | `zsh/zpty` harness driving a real zsh against the real daemon with a fake provider; assert on `BUFFER`, `POSTDISPLAY`, and `region_highlight` via a debug widget that dumps state |
| Latency | Keystroke widget cost, tier 0 time, end-to-end p50/p95 | Criterion benches for the ranker; a zpty harness that types at 15 cps and records timings |
| Quality | Is the completion right? | `tests/eval/` with about 200 cases (buffer, cwd fixture, history, expected completions or a regex). `ghst eval` reports exact match, prefix-accept rate, and cost per 1,000 requests per model |

Every rendering bug fixed in the Python version (see the `fix:` commits) becomes a zpty regression test before Phase 2 is called done.

---

## 14. Observability

- `tracing` logs to `$XDG_STATE_HOME/ghst/daemon.log`, rotated, default level `info`. Request bodies are never logged above `trace`, and they are redacted even then.
- Metrics held in memory and shown by `ghst status`: requests by tier, tier 0 hit rate, accept rate by source, LLM latency p50/p95, tokens and estimated cost today, circuit breaker state.
- `ghst doctor` is the first support tool: it prints a pass/fail checklist with fix hints.

---

## 15. Phased delivery

Each phase has exit criteria. Do not start the next phase until they are met.

### Phase 0: fix the shell side in the current codebase (1 to 2 weeks)

Valuable on its own, and it de-risks the rendering approach before any Rust is written.

1. Spike: `POSTDISPLAY` + `region_highlight` with `memo=ghst` on zsh 5.9 macOS in Ghostty, iTerm2, and Terminal.app. Check wrap, multi-line buffer, resize, `reset-prompt`, and coexistence with zsh-syntax-highlighting.
2. Replace `/dev/tty` ghost text drawing with that method, if the spike passes.
3. Replace per-keystroke `python3` JSON building with the framed protocol (section 6), with the Python daemon accepting both formats.
4. Move to one persistent socket per shell, with request IDs and daemon-side supersession.
5. Add a zpty test harness and port the regressions from recent `fix:` commits.

Exit criteria: no `python3` or `socat` in the keystroke path; the zpty suite is green; a week of daily use with no rendering glitches. **Decision gate:** continue to the Rust rebuild, or stop and polish Python (D1).

### Phase 1: Rust core daemon (2 to 3 weeks)

- Workspace scaffolding, `ghst-proto`, sessions, scheduler, config loading (reads the existing TOML), Anthropic and OpenAI providers, circuit breaker, rate limit, cache, safety port, prompts as embedded templates.
- `complete`, `nl`, `history_search` feature parity with the Python daemon (LLM-only, no tier 0 yet).
- The zsh adapter from Phase 0 runs unchanged against the Rust daemon.

Exit criteria: the Phase 0 zpty suite passes against the Rust daemon; `ghst eval` scores within 2 points of the Python daemon on the same models; cold start under 20 ms; resident memory under 20 MB idle.

### Phase 2: CLI, install, cutover (1 to 2 weeks)

- All CLI commands from section 10, with legacy aliases. `ghst setup` wizard. `ghst doctor`.
- `cargo-dist` releases, Homebrew tap, Nix flake and Home Manager module.
- Final Python release with a migration notice. Move Python to `legacy/`.

Exit criteria: fresh-machine install to first ghost suggestion in under 2 minutes via Homebrew and via Nix; existing config files load without edits.

### Phase 3: history store and tier 0 (2 weeks)

- SQLite store, `HISTFILE` import, optional Atuin source, `command_start`/`session` ingestion.
- Ranker with frecency, cwd and repo boosts, path and git argument completion, typo correction for exit 127.
- Early emit plus LLM replacement flow, `accept` feedback.
- FTS5-backed history search, with LLM reranking of the top 50.

Exit criteria: tier 0 serves at least 50 percent of accepted suggestions in a week of daily use; tier 0 p95 under 10 ms; LLM calls per hour of active typing down at least 60 percent from Phase 2.

### Phase 4: error correction and proactive suggestions (1 to 2 weeks)

- Auto suggestion on exit 127; `fix` hotkey; tmux, kitty, and WezTerm capture behind `capture = "auto"`.
- Tier 0 proactive next-command suggestions from history sequences.

Exit criteria: the fix suggestion is accepted on at least 30 percent of `fix` invocations in the eval set; no regressions in tty behavior of interactive programs (vim, less, fzf, htop) in the zpty suite.

### Phase 5: local model tier (1 week, optional)

- Ollama provider and `[local].model`; route `complete` to tier 1 before tier 2.
- `privacy.mode = "local-only"` tested end to end.

Exit criteria: with a small coder model on an M-series Mac, tier 1 p50 under 250 ms and eval score reported alongside the remote models.

### Phase 6: fish and bash (2 to 3 weeks)

- Spikes on fish ghost text and bash via ble.sh (section 9). Ship NL, history search, and fix for both regardless. Ship autocomplete where the spike succeeds.

Exit criteria: adapter e2e tests for each shell in CI; `ghst doctor` understands each shell.

### Phase 7: polish (ongoing)

- Starship segment (Appendix A), `ghst eval` model comparison report, per-project config overrides (`.ghst.toml` in the repo root, with an allowlist so a cloned repo cannot change the provider).

Rough total: 11 to 16 weeks of part-time work to reach Phase 5.

---

## 16. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| `region_highlight` rendering has edge cases on some terminals | Medium | Phase 0 spike across terminals before committing; zsh-autosuggestions has used the approach for years, so the edge cases are known |
| Conflicts with zsh-autosuggestions, zsh-syntax-highlighting, fzf-tab, and similar plugins | High | Detection in the adapter and in `ghst doctor`, a documented load order, and zpty tests with those plugins loaded |
| The Rust rewrite stalls and both versions rot | Medium | The Phase 0 decision gate; Phase 1 reuses the same adapter, so the Python daemon stays a working fallback until cutover |
| The tier 0 ranker feels worse than the LLM for novel commands | Medium | Confidence threshold tuned with `ghst eval` and accept-rate metrics; LLM replacement flow still runs below threshold |
| A secret leaks to a provider | Low, high impact | Redaction on every outbound path, `never_send` list, `local-only` mode, redaction tests on every provider request in CI |
| Provider API changes or model aliases are retired | Medium | Provider trait plus recorded fixtures; aliases in config; `ghst doctor` validates the model with a cheap call |
| Fish has no workable ghost text hook | Medium | Ship the non-ghost features for fish first (section 9.2) |

---

## 17. Rejected alternatives

- **Starship module or prompt-only integration.** Wrong layer: no keystroke access and no drawing after the cursor. See Appendix A for the small part that does fit.
- **Owning the PTY (`ghst run zsh`) or a terminal emulator integration.** It would give perfect rendering and real output capture for every shell, but it means reimplementing a terminal multiplexer layer, clashes with tmux and terminal features, and asks users to trust ghst with their whole session. Fig and Amazon Q's inline mode show how much surface area that is. Revisit only if error correction without capture proves too weak.
- **Keeping per-request connections.** Simple, but it forces the shell to manage connect, FD watching, and cleanup for every keystroke, which is the source of the stale-FD bugs.
- **JSON on the wire.** Fine for the daemon, but the shell cannot produce or parse it without forking or a fragile hand-written encoder.

---

## 18. Open questions

1. Is a persistent `zle -F` watch on the socket fd stable over hours-long sessions, across `exec zsh` and suspend/resume? (Phase 0 spike.)
2. Should Tab accept a visible suggestion, or stay reserved for normal completion with only Right arrow accepting? Today it accepts; zsh-autosuggestions users expect Right arrow.
3. How much history should be retained and indexed by default (all, or 100k entries)?
4. Should the LLM replacement flow (step 4 in section 8.1) be on by default, or is a suggestion changing under the user's eyes too distracting? Measure with accept rates.
5. Is a PyPI wheel of the Rust binary worth keeping, given Homebrew and Nix cover the main audience?

---

## Appendix A: Starship status segment

Starship can show ghst's state without any coupling. `ghst status --prompt` prints a short string and exits quickly (it reads a state file the daemon updates rather than calling the socket).

```toml
# ~/.config/starship.toml
[custom.ghst]
command = "ghst status --prompt"   # prints e.g. "👻" (healthy), "👻!" (provider down), "" (stopped)
when = "test -S ${XDG_RUNTIME_DIR:-$TMPDIR}/ghst/ghst.sock"
format = "[$output]($style) "
style = "dimmed"
shell = ["sh"]
```

## Appendix B: Mapping from current files

| Current | New home |
|---|---|
| `src/ghst/daemon.py` | `ghst-core` sessions + scheduler; post-processing moves to `ghst-core::complete::post` |
| `src/ghst/llm.py` | `ghst-llm` providers, breaker, cache |
| `src/ghst/prompts.py` | `ghst-llm/prompts/*.toml` (embedded, overridable) |
| `src/ghst/context.py` | `ghst-core::context` with per-cwd cache and watcher |
| `src/ghst/safety.py` | `ghst-core::safety` |
| `src/ghst/config.py` | `ghst::config` (same TOML schema plus new sections) |
| `src/ghst/cli.py` | `ghst` binary, clap commands with legacy aliases |
| `src/ghst/shell/*.zsh` | `shell/zsh/ghst.zsh`, one file, rewritten around the frame protocol and `region_highlight` |
| `tests/*.py` | Rust unit tests per crate, plus `tests/shell/` zpty suite and `tests/eval/` |
