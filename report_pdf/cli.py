"""PDF 生成入口。

用法：
    # 指定 report.json
    python -m report_pdf.cli reports/daily/C/1689/2026-10-20/report.json

    # 指定输出位置
    python -m report_pdf.cli <report.json> -o /workspace/日报.pdf

    # 只输出 HTML（便于调版式，浏览器里看效果）
    python -m report_pdf.cli <report.json> --html out.html

    # 自动找最近一份 report.json
    python -m report_pdf.cli --latest
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from report_pdf.render import load_report, render_html, render_pdf  # noqa: E402


def find_latest(report_root: Optional[Path] = None) -> Optional[Path]:
    """在 reports 目录下找最近修改的 report.json。"""
    root = report_root or (BASE_DIR / "reports")
    if not root.exists():
        return None
    cands = sorted(root.rglob("report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="把 report.json 渲染成汇报用 PDF")
    parser.add_argument("report", nargs="?", help="report.json 路径")
    parser.add_argument("-o", "--output", default=None, help="输出 PDF 路径")
    parser.add_argument("--html", default=None, help="只输出 HTML 到指定路径")
    parser.add_argument("--latest", action="store_true", help="自动使用最近一份 report.json")
    args = parser.parse_args(argv)

    src = None
    if args.report:
        src = Path(args.report)
    elif args.latest:
        src = find_latest()

    if src is None or not src.exists():
        print(f"找不到 report.json: {src}", file=sys.stderr)
        return 2

    try:
        report = load_report(src)
    except Exception as e:
        print(f"读取失败 {src}: {e}", file=sys.stderr)
        return 2

    # 结构校验：缺关键字段时给出明确提示，而不是渲染出一份空白报告
    missing = [k for k in ("meta",) if k not in report]
    if missing:
        print(f"report.json 缺少必需字段: {missing}", file=sys.stderr)
        return 2

    meta = report.get("meta") or {}
    if meta.get("status") not in (None, "success", "partial"):
        print(f"注意：meta.status = {meta.get('status')}，仍会生成但可能内容不全",
              file=sys.stderr)

    if args.html:
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(report), encoding="utf-8")
        print(f"HTML 已写入: {out}")
        return 0

    if args.output:
        out = Path(args.output)
    else:
        stem = f"{meta.get('factory_code', 'X')}_{meta.get('workshop_id', 'X')}_" \
               f"{str(meta.get('date_start', ''))[:10]}_{meta.get('report_type', 'daily')}"
        out = BASE_DIR / "output" / f"{stem}.pdf"

    try:
        render_pdf(report, out)
    except Exception as e:
        print(f"渲染失败: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    size_kb = out.stat().st_size / 1024
    print(f"PDF 已生成: {out}  ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
