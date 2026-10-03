#!/bin/bash
# GitSync in Docker: asks a few questions, then starts the container.
#
#   bash docker-setup.sh
#
# Like build.sh, but for Docker: nothing to install besides Docker itself. It
# can keep GitSync running in the background on a schedule (docker run -d), or
# run it once now. If a .env exists, its values are offered as the defaults -
# press Enter to keep each one. Nothing is written until the very last
# question, and the settings can be saved to .env or handed straight to Docker.
# At the end it shows how to follow the logs, read earlier runs, find the zips
# and stop or update the container.

set -e

# Work from the project folder, wherever the script was started from - .env
# and the data folder land next to it
cd "$(dirname "${BASH_SOURCE[0]}")"

REGISTRY_IMAGE="ghcr.io/scienmanas/gitsync"
CONTAINER="gitsync"

# Token pages, opened with the right boxes already ticked (same as build.sh)
GITHUB_TOKEN_URL="https://github.com/settings/tokens/new?scopes=repo&description=GitSync"
GITLAB_TOKEN_URL="https://gitlab.com/-/user_settings/personal_access_tokens?name=GitSync&scopes=api,write_repository"

# Everything this script may set, in the order it is written to .env
SETTINGS="GITHUB_USER GITHUB_TOKEN GITLAB_USER GITLAB_TOKEN GITLAB_GROUP REPO_VISIBILITY
          GITLAB_ALLOW_FORCE_PUSH CRON_SCHEDULE CRON_COMMAND RUN_ON_START TZ
          LOG_RETENTION_DAYS INCLUDE_FORKS MAX_WORKERS CODE_ARCHIVE_EXCLUDE_DIRS"

# --- looks (same as build.sh) -------------------------------------------------
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
cmd()   { printf "    ${BOLD}%s${RESET}\n" "$1"; }

# Dots for all but the last 2 characters: how a token is shown back to you
masked() {
    local secret="$1" mask="" i
    if [ ${#secret} -gt 2 ]; then
        # ${mask}, not $mask: bash 3.2 (macOS) misreads $mask• - see build.sh
        for (( i = 0; i < ${#secret} - 2; i++ )); do mask="${mask}•"; done
        printf '%s' "${mask}${secret: -2}"
    else
        printf '%s' "${secret//?/•}"
    fi
}

# Read a token without showing it: dots while typing or pasting, and after
# Enter only the last 2 characters are shown (same as build.sh).
read_secret() {
    local var="$1" prompt="$2" secret="" char
    printf "%s" "$prompt"
    while IFS= read -r -s -n 1 char; do
        if [ -z "$char" ]; then
            break
        elif [ "$char" = $'\x7f' ] || [ "$char" = $'\b' ]; then
            if [ -n "$secret" ]; then
                secret="${secret%?}"
                printf '\b \b'
            fi
        else
            secret="${secret}${char}"
            printf '•'
        fi
    done
    printf '\r\033[K%s%s\n' "$prompt" "$(masked "$secret")"
    printf -v "$var" '%s' "$secret"
}

# Ask with a default: Enter keeps DEFAULT. ask VARIABLE "Question" DEFAULT
ask() {
    local var="$1" question="$2" default="$3" answer
    if [ -n "$default" ]; then
        read -p "  $question [$default]: " answer
    else
        read -p "  $question: " answer
    fi
    printf -v "$var" '%s' "${answer:-$default}"
}

# Pick one of several options with the arrow keys (↑/↓ or j/k, a number jumps
# straight to it, Enter chooses). Without a terminal - piped input, tests -
# it falls back to typing the number, so scripted answers still work.
#   choose VARIABLE DEFAULT_NUMBER "Title" "option 1" "option 2" ...
# VARIABLE gets the chosen option's number, counting from 1.
choose() {
    local var="$1" default="$2" title="$3"
    shift 3
    local count=$# current=$(( default - 1 )) key rest i
    local options=("$@")

    if [ ! -t 0 ] || [ ! -t 1 ]; then
        say "$title"
        for (( i = 0; i < count; i++ )); do say "  $(( i + 1 ))) ${options[$i]}"; done
        read -p "  Choice [$default]: " key
        key="${key:-$default}"
        if ! [[ "$key" =~ ^[0-9]+$ ]] || [ "$key" -lt 1 ] || [ "$key" -gt "$count" ]; then
            fail "Choose a number from 1 to $count."
            exit 1
        fi
        printf -v "$var" '%s' "$key"
        return
    fi

    printf "  ${BOLD}%s${RESET}  ${DIM}↑/↓ to move, Enter to choose${RESET}\n" "$title"
    printf '\033[?25l'                       # hide the cursor while the menu is up
    MENU_OPEN=yes
    while true; do
        for (( i = 0; i < count; i++ )); do
            if [ "$i" -eq "$current" ]; then
                printf "  ${CYAN}❯ %s${RESET}\033[K\n" "${options[$i]}"
            else
                printf "    %s\033[K\n" "${options[$i]}"
            fi
        done
        IFS= read -r -s -n 1 key
        # Arrow keys arrive as ESC [ A / ESC [ B
        if [ "$key" = $'\033' ]; then
            read -r -s -n 2 rest
            key="${key}${rest}"
        fi
        case "$key" in
            $'\033[A'|k) current=$(( (current - 1 + count) % count )) ;;
            $'\033[B'|j) current=$(( (current + 1) % count )) ;;
            "") break ;;
            [1-9]) [ "$key" -le "$count" ] && current=$(( key - 1 )) ;;
        esac
        printf '\033[%dA' "$count"             # back to the top, draw it again
    done
    printf '\033[?25h'
    MENU_OPEN=no
    printf -v "$var" '%s' "$(( current + 1 ))"
}

# Set KEY=VALUE in .env: replace the line if it is there, add it if not, leave
# every other line alone. An empty VALUE removes the line (the default applies).
set_env() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    grep -v -E "^${key}=" .env > "$tmp" 2>/dev/null || true
    [ -n "$value" ] && echo "${key}=${value}" >> "$tmp"
    cat "$tmp" > .env          # cat, not mv: keeps .env's permissions
    rm -f "$tmp"
}

