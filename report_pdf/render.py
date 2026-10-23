"""把 report.json 渲染成可直接汇报的 PDF（管理层汇报版）。

设计原则
------------------------------------------------------------
1. **单一数据源**：只读 report.json，不重新查库、不重算指标。
   PDF 里的每个数字都必须能在 report.json 里找到，否则汇报时被追问会答不上。
2. **结构化优先**：先排「结论 -> 证据 -> 动作」，不按数据源顺序罗列。
3. **图表用纯 SVG 手绘**：不引入 matplotlib 图片，避免中文字体在图片里变方块，同时保证矢量清晰、文件小。
4. **缺失即降级**：真实数据里某个模块可能为空（这天没报警、趋势不足两月），缺就跳过该章节，不占位、不写"暂无数据"凑数。
5. **口径按业务约定**：故障类统计覆盖 device_state.state 的 2、3、4 三类，正文与页脚都不展开解释，只在页脚保留一行据实说明。
6. **只呈现结论，不呈现数据问题**：这是给管理层看的汇报材料，不承载取数过程的疑点。数据层面的核对由生成侧（agent 日志）负责。

用法：
    python -m report_pdf.cli reports/daily/C/1689/2026-10-20/report.json -o 日报.pdf
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# 品牌色：深蓝为主，红黄绿按中国习惯（红=恶化/故障，绿=正常）
C_INK = "#1a2332"
C_SUB = "#5a6b7d"
C_LINE = "#dde4ec"
C_ACCENT = "#0f4c81"
C_ACCENT_LT = "#e8f0f7"
C_RED = "#c0392b"
C_AMBER = "#d68910"
C_GREEN = "#1e8449"
C_BG = "#f7f9fb"

FONT_STACK = (
    "'Noto Sans CJK SC', 'Source Han Sans SC', 'Microsoft YaHei', "
    "'PingFang SC', 'Hiragino Sans GB', sans-serif"
)

REPORT_TYPE_CN = {"daily": "设备停机日报", "weekly": "设备停机周报", "monthly": "设备停机月报"}
PROFILE_COLOR = {
    "双高型": C_RED,
    "大故障型": C_RED,
    "高频小停型": C_AMBER,
    "亚健康型": C_AMBER,
    "正常": C_GREEN,
}
DIRECTION_CN = {"up": "上升", "down": "下降", "flat": "基本持平", "unknown": "无法判断"}


# ============================================================
# 取值工具：真实数据里字段经常缺失，统一在这里兜底
# ============================================================
def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _s(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v)


def _n(v: Any, default: int = 0) -> int:
    """整数取值；非数字一律回落，用于质检结果里的计数字段。"""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def esc(v: Any) -> str:
    """HTML 转义。LLM 输出里可能带 & < >，不转义会破坏版式。"""
    return html.escape(_s(v), quote=False)


def fmt(v: Any, digits: int = 1, suffix: str = "") -> str:
    """数字格式化，None 显示为 --。"""
    if v is None or v == "":
        return "--"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return esc(v)
    txt = f"{x:,.{digits}f}"
    return txt + suffix


# ============================================================
# SVG 图表
# ============================================================
def svg_pareto(items: List[Dict[str, Any]], total: float,
               width: int = 640, bar_h: int = 30) -> str:
    """停机 Pareto：横向条形 + 累计占比。

    横向而非纵向：设备编号是长字符串，纵向图的 x 轴标签会挤在一起。
    右侧标注宽度按内容自适应：累计占比到 100% 时文字最长，
    固定宽度会被裁掉（实测 96px 装不下「6.2% · 累计 100%」）。
    """
    items = [x for x in (items or []) if _f(x.get("red_hour")) > 0]
    if not items:
        return ""
    n = len(items)
    label_w = 78
    # 预估右侧文字宽度：'xx.x% · 累计 100%' ≈ 8 字符 * 6.4px + 间隔
    max_cum = max((_f(x.get("cumulative_percent")) for x in items), default=0)
    right_pad = 100 if max_cum < 99.95 else 122
    chart_w = width - label_w - right_pad
    gap = 12
    height = n * (bar_h + gap) + 12
    vmax = max(_f(x.get("red_hour")) for x in items) or 1.0

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="{FONT_STACK}" role="img">']
    for idx, it in enumerate(items):
        y = idx * (bar_h + gap) + 6
        hour = _f(it.get("red_hour"))
        w = max(chart_w * hour / vmax, 2)
        share = _f(it.get("share_percent"))
        cum = _f(it.get("cumulative_percent"))
        mid = esc(it.get("machine_id"))
        # 前三名用红色强调，其余降为灰蓝，形成视觉层级
        color = C_RED if idx < 3 else "#8fa3b8"

        parts.append(
            f'<text x="{label_w - 8}" y="{y + bar_h * 0.7:.0f}" text-anchor="end" '
            f'font-size="12.5" fill="{C_INK}">{mid}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{chart_w}" height="{bar_h}" '
            f'rx="3" fill="{C_BG}"/>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" '
            f'rx="3" fill="{color}"/>'
        )
        # 条内数值：白字，放在条尾之内
        parts.append(
            f'<text x="{label_w + w - 8:.1f}" y="{y + bar_h * 0.7:.0f}" '
            f'text-anchor="end" font-size="12" fill="#ffffff" '
            f'font-weight="600">{hour:,.1f}h</text>'
        )
        # 条外右侧：占比 + 累计
        parts.append(
            f'<text x="{label_w + chart_w + 10}" y="{y + bar_h * 0.7:.0f}" '
            f'font-size="11.5" fill="{C_SUB}">{share:.1f}%'
            f'<tspan fill="#9aa9b8"> · 累计 {cum:.0f}%</tspan></text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_trend(months: List[Dict[str, Any]], width: int = 640,
              height: int = 210) -> str:
    """
    月份停机趋势：折线 + 数据点 + 柱状底。
    同时画柱和线：柱便于比较绝对量，线便于看方向。只画柱看不出"趋势"，只画线又读不出具体数值。
    """
    rows = [m for m in (months or []) if m.get("month")]
    if len(rows) < 2:
        return ""

    pad_l, pad_r, pad_t, pad_b = 44, 16, 22, 30
    cw = width - pad_l - pad_r
    ch = height - pad_t - pad_b
    vals = [_f(m.get("red_hour")) for m in rows]
    vmax = max(vals) or 1.0
    vmax = vmax * 1.15

    step = cw / len(rows)
    bar_w = min(step * 0.42, 40)

    def px(i: int) -> float:
        return pad_l + step * (i + 0.5)

    def py(v: float) -> float:
        return pad_t + ch - (v / vmax) * ch

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="{FONT_STACK}" role="img">']

    # 网格线（4 条）+ y 轴刻度
    for k in range(5):
        v = vmax * k / 4
        y = py(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="{C_LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="10" fill="#9aa9b8">{v:.0f}</text>'
        )

    # 柱
    for i, m in enumerate(rows):
        v = _f(m.get("red_hour"))
        x = px(i) - bar_w / 2
        y = py(v)
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{pad_t + ch - y:.1f}" rx="2" fill="{C_ACCENT_LT}"/>'
        )

    # 折线
    pts = " ".join(f"{px(i):.1f},{py(_f(m.get('red_hour'))):.1f}"
                   for i, m in enumerate(rows))
    parts.append(
        f'<polyline points="{pts}" fill="none" stroke="{C_ACCENT}" '
        f'stroke-width="2.2" stroke-linejoin="round"/>'
    )

    # 点 + 数值标签 + 月份
    last = len(rows) - 1
    for i, m in enumerate(rows):
        v = _f(m.get("red_hour"))
        cx, cy = px(i), py(v)
        # 末点高亮：这是"当前水平"
        r = 4.5 if i == last else 3.2
        fill = C_RED if i == last else C_ACCENT
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{fill}"/>')
        parts.append(
            f'<text x="{cx:.1f}" y="{cy - 9:.1f}" text-anchor="middle" '
            f'font-size="10.5" fill="{C_INK}" font-weight="600">{v:.0f}</text>'
        )
        label = _s(m.get("month"))
        if len(label) >= 7:
            label = label[2:]      # 2026-09 -> 26-09，省空间
        parts.append(
            f'<text x="{cx:.1f}" y="{pad_t + ch + 17}" text-anchor="middle" '
            f'font-size="10.5" fill="{C_SUB}">{esc(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_gauge(pct: float, width: int = 152, height: int = 84,
              label: str = "") -> str:
    """半环仪表：用于时间稼动率这类"离满值还有多少"的指标。"""
    pct = max(0.0, min(100.0, pct))
    cx, cy = width / 2, height - 8
    r = min(width / 2 - 12, height - 22)
    # 半圆路径
    import math
    def pt(angle_deg: float) -> tuple[float, float]:
        a = math.radians(180 - angle_deg)
        return cx + r * math.cos(a), cy - r * math.sin(a)

    x0, y0 = pt(0)
    x1, y1 = pt(180)
    xm, ym = pt(180 * pct / 100.0)
    # 半环最大扫过角度就是 180°，large-arc-flag 恒为 0。
    # 之前写成「>90° 置 1」，扫描角 91~179° 时会走大弧，画出游离色块。
    large = 0

    color = C_GREEN if pct >= 80 else (C_AMBER if pct >= 60 else C_RED)
    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="{FONT_STACK}" role="img">']
    parts.append(
        f'<path d="M {x0:.1f} {y0:.1f} A {r} {r} 0 1 1 {x1:.1f} {y1:.1f}" '
        f'fill="none" stroke="{C_BG}" stroke-width="10" stroke-linecap="round"/>'
    )
    if pct > 0:
        parts.append(
            f'<path d="M {x0:.1f} {y0:.1f} A {r} {r} 0 {large} 1 {xm:.1f} {ym:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="10" stroke-linecap="round"/>'
        )
    parts.append(
        f'<text x="{cx:.1f}" y="{cy - 12:.1f}" text-anchor="middle" '
        f'font-size="19" font-weight="700" fill="{C_INK}">{pct:.1f}%</text>'
    )
    if label:
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + 4:.1f}" text-anchor="middle" '
            f'font-size="9.5" fill="{C_SUB}">{esc(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ============================================================
# 章节构建
# ============================================================
def chapter_note(report: Dict[str, Any], key: str) -> str:
    """
    渲染某一节的分析结论（由 LLM 针对该章节单独生成）。
    这些文字由 report_agent.analyze_chapters 写入 report["chapter_analysis"]。
    取不到时不占位 —— 章节本身还有图表和表格支撑，不需要写「暂无分析」凑数。
    """
    ca = (report.get("chapter_analysis") or {}).get(key) or {}
    if _s(ca.get("status")) != "ok":
        return ""

    analysis = _s(ca.get("analysis")).strip()
    hl = ca.get("highlights") or {}
    if isinstance(hl, dict):
        pts = [x for x in hl.values() if _s(x).strip()]
    elif isinstance(hl, list):
        pts = [x for x in hl if _s(x).strip()]
    else:
        pts = []

    body = ""
    if analysis:
        body += f'<p class="cn-lead">{esc(analysis)}</p>'
    if pts:
        lis = "".join(f"<li>{esc(x)}</li>" for x in pts)
        body += f'<ul class="cn-pts">{lis}</ul>'
    if not body:
        return ""
    return f'<div class="cn">{body}</div>'


def _dedupe_sections(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉正文完全相同或高度相似的重复 section。

    真实场景：模型偶尔对多个数据源返回同一段（或仅改动个别数字的）结论。
    PDF 里连排 5 段一样的文字会显得报告没做分析，不如合并保留第一条。
    相似度用 16-gram 粗判：前 16 个汉字相同即视为重复，
    避免逐字符比较的性能开销，也足够抓「开头一样只换数字」的输出。
    """
    seen: set = set()
    out: List[Dict[str, Any]] = []

    def _sig(text: str) -> tuple:
        t = "".join(_s(text).split())
        return (t[:16], t[-8:] if len(t) > 24 else "")

    for s in sections or []:
        key = (_sig(s.get("analysis")),
               json.dumps(s.get("highlight") or {}, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def build_summary_cards(report: Dict[str, Any]) -> str:
    """顶部的 4 张核心指标卡。"""
    pareto = report.get("pareto") or {}
    profiles = report.get("profiles") or []
    trend = report.get("trend") or {}
    sections = report.get("sections") or []

    total_hour = _f(pareto.get("total_red_hour"))
    top3_share = _f(pareto.get("top3_share_percent"))
    items = pareto.get("items") or []
    top1 = _s(items[0].get("machine_id")) if items else "--"
    top1_hour = _f(items[0].get("red_hour")) if items else 0.0

    abnormal = [p for p in profiles
                if _s(p.get("profile")) in ("双高型", "大故障型", "高频小停型")]
    chg = trend.get("change_percent")
    direction = _s(trend.get("direction"), "unknown")

    # 稼动率：从 breaking_time 的 facts 拿不到，退而求其次用 origin_data 的中位数
    act_pct = None
    for s in sections:
        if _s(s.get("section")) == "device_breaking_time":
            vals = [_f(r.get("green_percent")) for r in (s.get("origin_data") or [])
                    if _f(r.get("green_percent")) > 0]
            if vals:
                vals.sort()
                act_pct = vals[len(vals) // 2]
            break

    cards = []

    # 卡 1：故障停机总时长
    n_machine = len(profiles) or len(items)
    cards.append((
        "故障停机总时长", f"{fmt(total_hour, 1)} <span class='unit'>h</span>",
        f"{n_machine} 台设备累计", C_INK,
    ))

    # 卡 2：Top1 集中度
    cards.append((
        "最大损失设备", esc(top1),
        f"停机 {fmt(top1_hour, 1)}h，占 {fmt(_f(items[0].get('share_percent')) if items else None, 1)}%",
        C_RED,
    ))

    # 卡 3：异常设备数
    cards.append((
        "需重点治理设备", f"{len(abnormal)} <span class='unit'>台</span>",
        f"共 {len(profiles)} 台纳入分型", C_AMBER if abnormal else C_GREEN,
    ))

    # 卡 4：趋势
    chg_txt = "--" if chg is None else f"{'+' if _f(chg) >= 0 else ''}{fmt(chg, 1)}%"
    color = C_RED if direction == "up" else (C_GREEN if direction == "down" else C_INK)
    cards.append((
        "停机趋势", chg_txt,
        f"近 {len(trend.get('months') or [])} 月{DIRECTION_CN.get(direction, '')}",
        color,
    ))

    out = ['<div class="cards">']
    for title, value, sub, color in cards:
        out.append(
            f'<div class="card"><div class="card-t">{title}</div>'
            f'<div class="card-v" style="color:{color}">{value}</div>'
            f'<div class="card-s">{sub}</div></div>'
        )
    out.append("</div>")

    if act_pct is not None:
        out.append(
            f'<div class="gauge-row"><div class="gauge-box">'
            f'{svg_gauge(act_pct, label="时间稼动率中位数")}'
            f'<div class="gauge-note">时间稼动率 = 运行时长 / 统计时长，'
            f'低于 80% 说明设备有效产出时间不足。'
            f'该值为设备中位数，非全车间加权值。</div></div></div>'
        )
    return "".join(out)


def build_key_conclusions(report: Dict[str, Any]) -> str:
    """核心结论：取各 section 的 conclusion + 预计算事实拼成要点。

    这是整份报告最该被读的部分，所以放在第一页。
    """
    pareto = report.get("pareto") or {}
    trend = report.get("trend") or {}
    profiles = report.get("profiles") or []
    alarm_groups = report.get("alarm_groups") or []
    items = pareto.get("items") or []

    points: List[str] = []

    if items:
        p0 = items[0]
        pts = [f"<b>{esc(p0.get('machine_id'))}</b> 停机 {fmt(p0.get('red_hour'))} 小时，"
               f"占全车间 {fmt(p0.get('share_percent'))}%"]
        if len(items) >= 2:
            ratio = _f(p0.get("red_hour")) / max(_f(items[1].get("red_hour")), 0.01)
            if ratio >= 1.5:
                pts.append(f"是第 2 名 {esc(items[1].get('machine_id'))} 的 {ratio:.1f} 倍，"
                           f"属明显单点异常")
        if _f(pareto.get("top3_share_percent")) >= 70:
            pts.append(f"Top3 合计占 {fmt(pareto.get('top3_share_percent'))}%，"
                       f"治理资源应集中于此")
        points.append("；".join(pts) + "。")

    # 分类别点名，并给出不同治理方向 —— 这是分型的价值所在
    for ptype, act_key in (("大故障型", "缩短单次修复时间"),
                           ("高频小停型", "降低触发次数"),
                           ("双高型", "双线并治")):
        group = [p for p in profiles if _s(p.get("profile")) == ptype]
        if not group:
            continue
        names = "、".join(esc(p.get("machine_id")) for p in group[:4])
        if len(group) > 4:
            names += f" 等 {len(group)} 台"
        sample = group[0]
        points.append(
            f"<b>{ptype}</b>：{names}。"
            f"以 {esc(sample.get('machine_id'))} 为例，单次停机 "
            f"{fmt(sample.get('ave_stop_minutes'))} 分钟，"
            f"为全厂中位数的 {fmt(sample.get('ave_vs_median'))} 倍。"
            f"治理方向是{act_key}，与其他类型设备不可混用同一套措施。"
        )

    if alarm_groups:
        g = alarm_groups[0]
        seq = " → ".join(esc(c) for c in (g.get("codes") or []))
        points.append(
            f"<b>{esc(g.get('machine_id'))}</b> 出现组合报警 {seq}，"
            f"{_i(g.get('event_count'))} 条报警集中在 {fmt(g.get('span_min'))} 分钟内，"
            f"累计停机 {fmt(g.get('combined_stop_min'))} 分钟。"
            f"共现指向共同上游，应优先排查该上游环节，而非逐个报警分别处理。"
        )

    months = trend.get("months") or []
    if len(months) >= 2:
        d = _s(trend.get("direction"))
        txt = (f"近 {len(months)} 个月停机时长由 {fmt(months[0].get('red_hour'))} 小时"
               f"变化到 {fmt(months[-1].get('red_hour'))} 小时，"
               f"较历史均值{'高' if _f(trend.get('change_percent')) >= 0 else '低'} "
               f"{fmt(abs(_f(trend.get('change_percent'))))}%，趋势{DIRECTION_CN.get(d, '')}")
        if trend.get("is_abnormal"):
            txt += "。<b>属持续性恶化，不是单日波动</b>，需纳入月度跟踪"
        points.append(txt + "。")

    if not points:
        # 全空时不要把空页面糊过去，要给一句可执行的话
        concl = [esc(s.get("analysis")) for s in _dedupe_sections(report.get("sections"))
                 if _s(s.get("analysis")) and s.get("status") == "ok"]
        points = concl[:4] or ["本周期无有效停机记录，无需分析。"]

    lis = "".join(f"<li>{p}</li>" for p in points)
    return f'<ul class="conclusions">{lis}</ul>'


def build_pareto_section(report: Dict[str, Any]) -> str:
    pareto = report.get("pareto") or {}
    items = [x for x in (pareto.get("items") or []) if _f(x.get("red_hour")) > 0]
    if not items:
        return ""
    chart = svg_pareto(items, _f(pareto.get("total_red_hour")))
    src_note = ""
    if pareto.get("source") == "faults_count_estimate":
        src_note = ("<p class='note'>注：本次时长由「故障次数 × 平均单次时长」估算，"
                    "非精确统计值，仅用于排序定位。</p>")
    return f"""
<section class="block">
  <h2><span class="num">01</span>停机集中度（Pareto）</h2>
  <p class="lead">停机时长合计 <b>{fmt(pareto.get('total_red_hour'))} 小时</b>，
  Top3 设备合计 <b>{fmt(pareto.get('top3_hour'))} 小时</b>，
  占 <b>{fmt(pareto.get('top3_share_percent'))}%</b>。</p>
  {chapter_note(report, "pareto")}
  {chart}
  {src_note}
</section>"""


def build_profile_section(report: Dict[str, Any]) -> str:
    profiles = report.get("profiles") or []
    if not profiles:
        return ""
    rows = []
    for p in profiles:
        ptype = _s(p.get("profile"))
        color = PROFILE_COLOR.get(ptype, C_SUB)
        rows.append(f"""
    <tr>
      <td class="mono">{esc(p.get('machine_id'))}</td>
      <td><span class="tag" style="background:{color}1a;color:{color};border-color:{color}44">{esc(ptype)}</span></td>
      <td class="r">{_i(p.get('red_state_num') or p.get('red_num'))}</td>
      <td class="r">{fmt(p.get('ave_stop_minutes'))}</td>
      <td class="r">{fmt(p.get('ave_vs_median'))}×</td>
      <td class="r">{fmt(p.get('stop_hour_est'))}</td>
      <td class="act">{esc(p.get('action_direction'))}</td>
    </tr>""")
    return f"""
<section class="block">
  <h2><span class="num">03</span>设备分型与治理方向</h2>
  <p class="lead">按「故障频次 × 单次时长」两个维度分型。类型不同，治理动作完全不同：
  大故障型要压缩单次修复时间，高频小停型要减少触发次数。</p>
  {chapter_note(report, "profile")}
  <table class="tbl">
    <thead><tr>
      <th>设备</th><th>分型</th><th class="r">故障次数</th>
      <th class="r">单次时长(分)</th><th class="r">对比中位数</th>
      <th class="r">停机(时)</th><th>治理方向</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>"""


def build_alarm_section(report: Dict[str, Any]) -> str:
    groups = report.get("alarm_groups") or []
    if not groups:
        return ""
    cards = []
    for g in groups[:3]:
        seq = []
        for i, it in enumerate(g.get("sequence") or []):
            gap = it.get("gap_prev_min")
            gap_txt = "" if i == 0 or gap is None else f'<span class="gap">+{fmt(gap, 1)}min</span>'
            seq.append(
                f'<div class="step">{gap_txt}<div class="code">{esc(it.get("error_code"))}</div>'
                f'<div class="meta">{esc(it.get("begin_time"))}<br/>持续 {fmt(it.get("duration_min"))} 分钟</div></div>'
            )
        cards.append(f"""
    <div class="alarm">
      <div class="alarm-h">
        <span class="mono strong">{esc(g.get('machine_id'))}</span>
        <span class="alarm-stat">{_i(g.get('event_count'))} 条报警 ·
        {fmt(g.get('span_min'))} 分钟内 · 累计停机 {fmt(g.get('combined_stop_min'))} 分钟</span>
      </div>
      <div class="steps">{''.join(seq)}</div>
    </div>""")
    return f"""
<section class="block">
  <h2><span class="num">02</span>组合报警与共同上游</h2>
  <p class="lead">同一设备在短时间窗内接连出现不同报警，通常不是多个独立故障，
  而是同一上游环节劣化引发的连锁反应。</p>
  {chapter_note(report, "alarm")}
  {''.join(cards)}
</section>"""


def build_trend_section(report: Dict[str, Any]) -> str:
    trend = report.get("trend") or {}
    months = trend.get("months") or []
    if len(months) < 2:
        return ""
    chart = svg_trend(months)
    d = _s(trend.get("direction"))
    verdict = {
        "up": ("停机呈上升趋势", C_RED, "需排查是设备劣化还是产能爬坡带来的负荷增加"),
        "down": ("停机呈下降趋势", C_GREEN, "前期治理措施有效，建议固化标准"),
        "flat": ("停机水平基本持平", C_SUB, "属常态波动，维持现有维护策略即可"),
    }.get(d, ("趋势无法判断", C_SUB, "数据月份不足或波动过大"))
    return f"""
<section class="block">
  <h2><span class="num">05</span>月份趋势</h2>
  <p class="lead">{verdict[0]}：最近月 <b>{fmt(trend.get('latest_hour'))} 小时</b>，
  历史均值 <b>{fmt(trend.get('history_avg_hour'))} 小时</b>，
  变化 <b style="color:{verdict[1]}">{fmt(trend.get('change_percent'))}%</b>。</p>
  {chapter_note(report, "trend")}
  {chart}
</section>"""


def derive_actions(report: Dict[str, Any]) -> List[Dict[str, str]]:
    """从结构化结果推导带优先级的行动清单。

    不直接抄 LLM 的建议 —— LLM 的建议常常停留在「加强管理」。
    这里由规则推导，保证每条都有明确对象、动作和数据依据；
    LLM 只负责在正文里解释这套排序为什么合理。

    返回值供 pipeline（喂给章节分析）与渲染器（画表格）共用，
    确保两边看到的是同一份清单。
    """
    actions: List[Dict[str, str]] = []

    profiles = report.get("profiles") or []
    pareto = report.get("pareto") or {}
    hour_map = {_s(i.get("machine_id")): _f(i.get("red_hour"))
                for i in (pareto.get("items") or [])}

    for p in profiles:
        ptype = _s(p.get("profile"))
        mid = _s(p.get("machine_id"))
        h = hour_map.get(mid)

        if ptype in ("双高型", "大故障型"):
            actions.append({
                "priority": "P0", "target": mid,
                "action": "拆解单次长停机过程，前置关键备件、补齐故障处置手册、"
                          "对关键部件加装状态监测",
                "basis": f"单次平均 {fmt(p.get('ave_stop_minutes'))} 分钟，"
                         f"为中位数 {fmt(p.get('ave_vs_median'))} 倍"
                         + (f"；本期累计停机 {fmt(h)} 小时" if h else ""),
            })
        elif ptype == "高频小停型":
            actions.append({
                "priority": "P1", "target": mid,
                "action": "从触发源入手：来料稳定性、工艺参数窗口、传感器可靠性、"
                          "程序逻辑逐项排查",
                "basis": f"本期故障 {_i(p.get('red_state_num') or p.get('red_num'))} 次，"
                         f"为中位数 {fmt(p.get('count_vs_median'))} 倍，"
                         f"但单次仅 {fmt(p.get('ave_stop_minutes'))} 分钟",
            })
        elif ptype == "亚健康型":
            actions.append({
                "priority": "P2", "target": mid,
                "action": "纳入重点巡检名单，设定频次与判定阈值，观察是否向高频或大故障演变",
                "basis": "故障次数与单次时长均高于中位数但未达 2 倍",
            })

    for g in (report.get("alarm_groups") or [])[:2]:
        seq = " · ".join(_s(c) for c in (g.get("codes") or []))
        actions.append({
            "priority": "P0", "target": _s(g.get("machine_id")),
            "action": f"针对组合报警 {seq} 排查共同上游（气源/供电/通讯总线/冷却），"
                      f"先修上游再评估下游报警是否消失",
            "basis": f"{_i(g.get('event_count'))} 条报警集中在 {fmt(g.get('span_min'))} 分钟内，"
                     f"累计停机 {fmt(g.get('combined_stop_min'))} 分钟",
        })

    trend = report.get("trend") or {}
    if trend.get("is_abnormal"):
        actions.append({
            "priority": "P1", "target": "全车间",
            "action": "建立月度停机台账并跟踪拐点，"
                      "定位恶化起始月份对应的变更（设备、工艺、订单结构）",
            "basis": f"最近月较历史均值变化 {fmt(trend.get('change_percent'))}%，属持续恶化",
        })

    order = {"P0": 0, "P1": 1, "P2": 2}
    actions.sort(key=lambda x: order.get(x["priority"], 9))

    seen = set()
    uniq: List[Dict[str, str]] = []
    for a in actions:
        k = (a["target"], a["action"][:20])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return uniq


def build_actions_section(report: Dict[str, Any]) -> str:
    actions = derive_actions(report)
    if not actions:
        return ""

    pcolor = {"P0": C_RED, "P1": C_AMBER, "P2": C_ACCENT}
    rows = []
    for a in actions:
        pri = a["priority"]
        rows.append(f"""
    <tr>
      <td><span class="pri" style="background:{pcolor.get(pri, C_SUB)}">{esc(pri)}</span></td>
      <td class="mono strong">{esc(a['target'])}</td>
      <td>{esc(a['action'])}</td>
      <td class="basis">{esc(a['basis'])}</td>
    </tr>""")
    return f"""
<section class="block">
  <h2><span class="num">04</span>治理行动清单</h2>
  <p class="lead">按优先级排序。P0 为本周期必须处理，P1 为本月内跟进，P2 为纳入常规管理。</p>
  {chapter_note(report, "actions")}
  <table class="tbl act-tbl">
    <thead><tr><th style="width:44px">优先级</th><th style="width:88px">对象</th>
    <th>动作</th><th style="width:190px">数据依据</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>"""


def build_gaps_section(report: Dict[str, Any]) -> str:
    gaps = report.get("data_gaps") or []
    if not gaps:
        return ""
    lis = "".join(f"<li>{esc(g)}</li>" for g in gaps)
    return f"""
<section class="block gaps">
  <h2><span class="num">06</span>数据边界说明</h2>
  <p class="lead">以下维度当前数据无法支撑，本报告未给出结论。列出是为了避免把
  「没算」误读成「没问题」。</p>
  <ul class="gap-list">{lis}</ul>
</section>"""


# ============================================================
# 样式
# ============================================================
CSS = f"""
@page {{
  size: A4;
  margin: 18mm 16mm 16mm 16mm;
  @bottom-left {{
    content: "{'设备停机分析'} · 自动生成";
    font-size: 8pt; color: #9aa9b8; font-family: {FONT_STACK};
  }}
  @bottom-right {{
    content: "第 " counter(page) " / " counter(pages) " 页";
    font-size: 8pt; color: #9aa9b8; font-family: {FONT_STACK};
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: {FONT_STACK};
  color: {C_INK};
  font-size: 10.2pt;
  line-height: 1.68;
  margin: 0;
}}
h1, h2, h3 {{ margin: 0; font-weight: 700; }}
b, strong {{ font-weight: 700; }}
.mono {{ font-family: 'DejaVu Sans Mono', 'Noto Sans Mono', {FONT_STACK}; }}

/* 封面区 */
.cover {{ border-bottom: 3px solid {C_ACCENT}; padding-bottom: 12px; margin-bottom: 16px; }}
.cover .kicker {{
  font-size: 8.6pt; letter-spacing: 2.4px; color: {C_ACCENT};
  text-transform: uppercase; font-weight: 700;
}}
.cover h1 {{ font-size: 23pt; margin: 5px 0 8px; letter-spacing: -0.4px; }}
.cover .meta {{ font-size: 9.4pt; color: {C_SUB}; }}
.cover .meta span {{ margin-right: 14px; }}
.cover .meta b {{ color: {C_INK}; }}


/* 指标卡 */
.cards {{ display: flex; gap: 9px; margin: 0 0 6px; }}
.card {{
  flex: 1; background: {C_BG}; border: 1px solid {C_LINE};
  border-radius: 6px; padding: 10px 11px; border-top: 2.5px solid {C_ACCENT};
}}
.card-t {{ font-size: 8.4pt; color: {C_SUB}; margin-bottom: 3px; }}
.card-v {{ font-size: 19pt; font-weight: 700; line-height: 1.15; }}
.card-v .unit {{ font-size: 10pt; font-weight: 500; }}
.card-s {{ font-size: 8.2pt; color: {C_SUB}; margin-top: 2px; line-height: 1.4; }}

/* 仪表 */
.gauge-row {{ display: flex; gap: 10px; margin: 10px 0 2px; }}
.gauge-box {{
  display: flex; align-items: center; gap: 14px;
  background: {C_BG}; border: 1px solid {C_LINE}; border-radius: 6px;
  padding: 8px 14px 8px 6px;
}}
.gauge-note {{ font-size: 8.5pt; color: {C_SUB}; max-width: 330px; line-height: 1.55; }}

/* 章节 */
.block {{ margin-top: 15px; page-break-inside: avoid; }}
.block h2 {{
  font-size: 12.4pt; padding-bottom: 5px; margin-bottom: 8px;
  border-bottom: 1px solid {C_LINE}; display: flex; align-items: center; gap: 8px;
}}
.block h2 .num {{
  font-size: 8.6pt; color: #fff; background: {C_ACCENT};
  padding: 1.5px 6px; border-radius: 3px; letter-spacing: .5px;
}}
.lead {{ margin: 0 0 9px; font-size: 10pt; color: #2d3a4a; }}
.note {{ font-size: 8.4pt; color: {C_SUB}; margin: 6px 0 0; }}

/* 结论列表 */
.conclusions {{ margin: 0; padding-left: 0; list-style: none; }}
.conclusions li {{
  position: relative; padding-left: 16px; margin-bottom: 7px;
  font-size: 10pt; line-height: 1.66;
}}
.conclusions li::before {{
  content: ""; position: absolute; left: 3px; top: 8px;
  width: 5px; height: 5px; border-radius: 50%; background: {C_ACCENT};
}}

/* 表格 */
.tbl {{ width: 100%; border-collapse: collapse; font-size: 8.9pt; }}
.tbl th {{
  background: {C_ACCENT_LT}; color: {C_ACCENT}; font-weight: 700;
  text-align: left; padding: 6px 7px; border-bottom: 1.5px solid {C_ACCENT}33;
  font-size: 8.4pt;
}}
.tbl td {{ padding: 5.5px 7px; border-bottom: 1px solid {C_LINE}; vertical-align: top; }}
.tbl tbody tr:nth-child(even) {{ background: #fcfdfe; }}
.tbl .r {{ text-align: right; font-variant-numeric: tabular-nums; }}
.tbl .act {{ font-size: 8.4pt; color: {C_SUB}; line-height: 1.5; }}
.tag {{
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  border: 1px solid; font-size: 8.2pt; font-weight: 600; white-space: nowrap;
}}
.pri {{
  display: inline-block; color: #fff; font-size: 8pt; font-weight: 700;
  padding: 1px 5px; border-radius: 3px;
}}
.act-tbl td {{ font-size: 8.8pt; }}
.act-tbl .basis {{ font-size: 8.2pt; color: {C_SUB}; line-height: 1.5; }}

/* 组合报警 */
.alarm {{
  border: 1px solid {C_LINE}; border-left: 3px solid {C_RED};
  border-radius: 5px; padding: 9px 11px; margin-bottom: 9px; background: #fffdfd;
}}
.alarm-h {{ display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 7px; }}
.alarm-stat {{ font-size: 8.4pt; color: {C_SUB}; }}
.steps {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.step {{
  flex: 1; min-width: 118px; background: {C_BG}; border-radius: 4px;
  padding: 6px 8px; border: 1px solid {C_LINE}; position: relative;
}}
.step .code {{ font-size: 8.6pt; font-weight: 700; color: {C_INK};
  word-break: break-all; line-height: 1.35; }}
.step .meta {{ font-size: 7.6pt; color: {C_SUB}; margin-top: 2px; line-height: 1.4; }}
.step .gap {{
  position: absolute; top: -7px; right: 6px; background: {C_AMBER};
  color: #fff; font-size: 7pt; padding: 0 4px; border-radius: 2px; font-weight: 700;
}}

/* 分项 */
.detail {{ border-left: 2px solid {C_LINE}; padding: 2px 0 2px 10px; margin-bottom: 10px; }}
.detail-h {{ font-size: 9.8pt; font-weight: 700; margin-bottom: 3px; }}
.detail-meta {{ font-size: 8pt; color: {C_SUB}; font-weight: 400; margin-left: 6px; }}
.detail-c {{ margin: 0 0 3px; font-size: 9.4pt; color: #2d3a4a; }}
.detail-u {{ margin: 0; padding-left: 15px; font-size: 8.8pt; color: {C_SUB}; }}
.detail-u li {{ margin-bottom: 2px; line-height: 1.55; }}

/* 数据边界 */
.gaps {{ background: #fffbf2; border: 1px solid #f0e0c0; border-radius: 6px;
  padding: 11px 13px; }}
.gaps h2 {{ border-bottom-color: #f0e0c0; }}
.gap-list {{ margin: 0; padding-left: 16px; font-size: 9pt; color: #6b5636; }}
.gap-list li {{ margin-bottom: 4px; line-height: 1.6; }}

/* 数据质检 */
.qc h2 {{ border-bottom-color: {C_LINE}; }}
.qc-item {{
  border-left: 3px solid {C_LINE}; padding: 5px 0 5px 10px;
  margin-bottom: 5px; background: #fdfefe;
}}
.qc-h {{ display: flex; align-items: baseline; gap: 7px; margin-bottom: 3px; }}
.qc-tag {{
  font-size: 7.6pt; padding: 1px 6px; border-radius: 3px;
  border: 1px solid {C_LINE}; font-weight: 700; white-space: nowrap;
}}
.qc-title {{ font-size: 9.8pt; font-weight: 700; }}
.qc-d {{ font-size: 8.8pt; color: #33475c; line-height: 1.5; }}
.qc-i {{ font-size: 8.6pt; color: {C_SUB}; margin-top: 2px; line-height: 1.5; }}

/* 章节分析（LLM 针对该章节生成） */
.cn {{
  background: {C_ACCENT_LT}; border-left: 3px solid {C_ACCENT};
  border-radius: 0 5px 5px 0; padding: 9px 13px; margin: 8px 0 11px;
}}
.cn-lead {{ margin: 0; font-size: 9.6pt; line-height: 1.72; color: #17324d; }}
.cn-pts {{
  margin: 6px 0 0; padding-left: 15px;
  font-size: 9pt; color: #33475c; line-height: 1.65;
}}
.cn-pts li {{ margin-bottom: 2px; }}

/* 页脚声明 */
.foot {{
  margin-top: 10px; padding-top: 8px; border-top: 1px solid {C_LINE};
  font-size: 8pt; color: #9aa9b8; line-height: 1.6;
}}
"""


# ============================================================
# 主渲染
# ============================================================
def render_html(report: Dict[str, Any]) -> str:
    meta = report.get("meta") or {}
    rtype = _s(meta.get("report_type"), "daily")
    title = REPORT_TYPE_CN.get(rtype, "设备停机报告")

    factory = _s(meta.get("factory_code"), "--")
    workshop = _s(meta.get("workshop_id"), "--")
    ds = _s(meta.get("date_start"))[:16]
    de = _s(meta.get("date_end"))[:16]
    status = _s(meta.get("status"), "success")
    failed = meta.get("failed_sections") or []

    status_html = ""
    if status != "success":
        names = "、".join(esc(x) for x in failed) if failed else "部分"
        status_html = (f'<div class="warn">本报告为部分成功：'
                       f'{names} 数据源分析失败，相关章节缺失。</div>')

    body = [
        f"""
<div class="cover">
  <div class="kicker">{title}</div>
  <h1>{esc(factory)} 工厂 · 车间 {esc(workshop)}</h1>
  <div class="meta">
    <span>统计区间 <b>{esc(ds)} ~ {esc(de)}</b></span>
    <span>周期时长 <b>{fmt(meta.get('period_hours'), 0)} 小时</b></span>
  </div>
</div>
{status_html}
{build_summary_cards(report)}
""",
        f"""
<section class="block">
  <h2><span class="num">00</span>核心结论</h2>
  {build_key_conclusions(report)}
</section>""",
        build_pareto_section(report),
        build_alarm_section(report),
        build_profile_section(report),
        build_actions_section(report),
        build_trend_section(report),
        build_gaps_section(report),
        f"""
<div class="foot">
口径说明：故障统计覆盖设备状态 2（待机）、3（人为停止）、4（设备故障），
三类状态均计入故障时长与次数。
时间稼动率 = 运行时长 / 统计时长，非真实 OEE（缺产量与良率数据）。
本报告由设备数据自动分析生成，结构化指标来自数据库精确计算，
文字结论由大模型基于上述指标生成，涉及根因的判断需现场核实。
</div>""",
    ]
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>{esc(title)} {esc(factory)}/{esc(workshop)}</title>
<style>{CSS}</style></head>
<body>{''.join(body)}</body></html>"""


def render_pdf(report: Dict[str, Any], out_path: Path) -> Path:
    from weasyprint import HTML
    html_str = render_html(report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_str).write_pdf(str(out_path))
    return out_path


def load_report(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
