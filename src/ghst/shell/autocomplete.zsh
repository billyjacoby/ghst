# autocomplete.zsh — ZLE ghost text widget
# Inline suggestions rendered via direct terminal escape codes

# ── State ────────────────────────────────────────────────────────────────────
typeset -g __GHST_SUGGESTION=""
typeset -g __GHST_TIMER_FD=""
typeset -g __GHST_LAST_BUFFER=""
typeset -g __GHST_PENDING_BUFFER=""
typeset -g __GHST_REQ_BUFFER=""
typeset -gi __GHST_PENDING_CURSOR=0
typeset -gi __GHST_GHOST_VISIBLE=0
typeset -g __GHST_RESPONSE_FD=""
typeset -gi __GHST_AUTOCOMPLETE_DISABLED=0

# ── Configuration ────────────────────────────────────────────────────────────
typeset -gi __GHST_DELAY=${__GHST_DELAY:-100}
typeset -gi __GHST_DELAY_SHORT=${__GHST_DELAY_SHORT:-50}
typeset -gi __GHST_DELAY_THRESHOLD=${__GHST_DELAY_THRESHOLD:-8}
typeset -gi __GHST_MIN_CHARS=${__GHST_MIN_CHARS:-2}
typeset -g  __GHST_GHOST_ESC=${__GHST_GHOST_ESC:-$'\e[38;5;243m'}

# ── Erase ghost text from terminal ───────────────────────────────────────────
__ghst_erase_ghost() {
    if (( __GHST_GHOST_VISIBLE )); then
        echo -n $'\e[0K' > /dev/tty
        __GHST_GHOST_VISIBLE=0
    fi
}

# ── Draw ghost text at cursor position ───────────────────────────────────────
__ghst_draw_ghost() {
    if [[ -n "$__GHST_SUGGESTION" ]]; then
        # Save cursor, print gray text, restore cursor
        # Uses 256-color 243 instead of \e[90m (bright black) which is
        # theme-dependent and invisible in some terminals (e.g. Ghostty)
        echo -n $'\e7'"${__GHST_GHOST_ESC}${__GHST_SUGGESTION}"$'\e[0m\e8' > /dev/tty
        __GHST_GHOST_VISIBLE=1
    fi
}

# ── Clear suggestion state + erase from screen ──────────────────────────────
__ghst_clear_suggestion() {
    __ghst_erase_ghost
    __GHST_SUGGESTION=""
    __GHST_LAST_BUFFER=""
}

# ── JSON-escape $1 into REPLY ────────────────────────────────────────────────
__ghst_json_str() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\t'/\\t}"
    s="${s//[[:cntrl:]]/}"
    REPLY="\"$s\""
}

# ── Send async request to daemon ─────────────────────────────────────────────
__ghst_send_request() {
    local buffer="$1" cursor_pos="$2"

    [[ -S "$__GHST_SOCKET" ]] || return 1

    # Build the JSON request in pure zsh (no subprocess per request)
    local -a hist parts
    local h REPLY
    hist=("${(@f)$(fc -ln -5 -1 2>/dev/null)}")
    for h in "${hist[@]}"; do
        [[ -n "$h" ]] || continue
        __ghst_json_str "$h"; parts+=("$REPLY")
    done
    # zsh history source: prefix matches from this shell's history, newest first
    local -a matches match_parts
    if [[ "$__GHST_HISTORY_SOURCE" == zsh ]] && (( ${__GHST_HISTORY_LIMIT:-5} > 0 )); then
        zmodload -F zsh/parameter p:history 2>/dev/null
        # (b) + ~ match the typed text literally, even with GLOB_SUBST set
        matches=( "${(@M)${(@v)history}:#${~${(b)buffer}}?*}" )
        matches=( "${(@u)matches:#*$'\n'*}" )
        for h in "${(@)matches[1,${__GHST_HISTORY_LIMIT:-5}]}"; do
            __ghst_json_str "$h"; match_parts+=("$REPLY")
        done
    fi
    local match_field=""
    [[ "$__GHST_HISTORY_SOURCE" == zsh ]] && match_field=",\"history_matches\":[${(j:,:)match_parts}]"

    __ghst_json_str "$buffer"; local buf_json="$REPLY"
    __ghst_json_str "$PWD"; local cwd_json="$REPLY"
    local json_req="{\"type\":\"complete\",\"request_id\":\"ac-$RANDOM\",\"buffer\":$buf_json,\"cursor_pos\":$cursor_pos,\"cwd\":$cwd_json,\"shell\":\"zsh\",\"history\":[${(j:,:)parts}],\"exit_status\":${__GHST_LAST_EXIT:-0},\"stream_history\":true$match_field}"
    __GHST_REQ_BUFFER="$buffer"

    # Close any previous in-flight request
    if [[ -n "$__GHST_RESPONSE_FD" ]]; then
        zle -F $__GHST_RESPONSE_FD 2>/dev/null
        exec {__GHST_RESPONSE_FD}<&- 2>/dev/null
        __GHST_RESPONSE_FD=""
    fi

    # Connect via zsh native socket
    zmodload zsh/net/socket 2>/dev/null || return 1
    if ! zsocket "$__GHST_SOCKET" 2>/dev/null; then
        return 1
    fi
    __GHST_RESPONSE_FD=$REPLY

    print -r -u $__GHST_RESPONSE_FD -- "$json_req"

    # Watch for response (as a widget, so BUFFER is readable in the handler)
    zle -F -w $__GHST_RESPONSE_FD __ghst_on_response
}

