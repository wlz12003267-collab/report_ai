"""各数据源的 Prompt 模板（v2）。

每个 tool 单独调用、单独构造 prompt、单独送 LLM、单独输出，互不共享上下文。

相比 v1 的改动，以及改动的原因：
    1. **字段契约前置**：把「这份数据里有什么、没有什么」写在 prompt 最前面，并显式给出禁止引用的字段清单。
    2. **预计算事实块**：把排名、占比、倍数、中位数这些**算出来的结论**作为事实喂进去，要求模型引用而不是自己算。
       原因：7B 模型做多步算术不可靠，实测把 32 倍算成 3 倍。
    3. **三角度强制覆盖**：每个 section 指定必须回答的三个角度，解决「输出过于简单」——不给角度，模型只会复述第一个最大值。
    4. **根因分级**：有数据支撑才写根因，否则必须写「待现场核查」。杜绝「可能是设备老化」这类放之四海而皆准的废话。
"""
from typing import Dict

# ------------------------------------------------------------
# 统一的 system 角色设定
# ------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是资深工业设备运维数据分析师，为制造工厂管理层撰写设备停机日报。"
    "\n\n"
    "铁律（违反任一条，整段输出作废）："
    "\n1. 只能用【可用字段】里出现过的字段。禁止出现【禁止引用】里的字段或任何未列出的指标。"
    "\n2. 每个论断后面必须带具体数值或设备编号作为证据。没有证据的句子不要写。"
    "\n3. 根因必须有数据指向。数据不足以判断根因时，写「待现场核查」并说明需要核查什么。"
    "\n4. 不写开场白、不写「综上所述」、不写「建议加强管理」这类无对象无动作的空话。"
    "\n5. 数据为空时只输出一句「本周期无相关记录。」，不补充任何推测。"
)