# Read .env's values as CURRENT_<NAME>, for the defaults - parsed line by line,
# never sourced: a value like "0 2 * * *" must stay text, not run as a command
load_env() {
    local line key
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        key="${line%%=*}"
        case " $(echo $SETTINGS) " in *" $key "*) ;; *) continue ;; esac
        printf -v "CURRENT_$key" '%s' "${line#*=}"
    done < .env
}

# The image versions on GHCR, newest first ("2.0.0" etc. - not "latest" or the
# sha- tags). Empty when the registry can't be reached. Public packages answer
# with an anonymous token, so no login is needed.
list_versions() {
    local token
    token="$(curl -fsS --max-time 5 \
        "https://ghcr.io/token?scope=repository:${REGISTRY_IMAGE#ghcr.io/}:pull" 2>/dev/null \
        | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')" || return 0
    [ -n "$token" ] || return 0
    curl -fsS --max-time 5 -H "Authorization: Bearer $token" \
        "https://${REGISTRY_IMAGE%%/*}/v2/${REGISTRY_IMAGE#ghcr.io/}/tags/list" 2>/dev/null \
        | tr ',[' '\n\n' | sed -n 's/^"\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)"\]*}*$/\1/p' \
        | sort -t. -k1,1nr -k2,2nr -k3,3nr || true
}

# A version's release date from CHANGELOG.md ("## [2.0.0] - 2026-10-03" or
# "## [2.0.0][2.0.0] - ..."), or nothing
release_date() {
    [ -f CHANGELOG.md ] || return 0
    sed -n "s/^## \[$1\][^-]*- \([0-9-]*\).*/\1/p" CHANGELOG.md | head -1
}