# ── Handle async response ────────────────────────────────────────────────────
__ghst_on_response() {
    local fd="$1"

    local response=""
    if ! read -r -u $fd response 2>/dev/null; then
        zle -F $fd
        exec {fd}<&- 2>/dev/null
        __GHST_RESPONSE_FD=""
        return
    fi

    # A "partial" line is the instant history match; the model's answer follows
    if [[ "$response" != *'"partial": true'* ]]; then
        zle -F $fd
        exec {fd}<&- 2>/dev/null
        __GHST_RESPONSE_FD=""
    fi

    # Extract suggestion (pure shell). It is always the last field.
    local suggestion=""
    if [[ "$response" == *'"suggestion": "'* ]]; then
        suggestion="${response#*\"suggestion\": \"}"
        suggestion="${suggestion%\"\}}"
        suggestion="${suggestion//\\\\/$'\x01'}"
        suggestion="${suggestion//\\\"/\"}"
        suggestion="${suggestion//\\n/$'\n'}"
        suggestion="${suggestion//\\t/$'\t'}"
        suggestion="${suggestion//$'\x01'/\\}"
    fi

    [[ -z "$suggestion" ]] && return
    (( ${#suggestion} > 200 )) && return

    # Filter out LLM artifacts (markdown, prose, multiline)
    [[ "$suggestion" == *'```'* ]] && return
    [[ "$suggestion" == *$'\n'* ]] && return
    [[ "$suggestion" == *"don't"* ]] && return
    [[ "$suggestion" == *"cannot"* ]] && return
    [[ "$suggestion" == *"I "* ]] && return
    [[ "$suggestion" == *"Sorry"* ]] && return

    # The user may have typed on since the request was sent: keep only the
    # part of the answer that still continues what is on the line now.
    (( CURSOR == ${#BUFFER} )) || return
    [[ "${BUFFER:0:${#__GHST_REQ_BUFFER}}" == "$__GHST_REQ_BUFFER" ]] || return
    local extra="${BUFFER:${#__GHST_REQ_BUFFER}}"
    [[ "${suggestion:0:${#extra}}" == "$extra" ]] || return
    suggestion="${suggestion:${#extra}}"
    [[ -z "$suggestion" || "$suggestion" == "$__GHST_SUGGESTION" ]] && return

    __ghst_erase_ghost
    __GHST_SUGGESTION="$suggestion"
    __GHST_LAST_BUFFER="$BUFFER"
    zle -R
    __ghst_draw_ghost
}

# ── Debounce ─────────────────────────────────────────────────────────────────
__ghst_schedule_complete() {
    __ghst_cancel_debounce

    # Skip autocomplete when in NL/history mode
    (( __GHST_AUTOCOMPLETE_DISABLED )) && return

    local buffer="$BUFFER"
    local cursor_pos="$CURSOR"

    (( ${#buffer} < __GHST_MIN_CHARS )) && return

    # Prefix reuse
    if [[ -n "$__GHST_SUGGESTION" && -n "$__GHST_LAST_BUFFER" ]]; then
        if [[ "$buffer" == "${__GHST_LAST_BUFFER}"* ]]; then
            local typed_extra="${buffer#${__GHST_LAST_BUFFER}}"
            if [[ "$__GHST_SUGGESTION" == "${typed_extra}"* ]]; then
                __GHST_SUGGESTION="${__GHST_SUGGESTION#${typed_extra}}"
                __GHST_LAST_BUFFER="$buffer"
                __ghst_draw_ghost
                return
            fi
        fi
    fi

    __GHST_SUGGESTION=""
    __GHST_LAST_BUFFER=""

    # Adaptive debounce
    local delay_ms=$__GHST_DELAY
    (( ${#buffer} >= __GHST_DELAY_THRESHOLD )) && delay_ms=$__GHST_DELAY_SHORT
    local delay_s
    delay_s=$(printf '0.%03d' "$delay_ms")

    __GHST_PENDING_BUFFER="$buffer"
    __GHST_PENDING_CURSOR=$cursor_pos

    exec {__GHST_TIMER_FD}< <(sleep "$delay_s" && echo fire)
    zle -F $__GHST_TIMER_FD __ghst_debounce_fired
}

__ghst_debounce_fired() {
    local fd="$1" dummy
    read -r dummy <&$fd 2>/dev/null
    zle -F $fd
    exec {fd}<&-
    __GHST_TIMER_FD=""
    __ghst_send_request "$__GHST_PENDING_BUFFER" "$__GHST_PENDING_CURSOR"
}

__ghst_cancel_debounce() {
    if [[ -n "$__GHST_TIMER_FD" ]]; then
        zle -F $__GHST_TIMER_FD 2>/dev/null
        exec {__GHST_TIMER_FD}<&- 2>/dev/null
        __GHST_TIMER_FD=""
    fi
}

# ── Accept full suggestion (→) ───────────────────────────────────────────────
__ghst_accept_suggestion() {
    if [[ -n "$__GHST_SUGGESTION" ]]; then
        __ghst_erase_ghost
        BUFFER="${BUFFER}${__GHST_SUGGESTION}"
        CURSOR=${#BUFFER}
        __GHST_SUGGESTION=""
        __GHST_LAST_BUFFER=""
    else
        zle forward-char
    fi
}

# ── Dismiss suggestion (Esc) ─────────────────────────────────────────────────
__ghst_dismiss() {
    if [[ -n "$__GHST_SUGGESTION" ]]; then
        __ghst_clear_suggestion
        __ghst_cancel_debounce
    fi
    # In vi mode Esc must still leave insert mode
    [[ "$KEYMAP" == (main|viins) && "$(bindkey -lL main)" == *viins* ]] && zle vi-cmd-mode
}

# ── Accept one word (Shift+→) ───────────────────────────────────────────────
__ghst_accept_word() {
    if [[ -n "$__GHST_SUGGESTION" ]]; then
        __ghst_erase_ghost
        __ghst_cancel_debounce
        if [[ -n "$__GHST_RESPONSE_FD" ]]; then
            zle -F $__GHST_RESPONSE_FD 2>/dev/null
            exec {__GHST_RESPONSE_FD}<&- 2>/dev/null
            __GHST_RESPONSE_FD=""
        fi
        local first_word="${__GHST_SUGGESTION%% *}"
        local rest="${__GHST_SUGGESTION#* }"
        if [[ "$first_word" == "$__GHST_SUGGESTION" ]]; then
            __ghst_accept_suggestion
            return
        fi
        BUFFER="${BUFFER}${first_word} "
        CURSOR=${#BUFFER}
        __GHST_SUGGESTION="$rest"
        __GHST_LAST_BUFFER="$BUFFER"
        zle -R
        __ghst_draw_ghost
    else
        zle forward-char
    fi
}

# ── Tab: accept or native complete ───────────────────────────────────────────
__ghst_tab_accept() {
    if [[ -n "$__GHST_SUGGESTION" ]]; then
        __ghst_accept_suggestion
    else
        zle expand-or-complete
    fi
}

# ── Self-insert ──────────────────────────────────────────────────────────────
__ghst_self_insert() {
    # Typing the suggested text keeps the rest of the suggestion on screen
    if [[ -n "$__GHST_SUGGESTION" && "$KEYS" == "${__GHST_SUGGESTION[1]}" ]] \
        && (( CURSOR == ${#BUFFER} )); then
        zle .self-insert
        __GHST_SUGGESTION="${__GHST_SUGGESTION:1}"
        __GHST_LAST_BUFFER="$BUFFER"
        if [[ -n "$__GHST_SUGGESTION" ]]; then
            zle -R
            __ghst_draw_ghost
        else
            __GHST_GHOST_VISIBLE=0
            __ghst_schedule_complete
        fi
        return
    fi
    __ghst_clear_suggestion
    zle .self-insert
    __ghst_schedule_complete
}

# ── Backspace ────────────────────────────────────────────────────────────────
__ghst_backward_delete() {
    __ghst_clear_suggestion
    zle .backward-delete-char
    if (( ${#BUFFER} >= __GHST_MIN_CHARS )); then
        __ghst_schedule_complete
    fi
}

# ── Enter: clean up before execution ─────────────────────────────────────────
__ghst_line_finish() {
    __ghst_clear_suggestion
    __ghst_cancel_debounce
    if [[ -n "$__GHST_RESPONSE_FD" ]]; then
        zle -F $__GHST_RESPONSE_FD 2>/dev/null
        exec {__GHST_RESPONSE_FD}<&- 2>/dev/null
        __GHST_RESPONSE_FD=""
    fi
    zle .accept-line
}

# ── Register widgets ─────────────────────────────────────────────────────────
zle -N __ghst_self_insert
zle -N __ghst_backward_delete
zle -N __ghst_accept_suggestion
zle -N __ghst_accept_word
zle -N __ghst_tab_accept
zle -N __ghst_line_finish
zle -N __ghst_dismiss
zle -N __ghst_on_response

# ── Keybindings ──────────────────────────────────────────────────────────────
bindkey -M main -R ' '-'~' __ghst_self_insert   # All printable chars
bindkey '^?' __ghst_backward_delete              # Backspace
bindkey '^[[C' __ghst_accept_suggestion          # Right arrow — accept full
bindkey '^[OC' __ghst_accept_suggestion          # Right arrow (application mode)
bindkey '^[[1;2C' __ghst_accept_word             # Shift+Right — accept word
bindkey '\t' __ghst_tab_accept                   # Tab
bindkey '^M' __ghst_line_finish                  # Enter
bindkey '\e' __ghst_dismiss                      # Esc — dismiss suggestion