# ------------------------------------------------------------
# 各 section 的可用字段 / 禁止字段 / 分析角度
# ------------------------------------------------------------
SECTION_SPEC: Dict[str, Dict] = {
    "device_faults_count": {
        "title": "设备故障次数与停机时长",
        "fields": [
            "machine_id（设备编号）",
            "red_state_num（故障次数）",
            "yellow_state_num（警告次数）",
            "error_num（通讯故障次数）",
            "ave_stop_minutes（平均单次停机分钟）",
        ],
        "forbidden": [
            "green_hour / red_hour（本数据源没有时长字段，时长见 breakingTime）",
            "OEE、稼动率、MTBF、MTTR（本数据源不提供）",
            "产量、节拍、良率、成本、人员",
        ],
        "angles": [
            "集中度：故障次数与停机时长是否集中在少数设备上（引用 Pareto 事实）",
            "异常倍数：哪些设备的单次停机时长显著偏离中位数（引用倍数事实）",
            "分型与治理方向：按大故障型 / 高频小停型区分，两类治理动作完全不同",
        ],
    },
    "device_breaking_time": {
        "title": "设备运行与停机时长",
        "fields": [
            "machine_id（设备编号）",
            "green_hour（运行时间，小时）",
            "yellow_hour（警告时间，小时）",
            "red_hour（故障时间，小时）",
            "green_percent（时间稼动率，%）",
        ],
        "forbidden": [
            "red_state_num / ave_stop_minutes（本数据源没有次数与单次时长）",
            "真实 OEE（缺产量数据，只能给时间稼动率）",
            "产量、良率、成本",
        ],
        "angles": [
            "稼动水平：整体 green_percent 处于什么水平，哪些设备最低",
            "损失结构：停机时间里故障 / 警告各占多少，主体损失是故障还是警告",
            "可提升空间：这个数字离合理上限还有多少，缺口主要出在哪些设备",
        ],
    },
    "device_faults_catalog": {
        "title": "故障类型统计",
        "fields": [
            "machine_id（设备编号）",
            "error_code（故障类型编码）",
            "error_count（该故障发生次数）",
            "avg_stop_min（该类故障单次平均停机分钟）",
            "total_hour（该类故障累计停机小时）",
        ],
        "forbidden": [
            "green_hour / green_percent / OEE（本数据源没有运行时长）",
            "yellow_state_num（本数据源不含警告次数）",
            "待机、计划停机、人为停止的时长（本数据源只统计设备故障）",
        ],
        "notes": (
            "本数据源已按 raw_state = 3（设备故障）筛选，待机(2)和通讯故障(4) "
            "不在其中。因此这里的停机时长是**纯设备故障**造成的，"
            "不要把它解释成「停机总时长」，也不要说「可能包含计划停机」。"
        ),
        "angles": [
            "失效模式集中度：哪几类 error_code 贡献了主要停机（引用累计小时占比）",
            "高频 vs 长时：次数多的与单次时长长的是否是同一批故障，治理手法不同",
            "失效机理推断：从编码语义推测可能的失效部位，无把握则标注待现场确认",
        ],
    },
    "alarm_sequence": {
        "title": "组合报警与共现分析",
        "fields": [
            "machine_id（设备编号）",
            "codes / symbol（本次组合事件涉及的报警编码及顺序）",
            "event_count（组合事件内的报警条数）",
            "span_min（组合事件跨越的分钟数）",
            "combined_stop_min（组合事件累计停机分钟）",
        ],
        "forbidden": [
            "green_hour / red_hour / OEE（本数据源不含运行时长）",
            "单个报警的独立次数统计（见 faultsCatalog）",
            "待机、计划停机、人为停止（本数据源只含设备故障）",
        ],
        "notes": (
            "本数据源已按 raw_state = 3（设备故障）+ 未删除 筛选，"
            "序列中相邻报警间隔 <= 5 分钟且编码不同的被归为同一组合事件。"
            "共现只说明「同时发生」，不直接等于因果，下结论时用「疑为」「指向」而非「因为」。"
        ),
        "angles": [
            "共现模式：哪些报警总是成组出现，出现顺序是什么",
            "共同上游推断：成组出现的报警共享哪个上游环节（气源、供电、通讯总线、冷却）",
            "治理顺序：先修上游还是先修下游，为什么",
        ],
    },
    "monthly_trend": {
        "title": "月份停机趋势",
        "fields": [
            "month（统计月份）",
            "red_num（该月故障总次数）",
            "red_hour（该月故障总停机小时）",
            "yellow_num（该月警告总次数）",
            "error_num（该月通讯故障总次数）",
            "ave_stop_min（该月平均单次停机分钟）",
            "machine_count（该月有数据设备台数）",
        ],
        "forbidden": [
            "单台设备明细（本数据源是车间级汇总）",
            "产量、订单、排产计划",
        ],
        "angles": [
            "趋势方向：停机是上升、下降还是持平（引用变化百分比）",
            "异常判定：最近月是否显著偏离历史均值，属于偶发波动还是持续恶化",
            "拐点定位：若趋势变化，变化从哪个月开始，与设备数量变化是否同步",
        ],
    },
}

# ------------------------------------------------------------
# user prompt 组装
# ------------------------------------------------------------
USER_TEMPLATE = """【统计窗口】{date_start} ~ {date_end}（{report_type}，共 {period_hours} 小时）
【工厂/车间】{factory_code} / {workshop_id}
【数据源】{title}
{notes}
【可用字段】
{fields}

【禁止引用】
{forbidden}

【已计算的事实（直接引用，不要自己重算）】
{facts}

【查询结果（JSON，按重要度排序，只列前 {row_limit} 条）】
{origin_data}

【必须覆盖的分析角度】
{angles}

【输出格式（严格遵守，不要输出其他内容）】
【结论】
（一句话说清本期最该关注什么，不超过 120 字，必须含至少 2 个具体数值）

【关键点】
（3 到 5 条，每条必须带数值或设备编号。一条只讲一件事，不要合并。
 不要编号，不要加粗，不要【】标记，不要「首先/其次/综上」这类引导词，不要空行。）
- 
- 
"""

# 单次送入 LLM 的最大行数：太多会稀释重点并超出上下文
ROW_LIMIT = 30


