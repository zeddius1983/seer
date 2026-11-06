#!/bin/sh
# The directory and git segments of the `seer --tui` status bar. tmux runs this
# every few seconds for the active pane, and draws the tmux styles it prints.
#
# Usage: seer-status.sh <path> <sep> <right cap> <dir icon> <branch icon>
#
# The group it continues is drawn in tui.py, on the orange background below.

path=$1 sep=$2 cap_right=$3 dir_icon=$4 branch_icon=$5

ink='#282828' fg0='#fbf1c7' yellow='#d79921' aqua='#689d6a' orange='#d65d0e'

# ~/D/seer: last directory in full, the others to one letter.
case "$path" in
    "$HOME") shown='~' ;;
    "$HOME"/*) shown="~/${path#"$HOME"/}" ;;
    *) shown=$path ;;
esac
shown=$(printf '%s' "$shown" | awk -F/ '{
    for (i = 1; i < NF; i++) if ($i != "~" && $i != "") $i = substr($i, 1, 1)
    print
}' OFS=/)

# One git call, one awk pass: this runs all the time, and in big repositories.
git_state=$(git --no-optional-locks -C "$path" status --porcelain=v2 --branch 2>/dev/null | awk '
    /^# branch\.head / { head = $3 }
    /^# branch\.ab /   { ahead = $3 + 0; behind = -($4 + 0) }
    /^[12u] /          { tracked++ }
    /^\? /             { untracked++ }
    END {
        if (head == "") exit
        if (tracked)   out = out " ●" tracked
        if (untracked) out = out " ?" untracked
        if (ahead)     out = out " ⇡" ahead
        if (behind)    out = out " ⇣" behind
        print (tracked ? "dirty" : "clean") " " head out
    }')

# A # in a directory or branch name would otherwise start a tmux style.
escape() { printf '%s' "$1" | sed 's/#/##/g'; }

printf '#[fg=%s,bg=%s]%s#[fg=%s,bg=%s] %s%s ' \
    "$orange" "$yellow" "$sep" "$ink" "$yellow" "$dir_icon" "$(escape "$shown")"
last=$yellow
if [ -n "$git_state" ]; then
    case "$git_state" in
        dirty*) git_bg=$orange ;;
        *) git_bg=$aqua ;;
    esac
    printf '#[fg=%s,bg=%s]%s#[fg=%s,bg=%s] %s%s ' \
        "$last" "$git_bg" "$sep" "$fg0" "$git_bg" "$branch_icon" "$(escape "${git_state#* }")"
    last=$git_bg
fi
printf '#[fg=%s,bg=default]%s#[default]' "$last" "$cap_right"
