#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys
import tarfile


ALLOWED_ROOTS = {
    "app", "vendor", "nms-team2.service", "install_dev_release.sh",
    "upgrade_dev_release.sh", "verify_release.py", "restore_dev_database.py", "systemd_state.sh",
    "requirements-runtime.lock",
}
REQUIRED = {
    "app/main.py", "app/web/index.html", "vendor/fastapi/__init__.py",
    "nms-team2.service", "install_dev_release.sh", "upgrade_dev_release.sh", "verify_release.py",
    "restore_dev_database.py", "requirements-runtime.lock",
    "systemd_state.sh",
}
MAX_UNPACKED_SIZE = 128 * 1024 * 1024


def extract_release(archive: pathlib.Path, destination: pathlib.Path) -> None:
    total_size = 0
    members: list[tarfile.TarInfo] = []
    seen: set[str] = set()

    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            raw = pathlib.PurePosixPath(member.name)
            if raw.is_absolute() or any(part == ".." for part in raw.parts):
                raise ValueError("发布包包含不安全路径。")
            parts = [part for part in raw.parts if part not in ("", ".")]
            if not parts:
                continue
            normalized = "/".join(parts)
            if parts[0] not in ALLOWED_ROOTS:
                raise ValueError("发布包包含未登记路径。")
            if normalized in seen:
                raise ValueError("发布包包含重复路径。")
            if not (member.isdir() or member.isfile()):
                raise ValueError("发布包包含链接或特殊文件。")
            total_size += member.size
            if total_size > MAX_UNPACKED_SIZE:
                raise ValueError("发布包解压内容超过 128 MiB。")
            members.append(member)
            seen.add(normalized)

        missing = REQUIRED - seen
        if missing:
            raise ValueError("发布包缺少必要文件。")
        bundle.extractall(destination, members=members)


def main() -> int:
    if len(sys.argv) != 3:
        print("用法：verify_release.py <release.tar.gz> <destination>", file=sys.stderr)
        return 2
    archive = pathlib.Path(sys.argv[1])
    destination = pathlib.Path(sys.argv[2])
    try:
        destination.mkdir(parents=True, exist_ok=True)
        extract_release(archive, destination)
    except (OSError, tarfile.TarError, ValueError) as exc:
        print(f"发布包校验失败：{exc}", file=sys.stderr)
        return 1
    print("发布包路径、类型、大小和必要文件校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
