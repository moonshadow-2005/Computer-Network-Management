"""Portable local entry point. Default to an isolated, read-only sample mode."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第 2 组实验网络管理系统")
    parser.add_argument("--mode", choices=("demo", "live"), default="demo",
                        help="demo 自动装载本地样例；live 使用实验数据库（默认 demo）")
    parser.add_argument("--port", type=int, default=8000, help="本机监听端口（默认 8000）")
    parser.add_argument("--db", type=Path, help="指定数据库；相对路径以项目根目录为准")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1～65535 之间")

    try:
        import uvicorn
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        parser.exit(2, "请先安装运行依赖：python -m pip install -r requirements.txt\n")

    load_dotenv(ROOT / ".env", override=False)
    if args.mode == "demo":
        db_path = args.db or ROOT / "data" / "demo.sqlite3"
        # An inherited live configuration must never turn a sample launch into
        # network collection, active testing or administrative writes.
        os.environ.update({
            "NMS_ENABLE_LIVE": "0", "NMS_POLL_INTERVAL": "0",
            "NMS_ENABLE_SERVICE_PROBE": "0", "NMS_ENABLE_IPERF3": "0",
            "NMS_API_TOKEN": "",
        })
    else:
        db_path = args.db or Path(os.environ.get("NMS_DB") or "data/nms.sqlite3")
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db_path = db_path.resolve()
    if args.mode == "live" and db_path == (ROOT / "data/demo.sqlite3").resolve():
        parser.error("live 模式请使用独立实验数据库，不能使用默认样例库")
    os.environ["NMS_DB"] = str(db_path)

    if args.mode == "demo":
        from app.demo import load_demo
        load_demo(db_path)
        print("样例模式：自动装载 sample 数据，关闭网络采集、主动测试和管理写操作。", flush=True)
    else:
        print("实验模式：未装载 sample 数据；功能开关按环境配置启用。", flush=True)
    print(f"打开 http://127.0.0.1:{args.port}/ ；按 Ctrl+C 停止。", flush=True)
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