# Ctrl-C: say what state things were left in (same idea as build.sh)
STAGE="checks"
MENU_OPEN=no
on_interrupt() {
    stty echo 2>/dev/null || true
    printf '\033[?25h'                       # the cursor back, if a menu hid it
    printf "\n\n"
    case "$STAGE" in
        questions) warn "Setup cancelled - nothing was changed." ;;
        starting)  warn "Setup cancelled while starting the container."
                   say  "  Run 'bash docker-setup.sh' again to finish." ;;
        *)         warn "Setup cancelled." ;;
    esac
    todo "Start again any time:  ${BOLD}bash docker-setup.sh${RESET}"
    echo
    exit 130
}
trap on_interrupt INT

printf "\n${BOLD}  🐳 GitSync Docker setup${RESET}\n"
printf "  ${DIM}Back up your GitHub repos - to GitLab, or as zip files - from a container.${RESET}\n"

# --- tools ---------------------------------------------------------------------
step "Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    fail "Docker is not installed."
    todo "Install it:  https://docs.docker.com/get-docker/"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    fail "Docker is installed but not running."
    todo "Start Docker Desktop (or the Docker service), then run:  bash docker-setup.sh"
    exit 1
fi
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"

# --- image -----------------------------------------------------------------------
step "Step 1 of 6 · Which version"
VERSIONS="$(list_versions)"
OPTIONS=("latest  ${DIM}- always the newest release${RESET}")
TAGS=("latest")
for version in $VERSIONS; do
    date="$(release_date "$version")"
    OPTIONS+=("$version  ${DIM}${date:+- released $date}${RESET}")
    TAGS+=("$version")
done
if [ -f Dockerfile ]; then
    OPTIONS+=("Build it from this folder  ${DIM}- your local changes included${RESET}")
    TAGS+=("@build")
fi
OPTIONS+=("Another tag...  ${DIM}- type one, e.g. sha-6957340${RESET}")
TAGS+=("@other")
[ -z "$VERSIONS" ] && say "${DIM}(Couldn't list the versions on ghcr.io - showing what's always there.)${RESET}"
choose PICK 1 "Run which version of GitSync?" "${OPTIONS[@]}"
TAG="${TAGS[$(( PICK - 1 ))]}"
if [ "$TAG" = "@other" ]; then
    ask TAG "Tag to run" ""
    [ -n "$TAG" ] || { fail "No tag given."; exit 1; }
fi

if [ "$TAG" = "@build" ]; then
    IMAGE="gitsync:local"
    say "Building $IMAGE - the first time takes a minute..."
    echo
    docker build -t "$IMAGE" .
    echo
    ok "Built $IMAGE"
else
    IMAGE="$REGISTRY_IMAGE:$TAG"
    say "Downloading $IMAGE..."
    if ! docker pull -q "$IMAGE" >/dev/null; then
        fail "Could not download $IMAGE."
        todo "Check the tag and your internet connection - or build it from this folder."
        exit 1
    fi
    ok "Got $IMAGE"
fi

# --- settings: .env values as the defaults --------------------------------------
STAGE="questions"
for name in $SETTINGS; do printf -v "CURRENT_$name" '%s' ""; done
if [ -f .env ]; then
    echo
    warn "Found a .env with settings from before."
    read -p "  Use its values as the defaults? Enter keeps each one, or type a new one. [Y/n]: " USE_ENV
    case "$USE_ENV" in
        n|N) ok "Starting fresh - your .env is only touched if you save at the end." ;;
        *)   load_env; ok "Using your .env values as the defaults." ;;
    esac
fi

step "Step 2 of 6 · GitHub (required)"
if [ -z "$CURRENT_GITHUB_TOKEN" ]; then
    say "GitSync reads your repositories with a GitHub token. To create one:"
    echo
    todo "Open this link (it ticks the right box for you):"
    link "$GITHUB_TOKEN_URL"
    todo "Token type: ${BOLD}Personal access token (classic)${RESET}"
    todo "Scope:      ${GREEN}✔${RESET} ${BOLD}repo${RESET}  ${DIM}(already ticked by the link)${RESET}"
    todo "Click ${BOLD}Generate token${RESET} and copy it ${DIM}(it starts with ghp_)${RESET}"
    echo
