#!/bin/sh
# 50_preflight_readonly.sh - K1 / Bianbu LXQt read-only preflight inventory.
#
# Purpose:
#   Print what the board currently exposes. This script is intentionally
#   read-only: it does not install packages, edit files, restart services,
#   probe I2C addresses, or use sudo.
#
# Usage (after the board is connected, run manually):
#   sh scripts/board/50_preflight_readonly.sh
#
# Exit status:
#   Always 0. Missing devices/modules/commands are reported as [WARN], not as
#   failures. Do not use this output as functional acceptance or a score.
#
# Compatibility:
#   POSIX sh only (Bianbu/Debian dash/busybox compatible). Avoids bash arrays,
#   [[ ]], local, function, pipefail and other bash-only constructs.

set -u

warn() { printf '  [WARN] %s\n' "$*"; }
ok()   { printf '  [ OK ] %s\n' "$*"; }
section() { printf '\n=== %s ===\n' "$*"; }
indent() { sed 's/^/    /'; }

show_glob() {
    # $1=label, $2=glob, $3=optional max lines
    label=$1
    pattern=$2
    max=${3:-0}
    printf '  %s:\n' "$label"
    found=0
    count=0
    for item in $pattern; do
        [ -e "$item" ] || continue
        found=1
        count=$((count + 1))
        if [ "$max" -eq 0 ] || [ "$count" -le "$max" ]; then
            ls -l "$item" 2>/dev/null | indent
        fi
    done
    if [ "$found" -eq 0 ]; then
        warn "none found: $pattern"
    elif [ "$max" -gt 0 ] && [ "$count" -gt "$max" ]; then
        printf '    ... (%s total, showing first %s)\n' "$count" "$max"
    fi
}

run_brief() {
    # $@=command. Show the command line, then run it with a short timeout if
    # the timeout utility exists. Read-only commands only.
    printf '  $'
    for arg in "$@"; do
        printf ' %s' "$arg"
    done
    printf '\n'
    if command -v timeout >/dev/null 2>&1; then
        output=$(timeout 5 "$@" 2>&1)
        status=$?
    else
        output=$("$@" 2>&1)
        status=$?
    fi
    if [ -n "$output" ]; then
        printf '%s\n' "$output" | indent
    fi
    if [ "$status" -ne 0 ]; then
        warn "command exited $status"
    fi
}

check_command() {
    if command -v "$1" >/dev/null 2>&1; then
        ok "$1 -> $(command -v "$1")"
    else
        warn "$1 not found"
    fi
}

module_probe() {
    mod=$1
    if PYTHONDONTWRITEBYTECODE=1 python3 -c "import $mod" >/dev/null 2>&1; then
        ver=$(PYTHONDONTWRITEBYTECODE=1 python3 -c \
            'import importlib, sys; m = importlib.import_module(sys.argv[1]); print(getattr(m, "__version__", "importable"))' \
            "$mod" 2>/dev/null)
        ok "$mod ${ver:-importable}"
    else
        warn "$mod not importable"
    fi
}

printf '%s\n' '============================================================'
printf '%s\n' 'K1 / Bianbu LXQt preflight - READ-ONLY'
printf '%s\n' 'No package install, no config change, no sudo, no I2C address scan.'
printf '%s\n' 'Missing items are WARN only; the script exits 0.'
printf '%s\n' '============================================================'

section '1. Time and system'
printf '  time      : %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo unknown)"
printf '  uname     : %s\n' "$(uname -srm 2>/dev/null || echo unknown)"
printf '  arch      : %s\n' "$(uname -m 2>/dev/null || echo unknown)"
printf '  kernel    : %s\n' "$(uname -r 2>/dev/null || echo unknown)"
if command -v nproc >/dev/null 2>&1; then
    printf '  cpu count : %s\n' "$(nproc 2>/dev/null)"
elif command -v getconf >/dev/null 2>&1; then
    printf '  cpu count : %s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo unknown)"
