#!/bin/bash
# GitSync setup: writes .env and installs everything with uv.
#
#   bash build.sh
#
# Asks only what is needed - a GitHub user and token, and GitLab if you want to
# push there - with everything else behind an "advanced settings" question.
# Settings you leave blank are not written, so GitSync's own defaults apply.
# Nothing is run at the end: it tells you what to run.

set -e

# Work from the project folder, wherever the script was started from - so .env
# and .venv always land next to main.py
cd "$(dirname "${BASH_SOURCE[0]}")"

MIN_UV="0.12.22"

# Token pages, opened with the right boxes already ticked (both sites read these
# query parameters): GitHub's classic token with the "repo" scope, GitLab's
# personal access token with "api" + "write_repository"
GITHUB_TOKEN_URL="https://github.com/settings/tokens/new?scopes=repo&description=GitSync"
GITLAB_TOKEN_URL="https://gitlab.com/-/user_settings/personal_access_tokens?name=GitSync&scopes=api,write_repository"

# --- looks --------------------------------------------------------------------
# Colours only on a real terminal, and never when NO_COLOR is set
# (https://no-color.org) - so piping or logging the output stays plain text
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; BLUE=$'\033[34m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; CYAN=""; RESET=""
fi

step()  { printf "\n${BOLD}${BLUE}━━━ %s ━━━${RESET}\n\n" "$1"; }
say()   { printf "  %s\n" "$1"; }
todo()  { printf "  ${CYAN}➜${RESET} %s\n" "$1"; }
ok()    { printf "  ${GREEN}✔${RESET} %s\n" "$1"; }
warn()  { printf "  ${YELLOW}⚠${RESET} %s\n" "$1"; }
fail()  { printf "  ${RED}✖ %s${RESET}\n" "$1"; }
link()  { printf "    ${BOLD}%s${RESET}\n" "$1"; }

# Read a token without showing it: every character typed or pasted appears as
# a dot, Backspace works, and once Enter is pressed the line is redrawn with
# only the last 2 characters visible - enough to check it's the right token.
#   read_secret VARIABLE "  Prompt: "
read_secret() {
    local var="$1" prompt="$2" secret="" char mask="" i
    printf "%s" "$prompt"
    # One character at a time: -s don't echo, -n 1 one char, -r keep backslashes,
    # IFS= keep spaces. Enter arrives as an empty character.
    while IFS= read -r -s -n 1 char; do
        if [ -z "$char" ]; then
            break
        elif [ "$char" = $'\x7f' ] || [ "$char" = $'\b' ]; then
            if [ -n "$secret" ]; then
                secret="${secret%?}"
                printf '\b \b'                 # erase the last dot
            fi
        else
            secret="${secret}${char}"
            printf '•'
        fi
    done
    # Redraw: dots for all but the last 2 characters
    if [ ${#secret} -gt 2 ]; then
        # ${mask}, not $mask: before a non-ASCII character like •, bash 3.2
        # (macOS's /bin/bash) reads part of the character as the variable name
        for (( i = 0; i < ${#secret} - 2; i++ )); do mask="${mask}•"; done
        mask="${mask}${secret: -2}"
    else
        mask="${secret//?/•}"
    fi
    printf '\r\033[K%s%s\n' "$prompt" "$mask"
    printf -v "$var" '%s' "$secret"
}

# Ctrl-C: say what state things were left in, instead of just stopping. STAGE
# is moved along as the script goes. Echo is switched back on in case the
# interrupt came in the middle of typing a hidden token.
STAGE="checks"
on_interrupt() {
    stty echo 2>/dev/null || true
    printf "\n\n"
    case "$STAGE" in
        questions)
            warn "Setup cancelled - nothing was saved."
            if [ -f .env ]; then
                say "  Your existing .env is untouched."
            fi
            ;;
        installing)
            warn "Setup cancelled while installing - your .env is saved."
            say "  Run 'bash build.sh' again and choose (k) to keep it, to finish installing."
            ;;
        *)
            warn "Setup cancelled."
            ;;
    esac
    todo "Start again any time:  ${BOLD}bash build.sh${RESET}"
    echo
    exit 130
}
trap on_interrupt INT

printf "\n${BOLD}  🚀 GitSync setup${RESET}\n"
printf "  ${DIM}Back up your GitHub repos - to GitLab, or as zip files.${RESET}\n"

# --- uv: present and new enough (uv.lock is written by uv >= MIN_UV) --------
step "Checking your tools"
if ! command -v uv >/dev/null 2>&1; then
    fail "uv is not installed."
    todo "Install it:  https://docs.astral.sh/uv/getting-started/installation/"
    todo "Then run:    bash build.sh"
    exit 1
fi
UV_VERSION="$(uv --version | awk '{print $2}')"
# Compare dotted versions field by field: is UV_VERSION >= MIN_UV?
if ! awk -v have="$UV_VERSION" -v need="$MIN_UV" 'BEGIN {
        split(have, h, "."); split(need, n, ".")
        for (i = 1; i <= 3; i++) {
            if (h[i] + 0 > n[i] + 0) exit 0
            if (h[i] + 0 < n[i] + 0) exit 1
        }
        exit 0
    }'; then
    fail "uv $UV_VERSION is too old - GitSync needs uv $MIN_UV or newer."
    todo "Update it:  uv self update"
    todo "Then run:   bash build.sh"
    exit 1
fi
ok "uv $UV_VERSION"
if command -v git >/dev/null 2>&1; then
    ok "git $(git --version | awk '{print $3}')"
else
    fail "git is not installed - GitSync needs it to clone your repos."
    exit 1
fi
# Python is uv's job: `uv sync` downloads Python 3.14 if it's missing, so this
# only reports which one will be used - it never stops the script. Without an
# argument, `uv python find` asks for what the project wants (.python-version).
PYTHON_PATH="$(uv python find 2>/dev/null || true)"
if [ -n "$PYTHON_PATH" ]; then
    ok "$("$PYTHON_PATH" --version 2>&1)"
else
    warn "Python 3.14 not found - uv will download it while installing (no action needed)"
fi

# --- .env: never overwrite one without asking ---------------------------------
WRITE_ENV=yes
if [ -f .env ]; then
    step "Your settings"
    warn "A .env file already exists."
    read -p "  Keep it as it is (k) or replace it with new answers (r)? [k]: " ANSWER
    case "$ANSWER" in
        r|R) WRITE_ENV=yes ;;
        *)   WRITE_ENV=no; ok "Keeping your existing .env." ;;
    esac
