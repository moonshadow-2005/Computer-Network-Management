# Linux 离线部署

普通本机演示直接运行 `python run.py`，无需这里的系统安装步骤。本目录用于在授权的 Linux DEV 上运行单进程 systemd 服务。

## 1. 准备与构建

在 `project` 根目录，使用与目标 DEV 一致的 Linux CPython 3.10 x86_64 环境准备依赖。包含二进制扩展的 wheel 不能从 Windows 环境直接复制到 Linux。

```bash
python3 -m pip install --target data/vendor -r requirements.txt
bash deploy/build_dev_bundle.sh
python3 -I -S deploy/smoke_release.py data/nms-team2-release.tar.gz
sha256sum data/nms-team2-release.tar.gz
```

默认 vendor 为 `data/vendor`，可用 `NMS_VENDOR_SOURCE=/path/to/vendor` 覆盖。换依赖版本时使用新的空目录，避免混入旧发行包。发布包包含 `app/`、运行依赖、版本锁及安装/更新辅助脚本，不含数据库、课程原件、凭据或 `.env`。

构建规范归档顺序、时间和所有者；相同内容可重复得到相同摘要。冒烟校验归档路径、运行依赖的完整名称版本清单，以及在临时库中的五功能 API、页面资源和 JSON/CSV；实际数量以当前锁文件为准。此检查不监听端口，也不代表真实设备采集。

离线构建机可先在同平台联网机下载：

```bash
python3 -m pip download --only-binary=:all: -r requirements.txt -d data/wheelhouse
python3 -m pip install --no-index --find-links data/wheelhouse --target data/vendor -r requirements.txt
```

## 2. 首次安装

将发布包及 `install_dev_release.sh`、`verify_release.py`、`systemd_state.sh` 通过已核实的 SSH 连接放到 DEV 同一临时目录。DEV 上以 root 执行，摘要使用构建机记录的真实 64 位 SHA-256：

```bash
NMS_RELEASE_SHA256='替换为构建机记录的64位摘要'
bash /tmp/nms-install/install_dev_release.sh /tmp/nms-install/nms-team2-release.tar.gz "$NMS_RELEASE_SHA256"
```

安装器拒绝已有部署、路径穿越、链接和不完整归档。成功后创建服务账户、私有数据库目录和受控环境文件；管理令牌在 DEV 本机生成。安装失败时先确认服务已停，再清理本轮创建的内容。

| 位置 | 用途 |
|---|---|
| `/opt/nms-team2` | 代码和 vendor |
| `/etc/nms-team2/nms.env` | systemd 读取的受控配置 |
| `/var/lib/nms-team2/nms.sqlite3` | 实验数据库 |
| `nms-team2.service` | 单 worker、回环监听服务 |

首次启动的实时采集、轮询、端口检查和 iperf3 均关闭。按[实验配置](../docs/实验环境配置.md)准备后逐项启用。

## 3. 更新与回退

已有部署使用更新器，将更新脚本及校验辅助文件与发布包放到同一临时目录，按实际路径执行：

```bash
NMS_RELEASE_SHA256='替换为新包真实摘要'
bash /tmp/nms-update/upgrade_dev_release.sh /tmp/nms-update/nms-team2-release.tar.gz "$NMS_RELEASE_SHA256"
```

更新器校验包，备份代码、unit 和 SQLite，再停止服务并替换代码。新版本需通过 health 和数据库检查；失败时保留失败数据库副本，并在确认服务停止后恢复原代码、unit 和升级前数据库。不能确认停止时保留现状与备份，不替换数据库。配置文件不被覆盖。

备份位于 `/var/lib/nms-team2/deploy-backups/`，由维护者核对后管理。本地测试包含 mock systemd 的回退逻辑，不等于所有现场失败分支都已演练。

## 4. 日常操作与访问

```bash
systemctl status --no-pager nms-team2
systemctl restart nms-team2
curl --fail http://127.0.0.1:8000/api/v1/health
journalctl -u nms-team2 -n 50 --no-pager
```

需要停止或启动时分别用 `systemctl stop nms-team2`、`systemctl start nms-team2`。浏览器通过本机 SSH 转发访问，具体方法见[实验配置](../docs/实验环境配置.md)。管理服务保持回环监听，数据库升级前停止旧服务并使用支持 Unix 私有权限的文件系统。

## 本机加密自动授权

`local_gateway.py` 使用 Windows 标准 DPAPI 的 CurrentUser 范围加密令牌，密文位于 `%LOCALAPPDATA%\NMS-Team2\admin-token.dpapi`，目录 ACL 只授予当前 Windows 用户访问。加密密钥由 Windows 管理；`store` 子命令只从标准输入接收令牌，`check` 仅报告可解密性，均不输出明文。DPAPI 用法参考 [Microsoft 文档](https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata)。

当前维护电脑的启动脚本按以下链路启动，并在就绪前验证授权：

```text
浏览器 → 127.0.0.1:8000 本机授权代理
       → 127.0.0.1:18080 SSH 转发 → DEV 127.0.0.1:8000
```

网页调用只读 `/api/v1/auth/status` 检查授权；本机代理向固定上游附加 `X-Admin-Token`，页面仅获得 `authenticated=true` 和本机入口标志。浏览器请求只携带非秘密的 `X-NMS-Local-Auth: 1`，不会取得真实令牌。主页和详情页均显示授权状态，刷新、跳转后自动重新检查。

代理严格检查 Host、Origin、Fetch Metadata 和自定义请求头，拒绝跨站、嵌入页面和 CORS 预检；只允许列出的管理 POST 路径，限制请求体大小，不记录请求内容。代理和 SSH 均仅监听 `127.0.0.1`。此方式用于可信的个人维护电脑，本机进程仍属于信任范围。

本机既有 `start_windows_loopback_tunnel.ps1 -UseCoursePassword -Detached` 现在会读取密文并启动代理和 SSH；`stop_windows_loopback_tunnel.ps1` 会识别本项目进程后关闭二者。两个脚本及课程认证材料仍属于本地配置，不包含在公开仓库。重启 Windows 后再次运行启动脚本即可；DEV 改变令牌时，需重新取得令牌并写入本机密文，再重启连接。直接 SSH 访问和未配置本机凭据的电脑继续使用手动令牌。

## Windows 一键打开网页

`open_windows_dashboard.ps1` 是已有本机连接的启动入口，`project/一键打开真实网页.cmd` 调用它并在失败时保留窗口。维护者外层的 `计网管/一键打开网络管理网页.cmd` 也调用同一脚本。入口不携带实际令牌或课程口令。

启动流程：用互斥锁避免重复建立连接 → 核对本机 8000 的进程归属 → 已就绪则复用 → 校验加密凭据 → 确认现有 WarpProxy / 40000 模式并连接 WARP → 必要时重建本项目失效的通路 → 检查 health、自动授权和回环监听 → 打开默认浏览器。占用端口的其他进程不会被停止。模式不符时提示处理，不自动切换全局网络。

诊断时可运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/open_windows_dashboard.ps1 -NoBrowser`，只启动和检查，不打开浏览器。仍需本机已有的 start/stop 脚本、代理脚本、固定主机密钥、课程资料和当前 Windows 账户的 DPAPI 凭据；这些私有资料不随仓库提供。
