# nl-command.zsh — Natural language command construction (Cmd+I / Ctrl+G)
#
# Opens an inline prompt where the user describes what they want in English.
# The LLM generates a shell command and places it in the buffer for review.

# ── Undo state ──────────────────────────────────────────────────────────────
typeset -g __GHST_SAVED_BUFFER=""
typeset -gi __GHST_CAN_UNDO=0  # a flag: the saved line may be empty
typeset -gi __GHST_SAVED_CURSOR=0

# ── Colors (configurable via config.toml, fallback to ANSI defaults) ────────
typeset -g __GHST_C_ACCENT=$'\e[36m'
typeset -g __GHST_C_SUCCESS=$'\e[32m'
typeset -g __GHST_C_WARNING=$'\e[33m'
typeset -g __GHST_C_ERROR=$'\e[31m'
typeset -g __GHST_C_DIM=$'\e[2m'
typeset -g __GHST_C_RESET=$'\e[0m'
[[ -n "$__GHST_ACCENT_ESC" ]]  && __GHST_C_ACCENT="$__GHST_ACCENT_ESC"
[[ -n "$__GHST_SUCCESS_ESC" ]] && __GHST_C_SUCCESS="$__GHST_SUCCESS_ESC"
[[ -n "$__GHST_WARNING_ESC" ]] && __GHST_C_WARNING="$__GHST_WARNING_ESC"
[[ -n "$__GHST_ERROR_ESC" ]]   && __GHST_C_ERROR="$__GHST_ERROR_ESC"

# Show a status message below the prompt. zle -M keeps zle's idea of the
# screen correct (printing a newline to /dev/tty here left the prompt
# undrawn); it can't show colours, so escape codes are stripped.
__ghst_show_status() {
    setopt localoptions extendedglob
    local msg="${1//$'\e'\[[0-9;]#m/}"
    zle -M "  $msg"
}

# Clean up autocomplete state (call after spinner/request completes)
__ghst_cleanup_autocomplete() {
    __ghst_cancel_debounce 2>/dev/null
    if [[ -n "$__GHST_RESPONSE_FD" ]]; then
        zle -F $__GHST_RESPONSE_FD 2>/dev/null
        { exec {__GHST_RESPONSE_FD}<&- } 2>/dev/null
        __GHST_RESPONSE_FD=""
    fi
    __GHST_SUGGESTION=""
    __GHST_LAST_BUFFER=""
}

# ── Mode enter/exit helpers ─────────────────────────────────────────────────
typeset -g __GHST_SAVED_RPS1=""
typeset -g __GHST_SAVED_CURSOR_SHAPE=""

__ghst_cancel_mode() { zle send-break; }
zle -N __ghst_cancel_mode

__ghst_enter_mode() {
    local rps1_label="$1"
    __GHST_SAVED_RPS1="${RPS1:-}"
    RPS1="%{${__GHST_C_DIM}%}${rps1_label}%{${__GHST_C_RESET}%}"
    printf '\e[6 q' > /dev/tty
    __GHST_AUTOCOMPLETE_DISABLED=1
    # Bind ESC to cancel; set short KEYTIMEOUT so arrow keys still work
    __GHST_SAVED_ESC_BINDING=$(bindkey -L '\e' 2>/dev/null)
    __GHST_SAVED_KEYTIMEOUT=${KEYTIMEOUT:-40}
    KEYTIMEOUT=20
    bindkey '\e' __ghst_cancel_mode
}

__ghst_exit_mode() {
    RPS1="$__GHST_SAVED_RPS1"
    printf '\e[1 q' > /dev/tty
    __GHST_AUTOCOMPLETE_DISABLED=0
    KEYTIMEOUT=$__GHST_SAVED_KEYTIMEOUT
    if [[ -n "$__GHST_SAVED_ESC_BINDING" ]]; then
        eval "$__GHST_SAVED_ESC_BINDING"
    else
        bindkey -r '\e' 2>/dev/null
    fi
}

# Show placeholder text that disappears on first keystroke
__ghst_show_placeholder() {
    local text="$1"
    printf '%s%s%s' "${__GHST_C_DIM}" "$text" "${__GHST_C_RESET}" > /dev/tty
    # Move cursor back to start of placeholder
    printf '\e[%dD' "${#text}" > /dev/tty
}

