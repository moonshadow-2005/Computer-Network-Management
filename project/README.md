# 实验网络管理系统

FastAPI + SQLite + 原生网页实现故障、配置、性能、计费和安全五项管理功能。前端无需 Node.js 构建。

- [作业要求](docs/课程设计要求.md)
- [五项功能使用说明](docs/使用说明.md)
- [架构、代码模块和当前限制](docs/架构与模块说明.md)
- [实验环境与配置](docs/实验环境配置.md)
- [API 和数据合同](接口与数据合同.md)
- [Linux 离线部署](deploy/README.md)

## 快速开始

需要 Python 3.10+。以下命令都在本 README 所在的 `project` 目录执行。

已经有 Python 环境时：

```bash
python -m pip install -r requirements.txt
python run.py
```

浏览器打开 **http://127.0.0.1:8000/**，终端按 `Ctrl+C` 停止。启动器自动生成 `data/demo.sqlite3` 并装载样例，重复启动不会重复添加数据。

### 建议：使用虚拟环境

Ubuntu / WSL / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

Windows 示例直接调用虚拟环境解释器，无需更改 PowerShell 脚本执行策略。Linux 若缺少 venv 支持，可安装发行版对应的 `python3-venv` 包。运行依赖版本保存在 [requirements-runtime.lock](deploy/requirements-runtime.lock)，pip 根据本机平台下载对应 wheel，无需复制其他电脑的 `.vendor`。

## 启动选项

```bash
python run.py --help
python run.py --port 8001
python run.py --mode live
```

| 模式 | 数据库 | 行为 |
|---|---|---|
| `demo`（默认） | `data/demo.sqlite3` | 自动装载 `sample` 数据，关闭实时采集、主动测试和管理写操作 |
| `live` | `data/nms.sqlite3` 或 `NMS_DB` | 不装载样例，按本机环境配置启动；默认仍关闭网络采集和主动测试 |

需要指定其他数据库可用 `--db /path/to/file.sqlite3`。默认样例库不能用于 live 模式。服务仅监听本机 `127.0.0.1`，固定为单 worker。

默认样例模式即使读到本机 `.env` 中的实时开关，也会关闭实时功能并使用样例库。样例故障、风险和 iperf3 任务均为构造记录，页面会显示来源标记。

## 页面与 API

- 页面：<http://127.0.0.1:8000/>
- 健康状态：<http://127.0.0.1:8000/api/v1/health>
- 交互 API 文档：<http://127.0.0.1:8000/docs>
- OpenAPI：<http://127.0.0.1:8000/openapi.json>

本地演示先选择 `sample-host-1` 和 `sample-router:1`；快照比较选择 `sample-router`。完整操作和预期结果见[使用说明](docs/使用说明.md)。

## 接入实验网络

复制 `.env.example` 为 `.env`，按[实验配置说明](docs/实验环境配置.md)填写本机配置，再执行 `python run.py --mode live`。启动器自动读取项目目录的 `.env`，已存在的终端环境变量优先。

真实采集需要能够访问实验网的 Linux / WSL / DEV 环境及 `ping`；iperf3 自动任务还需要服务账户经 SSH 控制已预装工具的两台测试主机。本机样例无需这些条件。获取源代码不会自动取得课程实验网访问权限。

## 开发验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Python 测试包含 Linux 部署脚本的验证，完整套件在 Linux/WSL 中运行。安装 Node.js 的开发环境还可运行前端交互检查：

```bash
node --check app/web/app.js
node tests/web_dom_smoke.js
```

## 目录

```text
project/
├── run.py                  # 默认装载样例的启动器
├── requirements.txt        # 安装锁定的运行依赖
├── requirements-dev.txt    # 开发与测试依赖
├── .env.example            # 无凭据的配置模板
├── app/
│   ├── main.py             # FastAPI 和静态资源
│   ├── demo.py             # 样例生成
│   ├── service.py          # SNMP / ping 采集调度
│   ├── core/               # SQLite 与配置
│   ├── collectors/         # SNMP 与 ping
│   ├── modules/            # 五项管理逻辑
│   └── web/                # 页面、样式与交互
├── tests/                  # 业务、接口、部署和启动器测试
├── docs/                   # 作业要求、使用与架构说明
└── deploy/                 # Linux 打包、部署和回退
```

数据目录 `data/` 首次运行时自动创建。数据库、缓存、虚拟环境和 `.env` 不进入 Git。当前功能的边界见[架构说明](docs/架构与模块说明.md)，例如计费目前按接口方向统计字节，尚不支持任意用户对的流量归属或资费计算。
