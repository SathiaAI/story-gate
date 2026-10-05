#!/bin/sh
# story-gate installer for macOS and Linux:
#   curl -LsSf https://raw.githubusercontent.com/SathiaAI/story-gate/v0.7.0/install.sh -o /tmp/story-gate-install.sh && sh /tmp/story-gate-install.sh
# It installs uv (Astral's Python tool manager) if you don't have it, then story-gate at the pinned
# release, puts it on your PATH, and runs nothing else. Read it before you run it: it's short.
set -eu

# Everything runs from main(), called on the last line, so a download cut off halfway runs nothing.
main() {
  PATH_NOTE=
  REF="${STORY_GATE_REF:-v0.7.0}"
  SRC="git+https://github.com/SathiaAI/story-gate@$REF"

  say() { printf '%s\n' "story-gate install: $*"; }

  if ! command -v git >/dev/null 2>&1; then
    say "git is needed and isn't installed. Install git (https://git-scm.com/downloads), then run this again."
    exit 1
  fi
  if ! command -v uv >/dev/null 2>&1; then
    say "installing uv from https://astral.sh/uv (the official installer)"
    UV_INSTALLER="$(mktemp)"
    # download first, run only if the whole download worked (a pipe would report sh's status, not curl's)
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$UV_INSTALLER"; then
      rm -f "$UV_INSTALLER"
      say "couldn't download the uv installer. Check your internet connection, then run this again."
      exit 1
    fi
    sh "$UV_INSTALLER" || { rm -f "$UV_INSTALLER"; say "the uv installer failed; see the messages above"; exit 1; }
    rm -f "$UV_INSTALLER"
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
  fi
  say "installing story-gate $REF"
  uv tool install --force --python 3.12 "$SRC"
  BIN="$(uv tool dir --bin)"
  if ! uv tool update-shell >/dev/null 2>&1; then
    say "couldn't add story-gate to PATH for new terminals. Add this folder to your PATH yourself: $BIN"
    PATH_NOTE=1
  fi
  PATH="$BIN:$PATH"
  export PATH
  story-gate >/dev/null 2>&1 || { say "story-gate didn't start; see the messages above"; exit 1; }
  if [ -n "${PATH_NOTE:-}" ]; then
    say "done, except PATH (see above). In this terminal, run: \"$BIN/story-gate\" try"
  else
    say "done. Open a new terminal, then run: story-gate try"
  fi
}

main "$@"