fi
ask GITHUB_USER "GitHub username" "$CURRENT_GITHUB_USER"
[ -n "$GITHUB_USER" ] || { fail "A GitHub username is required."; exit 1; }
if [ -n "$CURRENT_GITHUB_TOKEN" ]; then
    read_secret GITHUB_TOKEN "  GitHub token (Enter keeps $(masked "$CURRENT_GITHUB_TOKEN")): "
    GITHUB_TOKEN="${GITHUB_TOKEN:-$CURRENT_GITHUB_TOKEN}"
else
    read_secret GITHUB_TOKEN "  GitHub token (paste it, then press Enter): "
fi
[ -n "$GITHUB_TOKEN" ] || { fail "A GitHub token is required."; exit 1; }
ok "GitHub set up."

step "Step 3 of 6 · GitLab (optional)"
say "Only needed to mirror your repos to GitLab ('sync')."
say "Zip backups ('archive') work without it."
if [ -z "$CURRENT_GITLAB_TOKEN" ]; then
    echo
    todo "To create a GitLab token, open this link (it ticks the right boxes):"
    link "$GITLAB_TOKEN_URL"
    todo "Scopes: ${GREEN}✔${RESET} ${BOLD}api${RESET}   ${GREEN}✔${RESET} ${BOLD}write_repository${RESET}  ${DIM}(already ticked by the link)${RESET}"
    todo "Click ${BOLD}Create token${RESET} and copy it ${DIM}(it starts with glpat-)${RESET}"
fi
echo
if [ -n "$CURRENT_GITLAB_USER" ]; then
    ask GITLAB_USER "GitLab username (type - to stop using GitLab)" "$CURRENT_GITLAB_USER"
    [ "$GITLAB_USER" = "-" ] && GITLAB_USER=""
else
    ask GITLAB_USER "GitLab username (press Enter to skip GitLab)" ""
fi
GITLAB_TOKEN=""; GITLAB_GROUP=""; REPO_VISIBILITY=""; GITLAB_ALLOW_FORCE_PUSH=""
if [ -n "$GITLAB_USER" ]; then
    if [ -n "$CURRENT_GITLAB_TOKEN" ]; then
        read_secret GITLAB_TOKEN "  GitLab token (Enter keeps $(masked "$CURRENT_GITLAB_TOKEN")): "
        GITLAB_TOKEN="${GITLAB_TOKEN:-$CURRENT_GITLAB_TOKEN}"
    else
        read_secret GITLAB_TOKEN "  GitLab token (paste it, then press Enter): "
    fi
    if [ -z "$GITLAB_TOKEN" ]; then
        fail "A GitLab username needs a GitLab token."
        todo "Run ${BOLD}bash docker-setup.sh${RESET} again and give both - or skip GitLab."
        exit 1
    fi
    ask GITLAB_GROUP "GitLab group to push into instead of your own namespace (optional)" "$CURRENT_GITLAB_GROUP"
    ask REPO_VISIBILITY "Visibility on GitLab - auto (match GitHub), private or public" "${CURRENT_REPO_VISIBILITY:-auto}"
    echo
    say "GitLab protects each project's main branch and refuses force pushes to it."
    say "If you ever rewrite history on GitHub (amend, rebase, force push), the mirror"
    say "can't be updated - unless GitSync may turn on 'allowed to force push' for"
    say "those branches when that happens (only then, only on the affected project)."
    if [ "$CURRENT_GITLAB_ALLOW_FORCE_PUSH" = "false" ]; then
        read -p "  Let GitSync allow force pushes when needed? [y/N]: " FORCE_PUSH
        case "$FORCE_PUSH" in y|Y) GITLAB_ALLOW_FORCE_PUSH=true ;; *) GITLAB_ALLOW_FORCE_PUSH=false ;; esac
    else
        read -p "  Let GitSync allow force pushes when needed? [Y/n]: " FORCE_PUSH
        case "$FORCE_PUSH" in n|N) GITLAB_ALLOW_FORCE_PUSH=false ;; *) GITLAB_ALLOW_FORCE_PUSH=true ;; esac
    fi
    ok "GitLab set up."