def build_user_prompt(
    section: str,
    title: str,
    report_type: str,
    date_start: str,
    date_end: str,
    factory_code: str,
    workshop_id: str,
    origin_data: str,
    facts: str = "",
    period_hours: float = 0.0,
    row_limit: int = ROW_LIMIT,
) -> str:
    """
    为每个 section 单独构造 user prompt。
    """
    spec = SECTION_SPEC.get(section, {})
    fields = spec.get("fields") or []
    forbidden = spec.get("forbidden") or []
    angles = spec.get("angles") or []
    notes = spec.get("notes") or ""

    return USER_TEMPLATE.format(
        date_start=date_start,
        date_end=date_end,
        report_type=report_type,
        period_hours=round(float(period_hours or 0), 1),
        factory_code=factory_code,
        workshop_id=workshop_id,
        title=title,
        notes=(f"\n【口径说明】{notes}" if notes else ""),
        fields="\n".join(f"- {f}" for f in fields) or "- （见查询结果字段）",
        forbidden="\n".join(f"- {f}" for f in forbidden) or "- 无",
        facts=facts.strip() or "（本数据源无预计算事实，请仅依据查询结果分析）",
        origin_data=origin_data,
        angles="\n".join(f"{i}) {a}" for i, a in enumerate(angles, start=1)) or "1) 按要求分析",
        row_limit=row_limit,
    )


# ============================================================
# 章节级 Prompt（报告正文每一节各一次 LLM 调用）
# ============================================================
# 与 SECTION_SPEC 的区别：
#   SECTION_SPEC  —— 面向「一张表」，问的是这张表说明了什么
#   CHAPTER_SPEC  —— 面向「报告的一节」，问的是这一节该给管理层讲什么
#
# 为什么要分两层：报告正文的章节并不等于数据源。Pareto 要用停机时长 +
# 故障次数两张表；治理行动清单要综合分型、故障类型、组合报警三张表。
# 如果只做数据源级分析，正文每节就只能去「借用」别处的结论，出现同一段话在两节重复、或者一节里答非所问。
CHAPTER_SYSTEM_PROMPT = (
    "你是资深工业设备运维顾问，正在为制造工厂管理层撰写设备停机日报的其中一节。"
    "\n\n"
    "铁律（违反任一条，整段输出作废）："
    "\n1. 只依据【本节事实】作判断，禁止引入本节未给出的数字或设备。"
    "\n2. 每个论断必须带具体数值或设备编号作为证据；没有证据的句子不要写。"
    "\n3. 只写这一节该回答的问题，不要重复其他章节的内容。"
    "\n4. 不写开场白、不写「综上所述」、不写「加强管理」这类无对象无动作的空话。"
    "\n5. 本节无数据时，只输出一句「本周期无相关记录。」"
    "\n\n"
    "输出格式（必须是 JSON，不要加代码块标记）："
    '\n{"conclusion": "一段 2~3 句的结论，先说最重要的判断",'
    '\n "highlights": {"highlight_1": "证据句", "highlight_2": "证据句", "highlight_3": "证据句"}}'
)

# 每一节要回答的核心问题 + 必须覆盖的角度
CHAPTER_SPEC: Dict[str, Dict[str, str]] = {
    "pareto": {
        "focus": "停机损失集中在哪几台设备，优先动谁",
        "angles": (
            "1) 用占比说明集中度：Top1/Top3 占全车间多少，是否已超过一半；"
            "2) 说明头部设备与第 2 名的差距，判断是单点异常还是普遍性偏高；"
            "3) 给出资源配置判断：应该集中攻坚还是需要全厂铺开"
        ),
    },
    "alarm": {
        "focus": "哪些报警是成链出现的，共同上游可能在哪",
        "angles": (
            "1) 指出最值得关注的一条报警链：涉及设备、报警顺序、总停机时长；"
            "2) 从报警组合推断共同上游环节（供料/气源/供电/通讯/工装），并说明推断依据；"
            "3) 说明「逐个报警处理」为什么无效，应该怎么改处置方式"
        ),
    },
    "profile": {
        "focus": "每台设备的问题性质不同，治理方向必须分开",
        "angles": (
            "1) 点名各自类型的代表设备及判定依据（次数与单次时长相对中位数的倍数）；"
            "2) 对比不同类型设备的治理动作差异，说明混用同一套措施为什么无效；"
            "3) 指出需要纳入观察的设备及其恶化信号"
        ),
    },
    "actions": {
        "focus": "下周具体做什么，谁先做",
        "angles": (
            "1) 说明优先级排序的依据（损失大小 × 可改善空间）；"
            "2) 每类动作要指向具体设备与具体环节，不停留在「加强巡检」；"
            "3) 给出验证方式：做完之后看哪个指标能判断是否有效"
        ),
    },
    "trend": {
        "focus": "本期是偶发峰值还是持续状态",
        "angles": (
            "1) 用最近月与历史均值对比，判断当前处于什么位置；"
            "2) 说明趋势的驱动因素（设备数量变化、单次时长变化、故障频次变化）；"
            "3) 给出是否需要升级为专项治理的判断"
        ),
    },
}

