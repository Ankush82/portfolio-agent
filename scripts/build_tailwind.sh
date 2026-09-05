#!/usr/bin/env bash
# Rebuilds static/css/tailwind.css from static/css/tailwind_source.css using
# the real Tailwind CSS v4 + daisyUI standalone CLI binary at bin/tailwindcss-extra
# (dobicinaitis/tailwind-cli-extra -- a real, no-npm-required distribution
# that bundles Tailwind CSS and daisyUI together). Tailwind only emits CSS
# for classes it actually finds referenced in the @source-scanned
# directories (templates/, design/mockups/), so this must be re-run any
# time a new template or mockup introduces classes that weren't used
# before -- the ux and dev agents both call this after writing new
# HTML/Jinja that uses new Tailwind/daisyUI classes.
set -euo pipefail
cd "$(dirname "$0")/.."

BIN="bin/tailwindcss-extra"
if [[ ! -x "$BIN" ]]; then
    echo "INFO: $BIN not present (it's gitignored -- ~75MB, platform-specific); downloading it now." >&2
    mkdir -p bin
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"
    case "$os" in
        darwin) os="macos" ;;
        linux)  os="linux" ;;
        *) echo "error: unsupported OS $os -- download a tailwindcss-extra build manually from https://github.com/dobicinaitis/tailwind-cli-extra/releases/latest" >&2; exit 1 ;;
    esac
    case "$arch" in
        arm64|aarch64) arch="arm64" ;;
        x86_64|amd64)  arch="x64" ;;
        *) echo "error: unsupported arch $arch" >&2; exit 1 ;;
    esac
    curl -sL -o "$BIN" "https://github.com/dobicinaitis/tailwind-cli-extra/releases/latest/download/tailwindcss-extra-${os}-${arch}"
    chmod +x "$BIN"
fi

"$BIN" -i static/css/tailwind_source.css -o static/css/tailwind.css --minify