# ── NL Command Widget ───────────────────────────────────────────────────────
__ghst_nl_command() {
    # Save current buffer for undo
    local saved_buffer="$BUFFER"
    local saved_cursor=$CURSOR

    # Clear any ghost text and cancel in-flight autocomplete
    __ghst_clear_suggestion 2>/dev/null
    __ghst_cleanup_autocomplete

    # Show context hint if buffer has content
    local context_hint=""
    if [[ -n "$BUFFER" ]]; then
        local short_buffer="${BUFFER:0:40}"
        [[ ${#BUFFER} -gt 40 ]] && short_buffer="${short_buffer}..."
        context_hint="${__GHST_C_DIM}($short_buffer)${__GHST_C_RESET} "
    fi

    # Use recursive-edit for full line editing (arrow keys, Ctrl+A/E, etc.)
    local orig_prompt="$PROMPT"
    PROMPT="%{${__GHST_C_ACCENT}%}ghst>%{${__GHST_C_RESET}%} ${context_hint}"
    BUFFER=""
    CURSOR=0
    POSTDISPLAY=""
    __ghst_enter_mode "ESC to cancel"
    zle reset-prompt
    __ghst_show_placeholder "describe what you want..."
    zle recursive-edit
    local edit_status=$?

    local nl_input="$BUFFER"
    PROMPT="$orig_prompt"
    __ghst_exit_mode

    # Handle cancel (Ctrl+C or empty)
    if (( edit_status != 0 )) || [[ -z "$nl_input" ]]; then
        BUFFER="$saved_buffer"
        CURSOR=$saved_cursor
        zle reset-prompt
        return
    fi

    # Show progress with zle's own message line. An animated spinner written
    # to /dev/tty here left zle redrawing the prompt a line too high.
    BUFFER=""
    POSTDISPLAY=""
    zle -M "  thinking…"
    zle -R

    # Build request
    local history_json
    history_json=$(__ghst_get_history | python3 -c "
import json, sys
history = [l.strip() for l in sys.stdin if l.strip()]
print(json.dumps(history[-10:]))
" 2>/dev/null)
    [[ -z "$history_json" ]] && history_json='[]'

    local escaped_prompt escaped_buffer
    escaped_prompt=$(printf '%s' "$nl_input" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null)
    escaped_buffer=$(printf '%s' "$saved_buffer" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null)

    local json_request="{\"type\":\"nl\",\"prompt\":$escaped_prompt,\"buffer\":$escaped_buffer,\"cwd\":\"$PWD\",\"shell\":\"zsh\",\"history\":$history_json}"

    # Send synchronous request (user is waiting, 15s timeout)
    local response
    response=$(__ghst_request "$json_request" 15)

    # Clean up stale autocomplete state
    __ghst_cleanup_autocomplete

    if [[ -z "$response" ]]; then
        # Try to auto-restart daemon
        ghst start --quiet &>/dev/null
        BUFFER="$saved_buffer"
        CURSOR=$saved_cursor
        zle reset-prompt
        __ghst_show_status "${__GHST_C_ERROR}✗${__GHST_C_RESET} ${__GHST_C_DIM}daemon restarted — try again${__GHST_C_RESET}"
        return
    fi

    # Parse response
    local command warning
    command=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('command', ''))
except: pass
" <<< "$response")

    warning=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('warning', ''))
except: pass
" <<< "$response")

    if [[ -z "$command" ]]; then
        BUFFER="$saved_buffer"
        CURSOR=$saved_cursor
        zle reset-prompt
        __ghst_show_status "${__GHST_C_WARNING}⚠${__GHST_C_RESET} ${__GHST_C_DIM}couldn't generate a command${__GHST_C_RESET}"
        return
    fi

    # Save for undo (Ctrl+Z)
    __GHST_SAVED_BUFFER="$saved_buffer"
    __GHST_CAN_UNDO=1
    __GHST_SAVED_CURSOR=$saved_cursor

    # Place generated command in buffer
    BUFFER="$command"
    CURSOR=${#BUFFER}
    zle reset-prompt

    # Show success hint (with warning if present)
    if [[ -n "$warning" ]]; then
        __ghst_show_status "${__GHST_C_WARNING}⚠ ${warning}${__GHST_C_RESET}"
    fi
    __ghst_show_status "${__GHST_C_SUCCESS}✓${__GHST_C_RESET} ${__GHST_C_DIM}Ctrl+Z to undo${__GHST_C_RESET}"
}
zle -N __ghst_nl_command

# ── History Search Widget (Ctrl+R) ──────────────────────────────────────────
__ghst_history_search() {
    local saved_buffer="$BUFFER"
    local saved_cursor=$CURSOR

    # Clear ghost text and cancel in-flight autocomplete
    __ghst_clear_suggestion 2>/dev/null
    __ghst_cleanup_autocomplete

    # Use recursive-edit for search query input
    local orig_prompt="$PROMPT"
    PROMPT="%{${__GHST_C_ACCENT}%}ghst history>%{${__GHST_C_RESET}%} "
    BUFFER=""
    CURSOR=0
    POSTDISPLAY=""
    __ghst_enter_mode "ESC to cancel"
    zle reset-prompt
    __ghst_show_placeholder "search by intent..."
    zle recursive-edit
    local edit_status=$?

    local query="$BUFFER"
    PROMPT="$orig_prompt"
    __ghst_exit_mode

    if (( edit_status != 0 )) || [[ -z "$query" ]]; then
        BUFFER="$saved_buffer"
        CURSOR=$saved_cursor
        zle reset-prompt
        return
    fi

    # Show progress with zle's message line (see the Ctrl+G widget)
    BUFFER=""
    POSTDISPLAY=""
    zle -M "  searching…"
    zle -R

    # Get history entries (deduplicated)
    local history_json
    history_json=$(__ghst_get_history | python3 -c "
import json, sys
history = []
seen = set()
for l in sys.stdin:
    l = l.strip()
    if l and l not in seen:
        seen.add(l)
        history.append(l)
print(json.dumps(history[-500:]))
" 2>/dev/null)
    [[ -z "$history_json" ]] && history_json='[]'

    local escaped_query
    escaped_query=$(printf '%s' "$query" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null)

    local json_request="{\"type\":\"history_search\",\"query\":$escaped_query,\"history\":$history_json,\"shell\":\"zsh\"}"

    local response
    response=$(__ghst_request "$json_request" 15)

    # Clean up stale autocomplete state
    __ghst_cleanup_autocomplete

    if [[ -z "$response" ]]; then
        ghst start --quiet &>/dev/null
        BUFFER="$saved_buffer"
        CURSOR=$saved_cursor
        zle reset-prompt
        __ghst_show_status "${__GHST_C_ERROR}✗${__GHST_C_RESET} ${__GHST_C_DIM}daemon restarted — try again${__GHST_C_RESET}"
        return
    fi

    # Parse first result
    local first_cmd
    first_cmd=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    results = d.get('results', [])
    if results:
        r = results[0]
        print(r.get('command', '') if isinstance(r, dict) else str(r))
except: pass
" <<< "$response")

    if [[ -n "$first_cmd" ]]; then
        __GHST_SAVED_BUFFER="$saved_buffer"
        __GHST_CAN_UNDO=1
        __GHST_SAVED_CURSOR=$saved_cursor
        BUFFER="$first_cmd"
        CURSOR=${#BUFFER}
        zle reset-prompt
        __ghst_show_status "${__GHST_C_SUCCESS}✓${__GHST_C_RESET} ${__GHST_C_DIM}Ctrl+Z to undo${__GHST_C_RESET}"
    else
        BUFFER="$saved_buffer"
        CURSOR=$saved_cursor
        zle reset-prompt
        __ghst_show_status "${__GHST_C_WARNING}⚠${__GHST_C_RESET} ${__GHST_C_DIM}no matching history found${__GHST_C_RESET}"
    fi
}
zle -N __ghst_history_search

# ── Undo: restore original buffer before NL/history ─────────────────────────
__ghst_nl_undo() {
    if (( __GHST_CAN_UNDO )); then
        # zle drops the "Ctrl+Z to undo" message on this keypress by itself;
        # clearing it with zle -M "" here made zle redraw a line too high
        BUFFER="$__GHST_SAVED_BUFFER"
        CURSOR=$__GHST_SAVED_CURSOR
        __GHST_SAVED_BUFFER=""
        __GHST_CAN_UNDO=0
        POSTDISPLAY=""
        zle reset-prompt
    else
        zle undo
    fi
}
zle -N __ghst_nl_undo

# ── Keybindings ──────────────────────────────────────────────────────────────
# Hotkeys come from the config (nl_hotkey, history_search_hotkey); empty = unbound
for __ghst_key in ${=__GHST_NL_HOTKEY-^G}; do bindkey "$__ghst_key" __ghst_nl_command; done
unset __ghst_key
[[ -n "$__GHST_HISTORY_HOTKEY" ]] && bindkey "$__GHST_HISTORY_HOTKEY" __ghst_history_search
bindkey '^Z' __ghst_nl_undo           # Ctrl+Z — Undo NL command
