#!/bin/sh
# story-gate installer for macOS and Linux:
#   curl -LsSf https://raw.githubusercontent.com/SathiaAI/story-gate/v0.7.0/install.sh | sh
# It installs uv (Astral's Python tool manager) if you don't have it, then story-gate at the pinned
# release, puts it on your PATH, and runs nothing else. Read it before you run it: it's short.
set -eu
REF="${STORY_GATE_REF:-v0.7.0}"
SRC="git+https://github.com/SathiaAI/story-gate@$REF"

say() { printf '%s\n' "story-gate install: $*"; }

if ! command -v git >/dev/null 2>&1; then
  say "git is needed and isn't installed. Install git (https://git-scm.com/downloads), then run this again."
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv from https://astral.sh/uv (the official installer)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
fi
say "installing story-gate $REF"
uv tool install --force --python 3.12 "$SRC"
uv tool update-shell >/dev/null 2>&1 || true
BIN="$(uv tool dir --bin)"
PATH="$BIN:$PATH"
export PATH
story-gate >/dev/null 2>&1 || { say "story-gate didn't start; see the messages above"; exit 1; }
say "done. Open a new terminal, then run: story-gate try"
