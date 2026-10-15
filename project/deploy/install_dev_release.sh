#!/usr/bin/env bash
set -euo pipefail

usage='用法：install_dev_release.sh <release.tar.gz> <expected-sha256>'
archive="${1:-}"
expected_sha="${2:-}"
if [[ -z "$archive" || -z "$expected_sha" || "$#" -ne 2 ]]; then
  echo "$usage" >&2
  exit 2
fi
if [[ ! "$expected_sha" =~ ^[a-f0-9]{64}$ ]]; then
  echo 'expected-sha256 必须是 64 位小写十六进制摘要。' >&2
  exit 2
fi
if [[ ! -f "$archive" || -L "$archive" ]]; then
  echo '发布包不存在、不是普通文件或是符号链接。' >&2
  exit 1
fi

actual_sha="$(sha256sum -- "$archive" | awk '{print $1}')"
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo '发布包 SHA-256 不匹配，停止安装。' >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo '必须以 root 运行。' >&2
  exit 1
fi

install_root=/opt/nms-team2
state_root=/var/lib/nms-team2
config_root=/etc/nms-team2
unit_path=/etc/systemd/system/nms-team2.service
service_name=nms-team2
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
verify_helper="$script_dir/verify_release.py"
systemd_state_helper="$script_dir/systemd_state.sh"

if [[ ! -f "$verify_helper" || -L "$verify_helper" ]]; then
  echo '缺少可信的同目录 verify_release.py；停止安装。' >&2
  exit 1
fi
if [[ ! -f "$systemd_state_helper" || -L "$systemd_state_helper" ]]; then
  echo '缺少可信的同目录 systemd_state.sh；停止安装。' >&2
  exit 1
fi
source "$systemd_state_helper"

for managed_path in "$install_root" "$state_root" "$config_root" "$unit_path"; do
  if [[ -e "$managed_path" || -L "$managed_path" ]]; then
    echo "目标路径 $managed_path 已存在；为避免覆盖现有内容，停止安装。" >&2
    exit 1
  fi
done
if id "$service_name" >/dev/null 2>&1 || getent group "$service_name" >/dev/null 2>&1; then
  echo "系统账户或用户组 $service_name 已存在；请先核对归属，停止安装。" >&2
  exit 1
fi
if systemctl cat "$service_name.service" >/dev/null 2>&1; then
  echo "systemd 单元 $service_name.service 已存在或已加载；停止安装。" >&2
  exit 1
fi

# Stage on the same filesystem as the final application path. No system state
# is changed until the archive has passed the shared path/type/size validator.
stage="$(mktemp -d /opt/.nms-team2-install.XXXXXXXX)"
staged_release="$stage/release"
install_started=0
install_succeeded=0
account_created=0
application_created=0
config_created=0
unit_created=0
systemd_touched=0
cleanup() {
  local exit_code=$?
  local cleanup_safe=1
  local service_state='unknown'
  trap - EXIT
  if [[ "$install_started" -eq 1 && "$install_succeeded" -eq 0 ]]; then
    set +e
    if [[ "$systemd_touched" -eq 1 ]]; then
      if ! nms_systemd_disable_and_confirm_stopped "$service_name.service"; then
        cleanup_safe=0
      fi
      service_state="${NMS_SYSTEMD_STATE:-unknown}"
    fi
    if [[ "$cleanup_safe" -eq 1 ]]; then
      if [[ "$unit_created" -eq 1 ]]; then
        rm -f -- "$unit_path"
        systemctl daemon-reload >/dev/null 2>&1
      fi
      if [[ "$application_created" -eq 1 ]]; then
        rm -rf -- "$install_root"
      fi
      if [[ "$systemd_touched" -eq 1 ]]; then
        rm -rf -- "$state_root"
      fi
      if [[ "$config_created" -eq 1 ]]; then
        rm -rf -- "$config_root"
      fi
      if [[ "$account_created" -eq 1 ]]; then
        userdel "$service_name" >/dev/null 2>&1
      fi
      echo '首次安装未完成；已尝试清理本脚本创建的文件和账户，请检查 systemd 状态。' >&2
    else
      echo "首次安装未完成；无法确认服务已停止（状态：$service_state），为保护运行数据保留新建文件和账户，请人工检查。" >&2
    fi
  fi
  rm -rf -- "$stage"
  exit "$exit_code"
}
trap cleanup EXIT

mkdir -m 0700 "$staged_release"
python3 "$verify_helper" "$archive" "$staged_release"

install_started=1
useradd --system --user-group --home-dir "$state_root" --shell /usr/sbin/nologin "$service_name"
account_created=1
mv -- "$staged_release" "$install_root"
application_created=1
chown -R root:root "$install_root"
find "$install_root" -type d -exec chmod 0755 {} +
find "$install_root" -type f -exec chmod 0644 {} +
install -d -o root -g "$service_name" -m 0750 "$config_root"
config_created=1
install -o root -g root -m 0644 "$install_root/nms-team2.service" "$unit_path"
unit_created=1

python3 - <<'PY'
import grp
import os
import secrets

path = "/etc/nms-team2/nms.env"
group_id = grp.getgrnam("nms-team2").gr_gid
contents = "\n".join((
    f"NMS_API_TOKEN={secrets.token_urlsafe(32)}",
    "NMS_DB=/var/lib/nms-team2/nms.sqlite3",
    "NMS_ENABLE_LIVE=0",
    "NMS_POLL_INTERVAL=0",
    "NMS_ENABLE_SERVICE_PROBE=0",
    "NMS_ENABLE_IPERF3=0",
    "",
)).encode("utf-8")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
try:
    os.write(fd, contents)
finally:
    os.close(fd)
os.chown(path, 0, group_id)
os.chmod(path, 0o640)
PY

systemctl daemon-reload
systemd_touched=1
systemctl enable --now "$service_name.service"
systemctl --no-pager --full status "$service_name.service"
install_succeeded=1
echo '首次安装完成；实时采集、周期轮询、服务探测和 iperf3 均保持关闭。'