CHAPTER_TEMPLATE = """【报告信息】
统计周期：{date_start} ~ {date_end}（{report_type}，共 {period_hours} 小时）
工厂/车间：{factory_code} / {workshop_id}
本节标题：{title}

【本节要回答的核心问题】
{focus}

【必须覆盖的角度】
{angles}

【本节事实】（以下数字均由系统精确计算，直接引用，不要重算）
{facts}

请严格按 JSON 格式输出：{{"conclusion": "...", "highlights": {{"highlight_1": "...", "highlight_2": "...", "highlight_3": "..."}}}}
篇幅控制：conclusion 2~3 句；highlights 每条 1 句，共 3 条。
"""


def _f(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def build_chapter_facts(chapter: str, payload, by_name: Dict[str, object]) -> str:
    """把该章节的结构化结果整理成「事实块」。

    只写事实，不写判断 —— 判断留给模型。这样模型有明确的引用对象，
    不会因为看不到数字而编造，也不会因为数字太多而只复述第一个。
    """
    lines = []

    if chapter == "pareto":
        pareto = payload if isinstance(payload, dict) else {}
        items = pareto.get("items") or []
        if not items:
            return ""
        lines.append(
            f"全车间停机合计 {_f(pareto.get('total_red_hour')):.2f} 小时；"
            f"Top3 合计 {_f(pareto.get('top3_hour')):.2f} 小时，"
            f"占 {_f(pareto.get('top3_share_percent')):.1f}%。"
        )
        for it in items:
            lines.append(
                f"第 {_i(it.get('rank'))} 名 {it.get('machine_id')}："
                f"{_f(it.get('red_hour')):.2f} 小时，占 {_f(it.get('share_percent')):.1f}%，"
                f"累计 {_f(it.get('cumulative_percent')):.1f}%。"
            )
        if len(items) >= 2:
            a, b = _f(items[0].get("red_hour")), _f(items[1].get("red_hour"))
            if b > 0:
                lines.append(f"第 1 名是第 2 名的 {a / b:.2f} 倍。")
        # 附上故障次数，用于判断「时长高」是次数多还是单次久
        faults = by_name.get("device_faults_count")
        rows = getattr(faults, "origin_data", None) or []
        top_ids = {it.get("machine_id") for it in items}
        picked = [
            f"{r.get('machine_id')} {_i(r.get('red_num') or r.get('red_state_num'))} 次、"
            f"单次 {_f(r.get('ave_stop_minutes')):.2f} 分钟"
            for r in rows if r.get("machine_id") in top_ids
        ]
        if picked:
            lines.append("上述设备的故障频次与单次时长：" + "；".join(picked) + "。")

    elif chapter == "alarm":
        groups = payload if isinstance(payload, list) else []
        if not groups:
            return ""
        lines.append(f"共识别 {len(groups)} 组组合报警。")
        for g in groups[:5]:
            seq = " → ".join(str(c) for c in (g.get("codes") or []))
            lines.append(
                f"{g.get('machine_id')}：{seq}，{_i(g.get('event_count'))} 条报警集中在 "
                f"{_f(g.get('span_min')):.1f} 分钟内，累计停机 "
                f"{_f(g.get('combined_stop_min')):.1f} 分钟。"
            )

    elif chapter == "profile":
        profiles = payload if isinstance(payload, list) else []
        if not profiles:
            return ""
        from collections import Counter
        cnt = Counter(str(p.get("profile")) for p in profiles)
        lines.append(
            "分型分布：" + "；".join(f"{k} {v} 台" for k, v in cnt.items()) + "。"
        )
        for p in profiles:
            if str(p.get("profile")) == "正常":
                continue
            lines.append(
                f"{p.get('machine_id')} 属于 {p.get('profile')}：故障 "
                f"{_i(p.get('red_state_num') or p.get('red_num'))} 次"
                f"（中位数 {_f(p.get('count_vs_median')):.2f} 倍），"
                f"单次平均 {_f(p.get('ave_stop_minutes')):.2f} 分钟"
                f"（中位数 {_f(p.get('ave_vs_median')):.2f} 倍），"
                f"累计停机 {_f(p.get('stop_hour_est')):.2f} 小时。"
            )
        normal = [p for p in profiles if str(p.get("profile")) == "正常"]
        if normal:
            lines.append(
                f"判定为正常的 {len(normal)} 台："
                + "、".join(str(p.get("machine_id")) for p in normal) + "。"
            )

    elif chapter == "actions":
        if not isinstance(payload, dict):
            return ""
        profiles = payload.get("profiles") or []
        groups = payload.get("alarm_groups") or []
        trend = payload.get("trend") or {}
        pareto = payload.get("pareto") or {}
        if not (profiles or groups or trend):
            return ""
        items = pareto.get("items") or []
        if items:
            top = items[0]
            lines.append(
                f"当前停机损失最高设备为 {top.get('machine_id')}，"
                f"停机 {_f(top.get('red_hour')):.2f} 小时，占 {_f(top.get('share_percent')):.1f}%。"
            )
        for p in profiles:
            if str(p.get("profile")) == "正常":
                continue
            lines.append(
                f"{p.get('machine_id')} 分型 {p.get('profile')}，"
                f"故障 {_i(p.get('red_state_num') or p.get('red_num'))} 次，"
                f"单次平均 {_f(p.get('ave_stop_minutes')):.2f} 分钟，"
                f"累计停机 {_f(p.get('stop_hour_est')):.2f} 小时。"
            )
        for g in groups[:3]:
            seq = " → ".join(str(c) for c in (g.get("codes") or []))
            lines.append(
                f"{g.get('machine_id')} 组合报警 {seq}，"
                f"{_i(g.get('event_count'))} 条报警集中在 {_f(g.get('span_min')):.1f} 分钟内，"
                f"累计停机 {_f(g.get('combined_stop_min')):.1f} 分钟。"
            )
        if trend:
            lines.append(
                f"月度趋势：最近月 {_f(trend.get('latest_hour')):.2f} 小时，"
                f"历史均值 {_f(trend.get('history_avg_hour')):.2f} 小时，"
                f"变化 {_f(trend.get('change_percent')):+.2f}%。"
            )

    elif chapter == "trend":
        trend = payload if isinstance(payload, dict) else {}
        months = trend.get("months") or []
        if len(months) < 2:
            return ""
        lines.append(
            "月度停机小时：" + "；".join(
                f"{m.get('month')} {_f(m.get('red_hour')):.1f}h"
                f"（{_i(m.get('machine_count'))} 台）" for m in months
            ) + "。"
        )
        lines.append(
            f"最近月 {_f(trend.get('latest_hour')):.2f} 小时，"
            f"历史均值 {_f(trend.get('history_avg_hour')):.2f} 小时，"
            f"变化 {_f(trend.get('change_percent')):+.2f}%。"
        )
        lines.append(
            "平均单次停机分钟：" + "；".join(
                f"{m.get('month')} {_f(m.get('ave_stop_min')):.2f}" for m in months
            ) + "。"
        )

    return "\n".join(lines)


def build_chapter_prompt(
    chapter: str,
    title: str,
    report_type: str,
    date_start: str,
    date_end: str,
    factory_code: str,
    workshop_id: str,
    facts: str,
    period_hours: float = 0.0,
) -> str:
    """构造某一节的 user prompt。"""
    spec = CHAPTER_SPEC.get(chapter, {})
    return CHAPTER_TEMPLATE.format(
        date_start=date_start,
        date_end=date_end,
        report_type=report_type,
        period_hours=round(float(period_hours or 0), 1),
        factory_code=factory_code,
        workshop_id=workshop_id,
        title=title,
        focus=spec.get("focus") or "总结本节要点",
        angles=spec.get("angles") or "1) 总结要点",
        facts=facts.strip(),
    )
