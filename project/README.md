# 实验网络管理系统

FastAPI + SQLite + 原生网页实现故障、配置、性能、计费和安全五项管理功能。前端无需 Node.js 构建。

- [作业要求](docs/课程设计要求.md)
- [图文使用教程（含实际页面截图）](docs/图文使用教程.md) · [独立 HTML](docs/图文使用教程.html) · [PDF](docs/图文使用教程.pdf)
- [五项功能使用说明](docs/使用说明.md)
- [架构、代码模块和当前限制](docs/架构与模块说明.md)
- [实验环境与配置](docs/实验环境配置.md)
- [API 和数据合同](接口与数据合同.md)
- [Linux 离线部署](deploy/README.md)

## 先选择使用方式

| 目的 | 入口 | 本机是否需要启动项目 |
|---|---|---|
| 练习页面、确认和保存操作 | 下文“测试确认、保存等按钮”，使用 local 模式 | 需要，示例端口 8001 |
| 查看或操作组内已部署的真实 DEV | 下文“方式 A：访问已有 DEV” | 无需再启动，建立 SSH 转发即可 |
| 在能访问实验内网的 Linux 上运行自己的采集后台 | 下文“方式 B：自己启动 live 后台” | 需要，示例端口 8002 |

端口只是访问地址：8000、8001、8002 都不固定代表真实或样例。模式、数据库和网络配置决定数据来源。图文教程第 12 节完整演示真实环境的访问与启动。

## 快速开始

需要 Python 3.10+。以下命令都在本 README 所在的 `project` 目录执行。

已经有 Python 环境时：

```bash
python -m pip install -r requirements.txt
python run.py --port 8001
```

浏览器打开 **http://127.0.0.1:8001/**，终端按 `Ctrl+C` 停止。启动器自动生成 `data/demo-v2.sqlite3` 并装载样例，重复启动不会重复添加数据。默认样例库按数据库结构版本命名；升级后重新生成样例，并保留旧版样例库。省略 `--port` 时程序默认使用 8000；教程统一将本地样例放在 8001，便于同时访问远端系统。

### 建议：使用虚拟环境

Ubuntu / WSL / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py --port 8001
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py --port 8001
```

Windows 示例直接调用虚拟环境解释器，无需更改 PowerShell 脚本执行策略。Linux 若缺少 venv 支持，可安装发行版对应的 `python3-venv` 包。运行依赖版本保存在 [requirements-runtime.lock](deploy/requirements-runtime.lock)，pip 根据本机平台下载对应 wheel，无需复制其他电脑的 `.vendor`。

## 启动选项

```bash
python run.py --help
python run.py --port 8001
python run.py --mode live --port 8002
```

| 模式 | 数据库 | 行为 |
|---|---|---|
| `demo`（默认） | `data/demo-v2.sqlite3` | 自动装载 `sample` 数据，关闭实时采集、主动测试和管理写操作 |
| `local` | `data/local-test-v2.sqlite3` | 独立样例库；配置令牌后可确认告警、确认风险和保存基线，关闭真实采集与主动测试 |
| `live` | `data/nms.sqlite3` 或 `NMS_DB` | 不装载样例，按本机环境配置启动；默认仍关闭网络采集和主动测试 |

需要指定其他数据库可用 `--db /path/to/file.sqlite3`。两个默认样例库及旧的 `data/demo.sqlite3` 不能用于 live 模式。服务仅监听本机 `127.0.0.1`，固定为单 worker。

默认样例模式即使读到本机 `.env` 中的实时开关，也会关闭实时功能并使用样例库。样例故障、风险和 iperf3 任务均为构造记录，页面会显示来源标记。

### 测试确认、保存等按钮

先按 `Ctrl+C` 停止旧进程。在 WSL / Bash 中运行：

```bash
NMS_API_TOKEN=local-test-only python3 run.py --mode local --port 8001
```

Windows PowerShell：

```powershell
$env:NMS_API_TOKEN = 'local-test-only'
.\.venv\Scripts\python.exe run.py --mode local --port 8001
```

打开 `http://127.0.0.1:8001/`，在页面底部“运行与采集 → 管理令牌”填 `local-test-only`。这是本机测试示例令牌，不用于真实部署。告警确认、风险确认和保存基线可以修改独立测试库；刷新或重启后结果保留。具体检查步骤见[使用说明](docs/使用说明.md#本地交互测试)。

