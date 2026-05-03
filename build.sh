#!/bin/bash
# Build script for GitSync

set -e

echo "==== GitSync Setup ===="

# Prompt for .env variables (with defaults)
read -p "GITHUB_USER [your-github-username]: " GITHUB_USER
GITHUB_USER=${GITHUB_USER:-your-github-username}
if [ -z "$GITHUB_USER" ] || [ "$GITHUB_USER" = "your-github-username" ]; then
    echo "Error: GITHUB_USER is required."
    exit 1
fi

read -p "GITHUB_TOKEN [your-github-token]: " GITHUB_TOKEN
GITHUB_TOKEN=${GITHUB_TOKEN:-your-github-token}
if [ -z "$GITHUB_TOKEN" ] || [ "$GITHUB_TOKEN" = "your-github-token" ]; then
    echo "Error: GITHUB_TOKEN is required."
    exit 1
fi

read -p "GITLAB_USER [your-gitlab-username]: " GITLAB_USER
GITLAB_USER=${GITLAB_USER:-your-gitlab-username}
if [ -z "$GITLAB_USER" ] || [ "$GITLAB_USER" = "your-gitlab-username" ]; then
    echo "Error: GITLAB_USER is required."
    exit 1
fi

read -p "GITLAB_TOKEN [your-gitlab-token]: " GITLAB_TOKEN
GITLAB_TOKEN=${GITLAB_TOKEN:-your-gitlab-token}
if [ -z "$GITLAB_TOKEN" ] || [ "$GITLAB_TOKEN" = "your-gitlab-token" ]; then
    echo "Error: GITLAB_TOKEN is required."
    exit 1
fi

read -p "GITLAB_GROUP (optional): " GITLAB_GROUP

read -p "REPO_VISIBILITY [auto/private/public]: " REPO_VISIBILITY
REPO_VISIBILITY=${REPO_VISIBILITY:-auto}

# Write .env file
cat > .env <<EOF
GITHUB_USER=$GITHUB_USER
GITHUB_TOKEN=$GITHUB_TOKEN
GITLAB_USER=$GITLAB_USER
GITLAB_TOKEN=$GITLAB_TOKEN
GITLAB_GROUP=$GITLAB_GROUP
REPO_VISIBILITY=$REPO_VISIBILITY
EOF

echo ".env file created."

# Ensure uv is installed
if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' is not installed."
    echo "Install it from https://docs.astral.sh/uv/getting-started/installation/ and re-run this script."
    exit 1
fi

# Recreate the project venv from scratch
if [ -d ".venv" ]; then
    echo "Removing existing Python virtual environment..."
    rm -rf .venv
fi

echo "Syncing dependencies with uv..."
uv sync

# Refresh requirements.txt for any downstream consumer that still reads it.
echo "Exporting requirements.txt from uv.lock..."
uv export --no-hashes --no-dev --output-file requirements.txt

echo
echo "Setup complete! Run 'uv run python main.py' to start GitSync, or activate the venv with 'source .venv/bin/activate'."