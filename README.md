# 第 2 组 · 计网管课程设计

本仓库分为首次开题材料和实际项目，两部分在同一个 Git 仓库中维护。

首次使用可先打开[图文教程 HTML](project/docs/图文使用教程.html)，或阅读[Markdown](project/docs/图文使用教程.md) / [PDF](project/docs/图文使用教程.pdf)，按截图了解五项功能并完成本地练习。

访问组内真实系统、自己启动 live 采集后台、SSH 转发和配置文件区别，见[项目 README 的真实环境章节](project/README.md)及图文教程第 12 节。文档已填好第二组实际 SSH 入口 `root@240a:a019::100`；端口号不决定真实或样例模式。

| 目录 | 内容 | 入口 |
|---|---|---|
| [project/](project/) | 网络管理系统代码、依赖、使用说明、架构和作业要求 | [项目 README](project/README.md) |
| [开题报告/](开题报告/) | 首次开题的最终 PPT、PDF、讲稿及组员分工 | [开题材料目录](开题报告/README.md) |

## 下载后运行

安装 Python 3.10 或更新版本，克隆仓库并进入实际项目目录：

```bash
git clone https://github.com/moonshadow-2005/Computer-Network-Management.git
cd Computer-Network-Management/project
python -m pip install -r requirements.txt
python run.py --port 8001
```

已经下载仓库时，在仓库根目录执行 `cd project`，然后安装依赖、启动即可。

浏览器打开 **http://127.0.0.1:8001/**，按 `Ctrl+C` 停止。默认自动生成独立的样例数据库，可以查看五项功能，不需要课程账号、WARP 或实验网络。Ubuntu 上 Python 命令可能需要写为 `python3`。

建议使用虚拟环境，Windows 和 Ubuntu 的具体步骤见[项目使用说明](project/README.md)。真实采集、端口检查和 iperf3 需要另行配置实验环境和授权。

## 继续开发和同步

代码变更在 `project/` 中完成，项目根目录就是本仓库。配置远程仓库后照常使用 `git pull`、`git add`、`git commit` 和 `git push` 同步。运行产生的数据库、依赖、配置凭据和本机资料均由 `.gitignore` 排除。

[课程设计要求](project/docs/课程设计要求.md)保留五项功能和实验环境约束。第二组连接方式已写入项目说明，SSH 密码使用课程原件中的本组口令，DEV 管理令牌由蒋天乐提供或登录后读取。开题材料反映首次汇报时的计划，当前实现与限制以[实际项目文档](project/docs/架构与模块说明.md)为准。
