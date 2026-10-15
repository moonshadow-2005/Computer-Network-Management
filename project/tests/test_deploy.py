from __future__ import annotations

import hashlib
import io
import os
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

from deploy.restore_dev_database import restore_database
from deploy.smoke_release import validate_runtime_lock
from deploy.verify_release import extract_release


REQUIRED_FILES = {
    "app/main.py": b"app",
    "app/web/index.html": b"<html></html>",
    "vendor/fastapi/__init__.py": b"vendor",
    "nms-team2.service": b"[Service]",
    "install_dev_release.sh": b"#!/bin/sh",
    "upgrade_dev_release.sh": b"#!/bin/sh",
    "verify_release.py": b"# validator",
    "restore_dev_database.py": b"# database rollback",
    "systemd_state.sh": b"nms_systemd_is_stopped",
    "requirements-runtime.lock": b"fastapi==0.1\n",
}


def write_bundle(path, extra_members=(), files=None):
    with tarfile.open(path, "w:gz") as bundle:
        for name, contents in (files or REQUIRED_FILES).items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            bundle.addfile(info, io.BytesIO(contents))
        for info, contents in extra_members:
            bundle.addfile(info, io.BytesIO(contents) if contents is not None else None)


def test_verify_release_extracts_required_files(tmp_path):
    archive = tmp_path / "valid.tar.gz"
    destination = tmp_path / "release"
    write_bundle(archive)

    extract_release(archive, destination)

    assert (destination / "app/main.py").read_bytes() == b"app"
    assert (destination / "vendor/fastapi/__init__.py").read_bytes() == b"vendor"
    assert (destination / "restore_dev_database.py").exists()


def test_verify_release_rejects_path_traversal(tmp_path):
    archive = tmp_path / "traversal.tar.gz"
    destination = tmp_path / "release"
    malicious = tarfile.TarInfo("../outside.txt")
    contents = b"outside"
    malicious.size = len(contents)
    write_bundle(archive, [(malicious, contents)])

    with pytest.raises(ValueError, match="不安全路径"):
        extract_release(archive, destination)

    assert not (tmp_path / "outside.txt").exists()


def test_verify_release_rejects_symlink_members(tmp_path):
    archive = tmp_path / "symlink.tar.gz"
    link = tarfile.TarInfo("app/untrusted-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    write_bundle(archive, [(link, None)])

    with pytest.raises(ValueError, match="链接或特殊文件"):
        extract_release(archive, tmp_path / "release")


def test_verify_release_requires_installer_and_systemd_guard(tmp_path):
    for missing_file in ("install_dev_release.sh", "systemd_state.sh"):
        archive = tmp_path / f"missing-{missing_file}.tar.gz"
        files = {name: contents for name, contents in REQUIRED_FILES.items()
                 if name != missing_file}
        write_bundle(archive, files=files)

        with pytest.raises(ValueError, match="缺少必要文件"):
            extract_release(archive, tmp_path / f"release-{missing_file}")


def _write_distribution(vendor, name, version):
    dist_info = vendor / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
        encoding="utf-8",
    )