else
    warn "No GitLab: 'archive' will work, 'sync' won't until GitLab is set up."
fi

# --- what and when ---------------------------------------------------------------
step "Step 4 of 6 · What to run, and when"
# The default: what .env had, else sync with GitLab and archive without
case "$CURRENT_CRON_COMMAND" in
    sync) DEFAULT_WHAT=1 ;;
    "archive --mode full"|archive) DEFAULT_WHAT=2 ;;
    "archive --mode code") DEFAULT_WHAT=3 ;;
    *) if [ -n "$GITLAB_TOKEN" ]; then DEFAULT_WHAT=1; else DEFAULT_WHAT=2; fi ;;
esac
[ -z "$GITLAB_TOKEN" ] && say "${DIM}(No GitLab token, so 'sync' would fail - archive is the default.)${RESET}"
[ -z "$GITLAB_TOKEN" ] && [ "$DEFAULT_WHAT" = 1 ] && DEFAULT_WHAT=2
choose WHAT "$DEFAULT_WHAT" "What should it do?" \
    "sync                  ${DIM}- mirror your repos to GitLab${RESET}" \
    "archive --mode full   ${DIM}- zip everything, full history${RESET}" \
    "archive --mode code   ${DIM}- zip just the latest code, small${RESET}"
case "$WHAT" in
    1) COMMAND="sync" ;;
    2) COMMAND="archive --mode full" ;;
    3) COMMAND="archive --mode code" ;;
esac
echo
choose MODE_PICK 1 "How should it run?" \
    "In the background on a schedule  ${DIM}- docker run -d, keeps going, survives reboots${RESET}" \
    "Just once, now                    ${DIM}- you watch it, then the container is removed${RESET}"
if [ "$MODE_PICK" = 1 ]; then MODE=schedule; else MODE=once; fi

CRON_SCHEDULE=""; RUN_ON_START=""; TZ_VALUE=""
if [ "$MODE" = schedule ]; then
    echo
    say "How often? A cron expression: minute hour day month weekday"
    say "  '0 2 * * *'  -> every day at 02:00      '0 * * * *'  -> every hour"
    say "  '0 3 * * 0'  -> every Sunday at 03:00   (more often than hourly is not supported)"
    ask CRON_SCHEDULE "Schedule" "${CURRENT_CRON_SCHEDULE:-0 2 * * *}"
    # Checked with GitSync's own parser, inside the image - the same rules the
    # container will apply, before anything is started
    if ! docker run --rm --entrypoint python "$IMAGE" -c '
import sys
from gitsync.schedule import CronError, check_min_interval, parse_cron
try:
    check_min_interval(parse_cron(sys.argv[1]))
except CronError as e:
    print(f"  Error: {e}")
    sys.exit(1)
' "$CRON_SCHEDULE"; then
        exit 1
    fi
    # The schedule is read in the container's timezone; default to this
    # machine's, so "02:00" means 02:00 here (macOS and most Linux link
    # /etc/localtime to .../zoneinfo/<Area>/<City>)
    HOST_TZ="$(readlink /etc/localtime 2>/dev/null | sed -n 's#.*zoneinfo/##p')"
    ask TZ_VALUE "Timezone" "${CURRENT_TZ:-${HOST_TZ:-UTC}}"
    if [ "$CURRENT_RUN_ON_START" = "false" ]; then
        read -p "  Run once right away when the container starts, too? [y/N]: " RUN_NOW
        case "$RUN_NOW" in y|Y) RUN_ON_START=true ;; *) RUN_ON_START=false ;; esac
    else
        read -p "  Run once right away when the container starts, too? [Y/n]: " RUN_NOW
        case "$RUN_NOW" in n|N) RUN_ON_START=false ;; *) RUN_ON_START=true ;; esac
    fi
fi

