# ghst PRD: Next Command for any terminal

Status: draft
Date: 2026-11-24 (revised the same day: plain zsh in any terminal is the baseline)
Owner: Matt
Supersedes: `docs/REBUILD_PLAN.md` (2026-10-02). `docs/PLAN.md` remains the record of the original goals.

---

## 1. Problem

Warp's one feature worth switching terminals for is **Next Command**: after a command finishes, an LLM reads that command, its output, the exit code, the directory, git state, and recent history, and predicts the next command as ghost text on the empty prompt. Right arrow accepts it. When the last command failed, the prediction is usually the fix.

Warp can do this because it is the terminal, so it always has each command's output. Leaving Warp for any other terminal loses the feature, and nothing mature replaces it as of November 2026:

| Tool | Gap |
|---|---|
| zsh-autosuggestions | History only. Never suggests on an empty buffer. |
| Atuin | Owns history and Ctrl-R. Its AI prompt (`?`) was removed in 18.25 and its hosted AI server is shut down. It can capture command output, but only through its experimental, opt-in PTY proxy. |
| deja | History and sequence prediction, no LLM, no output. Replaces zsh-autosuggestions. |
| nighthawk, zsh-localghost | Pre-alpha, or LLM completion while typing only. No output awareness. |
| Claude Code and other agents | Separate session, not the prompt you type at. |

ghst v0.5 targets the wrong thing. Its core is LLM completion on every keystroke, drawn by writing escape codes to `/dev/tty`. The proactive suggestion that matches Warp exists in `daemon.py` (`_handle_complete`, the `is_proactive` branch) but nothing in the shell sends it, because ghst never solved output capture.

## 2. Goal

Recreate Warp's Next Command for **plain zsh in any terminal**, with no other tools required. Use Atuin, tmux, and zsh-autosuggestions when they are present, but never depend on them.

### Supported baseline

- zsh 5.8 or newer (5.9 preferred; 5.8 gets a fallback for `region_highlight` cleanup).
- macOS or Linux.
- Any terminal that runs zsh: Terminal.app, iTerm2, Ghostty, kitty, WezTerm, Alacritty, GNOME Terminal, and inside or outside tmux.
- No zsh framework or plugin required.

### Success criteria

- In plain zsh (`zsh -f` plus the ghst init line) in Terminal.app and in Ghostty, after any command a prediction appears within **1.5 s p50, 3 s p95** of the prompt drawing, using the command's real output.
- After a failed command, the prediction is a plausible fix in at least **60 percent** of the eval set (T03).
- Accept rate of shown predictions is at least **25 percent** over a week of daily use (measured by T12 metrics).
- The PTY proxy is invisible: vim, less, fzf, htop, ssh, tmux, job control, bracketed paste, mouse, and window resize behave exactly as without it; added keystroke latency under **1 ms**; `cat` of a 100 MB file at least **90 percent** as fast as without it.
- Zero display corruption: no orphaned ghost text after resize, line wrap, `clear`, Ctrl-C, or `zle reset-prompt`.
- No per-keystroke LLM calls in the default configuration. No `python3` or `socat` process started by the shell side, ever.
- Output never leaves the machine unless the user opted in, and always passes secret redaction first.

### Non-goals

