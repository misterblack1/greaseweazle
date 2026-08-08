#!/usr/bin/env bash
#
# Run the gw probe tool straight from the source tree this script lives in,
# with no build or install step. Counterpart to gw-diag.sh.

set -u

# This script lives in the root of the greaseweazle fork and runs the gw probe
# tool straight from the source tree next to it.
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# Always show which commit this copy is at, first thing. A drive profile is
# compared against one taken months earlier, so which version measured it is
# part of the measurement.
if command -v git >/dev/null 2>&1; then
    banner=$(git -C "$ROOT" log -1 --format='%h %ci %s' 2>/dev/null)
    if [ -n "$banner" ]; then
        echo "gw-probe: $banner"
    else
        echo "gw-probe: (not a git checkout -- can't identify commit)"
    fi
else
    echo "gw-probe: (git not on PATH -- can't identify commit)"
fi

usage() {
    cat <<'EOF'

gw-probe.sh - characterise a floppy DRIVE, rather than read a disk. Runs
              straight from source, no build or install needed.

Measures what a drive can actually do -- how many cylinders its head reaches,
whether it has two heads, how fast it can be stepped, what pin 34 means on
it -- and saves the answers as a profile which can be compared against the
same drive months later to see what has changed.

Needs Python 3.8 or newer. The required Python packages (crcmod,
bitarray>=3, pyserial, requests) are installed automatically on first run,
into a virtual environment beside this script.

USAGE:
  ./gw-probe.sh --all --name "my drive" --save profile.json
  ./gw-probe.sh --all --dry-run          # what it would do, touches nothing
  ./gw-probe.sh --list-probes            # the probes and what each needs

WHAT IT ASKS OF YOU:
  A full run needs two disk changes and stops to wait at each one. In order:
  an EMPTY drive, then ANY readable disk, then a PC-FORMATTED one, then a
  SCRATCH disk which gets written over. --dry-run prints that list first so
  the disks can be collected before starting. It asks once before writing
  anything, not once per probe.

PARAMETERS:
  --device NAME    Serial port name, such as /dev/ttyACM0. Optional, only
                    needed if the Greaseweazle isn't auto-detected or you
                    have more than one plugged in.
  --drive ID       Which physical drive to use. A or B for an IBM/PC
                    cable, 0-3 for a Shugart cable. Default: A.
  --all            Run every probe, including the two which wear the
                    drive. Without it, a plain run does neither those nor
                    the ones which write.
  --write-test     Run the probes which write to the disk, but not the
                    ones which wear the drive.
  --allow-wear     Run the probes which wear the drive.
  --only PROBE     Run just this probe, repeatable. Probes it depends on
                    are run too, since a result without them would be
                    unqualified.
  --name NAME      A label for the drive, recorded in the profile. Never
                    guessed: naming the drive is yours to do.
  --save FILE      Write the drive profile to FILE as JSON.
  --compare FILE   Compare this run against a profile saved earlier, and
                    report what changed. Measurements are compared with a
                    tolerance each probe sets for its own fields, so
                    ordinary variation does not read as a change.
  --max-cylinder N Optional ceiling on how far the head is driven. No
                    default: how many cylinders a drive has is what the
                    probe exists to find out.
  --motor-on       Probe with the motor running even where not required.
  --yes            Approve writing without being asked.
  --non-interactive  Never ask anything: decline what needs approval and
                    do not wait for disk changes. The opposite of --yes,
                    which approves it.
  --dry-run        Print the plan and stop, without touching the drive.
  --list-probes    List the probes, what each needs in the drive, and stop.

TWO PROBES WEAR THE DRIVE, and are excluded unless asked for by name or with
--allow-wear:
  max-track    finds the outermost cylinder by driving the head into its
               stop, which is the only way to find where the stop is.
  step-timing  finds the fastest usable step rate by exceeding it. On one
               older drive a failed trial left the head so far out of step
               that the firmware would no longer move it at all, and the
               drive needed a power cycle.

Everything else either only reads, or writes to a scratch disk you were
asked for first.

If the Greaseweazle is found but can't be opened (a permission error on
/dev/tty*), install the udev rules shipped with this repo, from the folder
this script is in:
  sudo cp scripts/49-greaseweazle.rules /etc/udev/rules.d/
then unplug and reconnect the device.

EOF
    exit 1
}

case "${1-}" in
    -h|--help|-\?) usage ;;
esac

if [ ! -f "$ROOT/scripts/win/gw.py" ]; then
    echo "ERROR: gw-probe.sh must sit in the root of the greaseweazle fork," >&2
    echo "next to the scripts/ and src/ folders. \"$ROOT/scripts/win/gw.py\"" >&2
    echo "was not found." >&2
    exit 1
fi

# Pick an interpreter: python3 by preference, python only if it is really 3.x.
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
           >/dev/null 2>&1; then
        PY=$(command -v "$candidate")
        break
    fi
done

if [ -z "$PY" ]; then
    echo "ERROR: Python 3.8 or newer was not found on your PATH." >&2
    echo >&2
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "Install it with Homebrew <https://brew.sh>:" >&2
        echo "  brew install python3" >&2
    else
        echo "Install it with your package manager, for example:" >&2
        echo "  sudo apt install python3 python3-pip python3-venv   # Debian/Ubuntu" >&2
        echo "  sudo dnf install python3 python3-pip                # Fedora" >&2
        echo "  sudo pacman -S python python-pip                    # Arch" >&2
    fi
    exit 1
fi

DEPS='import crcmod, bitarray, serial, requests'
VENV="$ROOT/.venv"

# Prefer a virtual environment we made earlier over the system interpreter:
# once the packages live there, that is where they stay.
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "$DEPS" >/dev/null 2>&1; then
    PY="$VENV/bin/python"
elif ! "$PY" -c "$DEPS" >/dev/null 2>&1; then
    echo "First-time setup: installing the required Python packages"
    echo "(crcmod, bitarray, pyserial, requests)..."
    echo
    # Most current distributions mark the system Python "externally managed"
    # (PEP 668) and refuse a plain pip install into it, so go straight to a
    # virtual environment beside this script. It keeps the packages out of
    # the system site-packages, needs no root, and is easy to delete: it is
    # just the .venv folder here.
    if ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
        echo "ERROR: could not create a virtual environment at $VENV." >&2
        echo >&2
        echo "On Debian/Ubuntu the venv module is a separate package:" >&2
        echo "  sudo apt install python3-venv" >&2
        exit 1
    fi
    if ! "$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1; then
        : # a pip too old to upgrade itself is usually still good enough
    fi
    if ! "$VENV/bin/python" -m pip install --quiet \
            crcmod 'bitarray>=3' pyserial requests; then
        echo >&2
        echo "ERROR: automatic install failed. Install the packages by hand with:" >&2
        echo "  $VENV/bin/python -m pip install crcmod 'bitarray>=3' pyserial requests" >&2
        exit 1
    fi
    PY="$VENV/bin/python"
    echo
fi

# src/greaseweazle/__init__.py is gitignored and only gets written by a real
# build/version step. "make mypy" overwrites it with a type-stub-only line
# that breaks "from greaseweazle import __version__" at runtime, so always
# rewrite a working one here before running. No "+" in the string -- cli.py
# prints a "TEST/PRE-RELEASE" banner whenever one is present.
echo "__version__ = '0.1.dev0local'" > "$ROOT/src/greaseweazle/__init__.py"

export PYTHONPATH="$ROOT/src"
export GW_OPT=n

cd "$ROOT" || exit 1
exec "$PY" scripts/win/gw.py probe "$@"