else
    warn 'nproc/getconf not found; cpu count unknown'
fi

if [ -r /etc/os-release ]; then
    printf '  os-release:\n'
    indent < /etc/os-release
else
    warn '/etc/os-release not readable'
fi

if [ -r /proc/device-tree/model ]; then
    printf '  board     : %s\n' "$(tr -d '\000' < /proc/device-tree/model 2>/dev/null)"
else
    warn '/proc/device-tree/model not readable (maybe not a K1 board or no DT model)'
fi

section '2. Memory'
if command -v free >/dev/null 2>&1 && free -h >/dev/null 2>&1; then
    free -h 2>/dev/null | indent
else
    warn 'free not available or failed'
    if [ -r /proc/meminfo ]; then
        awk '/^MemTotal:|^MemAvailable:|^MemFree:/{print}' /proc/meminfo | indent
    else
        warn '/proc/meminfo not readable'
    fi
fi

section '3. Disk'
if command -v df >/dev/null 2>&1; then
    printf '  /:\n'
    df -h / 2>/dev/null | indent
    if [ -d /home ]; then
        printf '  /home:\n'
        df -h /home 2>/dev/null | indent
    fi
else
    warn 'df not found'
fi

section '4. Python 3 and module probes'
if command -v python3 >/dev/null 2>&1; then
    printf '  version   : '
    python3 --version 2>&1
    printf '  executable: %s\n' "$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || echo unknown)"
    for mod in gpiozero lgpio smbus2 cv2 onnxruntime sounddevice serial; do
        module_probe "$mod"
    done
else
    warn 'python3 not found; skip module probes'
fi

section '5. Device nodes'
show_glob '/dev/gpiochip*' '/dev/gpiochip*'
show_glob '/dev/i2c-*' '/dev/i2c-*'
show_glob '/dev/video*' '/dev/video*'
show_glob '/dev/snd/*' '/dev/snd/*'
show_glob '/dev/tty*' '/dev/tty*' 20

section '6. Commands and read-only queries'
for cmd in v4l2-ctl i2cdetect arecord ollama llama-server; do
    check_command "$cmd"
done

if command -v v4l2-ctl >/dev/null 2>&1; then
    run_brief v4l2-ctl --list-devices
else
    warn 'v4l2-ctl not found; cannot list video devices'
fi

if command -v i2cdetect >/dev/null 2>&1; then
    run_brief i2cdetect -l
else
    warn 'i2cdetect not found; cannot list I2C buses'
fi

if command -v arecord >/dev/null 2>&1; then
    run_brief arecord -l
else
    warn 'arecord not found; cannot list sound cards'
fi

if command -v ollama >/dev/null 2>&1; then
    run_brief ollama --version
else
    warn 'ollama not found'
fi

if command -v llama-server >/dev/null 2>&1; then
    run_brief llama-server --version
else
    home=${HOME:-}
    llama_candidate=''
    for cand in \
        "$home/llama/llama.cpp/build/installed/bin/llama-server" \
        "$home/llama/llama.cpp/build/bin/llama-server"; do
        if [ -x "$cand" ]; then
            llama_candidate=$cand
            break
        fi
    done
    if [ -n "$llama_candidate" ]; then
        warn "llama-server not on PATH; found executable at $llama_candidate"
    else
        warn 'llama-server not found on PATH or common build paths'
    fi
fi

section 'Preflight summary'
printf '%s\n' '  This was a READ-ONLY preflight.'
printf '%s\n' '  No packages were installed, no files were edited, no services were restarted,'
printf '%s\n' '  no I2C addresses were scanned, and no sudo was used.'
printf '%s\n' '  Missing items above are WARN; they do not make this script fail.'
printf '%s\n' '  This output is NOT functional acceptance and NOT a competition score.'
printf '%s\n' '  Save the terminal output manually if it is needed as evidence.'
printf '%s\n' '============================================================'

exit 0
