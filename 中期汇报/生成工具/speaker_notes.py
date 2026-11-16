"""Keep slide-specific reference notes when rebuilding the Markdown guide."""
from pathlib import Path
import re


def read_technical_notes(source: Path, count: int) -> dict[int, str]:
    parts = re.split(r"<!-- slide:(\d{2}) -->", source.read_text(encoding="utf-8"))
    numbers = [int(number) for number in parts[1::2]]
    if numbers != list(range(1, count + 1)):
        raise ValueError(f"Technical note pages do not match deck: {numbers}")
    sections = dict(zip(numbers, (body.strip() for body in parts[2::2])))
    if any(not section for section in sections.values()):
        raise ValueError("Empty technical note section")
    return sections


def write_guide(root: Path, slides: list[dict]) -> None:
    technical = read_technical_notes(root / "生成工具/逐页技术备注.md", len(slides))
    total = sum(slide["seconds"] for slide in slides)
    guide = [
        "# 第 2 组 · 网络管理系统中期汇报讲解稿", "",
        f"{len(slides)} 页；建议口播时间 {total // 60} 分 {total % 60} 秒，"
        f"距离 20 分钟留出 {(1200 - total) // 60} 分 {(1200 - total) % 60} 秒。", "",
        "每页分为口播讲解、技术备注与可能追问、事实来源。口播与 PPT 备注一致；"
        "技术备注用于备查，不逐条朗读，答疑另计时间。原“验证记录”页已删除，后续页码已顺延。", "",
        "网页截图采于 2026-11-15；测速和计费案例原始时间为 2026-10-17。"
        "安全页采用当日 10:35 的扫描监测截图；概览为升级前截图，风险数量不混用。最终监听修复与短时稳定记录另见技术备注。第 2、7 页的完整接口和地址表另据 2026-11-15 当日只读核对记录补充。", "",
    ]
    for slide in slides:
        timing, body = slide["notes"].split("\n", 1)
        spoken, sources = body.strip().removeprefix("讲解：\n").split("\n\n事实来源：\n", 1)
        guide.extend([
            f"## 第 {slide['slide']:02d} 页｜{slide['title']}", "",
            f"**{timing}**", "", "### 讲解", "", spoken, "",
            "### 技术备注与可能追问（备查，不逐条朗读）", "",
            technical[slide["slide"]], "", "### 事实来源", "", sources, "",
        ])
    (root / "中期汇报_讲解稿.md").write_text("\n".join(guide), encoding="utf-8")
