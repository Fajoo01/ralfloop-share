#!/usr/bin/env bash
set -euo pipefail

# Root-only deployment helper. It never downloads: source must be a locally
# reviewed, unpacked official veraPDF 1.30.2 CLI directory.
source_dir=${1:?usage: install_runts_verapdf.sh /reviewed/verapdf-cli-dir}
target=/opt/ralfloop/verapdf/1.30.2
expected=ea4d7949a4c9e5939e3419d03f9610920f1d7b761bc5599a38facbddea28ae09

test -x "$source_dir/verapdf"
test "$(sha256sum "$source_dir/verapdf" | awk '{print $1}')" = "$expected"
"$source_dir/verapdf" --version | head -n1 | grep -Fx 'veraPDF 1.30.2'

install -d -m 0755 /opt/ralfloop/verapdf
stage=$(mktemp -d /opt/ralfloop/verapdf/.1.30.2.XXXXXX)
trap 'rm -rf "$stage"' EXIT
cp -a "$source_dir"/. "$stage"/
test "$(sha256sum "$stage/verapdf" | awk '{print $1}')" = "$expected"
chmod -R go-w "$stage"
if [[ -e $target ]]; then
  test "$(sha256sum "$target/verapdf" | awk '{print $1}')" = "$expected"
  exit 0
fi
mv "$stage" "$target"
trap - EXIT
