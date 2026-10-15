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