fi

GITLAB_USER=""
GITLAB_TOKEN=""
if [ "$WRITE_ENV" = yes ]; then
    STAGE="questions"
    # --- GitHub (required) ----------------------------------------------------
    step "Step 1 of 3 · GitHub (required)"
    say "GitSync reads your repositories with a GitHub token. To create one:"
    echo
    todo "Open this link (it ticks the right box for you):"
    link "$GITHUB_TOKEN_URL"
    todo "Token type: ${BOLD}Personal access token (classic)${RESET}"
    todo "Scope:      ${GREEN}✔${RESET} ${BOLD}repo${RESET}  ${DIM}(already ticked by the link)${RESET}"
    todo "Expiration: your choice - when it runs out, just run ${BOLD}bash build.sh${RESET} again"
    todo "Click ${BOLD}Generate token${RESET} and copy it ${DIM}(it starts with ghp_)${RESET}"
    echo
    read -p "  GitHub username: " GITHUB_USER
    if [ -z "$GITHUB_USER" ]; then
        fail "A GitHub username is required."
        exit 1
    fi
    # Shown as dots, last 2 characters visible after Enter
    read_secret GITHUB_TOKEN "  GitHub token (paste it, then press Enter): "
    if [ -z "$GITHUB_TOKEN" ]; then
        fail "A GitHub token is required."
        exit 1
    fi
    ok "GitHub set up."

    # --- GitLab (optional) ----------------------------------------------------
    # GitLab is only needed for 'sync'; 'archive' works with the GitHub token alone
    step "Step 2 of 3 · GitLab (optional)"
    say "Only needed to mirror your repos to GitLab ('sync')."
    say "Zip backups ('archive') work without it - press Enter to skip."
    echo
    todo "To create a GitLab token, open this link (it ticks the right boxes):"
    link "$GITLAB_TOKEN_URL"
    todo "Scopes:     ${GREEN}✔${RESET} ${BOLD}api${RESET}   ${GREEN}✔${RESET} ${BOLD}write_repository${RESET}  ${DIM}(already ticked by the link)${RESET}"
    todo "Expiration: your choice"
    todo "Click ${BOLD}Create token${RESET} and copy it ${DIM}(it starts with glpat-)${RESET}"
    echo
    read -p "  GitLab username (press Enter to skip GitLab): " GITLAB_USER
    if [ -n "$GITLAB_USER" ]; then
        read_secret GITLAB_TOKEN "  GitLab token (paste it, then press Enter): "
        if [ -z "$GITLAB_TOKEN" ]; then
            fail "A GitLab username needs a GitLab token."
            todo "Run ${BOLD}bash build.sh${RESET} again and give both - or press Enter at the username to skip GitLab."
            exit 1
        fi
        read -p "  GitLab group to push into instead of your own namespace (optional): " GITLAB_GROUP
        read -p "  Visibility on GitLab - auto (match GitHub), private or public [auto]: " REPO_VISIBILITY
        echo
        say "GitLab protects each project's main branch and refuses force pushes to it."
        say "If you ever rewrite history on GitHub (amend, rebase, force push), the mirror"
        say "can't be updated - unless GitSync may turn on 'allowed to force push' for"
        say "those branches when that happens (only then, only on the affected project)."
        read -p "  Let GitSync allow force pushes when needed? [Y/n]: " FORCE_PUSH
        case "$FORCE_PUSH" in
            n|N) GITLAB_ALLOW_FORCE_PUSH=false ;;
            *)   GITLAB_ALLOW_FORCE_PUSH=true ;;
        esac
        ok "GitLab set up."
    else
        warn "Skipping GitLab: 'archive' will work, 'sync' won't until GitLab is set up."
    fi

    # --- extras -----------------------------------------------------------------
    # Everything else has a sensible default; only ask if wanted
    step "Step 3 of 3 · Extras (optional)"
    read -p "  Change advanced settings (parallelism, forks, folders, log retention)? [y/N]: " ADVANCED
    case "$ADVANCED" in
        y|Y)
            say "${DIM}Press Enter to keep the default shown in brackets.${RESET}"
            read -p "  CPU_LOAD_PERCENT - share of CPU cores to keep busy, 1-100 [70]: " CPU_LOAD_PERCENT
            read -p "  MAX_WORKERS - exact number of repos in parallel, 0 = from CPU_LOAD_PERCENT [0]: " MAX_WORKERS
            read -p "  INCLUDE_FORKS - back up your forks too, true/false [true]: " INCLUDE_FORKS
            read -p "  BACKUP_DIR - where local mirrors are kept [./repos-backup]: " BACKUP_DIR
            read -p "  ARCHIVE_DIR - where 'archive' writes zip files [./archives]: " ARCHIVE_DIR
            read -p "  LOGS_DIR - where log files go [./Logs]: " LOGS_DIR
            read -p "  LOG_RETENTION_DAYS - days to keep log files, 0 = forever [30]: " LOG_RETENTION_DAYS
            read -p "  CODE_ARCHIVE_EXCLUDE_DIRS - folders 'archive --mode code' strips, comma-separated [built-in list]: " CODE_ARCHIVE_EXCLUDE_DIRS
            ;;
        *) ok "Using the defaults." ;;
    esac

    # Write .env: required settings, then only the optional ones given - a blank
    # answer never overrides a default (and never puts a local path where the
    # Docker image expects its /data defaults)
    {
        echo "# Written by build.sh - see .env.example for every setting."
        echo "GITHUB_USER=$GITHUB_USER"
        echo "GITHUB_TOKEN=$GITHUB_TOKEN"
        for name in GITLAB_USER GITLAB_TOKEN GITLAB_GROUP REPO_VISIBILITY GITLAB_ALLOW_FORCE_PUSH \
                    CPU_LOAD_PERCENT MAX_WORKERS INCLUDE_FORKS BACKUP_DIR \
                    ARCHIVE_DIR LOGS_DIR LOG_RETENTION_DAYS CODE_ARCHIVE_EXCLUDE_DIRS; do
            value="${!name}"
            [ -n "$value" ] && echo "$name=$value"
        done
    } > .env
    # The file holds tokens: readable by you only
    chmod 600 .env
    ok ".env written ${DIM}(readable only by you)${RESET}"