# --- where the data lives ----------------------------------------------------------
step "Step 5 of 6 · Where your backups are kept"
choose WHERE 1 "Keep the mirrors, zips and logs in:" \
    "A folder here: ./gitsync-data  ${DIM}- see the zips and logs directly (recommended)${RESET}" \
    "A Docker volume: gitsync-data  ${DIM}- managed by Docker, out of sight${RESET}"
if [ "$WHERE" = 1 ]; then
    mkdir -p gitsync-data
    DATA_DIR="$(pwd)/gitsync-data"
    DATA_MOUNT="$DATA_DIR:/data"
    # Run as you, so the files in the folder are yours, not uid 1000's
    USER_FLAG="--user $(id -u):$(id -g)"
else
    DATA_DIR=""; DATA_MOUNT="gitsync-data:/data"; USER_FLAG=""
fi

# --- extras ----------------------------------------------------------------------------
step "Step 6 of 6 · Extras (optional)"
LOG_RETENTION_DAYS="$CURRENT_LOG_RETENTION_DAYS"; INCLUDE_FORKS="$CURRENT_INCLUDE_FORKS"
MAX_WORKERS="$CURRENT_MAX_WORKERS"; CODE_ARCHIVE_EXCLUDE_DIRS="$CURRENT_CODE_ARCHIVE_EXCLUDE_DIRS"
read -p "  Change log retention, forks or parallelism? [y/N]: " ADVANCED
case "$ADVANCED" in
    y|Y)
        say "${DIM}Press Enter to keep the value shown in brackets.${RESET}"
        ask LOG_RETENTION_DAYS "LOG_RETENTION_DAYS - days to keep log files (one per run), 0 = forever" "${LOG_RETENTION_DAYS:-30}"
        ask INCLUDE_FORKS "INCLUDE_FORKS - back up your forks too, true/false" "${INCLUDE_FORKS:-true}"
        ask MAX_WORKERS "MAX_WORKERS - repos handled at once, 0 = automatic (at least 4)" "${MAX_WORKERS:-0}"
        ask CODE_ARCHIVE_EXCLUDE_DIRS "CODE_ARCHIVE_EXCLUDE_DIRS - folders 'archive --mode code' strips (Enter = built-in list)" "$CODE_ARCHIVE_EXCLUDE_DIRS"
        ;;
    *) ok "Keeping logs ${LOG_RETENTION_DAYS:-30} days, forks ${INCLUDE_FORKS:-true}." ;;
esac

# --- save -----------------------------------------------------------------------------
# The Docker settings are only meant for a scheduled container: a one-off run
# must not inherit an old schedule and stay up by surprise.
CRON_COMMAND=""; [ "$MODE" = schedule ] && CRON_COMMAND="$COMMAND"
TZ="$TZ_VALUE"

# Saved to .env for next time - or not saved at all. Either way Docker itself
# keeps them in the container's settings (it has to, to start the container
# again after a reboot), so not saving means one copy, not none - and pasting
# the tokens again whenever you run this.
echo
say "Save these settings to .env for next time? With Y, running this again"
say "offers them as the defaults; with N nothing is written and you paste the"
say "tokens again next time."
read -p "  Save to .env? [Y/n]: " SAVE_ANSWER
case "$SAVE_ANSWER" in n|N) SAVE=no ;; *) SAVE=yes ;; esac

STAGE="starting"
if [ "$SAVE" = yes ]; then
    # Updated in place: lines this script doesn't manage (comments, your own
    # settings) stay as they are
    [ -f .env ] || echo "# Written by docker-setup.sh - see .env.example for every setting." > .env
    chmod 600 .env
    for name in $SETTINGS; do set_env "$name" "${!name}"; done
    ENV_ARGS="--env-file .env"
    ok ".env saved ${DIM}(readable only by you - your tokens never appear on the command line)${RESET}"
else
    # Straight to Docker, no file: `-e NAME` with no value makes docker take
    # the value from this script's environment - so the tokens never appear
    # in the docker command line (or in `ps` while it runs)
    ENV_ARGS=""
    for name in $SETTINGS; do
        if [ -n "${!name}" ]; then
            export "$name"
            ENV_ARGS="$ENV_ARGS -e $name"
        fi
    done
    ok "Nothing saved - your settings go straight to Docker."
    [ -f .env ] && warn "Your existing .env is left as it was, and not used."
