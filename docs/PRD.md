# ghst PRD: Next Command for any terminal

Status: draft
Date: 2026-11-24
Owner: Matt
Supersedes: `docs/REBUILD_PLAN.md` (2026-10-02). `docs/PLAN.md` remains the record of the original goals.

---

## 1. Problem

Warp's one feature worth switching terminals for is **Next Command**: after a command finishes, an LLM reads that command, its output, the exit code, the directory, git state, and recent history, and predicts the next command as ghost text on the empty prompt. Right arrow accepts it. When the last command failed, the prediction is usually the fix.

Leaving Warp for Ghostty loses this. Nothing mature replaces it in a normal terminal as of November 2026:

| Tool | Gap |
|---|---|
| zsh-autosuggestions | History only. Never suggests on an empty buffer. |
| Atuin | Owns history and Ctrl-R. Its AI prompt (`?`) was removed in 18.25 and its hosted AI server is shut down. It does capture command output (experimental). |
| deja | History and sequence prediction, no LLM, no output. Replaces zsh-autosuggestions. |
| nighthawk, zsh-localghost | Pre-alpha, or LLM completion while typing only. No output awareness. |
| Claude Code and other agents | Separate session, not the prompt you type at. |

ghst v0.5 targets the wrong thing. Its core is LLM completion on every keystroke, drawn by writing escape codes to `/dev/tty`. The proactive suggestion that matches Warp exists in `daemon.py` (`_handle_complete`, the `is_proactive` branch) but nothing in the shell sends it, because ghst never solved output capture.

## 2. Goal

Recreate Warp's Next Command in Ghostty with zsh, using the tools already installed (zsh-autosuggestions, Atuin, Ghostty shell integration), and keep the rest of ghst small.

### Success criteria

- After any command, a prediction appears on the empty prompt within **1.5 s p50, 3 s p95** of the prompt drawing, using the command's real output.
- After a failed command, the prediction is a plausible fix in at least **60 percent** of the eval set (task T03).
- Accept rate of shown predictions is at least **25 percent** over a week of daily use (Warp-like usefulness; measured by T09 metrics).
- Zero display corruption: no orphaned ghost text after resize, line wrap, `clear`, Ctrl-C, or `zle reset-prompt`.
- No per-keystroke LLM calls in the default configuration. No `python3` or `socat` process started by the shell side, ever.
- Output never leaves the machine unless the user opted in, and always passes secret redaction first.

### Non-goals

