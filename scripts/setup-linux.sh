#!/usr/bin/env bash
# Set Jarvis up on Linux Mint, Ubuntu, or anything else Debian-shaped.
#
# Does the boring parts: checks you have a new enough Python, installs the
# system libraries the Python wheels bind to, builds a virtualenv, installs
# Jarvis into it, and finishes by running `jarvis doctor` so you can see what
# is still missing. Safe to run twice.
#
#   ./scripts/setup-linux.sh              # terminal + web UI
#   ./scripts/setup-linux.sh --voice      # ...and speech in/out
#   ./scripts/setup-linux.sh --yes        # don't ask before apt

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
WANT_VOICE=0
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --voice) WANT_VOICE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --help|-h) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
fail() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }
step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- distro
PRETTY="unknown Linux"
IS_DEBIANISH=0
UBUNTU_BASE=""
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  PRETTY="${PRETTY_NAME:-$NAME}"
  case "${ID:-}:${ID_LIKE:-}" in
    *debian*|*ubuntu*) IS_DEBIANISH=1 ;;
  esac
  # On Mint, `lsb_release -cs` gives the Mint codename (wilma, virginia, ...),
  # which third-party apt repos have never heard of. UBUNTU_CODENAME is the
  # field that names the Ubuntu release underneath. This trips up every
  # MongoDB-on-Mint guide that copies Ubuntu's instructions verbatim.
  UBUNTU_BASE="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
fi

step "Checking this machine"
echo "  $PRETTY"
[ -n "$UBUNTU_BASE" ] && echo "  package base: $UBUNTU_BASE"

# ---------------------------------------------------------------- python
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  have="$(python3 -V 2>&1 || echo 'none')"
  warn "  Python 3.11 or newer is required; found: $have"
  cat <<'MSG'

  Jarvis uses tomllib and datetime.UTC, which arrived in 3.11.

  Linux Mint 21.x ships Python 3.10, so you need a newer one alongside it:

      sudo add-apt-repository ppa:deadsnakes/ppa
      sudo apt update
      sudo apt install python3.12 python3.12-venv

  Then run this script again. (Mint 22.x and LMDE 6 already have 3.11+.)
MSG
  exit 1
fi
echo "  using $PYTHON ($("$PYTHON" -V 2>&1))"

# ---------------------------------------------------------------- apt deps
# python3-venv: Debian and its derivatives split venv out of the base package.
# libportaudio2: what the `sounddevice` wheel binds to for the microphone.
# espeak-ng: the engine pyttsx3 speaks through on Linux.
PACKAGES=("python3-venv")
[ "$WANT_VOICE" = 1 ] && PACKAGES+=("libportaudio2" "espeak-ng")

if [ "$IS_DEBIANISH" = 1 ]; then
  MISSING=()
  for pkg in "${PACKAGES[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
  done
  if [ "${#MISSING[@]}" -gt 0 ]; then
    step "System packages"
    echo "  needed: ${MISSING[*]}"
    if [ "$ASSUME_YES" = 1 ]; then
      REPLY=y
    else
      read -rp "  run: sudo apt install ${MISSING[*]} ? [Y/n] " REPLY
    fi
    case "${REPLY:-y}" in
      [nN]*) warn "  skipped - install them yourself or voice will not work" ;;
      *) sudo apt update && sudo apt install -y "${MISSING[@]}" ;;
    esac
  fi
else
  warn "  not a Debian-based system; install python3-venv, portaudio and espeak-ng yourself"
fi

# ---------------------------------------------------------------- venv
step "Virtualenv"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV" || fail "could not create a virtualenv - is python3-venv installed?"
  echo "  created $VENV"
else
  echo "  reusing $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip

step "Installing Jarvis"
EXTRAS="web"
[ "$WANT_VOICE" = 1 ] && EXTRAS="web,voice"
# Editable, and in one call: installing an extra separately without -e quietly
# replaces the editable install with a copy.
"$VENV/bin/python" -m pip install --quiet -e "$ROOT[$EXTRAS]"
echo "  installed with extras: $EXTRAS"

# ---------------------------------------------------------------- database
step "Database"
if "$VENV/bin/python" - <<'PY' 2>/dev/null
import sys
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import os
uri = os.environ.get("MONGODB_URI") or "mongodb://localhost:27017"
try:
    MongoClient(uri, serverSelectionTimeoutMS=1500).admin.command("ping")
except PyMongoError:
    sys.exit(1)
PY
then
  echo "  reachable"
else
  cat <<MSG
  Nothing answering yet. Jarvis keeps memory, reminders and notices in MongoDB.
  Mint has no mongodb package - \`apt install mongodb\` will not work. Pick one:

  1. Atlas free tier (no install, and your memory follows you between machines)
       https://www.mongodb.com/atlas - create an M0 cluster, then:
       export MONGODB_URI='mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/'

  2. Docker, if you have it
       docker run -d --name jarvis-mongo -p 27017:27017 -v jarvis-mongo:/data/db mongo:7

  3. MongoDB's own apt repo. Their instructions use \`lsb_release -cs\`, which on
     Mint returns a codename MongoDB has never published for. Use the Ubuntu
     base instead - on this machine that is: ${UBUNTU_BASE:-<see /etc/os-release UBUNTU_CODENAME>}
MSG
fi

step "Done - running jarvis doctor"
"$VENV/bin/jarvis" doctor || true

cat <<MSG

$(bold "Next")
  source .venv/bin/activate     # then just: jarvis
  export ANTHROPIC_API_KEY=sk-ant-...
  jarvis                        # terminal
  jarvis web                    # http://127.0.0.1:8765
MSG
