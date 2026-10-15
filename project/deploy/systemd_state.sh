#!/usr/bin/env bash

# Shared fail-closed checks for install cleanup and upgrade rollback.
# Callers may inspect NMS_SYSTEMD_STATE after a failed check for diagnostics.
nms_systemd_is_stopped() {
  local unit="${1:-}"
  if [[ -z "$unit" ]]; then
    NMS_SYSTEMD_STATE='invalid-unit'
    return 1
  fi

  NMS_SYSTEMD_STATE="$(systemctl is-active "$unit" 2>/dev/null)" || true
  [[ -n "$NMS_SYSTEMD_STATE" ]] || NMS_SYSTEMD_STATE='unknown'
  [[ "$NMS_SYSTEMD_STATE" == 'inactive' || "$NMS_SYSTEMD_STATE" == 'failed' ]]
}

nms_systemd_disable_and_confirm_stopped() {
  local unit="${1:-}"
  if [[ -z "$unit" ]] || ! systemctl disable --now "$unit" >/dev/null 2>&1; then
    nms_systemd_is_stopped "$unit" >/dev/null 2>&1 || true
    return 1
  fi
  nms_systemd_is_stopped "$unit"
}

nms_systemd_stop_and_confirm_stopped() {
  local unit="${1:-}"
  if [[ -z "$unit" ]]; then
    NMS_SYSTEMD_STATE='invalid-unit'
    return 1
  fi
  systemctl stop "$unit" >/dev/null 2>&1 || true
  nms_systemd_is_stopped "$unit"
}
