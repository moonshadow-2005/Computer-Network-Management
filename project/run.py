"""Portable local entry point. Default to an isolated, read-only sample mode."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第 2 组实验网络管理系统")
    parser.add_argument("--mode", choices=("demo", "local", "live"), default="demo",
                        help="demo 只读样例；local 本地交互测试；live 实验环境（默认 demo）")
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
    from app.demo import DEMO_DB, LOCAL_TEST_DB, load_demo

    if args.mode in {"demo", "local"}:
        db_path = args.db or (DEMO_DB if args.mode == "demo" else LOCAL_TEST_DB)
        # An inherited live configuration must never turn a sample launch into
        # network collection or active testing. Local writes require a token.
        os.environ.update({
            "NMS_ENABLE_LIVE": "0", "NMS_POLL_INTERVAL": "0",
            "NMS_ENABLE_SERVICE_PROBE": "0", "NMS_ENABLE_IPERF3": "0",
        })
        if args.mode == "demo":
            os.environ["NMS_API_TOKEN"] = ""
        elif not os.environ.get("NMS_API_TOKEN", "").strip():
            parser.error("local 模式请先设置 NMS_API_TOKEN，并在网页填写相同的管理令牌")
    else:
        db_path = args.db or Path(os.environ.get("NMS_DB") or "data/nms.sqlite3")
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db_path = db_path.resolve()
    if args.mode == "live" and db_path in {
        DEMO_DB.resolve(), LOCAL_TEST_DB.resolve(), (ROOT / "data/demo.sqlite3").resolve(),
    }:
        parser.error("live 模式请使用独立实验数据库，不能使用默认样例库")
    os.environ["NMS_DB"] = str(db_path)

    if args.mode in {"demo", "local"}:
        load_demo(db_path)
        if args.mode == "demo":
            print("样例模式：自动装载 sample 数据，关闭网络采集、主动测试和管理写操作。", flush=True)
        else:
            print("本地交互测试：允许确认告警、确认风险和保存基线，关闭真实采集、端口探测和 iperf3。", flush=True)
            print("请在网页“运行与采集”填写本次设置的 NMS_API_TOKEN。", flush=True)
        print(f"样例数据库：{db_path}", flush=True)
    else:
        print("实验模式：未装载 sample 数据；功能开关按环境配置启用。", flush=True)
    print(f"打开 http://127.0.0.1:{args.port}/ ；按 Ctrl+C 停止。", flush=True)
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
