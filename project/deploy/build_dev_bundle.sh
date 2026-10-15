#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vendor_source="${NMS_VENDOR_SOURCE:-$repo_root/data/vendor}"
output_path="${1:-$repo_root/data/nms-team2-release.tar.gz}"

if [[ ! -d "$vendor_source" ]]; then
  printf '依赖目录不存在：%s\n' "$vendor_source" >&2
  printf '请准备与 DEV 相同的 Linux CPython 3.10 x86_64 依赖目录，或设置 NMS_VENDOR_SOURCE。\n' >&2
  exit 2
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/package/app" "$stage/package/vendor"
cp -a "$repo_root/app/." "$stage/package/app/"
cp -a "$vendor_source/." "$stage/package/vendor/"
install -m 0644 "$repo_root/deploy/nms-team2.service" "$stage/package/nms-team2.service"
install -m 0644 "$repo_root/deploy/install_dev_release.sh" "$stage/package/install_dev_release.sh"
install -m 0644 "$repo_root/deploy/upgrade_dev_release.sh" "$stage/package/upgrade_dev_release.sh"
install -m 0644 "$repo_root/deploy/verify_release.py" "$stage/package/verify_release.py"
install -m 0644 "$repo_root/deploy/restore_dev_database.py" "$stage/package/restore_dev_database.py"
install -m 0644 "$repo_root/deploy/systemd_state.sh" "$stage/package/systemd_state.sh"
install -m 0644 "$repo_root/deploy/requirements-runtime.lock" "$stage/package/requirements-runtime.lock"
find "$stage/package" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$stage/package" -type d -exec chmod 0755 {} +
find "$stage/package" -type f -exec chmod 0644 {} +
chmod 0755 "$stage/package/install_dev_release.sh"

mkdir -p "$(dirname "$output_path")"
# Normalize archive ordering, metadata, and the gzip header so identical
# sources and dependencies produce the same release digest.
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
  -C "$stage/package" -cf - . | gzip -n > "$output_path"
chmod 0600 "$output_path"
sha256sum "$output_path"
du -h "$output_path"
