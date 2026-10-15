# 第 2 组 · 计网管课程设计

本仓库分为首次开题材料和实际项目，两部分在同一个 Git 仓库中维护。

| 目录 | 内容 | 入口 |
|---|---|---|
| [project/](project/) | 网络管理系统代码、依赖、使用说明、架构和作业要求 | [项目 README](project/README.md) |
| [开题报告/](开题报告/) | 首次开题的最终 PPT、PDF、讲稿及组员分工 | [开题材料目录](开题报告/README.md) |

## 下载后运行

安装 Python 3.10 或更新版本，进入实际项目目录：

```bash
cd project
python -m pip install -r requirements.txt
python run.py
```

浏览器打开 **http://127.0.0.1:8000/**，按 `Ctrl+C` 停止。默认自动生成独立的样例数据库，可以查看五项功能，不需要课程账号、WARP 或实验网络。Ubuntu 上 Python 命令可能需要写为 `python3`。

建议使用虚拟环境，Windows 和 Ubuntu 的具体步骤见[项目使用说明](project/README.md)。真实采集、端口检查和 iperf3 需要另行配置实验环境和授权。

## 继续开发和同步

代码变更在 `project/` 中完成，项目根目录就是本仓库。配置远程仓库后照常使用 `git pull`、`git add`、`git commit` 和 `git push` 同步。运行产生的数据库、依赖、配置凭据和本机资料均由 `.gitignore` 排除。

[课程设计要求](project/docs/课程设计要求.md)保留五项功能和实验环境约束，登录凭据及外部入口由课程另行提供。开题材料反映首次汇报时的计划，当前实现与限制以[实际项目文档](project/docs/架构与模块说明.md)为准。
