#!/usr/bin/env bash
set -euo pipefail

usage='用法：upgrade_dev_release.sh <release.tar.gz> <expected-sha256>'
archive="${1:-}"
expected_sha="${2:-}"
if [[ -z "$archive" || -z "$expected_sha" ]]; then
  echo "$usage" >&2
  exit 2
fi

install_root=/opt/nms-team2
service_name=nms-team2
environment_file=/etc/nms-team2/nms.env
systemd_unit=/etc/systemd/system/nms-team2.service
database_path=/var/lib/nms-team2/nms.sqlite3
backup_base=/var/lib/nms-team2/deploy-backups

if [[ "$(id -u)" -ne 0 ]]; then
  echo '必须以 root 运行。' >&2
  exit 1
fi
if [[ ! -f "$archive" ]]; then
  echo '发布包不存在。' >&2
  exit 1
fi
if [[ ! "$expected_sha" =~ ^[a-f0-9]{64}$ ]]; then
  echo 'expected-sha256 必须是 64 位小写十六进制摘要。' >&2
  exit 1
fi
if [[ ! -d "$install_root" || -L "$install_root" || ! -f "$environment_file" || -L "$environment_file" || ! -f "$systemd_unit" || -L "$systemd_unit" || ! -f "$database_path" || -L "$database_path" ]]; then
  echo '现有安装、环境文件、systemd 单元或数据库缺失/类型不符，停止更新。' >&2
  exit 1
fi
if ! id "$service_name" >/dev/null 2>&1; then
  echo "服务账户 $service_name 不存在，停止更新。" >&2
  exit 1
fi
if ! systemctl is-active --quiet "$service_name"; then
  echo "服务 $service_name 当前未运行；为避免覆盖异常状态，停止更新。" >&2
  exit 1
fi

actual_sha="$(sha256sum -- "$archive" | awk '{print $1}')"
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo '发布包 SHA-256 不匹配，停止更新。' >&2
  exit 1
fi

stage="$(mktemp -d /opt/.nms-team2-upgrade.XXXXXXXX)"
staged_release="$stage/release"
mkdir -m 0755 "$staged_release"
verify_helper="$(dirname -- "$0")/verify_release.py"
restore_helper="$(dirname -- "$0")/restore_dev_database.py"
systemd_state_helper="$(dirname -- "$0")/systemd_state.sh"
if [[ ! -f "$verify_helper" || ! -f "$restore_helper" || ! -f "$systemd_state_helper" ]]; then
  echo '缺少 verify_release.py、restore_dev_database.py 或 systemd_state.sh；请将更新脚本及辅助脚本放在同一目录。' >&2
  exit 1
fi
source "$systemd_state_helper"
python3 "$verify_helper" "$archive" "$staged_release"

chown -R root:root "$staged_release"
find "$staged_release" -type d -exec chmod 0755 {} +
find "$staged_release" -type f -exec chmod 0644 {} +

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -o root -g root -m 0700 "$backup_base"
backup_dir="$backup_base/$stamp"
install -d -o root -g root -m 0700 "$backup_dir"
cp -a "$install_root" "$backup_dir/code"
install -o root -g root -m 0644 "$systemd_unit" "$backup_dir/nms-team2.service"
python3 - "$database_path" "$backup_dir/nms.sqlite3" <<'PY'
import os
import sqlite3
import sys

source_path, backup_path = sys.argv[1:]
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
target = sqlite3.connect(backup_path)
try:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit("数据库备份完整性检查失败。")
finally:
    target.close()
    source.close()
os.chmod(backup_path, 0o600)
PY

upgrade_started=0
upgrade_succeeded=0
rollback_ok=0

wait_health() {
  local body
  for _ in $(seq 1 30); do
    body="$(curl --silent --show-error --fail --max-time 3 http://127.0.0.1:8000/api/v1/health 2>/dev/null)" || {
      sleep 1
      continue
    }
    if python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("data",{}).get("database")=="ok" else 1)' <<<"$body"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback_code() {
  local service_state='unknown'
  set +e
  if ! nms_systemd_stop_and_confirm_stopped "$service_name"; then
    service_state="${NMS_SYSTEMD_STATE:-unknown}"
    echo "无法确认服务已停止（状态：$service_state）；保留当前数据库、代码和备份目录 $backup_dir，请人工检查。" >&2
    return
  fi
  if ! python3 "$restore_helper" "$database_path" "$backup_dir/nms.sqlite3" \
      "$backup_dir/nms-failed-upgrade.sqlite3"; then
    echo "数据库自动回退失败；服务保持停止，备份保留在 $backup_dir" >&2
    return
  fi
  rm -rf -- "$install_root"
  cp -a "$backup_dir/code" "$install_root"
  install -o root -g root -m 0644 "$backup_dir/nms-team2.service" "$systemd_unit"
  systemctl daemon-reload >/dev/null 2>&1
  systemctl start "$service_name" >/dev/null 2>&1
  if [[ $? -eq 0 ]] && wait_health; then
    rollback_ok=1
    echo "新版本健康检查失败，已恢复旧代码；备份保留在 $backup_dir" >&2
  else
    echo "自动恢复未通过 health 检查；请检查 $backup_dir 并手工恢复服务。" >&2
  fi
  set -e
}

cleanup() {
  local exit_code=$?
  trap - EXIT
  if [[ "$upgrade_started" -eq 1 && "$upgrade_succeeded" -eq 0 ]]; then
    rollback_code
    if [[ "$rollback_ok" -ne 1 ]]; then
      exit_code=2
    fi
  fi
  rm -rf -- "$stage"
  exit "$exit_code"
}
trap cleanup EXIT

upgrade_started=1
systemctl stop "$service_name"
mv -- "$install_root" "$stage/previous-code"
mv -- "$staged_release" "$install_root"
install -o root -g root -m 0644 "$install_root/nms-team2.service" "$systemd_unit"
systemctl daemon-reload
systemctl start "$service_name"
if ! wait_health; then
  echo '新版本未在 30 秒内通过 health 检查。' >&2
  exit 1
fi

upgrade_succeeded=1
echo "更新成功；SQLite 备份与旧版本代码保存在 $backup_dir"