fi

# The data paths are pinned to /data on the command line: -e beats --env-file,
# so a local BACKUP_DIR=./repos-backup in .env can't move the backups out of
# the volume or folder
PATHS="-e BACKUP_DIR=/data/repos-backup -e ARCHIVE_DIR=/data/archives -e LOGS_DIR=/data/logs"

# --- start -----------------------------------------------------------------------------
if [ "$MODE" = once ]; then
    step "Running '$COMMAND' once"
    say "${DIM}Output appears below; the container is removed when it finishes.${RESET}"
    echo
    STATUS=0
    # shellcheck disable=SC2086  # word splitting of the flags and COMMAND is wanted
    docker run --rm $ENV_ARGS $PATHS $USER_FLAG -v "$DATA_MOUNT" "$IMAGE" $COMMAND || STATUS=$?
    echo
    if [ "$STATUS" -eq 0 ]; then ok "Finished."; else warn "Finished with exit code $STATUS - see the summary above."; fi
else
    step "Starting GitSync in the background"
    if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        warn "A container called '$CONTAINER' already exists."
        read -p "  Replace it with the new settings? Your backups are kept. [Y/n]: " REPLACE
        case "$REPLACE" in
            n|N) fail "Left the existing container alone - nothing started."; exit 1 ;;
        esac
        docker rm -f "$CONTAINER" >/dev/null
        ok "Removed the old container."
    fi
    # shellcheck disable=SC2086
    docker run -d --name "$CONTAINER" --restart unless-stopped \
        $ENV_ARGS $PATHS $USER_FLAG -v "$DATA_MOUNT" "$IMAGE" >/dev/null
    ok "Running $IMAGE: '$COMMAND' on '$CRON_SCHEDULE' ($TZ_VALUE)"
    [ "$RUN_ON_START" = true ] && ok "A first run has started right away."
fi

# --- cheat sheet ---------------------------------------------------------------------
step "Good to know 📖"
if [ "$MODE" = schedule ]; then
    todo "Watch it live ${DIM}(Ctrl-C stops watching, not GitSync)${RESET}:"
    cmd "docker logs -f $CONTAINER"
    todo "How did the last run go? Each run ends with a summary:"
    cmd "docker logs $CONTAINER 2>&1 | grep -A20 finished | tail -25"
fi
if [ -n "$DATA_DIR" ]; then
    todo "Every run's full log, one file per run (kept ${LOG_RETENTION_DAYS:-30} days):"
    cmd "ls -lt gitsync-data/logs/"
    todo "Your zips and mirrors:"
    cmd "ls gitsync-data/archives/   gitsync-data/repos-backup/"
else
    todo "Every run's full log, one file per run (kept ${LOG_RETENTION_DAYS:-30} days):"
    cmd "docker run --rm -v gitsync-data:/data alpine ls -lt /data/logs"
    todo "Copy your zips out of the volume:"
    cmd "docker run --rm -v gitsync-data:/data -v \"\$PWD\":/out alpine cp -r /data/archives /out/"
fi
if [ "$MODE" = schedule ]; then
    todo "Stop / start again:        ${BOLD}docker stop $CONTAINER${RESET}  /  ${BOLD}docker start $CONTAINER${RESET}"
    todo "Run it once right now:     ${BOLD}docker exec $CONTAINER python /app/main.py $COMMAND${RESET}"
    todo "Remove it (keeps backups): ${BOLD}docker rm -f $CONTAINER${RESET}"
fi
if [ "$SAVE" = yes ]; then
    todo "Change settings, or switch version:  ${BOLD}bash docker-setup.sh${RESET}  ${DIM}(your .env values are the defaults)${RESET}"
else
    todo "Your settings are kept only by Docker, in the container. To change them or"
    say  "  switch version, run ${BOLD}bash docker-setup.sh${RESET} again and paste your tokens."
fi
echo