- LLM completion while typing as a default feature (Warp didn't have it either). It may return later as an opt-in (T31).
- Replacing zsh-autosuggestions, Atuin, or the terminal. ghst works alone and cooperates with them when present.
- fish, bash, Windows.
- A chat UI or agent that runs commands. **Never auto-execute.**
- Syncing anything across machines.

## 3. User experience

### 3.1 Next Command (P0)

```
$ git commit -m "fix: handle empty config"
[main 3f2a1c9] fix: handle empty config
 1 file changed, 4 insertions(+)
$ git push                         <- gray ghost text, appears ~1 s after the prompt
```

```
$ npm run biuld
npm error Missing script: "biuld"
npm error Did you mean this?
npm error   npm run build
$ npm run build                    <- gray ghost text, marked as a fix
```

- Shown while the buffer is empty, and kept while what the user types is a prefix of the prediction. This works with or without zsh-autosuggestions (T15).
- **Right arrow** or **End** accepts the whole prediction. **Alt+F / Ctrl+Right** accepts one word. **Esc** dismisses. A keystroke that no longer matches hides it (and, if zsh-autosuggestions is loaded, hands over to its history suggestions).
- A new command or a new prompt clears any stale prediction. A prediction that arrives after the user has typed something incompatible is dropped.
- Dangerous predictions (per `safety.py`) show a warning marker and still never execute on their own.

### 3.2 Natural language to command, Ctrl+G (P1)

Kept from v0.5: enter English, get a command in the buffer, Ctrl+Z restores the previous buffer. It moves onto the new transport and gets the same context as Next Command.

### 3.3 Natural language history search (P2)

ghst does not bind Ctrl-R (users commonly have Atuin, fzf, or the zsh default there). It offers a configurable key (default unbound, suggested `^X^R`). See T30.

### 3.4 Setup

One line in `.zshrc`, as early as possible:

```zsh
eval "$(ghst init zsh)"
```

That line, in an interactive shell with a real tty, re-executes zsh inside `ghst-pty` (the output-capturing proxy) once, then loads the zsh integration in the inner shell. It skips the proxy when output already comes from elsewhere (Atuin's pty-proxy is active) or when `GHST_NO_PROXY=1` is set.

`ghst setup` (the wizard) asks explicitly whether command output may be sent to the configured provider, offers the local Apple model (`fm`) on macOS 27 as the private option, and runs `ghst doctor`.

## 4. Architecture

```
any terminal
  └─ ghst-pty                 Rust; forwards all bytes both ways plus SIGWINCH; records output between
       │                      ghst's private OSC markers; strips the markers; pushes each command's
       │                      output to the daemon at command end
       └─ zsh (interactive)
            ├─ ghst.zsh       preexec/precmd: emit start/end markers (only when inside ghst-pty)
            │                 precmd: send `session` + `next` frames; zle -F: receive
            │                 render via POSTDISPLAY + region_highlight (memo=ghst)
            │                 prefix continuation: own widgets, or a zsh-autosuggestions strategy if loaded
            └─ one persistent zsocket fd per shell
                         │  framed text protocol (no forks on the shell side)
                         ▼
ghst daemon (Python, asyncio)
  ├─ OutputSource      ghst-pty (default) | atuin | tmux | none
  ├─ HistorySource     $HISTFILE (default) | atuin (when installed)
  ├─ ContextCache      context.py: git, project type, venv
  ├─ next handler      prompts.proactive_*: one LLM call per finished command, newer supersedes older
  ├─ nl handler        Ctrl+G
  └─ llm.py            anthropic | openai-compatible (incl. Apple `fm serve`); breaker, cache, prompt caching
```

### Key decisions

| ID | Decision | Why |
|---|---|---|
| D1 | Keep the daemon in Python. | Latency is dominated by the network or model (300 to 1500 ms), not Python. The valuable code (`llm.py`, `prompts.py`, `safety.py`, `context.py`) already exists. |
| D2 | Ship a small compiled PTY proxy, `ghst-pty`, as part of the baseline. | Output is what makes Next Command good, and the only terminal-independent way to get it is to sit between the terminal and the shell (what Warp gets for free and what Atuin's experimental proxy does). It must be compiled: it is in the path of every byte. Rust, built with the system cargo (fenix via Nix on Matt's machine; no rustup). |
| D3 | ghst emits its own private OSC markers from zsh hooks; the proxy strips them. | Terminal-independent (does not rely on Ghostty's or any terminal's OSC 133 integration), carries a sequence number so output is matched to the right command, and the terminal never sees them. |
| D4 | Render through ZLE only (`POSTDISPLAY` + `region_highlight`). | Works in every terminal. zsh-autosuggestions has done this for years. The `AGENTS.md` claim that POSTDISPLAY cannot be colored is about raw ANSI escapes; `region_highlight` is the supported way. Removes the `/dev/tty` drawing and its bug class. |
| D5 | Optional integrations are sources, not dependencies. | `OutputSource` and `HistorySource` each have a default that needs nothing installed. Atuin and tmux are alternatives, used when present or configured. |
| D6 | One LLM call per finished command. | Cost and rate limits stop mattering, so a stronger model and prompt caching are affordable. |
| D7 | Shell and proxy talk a length-prefixed frame protocol over the daemon's socket. | zsh can build and parse it with parameter expansion only. Ends `python3` spawns per request (see `docs/REBUILD_PLAN.md` section 6, adopted here). |
| D8 | The proxy fails open. | If `ghst-pty` cannot start, `ghst init` falls back to running zsh directly. If it hits an internal error after start, it stops capturing and keeps forwarding bytes. A ghst bug must never cost the user their shell. |

### 4.1 Marker format

Emitted by the zsh hooks only when `GHST_PTY` is set (the proxy sets it for its child):

```
ESC ] 4478 ; start ; <seq> BEL       in preexec, just before the command runs
ESC ] 4478 ; end ; <seq> ; <exit> BEL in precmd, before anything else prints
```

`4478` spells GHST on a phone keypad. T01 confirms it collides with no OSC number in common use (known: 0 to 9, 10 to 19, 52, 104, 110 to 119, 133, 633, 777, 1337, 18188735, 9;4). `<seq>` is a per-shell counter. The proxy records bytes between `start` and `end` for the same `seq`, skips anything drawn on the alternate screen, and removes the markers from the stream it forwards.

### 4.2 Proxy to daemon

At each `end` marker the proxy sends an `output` frame: `session` (the shell's PID, from `hello`), `seq`, `exit`, `bytes_total`, `truncated`, and `text` (raw bytes, at most `max_capture_bytes`, keeping the head and tail). The daemon stores the last few per session in memory. `OutputSource` for ghst-pty is a lookup, not a call, so there is no added latency at prediction time.

## 5. Constraints and environment

- **Users:** the supported baseline in section 2.
- **Development machine (Matt):** macOS 27.2, Apple silicon, zsh 5.9.2, Ghostty 1.4, Atuin 18.24.4 (Ctrl-R), zsh-autosuggestions 0.7.1, zsh-syntax-highlighting, fzf-tab, Starship. All managed by Home Manager in `~/Developer/nix-darwin-config`; files under `~/.config` that are Nix store symlinks are read-only. `cargo` and `rustc` are on PATH from the system profile.
- Python tooling: `uv run pytest`, `uv run ruff check src/`, `uv run basedpyright src/ghst/`. Never bare `pip`/`python`/`pytest`.
- Rust tooling: `cargo nextest run`, `cargo clippy -- -D warnings`, `cargo fmt --check`.
- `/usr/bin/fm` exists on macOS 27 but needs a one-time `sudo fm license` (human).
- Model aliases only, never dated snapshots.

## 6. Risks

| Risk | Mitigation |
|---|---|
| The proxy breaks something in the terminal (keys, mouse, resize, job control, terminal queries, paste). | T01 tests a long list of programs and terminals before production work; fail-open design (D8); `GHST_NO_PROXY=1` escape hatch; `ghst doctor` reports proxy state. |
| A proxy crash closes the user's terminal window. | Panics caught at the top of each loop and converted to pure pass-through; no `unwrap` on I/O paths; fuzzing of the marker parser in T06. |
| Two proxies stacked (ghst-pty inside Atuin's pty-proxy, or the reverse). | `ghst init` detects Atuin's proxy (environment and parent process) and uses Atuin as the output source instead of starting `ghst-pty`. Documented load order. |
| Packaging a Rust binary with a Python package. | T18: one wheel per platform built with maturin (Python package plus the `ghst-pty` binary), plus a Nix flake. |
| Predictions arrive too late to feel useful. | Request starts in `precmd`; the proxy has already delivered output by then; measure with T03; allow a faster model for `next`. |
| Sending terminal output to a provider surprises the user (Warp drew criticism for this). | Explicit opt-in in `ghst setup`, redaction, `never_send` command list, `local-only` mode with `fm`. |
| Interference with zsh-syntax-highlighting, zsh-autosuggestions, fzf-tab, Atuin widgets. | T02 and T13 test both plain zsh and a plugin-heavy setup; `memo=ghst` scoping of highlight entries. |
| Apple `fm` quality or its ~4k token context is too small. | Eval it alongside a hosted model in T03 before recommending it. |

---

## 7. Tasks

### 7.1 How to hand these off

Each task below is written to be given to a subagent as-is. The agent gets this PRD plus the task ID.

Conventions for every task:

- Work in an isolated git worktree on a branch named `prd/<task-id>-<slug>`. Do not commit to `main`, push, or open PRs unless the delegating session says so.
- Read `AGENTS.md` and this PRD's sections 2, 4, and 5 before starting.
- Python work is done only when `uv run pytest`, `uv run ruff check src/`, and `uv run basedpyright src/ghst/` all pass. Rust work is done only when `cargo nextest run`, `cargo clippy -- -D warnings`, and `cargo fmt --check` pass.
- zsh files must pass `zsh -n`. Shell tests use the zpty harness from T13 once it exists, and run against **plain zsh first** (`zsh -f`, no plugins), then the plugin setup.
- Do not edit `~/Developer/nix-darwin-config` or anything under `~/.config` that is a Nix store symlink. Use temporary config dirs (`XDG_CONFIG_HOME`, `ATUIN_CONFIG_DIR`, a temp `ZDOTDIR`) for experiments.
- Never use em dashes or middle dots in code, comments, or docs.
- Steps marked **[human]** need Matt (sudo, interactive feel in a real terminal window, Nix activation). The agent stops and lists them in its report instead of working around them.
- Hand back a short report: what changed (files), how it was verified (commands and results), open questions, and anything marked [human].

### 7.2 Dependency map

```
Wave 0 (parallel)   T01 proxy spike     T02 render spike     T03 eval harness     T04 frame protocol
Wave 1 (parallel)   T05 zsh markers     T06 ghst-pty (T01 T04)     T09 HistorySource     T10 fm provider     T11 config+privacy
Wave 2 (parallel)   T07 proxy launch (T05 T06)     T08 OutputSource (T04 T06)     T13 zpty harness (T02)
Wave 3              T12 daemon `next` (T04 T08 T09 T11)
Wave 4 (parallel)   T14 next display (T02 T05 T12 T13)     T16 Ctrl+G port (T04 T13)     T18 packaging (T06)
Wave 5 (parallel)   T15 prefix continuation (T14)     T17 remove legacy (T14 T16)
Wave 6              T19 setup + doctor (T07 T08 T10 T11 T18)     T20 docs (T17)     T21 dogfood [human]
Later               T30 NL history search     T31 typing-time LLM
```

---

### T01. Spike: PTY proxy prototype

- **Depends on:** none. **Parallel with:** T02, T03, T04.
- **Read first:** section 4.1 and 4.2 of this PRD; https://docs.atuin.sh/latest/reference/pty-proxy/ (prior art); the `portable-pty`, `nix` (pty, termios, signal), and `vte` crate docs.
- **Question to answer:** can a small Rust proxy sit between any terminal and zsh invisibly, and capture each command's output reliably using the section 4.1 markers?
- **Do:**
  1. Create `crates/ghst-pty/` (Cargo workspace at the repo root). Prototype: open a PTY, spawn `$SHELL -i` (or a given shell) with `GHST_PTY=1`, put the outer tty in raw mode, forward stdin to the PTY and PTY output to stdout, forward `SIGWINCH` (copy the window size), restore termios on exit, exit with the child's status.
  2. Parse the output stream for the 4.1 markers (a byte-level state machine, not regex over chunks: markers can split across reads), strip them, and write each captured command's output to a debug file.
  3. Track alternate screen (`ESC [ ? 1049 h/l`, `47`, `1047`) and exclude alt-screen bytes from capture.
  4. Confirm OSC 4478 is unused (search terminal docs and source for kitty, iTerm2, WezTerm, Ghostty, VTE, foot, tmux). Pick another number if not.
- **Test matrix (automate what you can with `script`, `tmux`, or `expect`; list the rest for the human):** vim, less, fzf, htop, `ssh localhost`, tmux inside the proxy, Ctrl-C, Ctrl-Z and `fg`, bracketed paste of multi-line text, mouse in vim, resize during vim and at the prompt, `cat` of a 100 MB file (time vs direct), keystroke echo latency, a command printing 5,000 lines, a binary-output command, UTF-8 and wide characters, terminal queries (`printf '\e[c'`, `\e[>q`) round-tripping.
- **Deliverable:** the prototype crate and `docs/spikes/T01-pty-proxy.md`: what works, measured latency and throughput, failures and their causes, the confirmed OSC number, and a go/no-go for T06.
- **[human]:** 15 minutes in Terminal.app and Ghostty under the prototype (resize, vim, fzf, ssh, tmux), with notes on anything that feels off.

### T02. Spike: empty-prompt ghost text through ZLE

- **Depends on:** none. **Parallel with:** T01, T03, T04.
- **Read first:** `src/ghst/shell/autocomplete.zsh` (what to stop doing); zsh-autosuggestions 0.7.1 source (`_zsh_autosuggest_highlight_apply`, `_zsh_autosuggest_async_request`) from https://github.com/zsh-users/zsh-autosuggestions.
- **Do:** write a standalone prototype `prototypes/next-display.zsh` that:
  1. In `precmd`, opens an fd to a fake responder (`exec {fd}< <(sleep 0.8; print -r -- "git push")`) and registers `zle -F`.
  2. In the handler, if `BUFFER` is empty, sets `POSTDISPLAY` and appends a `region_highlight` entry with `memo=ghst`, then `zle -R`. On zsh 5.8, removes its entry by recorded index instead of `memo`.
  3. Accepts with Right arrow and End on an empty buffer, dismisses with Esc, clears on any other edit and in `zle-line-finish`.
- **Verify with zpty, in two setups:** plain `zsh -f`, then with zsh-autosuggestions, zsh-syntax-highlighting, fzf-tab, and Atuin loaded. Dump `BUFFER`, `POSTDISPLAY`, `region_highlight` through a debug widget and test: arrival before and after the prompt draws, arrival after the user typed, accept, dismiss, Ctrl-C, `clear`, a long prediction that wraps. Test zsh 5.8 if a build is available (`nix shell nixpkgs#zsh` pinned or a container).
- **Deliverable:** the prototype plus `docs/spikes/T02-render.md` listing what works, widget-ordering requirements with plugins, the 5.8 fallback, and the zsh calls T14 should use.
- **[human]:** look at it in Terminal.app and Ghostty, including window resize while a prediction is shown.

### T03. Eval harness for Next Command

- **Depends on:** none. **Parallel with:** T01, T02, T04.
- **Read first:** `src/ghst/prompts.py` (`proactive_system`, `proactive_user`, `error_correction_user`), `src/ghst/llm.py`.
- **Do:**
  1. Create `tests/eval/next_command/cases.jsonl` with at least 40 cases: `last_command`, `exit_status`, `output`, `cwd_fixture` (git branch, project type), `recent_history`, and `accept` (a list of regexes; any match passes). At least 15 failure cases (typos, missing script, missing dependency, merge conflict, permission denied, wrong flag) and 15 success cases with an obvious next step (commit then push, build then test, `cd` into a fresh clone then install). Include 5 cases with **no output** to measure how much output matters.
  2. Write `scripts/eval_next_command.py` (run with `uv run`) that builds prompts with the existing functions, calls a configured model, and reports pass rate overall, on failures, and with output removed; p50/p95 latency; input tokens; and cost per 1,000 predictions.
  3. Support at least: Anthropic fast model by alias, and any OpenAI-compatible base URL (for `fm serve` and others). API keys from env only.
- **Done when:** the script runs end to end against a stub provider with no network, and against a real provider when a key is present.
- **Out of scope:** prompt tuning beyond fixing obvious breakage.
- **[human]:** `sudo fm license`, then run the eval with `fm serve` and with the hosted model and paste results into `docs/spikes/T03-eval-results.md`.

### T04. Frame protocol in the daemon and a zsh codec

- **Depends on:** none. **Parallel with:** T01, T02, T03.
- **Read first:** `docs/REBUILD_PLAN.md` section 6 (framing), `src/ghst/daemon.py` `_handle_connection`, `tests/test_integration.py`, section 4.2 of this PRD.
- **Do:**
  1. Add `src/ghst/protocol.py`: encode/decode frames `<kind> <id> <nfields>\n` followed by `<key>:<bytelen>:<bytes>\n` per field. Byte lengths; arbitrary bytes allowed in values.
  2. `_handle_connection` detects the format per message (first byte `{` means JSON, otherwise frame) so the CLI and existing tests keep working. Responses to frame clients are frames; responses echo the request `id` as `for`.
  3. Define the message kinds used by this PRD: `hello`, `session`, `next`, `accept`, `nl`, `cancel`, `output` (from the proxy), and responses `welcome`, `suggestion`, `command`, `error`.
  4. Add `src/ghst/shell/lib/proto.zsh` with `__ghst_frame_begin`, `__ghst_frame_field`, `__ghst_frame_send`, and a `sysread`-based `__ghst_frame_read` that fills an associative array. No forks; `setopt localoptions nomultibyte` for lengths.
  5. Add a Rust encoder for `output` frames in `crates/ghst-proto/` (or a module T06 can depend on), with a cross-language test.
- **Done when:** round-trip tests in `tests/test_protocol.py` (newlines, tabs, quotes, backslashes, emoji, NUL, empty values, 1 MB value); a zsh test that encodes every one of those and the daemon decodes it identically; the Rust encoder's output decodes in Python.

### T05. zsh markers

- **Depends on:** none (format fixed in section 4.1; if T01 changes the OSC number, update one constant). **Parallel with:** wave 1.
- **Do:** in `src/ghst/shell/markers.zsh`, add `preexec` and `precmd` hooks that emit the section 4.1 markers only when `GHST_PTY` is set. `precmd` must run first among precmd hooks so the `end` marker precedes any prompt output (prepend to `precmd_functions`, and re-check order once on the first prompt because other plugins append later). Capture `$?` before anything else runs.
- **Done when:** zpty tests assert marker bytes and order around: a normal command, a failing command, an empty Enter (no start marker, so no end marker), Ctrl-C at the prompt, a command run with Starship-like prompts that print in precmd.

### T06. `ghst-pty`, production

- **Depends on:** T01 go, T04.
- **Read first:** `docs/spikes/T01-pty-proxy.md`, sections 4.1, 4.2, and D8.
- **Do:** turn the prototype into the shipped binary:
  - Command line: `ghst-pty [--shell PATH] [--socket PATH] [--check]`. `--check` verifies it can open a PTY and exits 0, for use by `ghst init`.
  - Per-command capture into a bounded buffer (`max_capture_bytes`, default 256 KB, keeping head and tail); alt-screen excluded; markers stripped.
  - At each `end` marker, send an `output` frame to the daemon socket without blocking the forwarding loop (bounded channel; drop the frame if the daemon is down or slow).
  - Fail open: catch panics per loop iteration, switch to pass-through on any capture error, never exit while the child is alive.
  - Set `GHST_PTY=1` and `GHST_PTY_PID` in the child's environment.
- **Done when:** unit tests and a `cargo fuzz` (or proptest) target for the marker parser across arbitrary chunk boundaries; integration tests that run a shell under the proxy and check captured output for the T01 matrix items that can be automated; throughput and latency within section 2 targets.

### T07. Launching zsh inside the proxy

- **Depends on:** T05, T06.
- **Read first:** `src/ghst/cli.py` (`shell-init` output, around line 83 to 110).
- **Do:** `ghst init zsh` prints code that, at the top of `.zshrc`:
  1. Does nothing more if any of these hold: not interactive, stdin or stdout not a tty, `GHST_PTY` already set, `GHST_NO_PROXY=1`, Atuin's pty-proxy is active (detect via its environment variable or parent process name; record which in T01's doc), `ghst-pty` missing, or `ghst-pty --check` fails.
  2. Otherwise `exec ghst-pty --shell "$(command -v zsh)"`, so the inner zsh reads `.zshrc` again with `GHST_PTY` set.
  3. In all cases continues to load the zsh integration (markers, display, Ctrl+G).
  Keep the existing inlining approach (shell files printed, not sourced).
- **Done when:** tests for each skip condition; a test that a broken `ghst-pty` (exits 1) leaves a working shell; a test that the proxy is entered exactly once (no nesting on `exec zsh` or a nested `zsh`).

### T08. OutputSource

- **Depends on:** T04, T06.
- **Read first:** `src/ghst/safety.py` (`sanitize_output`), `src/ghst/config.py` (`proactive_capture_blocklist`, `proactive_output_lines`), https://docs.atuin.sh/latest/guide/output-capture/.
- **Do:** add `src/ghst/output.py` with:
  - `class OutputSource(Protocol): async def last_output(self, session: SessionInfo, ref: CommandRef) -> CapturedOutput | None`.
  - `ProxyOutputSource` (default): reads the in-memory store fed by `output` frames, matched by session and `seq`.
  - `TmuxOutputSource`: `tmux capture-pane -p -t <pane> -S -<n>` when the session reports `TMUX_PANE`, cut at the previous prompt.
  - `AtuinOutputSource`: only if Atuin output capture can return a specific command's output. Start by answering that question (CLI `atuin output`, `atuin mcp` tools, the Atuin daemon socket, or the on-disk store, using a temporary `ATUIN_CONFIG_DIR`). If no supported surface exists, write that down in `docs/spikes/T08-atuin-output.md` and skip this class.
  - `NullOutputSource`.
  - `select_source(config, session)`: configured choice, else proxy if the session is inside `ghst-pty`, else Atuin if usable and active, else tmux if in tmux, else none.
  - Shared post-processing: strip ANSI and OSC sequences, keep the first 20 and last N lines with a `[... k lines omitted ...]` marker, cap bytes, run `sanitize_output`, return `None` for commands on the never-send list.
- **Done when:** unit tests for each source with fakes, truncation, ANSI stripping, redaction, never-send, and source selection.

### T09. HistorySource

- **Depends on:** none. **Parallel with:** wave 1.
- **Read first:** `src/ghst/shell/ghst.zsh` `__ghst_get_history` (being replaced), `atuin search --help`.
- **Do:** add `src/ghst/history.py` with `async def recent(session, limit) -> list[HistoryEntry]` (command, exit if known, cwd if known, timestamp if known).
  - Default: parse `$HISTFILE` (from `hello`), handling zsh extended history format (`: <ts>:<dur>;cmd`), multi-line entries, and zsh's metafied bytes. Supplement with commands seen in this daemon's sessions (which carry cwd and exit).
  - When Atuin is installed: `atuin search --limit N --cwd <cwd> --format ...` plus a global query, merged and de-duplicated.
  - Pass every command through `sanitize_history`.
- **Done when:** tests with fixture HISTFILEs (plain and extended, multi-line, metafied) and a mocked `atuin` on PATH.

### T10. Apple `fm serve` as a provider

- **Depends on:** none. **Parallel with:** wave 1.
- **Read first:** `src/ghst/llm.py`, `src/ghst/config.py` (`_default_api_base_url`), `fm --help` (after the license is accepted).
- **Do:** add `provider.name = "apple"`: OpenAI-compatible client against a local `fm serve` URL, no API key, and a context budget setting (default 4,000 tokens) that prompt builders use to shrink output and history. Optionally start `fm serve` as a daemon child if it is not running. Only offered on macOS 27+.
- **Done when:** unit tests with a mocked HTTP server; `ghst status` shows the provider; a prompt over budget is trimmed (output first, then history).
- **[human]:** `sudo fm license` and one live request.

### T11. Config and privacy model

- **Depends on:** none. **Parallel with:** wave 1.
- **Read first:** `src/ghst/config.py`, `config/default.toml`, `tests/test_config.py`.
- **Do:** add sections, keeping old keys loading without error (map or ignore with a logged notice):
  ```toml
  [next_command]
  enabled = true
  model = ""                  # empty: use provider.autocomplete_model
  share_output = false        # set by `ghst setup` after an explicit question
  output_source = "auto"      # auto | proxy | atuin | tmux | none
  max_output_lines = 80
  never_send = ["pass", "op", "gpg", "ssh-keygen", "vault", "aws configure"]

  [proxy]
  enabled = true              # start ghst-pty from `ghst init zsh`
  max_capture_bytes = 262144

  [history]
  source = "auto"             # auto | histfile | atuin

  [privacy]
  mode = "default"            # default | local-only (forces provider "apple" or disables LLM calls)

  [keys]
  nl = "^G"
  history_search = ""         # unbound by default; ghst never takes ^R
  ```
- **Done when:** load/save/get/set tests for every new key, and a test that `local-only` refuses a non-local provider.

### T12. Daemon `next` request

- **Depends on:** T04, T08, T09, T11.
- **Read first:** `src/ghst/daemon.py` (`_handle_complete`, `SessionBuffer`, rate limiter), `src/ghst/prompts.py`.
- **Do:**
  1. Track a session per connection: `hello` fields (`pid`, `tty`, `HISTFILE`, `GHST_PTY_PID`, `TMUX_PANE`, `TERM_PROGRAM`, zsh version) and `session` fields (`cwd`, `exit`, `duration_ms`, `last_command`, `seq`).
  2. `next`: gather output (T08), history (T09), and context (`context.py`) concurrently; build the prompt from `proactive_system`/`proactive_user` (failures use or merge `error_correction_user`); call the LLM; post-process with the existing code-fence and single-line rules; return `suggestion`, `kind` (`next` or `fix`), and `warning`.
  3. If the proxy's `output` frame for this `seq` has not arrived yet, wait up to 150 ms for it, then proceed without output.
  4. A newer `next`, or any `nl`, on the same session cancels the in-flight `next`. Deadline 4 s.
  5. If `share_output` is false, predict from command, exit code, and history only.
  6. Metrics in memory: requests, shown, accepted (from `accept` frames), latency p50/p95, share of predictions that had output. Shown in `ghst status`.
- **Done when:** async tests with a fake provider cover: success, failure gives `kind=fix`, late output frame, missing output, supersession cancels the older call, deadline, `share_output=false` omits output (assert on the captured prompt), never-send omits output, local-only mode.

### T13. zpty test harness

- **Depends on:** T02 (reuse its driver). **Parallel with:** T07, T08.
- **Do:** `tests/shell/` with a pytest plugin that starts a real `zsh -f` in a pty with a temp `ZDOTDIR`, loads the ghst init output, points ghst at a fake daemon (a small asyncio server speaking the T04 protocol with scripted responses), sends keystrokes, and reads state from a `__ghst_debug_dump` widget (`BUFFER`, `CURSOR`, `POSTDISPLAY`, `region_highlight`). Two profiles: `plain` (no plugins) and `plugins` (zsh-autosuggestions, zsh-syntax-highlighting), the latter skipped cleanly if the plugins are not installed.
- **Done when:** a sample test types a command, simulates a prediction, presses Right arrow, and asserts `BUFFER`, in both profiles. Runs in under 10 s.

### T14. zsh Next Command display

- **Depends on:** T02, T05, T12, T13.
- **Read first:** `docs/spikes/T02-render.md`, `prototypes/next-display.zsh`, `src/ghst/shell/ghst.zsh`, `src/ghst/cli.py` (shell-init inlining).
- **Do:** new `src/ghst/shell/next.zsh`, inlined by `shell-init`:
  1. On load: connect once with `zsocket`, send `hello`, watch the fd with `zle -F`. Reconnect at most once per 5 s from `precmd` if the socket dropped; start the daemon if it is not running.
  2. `precmd` (after the T05 end marker): send `session` then `next` with the current `seq`; clear any shown prediction.
  3. On a `suggestion` whose `for` matches: store it in `__GHST_PREDICTION` and render it on an empty buffer per T02. Mark `fix` and `warning` predictions with the configured colors via `region_highlight`, not escape codes.
  4. Accept and dismiss bindings per section 3.1; send `accept` with `full`, `word`, or `none`.
- **Done when:** zpty tests in both profiles for every case listed in T02, plus: daemon not running (no hang, no output), daemon killed mid-request, two shells at once each get their own prediction, shell not under the proxy (prediction still works, without output).
- **[human]:** a day of use.

### T15. Prefix continuation while typing

- **Depends on:** T14.
- **Do:** keep the prediction visible while the typed buffer is a prefix of it.
  - If zsh-autosuggestions is loaded: define `_zsh_autosuggest_strategy_ghst` (if `__GHST_PREDICTION` starts with `$1`, set `suggestion=$__GHST_PREDICTION`) and prepend `ghst` to `ZSH_AUTOSUGGEST_STRATEGY` without dropping existing entries. Re-check once on the first prompt, since plugins like Atuin set the strategy later.
  - Otherwise: wrap `self-insert`, `backward-delete-char`, and `vi-backward-delete-char` with widgets that, after calling the original, show the remainder of the prediction if it still matches, and hide it if not. Pure string comparison, no IPC.
- **Done when:** zpty tests in both profiles: typing `git p` with prediction `git push` shows `ush`; typing `git s` hides it (and in the plugins profile falls through to history); Right arrow accepts.

### T16. Port Ctrl+G to the new transport

- **Depends on:** T04, T13. **Parallel with:** T14.
- **Read first:** `src/ghst/shell/nl-command.zsh` (`__ghst_nl_command`, `__ghst_nl_undo`, mode enter/exit), `AGENTS.md` notes on recursive-edit and FD cleanup.
- **Do:** move NL to `src/ghst/shell/nl.zsh` using the persistent socket and frames (no `python3`, no `socat`). Keep recursive-edit and Ctrl+Z undo. Draw the spinner and status in `POSTDISPLAY` with `region_highlight`. Stop binding `^R`. Daemon side: `nl` gets the same session context as `next`, including the last command's output when shared.
- **Done when:** zpty tests for generate, cancel with Esc, undo, daemon down; `rg 'python3|socat|/dev/tty' src/ghst/shell/nl.zsh` returns nothing.

### T17. Remove the legacy shell path

- **Depends on:** T14, T16.
- **Do:** delete `autocomplete.zsh` and the old request helpers in `ghst.zsh`; remove the shell's per-keystroke `complete` path; keep the daemon's `complete` handler only behind a flag for T31; replace the `.py` mtime auto-reload with a version check in `hello`/`welcome`; update `cli.py` inlining.
- **Done when:** `rg 'python3|socat|/dev/tty' src/ghst/shell/` is empty; all tests pass; `ghst init zsh | zsh -n` passes.

### T18. Packaging

- **Depends on:** T06.
- **Do:**
  - Build `ghst` wheels with maturin (mixed project: the Python package plus the `ghst-pty` binary as a script entry), for macOS arm64/x86_64 and Linux x86_64/arm64 (manylinux and musl). `uv tool install ghst` and `pipx install ghst` must put both `ghst` and `ghst-pty` on PATH.
  - A source distribution that builds with a local cargo.
  - `flake.nix` exposing `packages.default` (both binaries) and a dev shell (uv, Python, Rust toolchain via fenix).
  - CI workflow: Python checks, Rust checks, zpty tests on macOS and Linux, wheel builds. Lint with `actionlint`.
- **Done when:** a wheel built in CI installs with `uv tool install ./dist/<wheel>` on a clean macOS and Linux runner and `ghst-pty --check` passes; `nix build` succeeds.

### T19. `ghst setup` and `ghst doctor`

- **Depends on:** T07, T08, T10, T11, T18.
- **Do:** rename or alias the wizard to `ghst setup` (keep `ghst init` printing shell code, matching other tools). The wizard asks the output-sharing question (default no; explains what is sent and the redaction), offers `apple` when `/usr/bin/fm` exists, writes the `.zshrc` line at the top of the file. `ghst doctor` checks: socket and daemon version, provider reachability, `ghst-pty --check`, whether the current shell is under the proxy (or why not), marker order in `precmd_functions`, zsh version and `memo` support, zsh-autosuggestions presence and strategy order, Atuin version and proxy status, tmux, `fm` license state. Each check prints pass or fail with a one-line fix.
- **Done when:** tests for each doctor check with a mocked environment; the wizard tested with scripted stdin.

### T20. Docs

- **Depends on:** T17.
- **Do:** rewrite `README.md` around Next Command, leading with the plain-zsh install; a short "works with Atuin, tmux, zsh-autosuggestions" section; a privacy section (what is sent, when, redaction, local-only). Update `AGENTS.md` (architecture, file layout, IPC table, the proxy, and correct the POSTDISPLAY note). Update `config/default.toml`. Mark `docs/REBUILD_PLAN.md` and `docs/autocomplete-flow.md` as historical.
- **Done when:** every command and config key in the docs exists in the code.

### T21. Dogfood week [human]

- **Depends on:** T15, T16, T19.
- One week of daily use: mostly in Matt's full setup (Ghostty, Atuin, plugins), plus at least two sessions in plain zsh in Terminal.app. Check `ghst status` metrics against section 2. Decide on T30 and T31. Then wire ghst into Home Manager in `nix-darwin-config` (separate repo, separate change).

### Later

#### T30. NL history search (P2)

- The LLM produces up to 3 search queries and filters (cwd, exit) from the question; the daemon runs them against the HistorySource (HISTFILE scan or `atuin search`), merges, and optionally reranks the top 30. Picker drawn in `POSTDISPLAY`. Key from `[keys] history_search`.

#### T31. Typing-time LLM suggestions (P3, opt-in)

- An LLM suggestion while typing, used only when history and the Next Command prediction have nothing. With zsh-autosuggestions: a second strategy `ghst_llm` placed last. Without it: an async request from the T15 widgets with a debounce. Off by default. Only if dogfooding shows gaps.

---

## 8. Open questions

1. Is a 1 to 1.5 s delay after the prompt acceptable, or should the daemon start predicting when the proxy reports command end, before `precmd`, to save the prompt-drawing time?
2. Should a `fix` prediction look different from a `next` prediction (color, a leading marker)?
3. Default provider for `next`: hosted fast model or `fm`? Decide from T03 numbers.
4. Should ghst keep a log of its own predictions and acceptances for evals, and where?
5. Should ghst-pty also be offered to Atuin users instead of Atuin's proxy, or always defer to Atuin's when present?

## 9. References

- Warp Next Command and Active AI: https://docs.warp.dev/agents/active-ai
- Atuin pty-proxy (prior art): https://docs.atuin.sh/latest/reference/pty-proxy/
- Atuin output capture: https://docs.atuin.sh/latest/guide/output-capture/
- Atuin AI server (self-host only): https://github.com/atuinsh/atuin-ai
- zsh-autosuggestions: https://github.com/zsh-users/zsh-autosuggestions
- Ghostty shell integration: https://ghostty.org/docs/features/shell-integration
- macOS 27 `fm` CLI: https://modelfit.io/blog/macos-27-fm-cli-local-model-terminal/