- LLM completion while typing as a default feature (Warp didn't have it either). It may return later as an opt-in (T32).
- Replacing zsh-autosuggestions, Atuin, or the terminal. ghst plugs into them.
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

- Shown only while the buffer is empty, or while what the user typed is a prefix of the prediction.
- **Right arrow** or **End** accepts the whole prediction. **Alt+F / Ctrl+Right** accepts one word. **Esc** dismisses. Any non-matching keystroke hands over to zsh-autosuggestions (Atuin history).
- A new command or a new prompt clears any stale prediction. A prediction that arrives after the user has typed something incompatible is dropped.
- Dangerous predictions (per `safety.py`) show a warning marker and still never execute on their own.

### 3.2 Natural language to command, Ctrl+G (P1)

Kept from v0.5: enter English, get a command in the buffer, Ctrl+Z restores the previous buffer. It moves onto the new transport and gets the same context as Next Command.

### 3.3 Natural language history search (P2)

Atuin keeps Ctrl-R. ghst offers a separate, configurable key (default unbound, suggested `^X^R`). The LLM turns the question into a few `atuin search` queries, runs them locally, and optionally reranks the top 30. Raw history is no longer sent in bulk.

### 3.4 Setup

`ghst init` asks explicitly whether command output may be sent to the configured provider, offers the local Apple model (`fm`) as the private option, and checks that Atuin output capture (or tmux) is available.

## 4. Architecture

```
Ghostty
  └─ atuin pty-proxy          records each command's output between OSC markers (experimental, opt-in)
       └─ zsh
            ├─ zsh-autosuggestions   strategy chain: (ghst atuin)
            │     └─ _zsh_autosuggest_strategy_ghst   returns the cached prediction if $1 is a prefix of it
            ├─ ghst.zsh               precmd: send `next` frame; zle -F: receive; render on empty buffer
            │                         via POSTDISPLAY + region_highlight (memo=ghst)
            └─ one persistent zsocket fd per shell
                         │  framed text protocol (no forks on the shell side)
                         ▼
ghst daemon (Python, asyncio)
  ├─ OutputSource      atuin | tmux | none      -> last command's output, trimmed, ANSI-stripped, redacted
  ├─ HistorySource     atuin search             -> recent commands for this cwd, with exit codes
  ├─ ContextCache      context.py               -> git, project type, venv
  ├─ next handler      prompts.proactive_*       -> one LLM call per finished command, supersedes older ones
  ├─ nl handler        Ctrl+G
  └─ llm.py            anthropic | openai-compatible (incl. `fm serve`) ; breaker, cache, prompt caching
```

### Key decisions

| ID | Decision | Why |
|---|---|---|
| D1 | Keep the daemon in Python. | Latency is dominated by the network or model (300 to 1500 ms), not Python. The valuable code (`llm.py`, `prompts.py`, `safety.py`, `context.py`) already exists. A compiled binary is reserved for the PTY proxy (T30) if one is needed. |
| D2 | Render through ZLE only (`POSTDISPLAY` + `region_highlight`). | zsh-autosuggestions has done this for years on this machine. The `AGENTS.md` claim that POSTDISPLAY cannot be colored is about raw ANSI escapes; `region_highlight` is the supported way. Removes the `/dev/tty` drawing and its bug class. |
| D3 | Delegate typing-time suggestions to zsh-autosuggestions. | It already handles async fetch, cancellation, accept and partial accept. ghst only adds a strategy that serves the cached prediction. |
| D4 | Delegate history to Atuin. | Already installed and owning Ctrl-R. No ghst history store. |
| D5 | Output comes from a pluggable `OutputSource`. | Atuin's capture is experimental and its AI features churn. The daemon must not care where output came from. |
| D6 | One LLM call per finished command. | Cost and rate limits stop mattering, so a stronger model and prompt caching are affordable. |
| D7 | Shell talks a length-prefixed frame protocol over a persistent socket. | zsh can build and parse it with parameter expansion only. Ends `python3` spawns per request (see `docs/REBUILD_PLAN.md` section 6, which this PRD adopts). |

## 5. Constraints and environment

- macOS 27.2, Apple silicon, zsh 5.9.2, Ghostty 1.4, Atuin 18.24.4, zsh-autosuggestions 0.7.1, zsh-syntax-highlighting, fzf-tab, Starship. All managed by Home Manager in `~/Developer/nix-darwin-config`; `~/.config/atuin/config.toml` is a read-only Nix store symlink.
- Python tooling: `uv run pytest`, `uv run ruff check src/`, `uv run basedpyright src/ghst/`. Never bare `pip`/`python`/`pytest`.
- `/usr/bin/fm` exists but needs a one-time `sudo fm license` (human).
- Model aliases only, never dated snapshots.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Atuin's output capture cannot return the output of a specific command, or is unstable in Ghostty. | T01 answers this first. Fallbacks: tmux `capture-pane`, then ghst's own OSC 133 proxy (T30). |
| Atuin renames or removes capture features (it removed its AI agent in 18.25). | `OutputSource` interface; depend only on documented CLI or MCP surfaces; version check in `ghst doctor`. |
| Predictions arrive too late to feel useful. | Start the request in `precmd` before the prompt draws; measure with T03; allow a faster model for `next`. |
| Sending terminal output to a provider surprises the user (Warp drew criticism for this). | Explicit opt-in in `ghst init`, redaction, `never_send` command list, `local-only` mode with `fm`. |
| Interference with zsh-syntax-highlighting, fzf-tab, Atuin widgets. | T02 tests with all of them loaded; `memo=ghst` scoping of highlight entries. |
| Apple `fm` quality or its ~4k token context is too small. | Eval it alongside a hosted model in T03 before making it a recommended default. |

---

## 7. Tasks

### 7.1 How to hand these off

Each task below is written to be given to a subagent as-is. The agent gets this PRD plus the task ID.

Conventions for every task:

- Work in an isolated git worktree on a branch named `prd/<task-id>-<slug>`. Do not commit to `main`, push, or open PRs unless the delegating session says so.
- Read `AGENTS.md` and this PRD's sections 2, 4, and 5 before starting.
- Use `uv run` for all Python. A task that touches Python is done only when `uv run pytest`, `uv run ruff check src/`, and `uv run basedpyright src/ghst/` all pass.
- zsh files must pass `zsh -n`. Shell tests use the zpty harness from T10 once it exists.
- Do not edit `~/Developer/nix-darwin-config` or anything under `~/.config` that is a Nix store symlink. Use temporary config dirs (`ATUIN_CONFIG_DIR`, `XDG_CONFIG_HOME`, a temp `ZDOTDIR`) for experiments.
- Never use em dashes or middle dots in code, comments, or docs.
- Steps marked **[human]** need Matt (sudo, interactive feel in Ghostty, Nix activation). The agent stops and lists them in its report instead of working around them.
- Hand back a short report: what changed (files), how it was verified (commands and results), open questions, and anything marked [human].

### 7.2 Dependency map

```
Wave 0 (parallel, no deps)     T01 output spike   T02 render spike   T03 eval harness   T04 frame protocol
Wave 1 (parallel)              T05 OutputSource (T01)   T06 HistorySource   T07 fm provider   T08 config+privacy
Wave 2                         T09 daemon `next` (T04 T05 T06 T08)      T10 zpty harness (T02)
Wave 3 (parallel)              T11 zsh next display (T02 T04 T09 T10)   T13 Ctrl+G port (T04 T10)
Wave 4                         T12 autosuggest strategy (T11)    T14 remove legacy (T11 T13)
Wave 5                         T15 init + doctor (T05 T07 T08)   T16 docs (T14)   T17 dogfood [human]
Conditional                    T30 own PTY proxy (if T01 fails)  T31 NL history search  T32 typing-time LLM
```

---

### T01. Spike: get the last command's output from Atuin

- **Depends on:** none. **Parallel with:** T02, T03, T04.
- **Read first:** https://docs.atuin.sh/latest/guide/output-capture/, https://docs.atuin.sh/latest/reference/pty-proxy/, `atuin output search --help`, `atuin mcp --help`, `atuin history last --help`.
- **Question to answer:** given a finished command in the current shell, can a separate process (the ghst daemon) get that command's full captured output, reliably and within 100 ms? Through which surface: CLI, MCP tool, the Atuin daemon socket, or the on-disk store?
- **Do:**
  1. Enable pty-proxy, the Atuin daemon, and output capture using a temporary `ATUIN_CONFIG_DIR` (the real config is a read-only Nix symlink). Start a zsh under `atuin pty-proxy` inside a `tmux` or `script` session so it can be driven non-interactively.
  2. Run representative commands: success with short output, failure with stderr, 5,000-line output, a command printing a fake `sk-ant-...` key, `vim` (alt screen), a command interrupted with Ctrl-C.
  3. For each surface, try to fetch output for the last command by history ID. Record latency, truncation, redaction, ANSI handling, and what happens for alt-screen and interrupted commands.
  4. Note how a shell hook can learn the history ID of the command that just finished (env var, `atuin history last`, OSC marker, other).
- **Deliverable:** `docs/spikes/T01-atuin-output.md` with a yes/no verdict, the exact retrieval call to use, measured latency, known failure cases, and a recommendation: use Atuin, use tmux only, or build T30.
- **Done when:** the verdict is backed by commands someone else can rerun.
- **[human]:** a 10-minute check in a real Ghostty window under pty-proxy (resize, fzf, vim, ssh, Ctrl-C) with notes on anything that feels off.

### T02. Spike: empty-prompt ghost text through ZLE

- **Depends on:** none. **Parallel with:** T01, T03, T04.
- **Read first:** `src/ghst/shell/autocomplete.zsh` (what to stop doing), zsh-autosuggestions 0.7.1 source in the Nix store (`fd zsh-autosuggestions.zsh /nix/store -d 6`), especially `_zsh_autosuggest_highlight_apply` and `_zsh_autosuggest_async_request`.
- **Do:** write a standalone prototype `prototypes/next-display.zsh` that:
  1. In `precmd`, opens an fd to a fake responder (`exec {fd}< <(sleep 0.8; print -r -- "git push")`) and registers `zle -F`.
  2. In the handler, if `BUFFER` is empty, sets `POSTDISPLAY` and appends a `region_highlight` entry with `memo=ghst`, then `zle -R`.
  3. Accepts with Right arrow and End on an empty buffer, dismisses with Esc, clears on any other edit and in `zle-line-finish`.
  4. Coexists with zsh-autosuggestions, zsh-syntax-highlighting, fzf-tab, and Atuin's zsh widgets loaded in the same order as Matt's `.zshrc` (read the generated `~/.zshrc`, do not edit it).
- **Verify with zpty:** drive a real zsh, dump `BUFFER`, `POSTDISPLAY`, `region_highlight` through a debug widget, and test: arrival before and after the prompt draws, arrival after the user typed, accept, dismiss, Ctrl-C, `clear`, a long prediction that wraps.
- **Deliverable:** the prototype plus `docs/spikes/T02-render.md` listing what works, any widget-ordering requirements, and the zsh calls T11 should use.
- **[human]:** look at it in Ghostty, including window resize while a prediction is shown.

### T03. Eval harness for Next Command

- **Depends on:** none. **Parallel with:** T01, T02, T04.
- **Read first:** `src/ghst/prompts.py` (`proactive_system`, `proactive_user`, `error_correction_user`), `src/ghst/llm.py`.
- **Do:**
  1. Create `tests/eval/next_command/cases.jsonl` with at least 40 cases: `last_command`, `exit_status`, `output`, `cwd_fixture` (git branch, project type), `recent_history`, and `accept` (a list of regexes; any match passes). At least 15 failure cases (typos, missing script, missing dependency, merge conflict, permission denied, wrong flag) and 15 success cases with an obvious next step (commit then push, build then test, `cd` into a fresh clone then install).
  2. Write `scripts/eval_next_command.py` (run with `uv run`) that builds prompts with the existing functions, calls a configured model, and reports pass rate, pass rate on failures, p50/p95 latency, input tokens, and cost estimate per 1,000 predictions.
  3. Support at least: Anthropic fast model by alias, any OpenAI-compatible base URL (for `fm serve` and others). API keys from env only.
- **Done when:** the script runs end to end against a stub provider in CI-like conditions (no network), and against a real provider when a key is present.
- **Out of scope:** prompt tuning beyond fixing obvious breakage (that is iterative work after T09).
- **[human]:** `sudo fm license`, then run the eval with `fm serve` and with the hosted model and paste results into `docs/spikes/T03-eval-results.md`.

### T04. Frame protocol in the daemon and a zsh codec

- **Depends on:** none. **Parallel with:** T01, T02, T03.
- **Read first:** `docs/REBUILD_PLAN.md` section 6 (framing), `src/ghst/daemon.py` `_handle_connection`, `tests/test_integration.py`.
- **Do:**
  1. Add `src/ghst/protocol.py`: encode/decode frames `<kind> <id> <nfields>\n` followed by `<key>:<bytelen>:<bytes>\n` per field. Byte lengths, arbitrary bytes allowed in values.
  2. `_handle_connection` detects the format per message (first byte `{` means JSON, otherwise frame) so the CLI and existing tests keep working.
  3. Responses to frame clients are frames. Requests carry an `id`; responses echo it as `for`.
  4. Add `src/ghst/shell/lib/proto.zsh` with `__ghst_frame_begin`, `__ghst_frame_field`, `__ghst_frame_send`, and a `sysread`-based `__ghst_frame_read` that fills an associative array. No forks, `setopt localoptions nomultibyte` for lengths.
- **Done when:** property-style round-trip tests in `tests/test_protocol.py` (newlines, tabs, quotes, backslashes, emoji, empty values, 1 MB value); a zsh-side test that encodes a frame containing every one of those and the daemon decodes it identically.

### T05. OutputSource

- **Depends on:** T01 verdict. **Parallel with:** T06, T07, T08.
- **Read first:** `docs/spikes/T01-atuin-output.md`, `src/ghst/safety.py` (`sanitize_output`), `src/ghst/config.py` (`proactive_capture_blocklist`, `proactive_output_lines`).
- **Do:** add `src/ghst/output.py` with:
  - `class OutputSource(Protocol): async def last_output(self, session: SessionInfo, ref: CommandRef) -> CapturedOutput | None`.
  - `AtuinOutputSource` using the retrieval call from T01; `TmuxOutputSource` using `tmux capture-pane -p -t <pane> -S -<n>` when the session reports `TMUX_PANE`, cutting at the previous prompt line; `NullOutputSource`.
  - `select_source(config, session)` that picks per session: configured choice, else Atuin if available, else tmux if in tmux, else none.
  - Post-processing shared by all sources: strip ANSI and OSC sequences, keep the first 20 and last N lines with a `[... k lines omitted ...]` marker, cap bytes, run `sanitize_output`, and return `None` for commands on the never-send list.
- **Done when:** unit tests cover each source with fakes (subprocess mocked), truncation, ANSI stripping, redaction, and the never-send list. Each source returns within its timeout (default 300 ms) or yields `None`.

### T06. HistorySource via Atuin

- **Depends on:** none. **Parallel with:** T05, T07, T08.
- **Read first:** `atuin search --help`, `src/ghst/shell/ghst.zsh` `__ghst_get_history` (being replaced).
- **Do:** add `src/ghst/history.py` with `async def recent(cwd, limit) -> list[HistoryEntry]` (command, exit, cwd, duration) using `atuin search --limit N --cwd <cwd> --format ...` plus a global query, merged and de-duplicated. Fall back to reading `$HISTFILE` when Atuin is absent. Pass every command through `sanitize_history`.
- **Done when:** tests with a mocked `atuin` binary on `PATH`, including commands containing tabs and newlines, and the fallback path.

### T07. Apple `fm serve` as a provider

- **Depends on:** none. **Parallel with:** T05, T06, T08.
- **Read first:** `src/ghst/llm.py`, `src/ghst/config.py` (`_default_api_base_url`), `fm --help` (after the license is accepted).
- **Do:** add `provider.name = "apple"`: OpenAI-compatible client against a local `fm serve` URL, no API key, and a context budget setting (default 4,000 tokens) that `prompts.py` callers use to shrink output and history. Optionally let the daemon start `fm serve` itself if it is not running, as a child process stopped at daemon exit.
- **Done when:** unit tests with a mocked HTTP server; `ghst status` shows the provider; a prompt that exceeds the budget is trimmed (output first, then history).
- **[human]:** `sudo fm license` and one live request.

### T08. Config and privacy model

- **Depends on:** none. **Parallel with:** T05, T06, T07.
- **Read first:** `src/ghst/config.py`, `config/default.toml`, `tests/test_config.py`.
- **Do:** add sections, keeping old keys loading without error (map or ignore with a logged notice):
  ```toml
  [next_command]
  enabled = true
  model = ""                  # empty: use provider.autocomplete_model
  share_output = false        # set by `ghst init` after an explicit question
  output_source = "auto"      # auto | atuin | tmux | none
  max_output_lines = 80
  never_send = ["pass", "op", "gpg", "ssh-keygen", "vault", "aws configure"]

  [privacy]
  mode = "default"            # default | local-only (forces provider "apple" or disables LLM calls)

  [keys]
  nl = "^G"
  history_search = ""         # Atuin owns ^R
  ```
- **Done when:** load/save/get/set tests for every new key, and a test that `local-only` refuses a non-local provider.

### T09. Daemon `next` request

- **Depends on:** T04, T05, T06, T08.
- **Read first:** `src/ghst/daemon.py` (`_handle_complete`, `SessionBuffer`, rate limiter), `src/ghst/prompts.py`.
- **Do:**
  1. Track a session per connection: `hello` frame fields (`pid`, `tty`, `TMUX_PANE`, `TERM_PROGRAM`, zsh version) and `session` frame fields (`cwd`, `exit`, `duration_ms`, `last_command`, `history_id`).
  2. Add `next` handling: gather output (T05), history (T06), and context (`context.py`) concurrently; build the prompt from `proactive_system`/`proactive_user` (failure cases use or merge `error_correction_user`); call the LLM; post-process with the existing code-fence and single-line rules; return `suggestion`, `kind` (`next` or `fix`), and `warning`.
  3. A newer `next` or any `complete`/`nl` on the same session cancels the in-flight `next` (cancel the task; the httpx request is dropped). Deadline 4 s.
  4. If `share_output` is false, still predict, but from command, exit code, and history only.
  5. Record metrics in memory: requests, shown, accepted (from an `accept` frame), latency p50/p95, and expose them in `ghst status`.
- **Done when:** async tests with a fake provider cover: success, failure becomes `kind=fix`, supersession cancels the older call, deadline, `share_output=false` omits output from the prompt (assert on the captured prompt), never-send command omits output, local-only mode.

### T10. zpty test harness

- **Depends on:** T02 (reuse its driver). **Parallel with:** T09.
- **Do:** `tests/shell/` with a pytest plugin that starts a real `zsh -f` in a pty with a temp `ZDOTDIR`, loads zsh-autosuggestions, zsh-syntax-highlighting, and the ghst init output, points ghst at a fake daemon (a small asyncio server speaking the T04 protocol with scripted responses), sends keystrokes, and reads state from a `__ghst_debug_dump` widget (`BUFFER`, `CURSOR`, `POSTDISPLAY`, `region_highlight`). Skip cleanly if a dependency is missing.
- **Done when:** a sample test types a command, simulates a prediction, presses Right arrow, and asserts `BUFFER`. Runs in under 10 s.

### T11. zsh Next Command display

- **Depends on:** T02, T04, T09, T10.
- **Read first:** `docs/spikes/T02-render.md`, `prototypes/next-display.zsh`, `src/ghst/shell/ghst.zsh`, `src/ghst/cli.py` (shell-init inlining at line ~107).
- **Do:** new `src/ghst/shell/next.zsh`, inlined by `shell-init`:
  1. On load: connect once with `zsocket`, send `hello`, watch the fd with `zle -F`. Reconnect at most once per 5 s from `precmd` if the socket dropped; start the daemon if it is not running (existing logic).
  2. `preexec`: record command and start time. `precmd`: send `session` then `next` with a fresh request ID; clear any shown prediction.
  3. On a `next` response whose `for` matches the current ID: store it in `__GHST_PREDICTION` and render it on an empty buffer per T02. Mark `fix` and `warning` predictions with the configured colors via `region_highlight` (not escape codes).
  4. Accept/dismiss bindings per section 3.1; send an `accept` frame with `full`, `word`, or `none`.
- **Done when:** zpty tests for every case listed in T02, plus: daemon not running (no hang, no error output), daemon killed mid-request, two shells at once each get their own prediction.
- **[human]:** a day of use in Ghostty.

### T12. zsh-autosuggestions strategy

- **Depends on:** T11.
- **Do:** define `_zsh_autosuggest_strategy_ghst` in `next.zsh`: if `__GHST_PREDICTION` starts with `$1`, set `suggestion=$__GHST_PREDICTION`. At init, prepend `ghst` to `ZSH_AUTOSUGGEST_STRATEGY` without dropping what the user or Atuin configured, and do it after Atuin's init has run (document required order, and re-check in `precmd` once). If zsh-autosuggestions is not loaded, ghst only shows the empty-buffer prediction and `ghst doctor` says so.
- **Done when:** zpty tests: typing `git p` with prediction `git push` shows `ush`; typing `git s` falls through to history; Right arrow accepts through zsh-autosuggestions' own widget.

### T13. Port Ctrl+G to the new transport

- **Depends on:** T04, T10. **Parallel with:** T11.
- **Read first:** `src/ghst/shell/nl-command.zsh` (`__ghst_nl_command`, `__ghst_nl_undo`, mode enter/exit), `AGENTS.md` notes on recursive-edit and FD cleanup.
- **Do:** move NL to `src/ghst/shell/nl.zsh` using the persistent socket and frames (no `python3`, no `socat`). Keep recursive-edit and Ctrl+Z undo. Draw the spinner and status in `POSTDISPLAY` with `region_highlight`. Stop binding `^R`. Daemon side: `nl` gets the same session context as `next`.
- **Done when:** zpty tests for generate, cancel with Esc, undo, daemon down; `rg 'python3|socat|/dev/tty' src/ghst/shell/` returns nothing for this file.

### T14. Remove the legacy shell path

- **Depends on:** T11, T13.
- **Do:** delete `autocomplete.zsh` and the old request helpers in `ghst.zsh`; remove the per-keystroke `complete` path from the shell; keep the daemon's `complete` handler only if T32 is planned, behind a config flag; drop the `.py` mtime auto-reload (replace with a version check in `hello`/`welcome`); update `cli.py` inlining.
- **Done when:** `rg 'python3|socat|/dev/tty' src/ghst/shell/` is empty; all tests pass; `ghst shell-init zsh | zsh -n` passes.

### T15. `ghst init` and `ghst doctor`

- **Depends on:** T05, T07, T08.
- **Do:** update the wizard to ask the output-sharing question (default no, explains what is sent and redaction), offer `apple` as provider when `/usr/bin/fm` exists, and detect Atuin capture. Add `ghst doctor`: socket, daemon version, provider reachability, zsh-autosuggestions presence and strategy order, Atuin version and capture status, tmux, `fm` license state, region_highlight `memo` support. Each check prints pass/fail with a one-line fix.
- **Done when:** tests for each doctor check with mocked environment; wizard tested with scripted stdin.

### T16. Docs

- **Depends on:** T14.
- **Do:** rewrite `README.md` around Next Command; update `AGENTS.md` (architecture, file layout, IPC table, and correct the POSTDISPLAY note: raw ANSI no, `region_highlight` yes); update `config/default.toml`; mark `docs/REBUILD_PLAN.md` and `docs/autocomplete-flow.md` as historical.
- **Done when:** every command and config key in the docs exists in the code.

### T17. Dogfood week [human]

- **Depends on:** T12, T13, T15.
- Use it daily for a week in Ghostty. Check `ghst status` metrics against section 2. Decide on T30, T31, T32. Then wire ghst into Home Manager in `nix-darwin-config` (separate repo, separate change).

### Conditional tasks

#### T30. Minimal OSC 133 PTY proxy (only if T01 says Atuin capture is not usable)

- A small compiled binary (Rust, via the fenix toolchain; no rustup) that spawns the shell in a PTY, forwards bytes both ways and `SIGWINCH`, and records output between Ghostty's OSC 133 `C` (command start) and `D` (command end) marks into a ring buffer served over the ghst socket. Skips alt-screen output. Implements `OutputSource`.
- Done when: transparent in vim, fzf, ssh, htop, and resize; added latency under 1 ms; zpty-style tests.

#### T31. NL history search over Atuin (P2)

- LLM produces up to 3 search queries and filters (cwd, exit) from the question; the daemon runs them with `atuin search`, merges, and optionally reranks the top 30. Picker drawn in `POSTDISPLAY`. Key from `[keys] history_search`.

#### T32. Typing-time LLM suggestions (P3, opt-in)

- A second strategy `ghst_llm` placed after `atuin`, calling the daemon's `complete` over the socket from zsh-autosuggestions' async subshell. Off by default. Only if dogfooding shows gaps that history and Next Command miss.

---

## 8. Open questions

1. Is a 1 to 1.5 s delay after the prompt acceptable, or should the request start at command end (from the PTY proxy) instead of `precmd`?
2. Should a `fix` prediction look different from a `next` prediction (color, a leading marker)?
3. Default provider for `next`: hosted fast model or `fm`? Decide from T03 numbers.
4. Should ghst keep any history of its own predictions for evals, and where (Atuin's KV store, or a local file)?

## 9. References

- Warp Next Command and Active AI: https://docs.warp.dev/agents/active-ai
- Atuin output capture: https://docs.atuin.sh/latest/guide/output-capture/
- Atuin pty-proxy: https://docs.atuin.sh/latest/reference/pty-proxy/
- Atuin AI server (self-host only): https://github.com/atuinsh/atuin-ai
- zsh-autosuggestions: https://github.com/zsh-users/zsh-autosuggestions
- Ghostty shell integration (OSC 133): https://ghostty.org/docs/features/shell-integration
- macOS 27 `fm` CLI: https://modelfit.io/blog/macos-27-fm-cli-local-model-terminal/