else
    # Kept .env: read whether GitLab is set, for the "what next" hint below
    GITLAB_TOKEN="$(grep -E '^GITLAB_TOKEN=' .env | tail -1 | cut -d= -f2-)"
    [ "$GITLAB_TOKEN" = "your-gitlab-token" ] && GITLAB_TOKEN=""
fi

# --- install ------------------------------------------------------------------
step "Installing"
STAGE="installing"
# A .venv whose Python no longer exists - the folder was copied from another
# machine, moved, or its Python was upgraded or removed. uv would notice too,
# but only with an alarming "Ignoring existing virtual environment linked to
# non-existent Python interpreter" warning. It is harmless (the environment is
# just rebuilt), so do it here and say so plainly. Safe to delete: .venv only
# holds installed packages, recreated in seconds - settings and backups live
# elsewhere.
if [ -e .venv ] && ! .venv/bin/python -c "" >/dev/null 2>&1; then
    warn "Your .venv was made with a Python that isn't on this machine any more"
    say  "  (copied from another computer, moved, or Python was upgraded). That's fine -"
    say  "  rebuilding it now. Nothing else is affected."
    echo
    rm -rf .venv
fi
say "uv installs everything, and downloads Python 3.14 if you don't have it..."
echo
uv sync
echo
ok "Installed."

# Nothing is run from here - just say what can be run, and what can't yet
STAGE="done"
step "All set! 🎉"
say "Nothing has been run yet - start it yourself when you're ready:"
echo
todo "${BOLD}uv run python main.py archive${RESET}   ${DIM}# fetch your repos from GitHub and zip them${RESET}"
if [ -n "$GITLAB_TOKEN" ]; then
    todo "${BOLD}uv run python main.py${RESET}           ${DIM}# mirror your repos to GitLab ('sync')${RESET}"
else
    echo
    warn "GitLab isn't set up, so only 'archive' is available - 'sync' needs a GitLab"
    say  "  user and token. To add them later, run 'bash build.sh' again and choose (r),"
    say  "  or add GITLAB_USER and GITLAB_TOKEN to .env."
fi
echo
todo "Run it automatically on a schedule:  ${BOLD}bash deploy.sh${RESET}"
echo