local 模式不会自动执行 ping、SNMP、端口检查或 iperf3，即使本机环境中原先开启了相关开关。上述网络操作仍返回功能关闭提示。

## 页面与 API

- 页面：<http://127.0.0.1:8001/>
- 健康状态：<http://127.0.0.1:8001/api/v1/health>
- 交互 API 文档：<http://127.0.0.1:8001/docs>
- OpenAPI：<http://127.0.0.1:8001/openapi.json>

本地演示先选择 `sample-host-1` 和 `sample-router:1`；快照比较选择 `sample-router`。完整操作和预期结果见[使用说明](docs/使用说明.md)。

## 真实环境 live：访问与启动

### 方式 A：访问已有 DEV

本组已有 DEV 后台，由 systemd 运行；组员电脑只需 SSH 和浏览器。

| 第 2 组连接信息 | 当前值 |
|---|---|
| DEV SSH 入口 | `240a:a019::100`，TCP/22 |
| SSH 用户 | `root` |
| SSH 密码 | 课程说明中“第 2 组”的登录口令 |
| DEV 内网地址 | `192.168.0.100` |
| DEV 网页监听 | `127.0.0.1:8000` |

1. 使用上表的第二组账号。当前这台电脑已验证的通路经过 Windows WARP SOCKS5 `127.0.0.1:40000`；继续沿用已有代理和固定主机密钥配置。
2. 在本机 WSL / Bash 建立转发。下面已填好本组账号和入口，本机使用 18000，避免占用已有的 8000/8001：

   ```bash
   ssh -N -T -o ExitOnForwardFailure=yes \
     -o ServerAliveInterval=20 \
     -L 127.0.0.1:18000:127.0.0.1:8000 \
     root@240a:a019::100
   ```

   这条命令适用于本机 IPv6 已可达或 SSH 已配置 ProxyCommand 的环境。需要代理时，保留已验证的 ProxyCommand 和固定主机密钥配置；仅在 Windows 打开 WARP 的 SOCKS5 端口，不会让裸 `ssh` 自动使用它。新电脑先打通到上述 DEV 入口的连接，再建立转发。
3. 保持 SSH 终端运行，浏览器打开 **http://127.0.0.1:18000/**。这台电脑原先已有的 8000 转发仍可继续使用，无需重复建立。
4. 在“运行与采集”填写 **DEV 服务的管理令牌**。由蒋天乐提供，或按下文登录 DEV 读取；它与 SSH 登录口令、SNMP community、本地练习令牌分别配置。
5. 点击“执行一轮只读采集”，等本轮结束后再执行下一轮。查看新增运行编号、采样时间和 `live` 来源，然后选择真实主机及接口查看性能和流量。

关闭 SSH 转发只会中断本机访问，DEV 后台和数据仍保留。打开网页只是在读取已有记录；`poll_interval_s=0` 时需手动采集。速率需要有效的相邻计数样本，久未采集后的第一轮可能无效，后续连续样本才可计算。

### 方式 B：自己启动 live 后台

此方式要求**运行 Python 的那台 Linux / WSL / DEV 本身能访问课程内网**，具备 `ping`，并能通过 SNMP 读取登记设备。普通电脑仅有一个网页 SSH 转发时，不会自动获得到课程 `192.168.x.x` 的采集通路；这种情况使用方式 A。

在 `project` 目录、已安装依赖的环境中准备配置，保留已有 `.env`：

```bash
test -e .env || cp .env.example .env
```