def test_runtime_lock_matches_exact_vendor_inventory_and_versions(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _write_distribution(vendor, "fastapi", "0.142.2")
    _write_distribution(vendor, "pydantic_core", "2.46.5")
    lock_path = tmp_path / "requirements-runtime.lock"
    lock_path.write_text("fastapi==0.142.2\npydantic-core==2.46.5\n", encoding="utf-8")

    assert validate_runtime_lock(vendor, lock_path) == 2

    lock_path.write_text("fastapi==0.142.2\nmissing-package==1.0\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="缺少"):
        validate_runtime_lock(vendor, lock_path)

    _write_distribution(vendor, "other-package", "1.0")
    lock_path.write_text("fastapi==0.142.2\npydantic-core==2.46.5\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="未锁定"):
        validate_runtime_lock(vendor, lock_path)

    lock_path.write_text("fastapi==0.142.1\npydantic-core==2.46.5\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="版本不符"):
        validate_runtime_lock(vendor, lock_path)

    duplicate = vendor / "fastapi-copy.dist-info"
    duplicate.mkdir()
    (duplicate / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fastapi\nVersion: 0.142.2\n\n",
        encoding="utf-8",
    )
    lock_path.write_text("fastapi==0.142.2\npydantic-core==2.46.5\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="重复发行包"):
        validate_runtime_lock(vendor, lock_path)


def test_initial_installer_validates_sha_and_extracts_before_mutation():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install_dev_release.sh").read_text()
    validate_at = script.index('python3 "$verify_helper" "$archive" "$staged_release"')
    mutate_at = script.index('useradd --system --user-group')

    assert "expected_sha" in script and "sha256sum -- \"$archive\"" in script
    assert validate_at < mutate_at
    assert 'tar -xzf "$archive"' not in script


def test_initial_installer_rejects_wrong_archive_digest_before_root_actions(tmp_path):
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(b"not a real release")
    script = Path(__file__).resolve().parents[1] / "deploy" / "install_dev_release.sh"

    result = subprocess.run(
        ["bash", str(script), str(archive), "0" * 64],
        text=True, capture_output=True, check=False, timeout=5,
    )

    assert result.returncode == 1
    assert "SHA-256 不匹配" in result.stderr


def test_deploy_rollback_preserves_state_when_service_stop_is_unconfirmed():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "deploy" / "install_dev_release.sh").read_text()
    updater = (root / "deploy" / "upgrade_dev_release.sh").read_text()

    installer_cleanup = installer[installer.index("cleanup() {"):installer.index("trap cleanup EXIT")]
    installer_guard = installer_cleanup.index('if [[ "$cleanup_safe" -eq 1 ]]')
    assert 'nms_systemd_disable_and_confirm_stopped "$service_name.service"' in installer_cleanup
    assert installer_cleanup.index('rm -rf -- "$state_root"') > installer_guard
    assert installer_cleanup.index('rm -rf -- "$install_root"') > installer_guard
    assert "无法确认服务已停止" in installer_cleanup

    rollback = updater[updater.index("rollback_code() {"):updater.index("cleanup() {")]
    state_guard = rollback.index('if ! nms_systemd_stop_and_confirm_stopped "$service_name"; then')
    assert rollback.index('python3 "$restore_helper"') > state_guard
    assert rollback.index('rm -rf -- "$install_root"') > state_guard
    assert "无法确认服务已停止" in rollback


@pytest.mark.parametrize("rollback_stop_unconfirmed", [False, True])
def test_upgrade_health_failure_restores_previous_release_and_database(
    tmp_path, rollback_stop_unconfirmed,
):
    root = Path(__file__).resolve().parents[1]
    deploy_source = root / "deploy"
    deploy_copy = tmp_path / "deploy"
    deploy_copy.mkdir()
    for name in ("verify_release.py", "restore_dev_database.py", "systemd_state.sh"):
        (deploy_copy / name).write_bytes((deploy_source / name).read_bytes())

    install_root = tmp_path / "opt" / "nms-team2"
    environment_file = tmp_path / "etc" / "nms-team2" / "nms.env"
    systemd_unit = tmp_path / "etc" / "systemd" / "system" / "nms-team2.service"
    database_path = tmp_path / "var" / "lib" / "nms-team2" / "nms.sqlite3"
    backup_base = tmp_path / "var" / "lib" / "nms-team2" / "deploy-backups"
    (install_root / "app").mkdir(parents=True)
    (install_root / "app" / "main.py").write_bytes(b"old-version")
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text("unchanged\n", encoding="utf-8")
    systemd_unit.parent.mkdir(parents=True)
    systemd_unit.write_bytes(b"old-systemd-unit")
    database_path.parent.mkdir(parents=True)
    _make_database(database_path, 1, ["before-upgrade"])

    release_files = dict(REQUIRED_FILES)
    release_files["app/main.py"] = b"new-version"
    archive = tmp_path / "release.tar.gz"
    write_bundle(archive, files=release_files)
    updater_text = (deploy_source / "upgrade_dev_release.sh").read_text(encoding="utf-8")
    replacements = {
        "install_root=/opt/nms-team2": f"install_root={shlex.quote(str(install_root))}",
        "environment_file=/etc/nms-team2/nms.env": f"environment_file={shlex.quote(str(environment_file))}",
        "systemd_unit=/etc/systemd/system/nms-team2.service": f"systemd_unit={shlex.quote(str(systemd_unit))}",
        "database_path=/var/lib/nms-team2/nms.sqlite3": f"database_path={shlex.quote(str(database_path))}",
        "backup_base=/var/lib/nms-team2/deploy-backups": f"backup_base={shlex.quote(str(backup_base))}",
        'stage="$(mktemp -d /opt/.nms-team2-upgrade.XXXXXXXX)"':
            f'stage="$(mktemp -d {shlex.quote(str(tmp_path / "opt" / ".nms-team2-upgrade.XXXXXXXX"))})"',
    }
    for source, destination in replacements.items():
        assert source in updater_text
        updater_text = updater_text.replace(source, destination, 1)
    updater = deploy_copy / "upgrade_dev_release.sh"
    updater.write_text(updater_text, encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_file = tmp_path / "systemd-state"
    state_file.write_text("active\n", encoding="utf-8")
    stop_count_file = tmp_path / "stop-count"
    stop_count_file.write_text("0\n", encoding="utf-8")
    real_install = shutil.which("install")
    assert real_install
    mocks = {
        "id": "#!/usr/bin/env bash\n[[ \"${1:-}\" == -u ]] && { printf '0\\n'; exit 0; }\nexit 0\n",
        "chown": "#!/usr/bin/env bash\nexit 0\n",
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
        "install": (
            "#!/usr/bin/env bash\nset -e\nargs=()\n"
            "while (($#)); do case \"$1\" in -o|-g|-m) shift 2 ;; *) args+=(\"$1\"); shift ;; esac; done\n"
            f"exec {shlex.quote(real_install)} \"${{args[@]}}\"\n"
        ),
        "systemctl": (
            "#!/usr/bin/env bash\nset -e\nstate_file=\"$MOCK_SYSTEMD_STATE_FILE\"\n"
            "case \"${1:-}\" in\n"
            "  is-active) state=$(cat \"$state_file\"); if [[ \"${2:-}\" == --quiet ]]; then [[ \"$state\" == active ]]; else printf '%s\\n' \"$state\"; [[ \"$state\" == active ]]; fi ;;\n"
            "  stop) count=$(cat \"$MOCK_STOP_COUNT_FILE\"); count=$((count + 1)); printf '%s\\n' \"$count\" > \"$MOCK_STOP_COUNT_FILE\"; if [[ \"$MOCK_UNCONFIRMED_ROLLBACK_STOP\" == 1 && \"$count\" -ge 2 ]]; then printf 'active\\n' > \"$state_file\"; else printf 'inactive\\n' > \"$state_file\"; fi ;;\n"
            "  start)\n"
            f"    if grep -q new-version {shlex.quote(str(install_root / 'app' / 'main.py'))}; then\n"
            "      python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"INSERT INTO records(value) VALUES (?)\", (\"written-during-failed-upgrade\",)); c.execute(\"PRAGMA user_version=2\"); c.commit(); c.close()' \"$MOCK_DATABASE_PATH\"\n"
            "    fi\n"
            "    printf 'active\\n' > \"$state_file\" ;;\n"
            "  daemon-reload) exit 0 ;;\n"
            "  *) exit 9 ;;\n"
            "esac\n"
        ),
        "curl": (
            "#!/usr/bin/env bash\n"
            f"if grep -q new-version {shlex.quote(str(install_root / 'app' / 'main.py'))}; then exit 22; fi\n"
            "printf '%s\\n' '{\"status\":\"ok\",\"data\":{\"database\":\"ok\"}}'\n"
        ),
    }
    for name, contents in mocks.items():
        path = fake_bin / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"
    environment["MOCK_SYSTEMD_STATE_FILE"] = str(state_file)
    environment["MOCK_STOP_COUNT_FILE"] = str(stop_count_file)
    environment["MOCK_UNCONFIRMED_ROLLBACK_STOP"] = "1" if rollback_stop_unconfirmed else "0"
    environment["MOCK_DATABASE_PATH"] = str(database_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = subprocess.run(
        ["bash", str(updater), str(archive), digest],
        text=True, capture_output=True, check=False, env=environment, timeout=15,
    )

    assert environment_file.read_text(encoding="utf-8") == "unchanged\n"
    backups = list(backup_base.iterdir())
    assert len(backups) == 1
    assert state_file.read_text(encoding="utf-8").strip() == "active"
    if rollback_stop_unconfirmed:
        assert result.returncode == 2
        assert "无法确认服务已停止" in result.stderr
        assert (install_root / "app" / "main.py").read_bytes() == b"new-version"
        assert systemd_unit.read_bytes() == b"[Service]"
        with sqlite3.connect(database_path) as current:
            assert current.execute("PRAGMA user_version").fetchone() == (2,)
            assert current.execute("SELECT value FROM records ORDER BY rowid").fetchall() == [
                ("before-upgrade",), ("written-during-failed-upgrade",),
            ]
            assert current.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not (backups[0] / "nms-failed-upgrade.sqlite3").exists()
        assert (backups[0] / "code" / "app" / "main.py").read_bytes() == b"old-version"
    else:
        assert result.returncode == 1
        assert "已恢复旧代码" in result.stderr
        assert (install_root / "app" / "main.py").read_bytes() == b"old-version"
        assert systemd_unit.read_bytes() == b"old-systemd-unit"
        with sqlite3.connect(database_path) as restored:
            assert restored.execute("PRAGMA user_version").fetchone() == (1,)
            assert restored.execute("SELECT value FROM records").fetchall() == [("before-upgrade",)]
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        failed_database = backups[0] / "nms-failed-upgrade.sqlite3"
        with sqlite3.connect(failed_database) as failed:
            assert failed.execute("PRAGMA user_version").fetchone() == (2,)
            assert failed.execute("SELECT value FROM records ORDER BY rowid").fetchall() == [
                ("before-upgrade",), ("written-during-failed-upgrade",),
            ]
            assert failed.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_systemd_state_helpers_require_confirmed_stopped_state(tmp_path):
    helper = Path(__file__).resolve().parents[1] / "deploy" / "systemd_state.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  disable) exit \"${MOCK_DISABLE_EXIT:-0}\" ;;\n"
        "  stop) exit \"${MOCK_STOP_EXIT:-0}\" ;;\n"
        "  is-active)\n"
        "    if [[ \"${MOCK_SYSTEMD_STATE+x}\" == x ]]; then printf '%s\\n' \"$MOCK_SYSTEMD_STATE\"; fi\n"
        "    exit \"${MOCK_IS_ACTIVE_EXIT:-3}\"\n"
        "    ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    cases = [
        ("nms_systemd_is_stopped", "inactive", 0, 0, 0, "inactive"),
        ("nms_systemd_is_stopped", "failed", 0, 0, 0, "failed"),
        ("nms_systemd_is_stopped", "active", 0, 0, 1, "active"),
        ("nms_systemd_is_stopped", None, 0, 0, 1, "unknown"),
        ("nms_systemd_disable_and_confirm_stopped", "inactive", 1, 0, 1, "inactive"),
        ("nms_systemd_disable_and_confirm_stopped", "inactive", 0, 0, 0, "inactive"),
        ("nms_systemd_stop_and_confirm_stopped", "inactive", 0, 1, 0, "inactive"),
        ("nms_systemd_stop_and_confirm_stopped", "activating", 0, 0, 1, "activating"),
    ]
    for function, state, disable_exit, stop_exit, expected_exit, expected_state in cases:
        environment = dict(os.environ)
        environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"
        environment["MOCK_DISABLE_EXIT"] = str(disable_exit)
        environment["MOCK_STOP_EXIT"] = str(stop_exit)
        if state is not None:
            environment["MOCK_SYSTEMD_STATE"] = state
        else:
            environment.pop("MOCK_SYSTEMD_STATE", None)
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; "$2" "$3"; result=$?; printf "%s\\n" "${NMS_SYSTEMD_STATE:-unset}"; exit "$result"',
             "bash", str(helper), function, "nms-team2.service"],
            text=True, capture_output=True, check=False, env=environment, timeout=5,
        )
        assert result.returncode == expected_exit, (function, state, result.stderr)
        assert result.stdout.strip() == expected_state


def _make_database(path, version, rows):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
        connection.executemany("INSERT INTO records(value) VALUES (?)", [(value,) for value in rows])
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
    finally:
        connection.close()


def test_database_rollback_restores_pre_upgrade_and_preserves_failed_copy(tmp_path):
    database = tmp_path / "nms.sqlite3"
    pre_upgrade = tmp_path / "nms-before.sqlite3"
    failed_copy = tmp_path / "nms-failed.sqlite3"
    _make_database(pre_upgrade, 1, ["before"])
    _make_database(database, 2, ["before", "written-during-failed-upgrade"])
    database.chmod(0o640)

    restore_database(database, pre_upgrade, failed_copy)

    restored = sqlite3.connect(database)
    failed = sqlite3.connect(failed_copy)
    try:
        assert restored.execute("PRAGMA user_version").fetchone() == (1,)
        assert restored.execute("SELECT value FROM records").fetchall() == [("before",)]
        assert failed.execute("PRAGMA user_version").fetchone() == (2,)
        assert failed.execute("SELECT value FROM records ORDER BY rowid").fetchall() == [
            ("before",), ("written-during-failed-upgrade",),
        ]
    finally:
        restored.close()
        failed.close()
    assert database.stat().st_mode & 0o777 == 0o640
    assert failed_copy.stat().st_mode & 0o777 == 0o600
