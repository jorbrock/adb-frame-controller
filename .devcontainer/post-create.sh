#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

umask 077
mkdir -p .devcontainer/local/data/.android
if [ ! -e .devcontainer/local/config.json ]; then
    cp config.example.json .devcontainer/local/config.json
fi

# Keep development ADB keys in the mounted workspace across container rebuilds.
if [ ! -e "$HOME/.android" ] && [ ! -L "$HOME/.android" ]; then
    ln -s "$PWD/.devcontainer/local/data/.android" "$HOME/.android"
fi