用编辑器打开 `.env`，写入或合并以下配置。本组目前已实测 `public` 可读取基础信息和接口、路由表；它是否为课程正式指定凭据仍待确认：

```dotenv
NMS_ENABLE_LIVE=1
NMS_SNMP_COMMUNITY=public
NMS_POLL_INTERVAL=0
NMS_ENABLE_SERVICE_PROBE=0
NMS_ENABLE_IPERF3=0
```

在终端执行下面命令：保留 `.env` 已有的管理令牌；尚未设置时生成并保存随机值。将终端显示的值填入自己新启动的网页，这不会改变已有 DEV 的令牌：

```bash
python3 - <<'PY'
from dotenv import dotenv_values, set_key
from secrets import token_urlsafe

token = dotenv_values('.env').get('NMS_API_TOKEN') or token_urlsafe(32)
set_key('.env', 'NMS_API_TOKEN', token)
print('本后台管理令牌：', token)
PY
```

启动独立实验库；WSL 下将数据库放在 Linux 用户目录，便于后续迁移时创建私有备份：

```bash
NMS_DB="$HOME/.local/share/nms-team2/live.sqlite3" \
  python3 run.py --mode live --port 8002
```

打开 **http://127.0.0.1:8002/**，检查状态：

```bash
curl --noproxy '*' --fail http://127.0.0.1:8002/api/v1/health
```

预期 `database=ok`、`live_enabled=true`、`snmp_configured=true`。然后填管理令牌并手动采集。全新实验库最初只有文档候选清单，实际接口和历史要等采集成功后才出现。

启动器读取 `project/.env`，已有终端环境变量优先，`--db` 优先于 `NMS_DB`；切换到 live 不会自动开启所有功能，也不会把样例库转换成真实测量。

### DEV 后台的启动与配置位置

已有 systemd 部署在 DEV 上执行以下命令；这些命令不在本机样例终端执行：

```bash
sudo systemctl status --no-pager nms-team2
sudo systemctl start nms-team2
```

DEV 维护者修改 `/etc/nms-team2/nms.env` 后，执行 `sudo systemctl restart nms-team2`。该服务读取这个受控配置文件，数据库为 `/var/lib/nms-team2/nms.sqlite3`；本机 `project/.env` 的修改不会影响它。全新 DEV 安装与更新见[部署说明](deploy/README.md)。

**取得已有 DEV 的管理令牌：**维护者登录 DEV 后，可只读查看配置：

```bash
sudo less /etc/nms-team2/nms.env
```

查找 `NMS_API_TOKEN=`，只复制等号后面的值（不包含包裹值的引号），填入网页“管理令牌”，按 `q` 退出查看。查看现有值无需重启服务；没有读取权限的使用者向维护者取得令牌。仓库模板不含实际值，`local-test-only` 仅适用于前文的本地练习。

### 真实环境中的功能边界

| 功能 | 条件 |
|---|---|
| SNMP 接口/路由、计数、速率、流量报表 | live 开启、正确 community、内网可达、足够的有效采样 |
| DEV 到主机的 ping 延迟与丢包 | live 开启、DEV 能执行 ping 并到达登记主机 |
| 确认告警、确认风险、保存基线 | 正确管理令牌 |
| 指定端口检查 | live 与 `NMS_ENABLE_SERVICE_PROBE=1`、已配置目标端口基线 |
| 网页 iperf3 | live 与 `NMS_ENABLE_IPERF3=1`，以及服务账户到两端的专用 SSH 认证、固定主机密钥和工具 |

两端 SSH 认证属于 DEV 服务账户的统一配置，访问网页的每台电脑只需配置自己的 DEV 连接。近期受控试运行仍因服务 SSH 条件缺失而失败，iperf3 已恢复关闭；完成认证条件前不能把打开开关当作成功测速。配置项和排障细节见[实验环境配置](docs/实验环境配置.md)，操作截图见[图文教程](docs/图文使用教程.md)第 12 节。

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
