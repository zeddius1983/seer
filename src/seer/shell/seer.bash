# seer bash integration
# Source this file in your ~/.bashrc:
#   source "$(seer --shell-path bash)"
# or manually:
#   source /path/to/seer/shell/seer.bash

if [ "$(uname)" = "Darwin" ]; then
    _seer_context_file="${XDG_CACHE_HOME:-$HOME/Library/Caches}/seer/context"
else
    _seer_context_file="${XDG_CACHE_HOME:-$HOME/.cache}/seer/context"
fi

# Capture terminal context after each command.
# Uses tmux if available (captures real screen output including stderr).
# Falls back to saving the last command from history.
_seer_save_context() {
    local exit_code=$?
    mkdir -p "$(dirname "$_seer_context_file")"

    local last_cmd
    last_cmd=$(fc -ln -1 2>/dev/null | sed 's/^[[:space:]]*//')

    # Don't overwrite context when the last command was seer itself —
    # keep the previous command's context so 'seer help' sees the real output.
    case "$last_cmd" in
        seer*) return $exit_code ;;
    esac

    local _seer_fallback=0
    if [ -n "$TMUX" ]; then
        # tmux: capture last 200 lines of pane scrollback (includes stdout+stderr)
        local target_pane=""
        [ -n "$TMUX_PANE" ] && target_pane="-t $TMUX_PANE"
        if ! tmux capture-pane $target_pane -p -S -200 2>/dev/null > "$_seer_context_file" || [ ! -s "$_seer_context_file" ]; then
            _seer_fallback=1
        fi
    else
        _seer_fallback=1
    fi

    if [ "$_seer_fallback" -eq 1 ]; then
        # No tmux or capture failed: save last command + exit code as minimal context
        {
            echo "$ ${last_cmd}"
            if [ "$exit_code" -ne 0 ]; then
                echo "[exited with code $exit_code]"
            fi
        } > "$_seer_context_file"
    fi
    return $exit_code
}

# Prepend to PROMPT_COMMAND (idempotent)
if [[ "$PROMPT_COMMAND" != *"_seer_save_context"* ]]; then
    PROMPT_COMMAND="_seer_save_context${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
fi
