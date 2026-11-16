#!/usr/bin/env bash
set -Eeuo pipefail
if command -v actionlint >/dev/null 2>&1; then
  actionlint "$@"
else
  temp="$(mktemp -d)"
  trap 'rm -rf "$temp"' EXIT
  curl -fsSL https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_linux_amd64.tar.gz -o "$temp/actionlint.tgz"
  printf '%s  %s\n' '023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757' "$temp/actionlint.tgz" | sha256sum -c -
  tar -xzf "$temp/actionlint.tgz" -C "$temp" actionlint
  "$temp/actionlint" "$@"
fi
