# seer zsh integration
# Source this file in your ~/.zshrc:
#   source "$(seer --shell-path zsh)"
# or manually:
#   source /path/to/seer/shell/seer.zsh

# Only load in zsh
[ -n "$ZSH_VERSION" ] || return 0

if [ "$(uname)" = "Darwin" ]; then
    _seer_context_file="${XDG_CACHE_HOME:-$HOME/Library/Caches}/seer/context"
else
    _seer_context_file="${XDG_CACHE_HOME:-$HOME/.cache}/seer/context"
fi

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
        local target_pane=""
        [ -n "$TMUX_PANE" ] && target_pane="-t $TMUX_PANE"
        # Try capturing pane. If it fails or outputs nothing, fallback
        if ! tmux capture-pane $target_pane -p -S -200 2>/dev/null > "$_seer_context_file" || [ ! -s "$_seer_context_file" ]; then
            _seer_fallback=1
        fi
    else
        _seer_fallback=1
    fi

    if [ "$_seer_fallback" -eq 1 ]; then
        {
            echo "$ ${last_cmd}"
            if [ "$exit_code" -ne 0 ]; then
                echo "[exited with code $exit_code]"
            fi
        } > "$_seer_context_file"
    fi
    return $exit_code
}

autoload -Uz add-zsh-hook
add-zsh-hook precmd _seer_save_context
