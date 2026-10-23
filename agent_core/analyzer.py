"""停机分析方法论（代码预计算层）。

设计原则：**能算的绝不让模型算**。
    - 排序、占比、倍数、中位数、趋势斜率 → 代码算，结果确定可复现；
    - 归因、影响、治理建议 → 模型解释，但必须引用代码给的事实。

这样做的原因：
    1. 7B 级别模型做多步算术极易出错（实测把 32 倍算成 3 倍）；
    2. 报告要能被复现和审计，关键数字必须来自代码而非模型拟合；
    3. prompt 里塞「算好的事实」，模型只需要做「解释」，输出质量显著提升。

模块内所有函数都是纯函数（输入 dict / list，输出 dict / list），不查库、不读配置，
便于单测。原始数据由 tools_setup 提供。
"""
from typing import Any, Dict, List, Optional, Sequence

# 设备分型阈值
BIG_FAILURE_MULTIPLE = 2.0    # 单次时长 >= 中位数 2 倍 → 大故障型
FREQ_MULTIPLE = 2.0           # 故障次数 >= 中位数 2 倍 → 高频小停型
# 「亚健康型」的放宽倍数：次数/时长只要超过中位数的 1.5 倍就纳入观察。
WATCH_MULTIPLE = 1.5

# 组合报警共现窗口（与 tools_setup.CO_OCCUR_WINDOW_MIN 保持一致）
DEFAULT_CO_OCCUR_WINDOW = 5


# ============================================================
# 基础工具
# ============================================================
def _f(value: Any, default: float = 0.0) -> float:
    """安全转 float。"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    """安全转 int。"""
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def median(values: Sequence[float]) -> float:
    """中位数。空序列返回 0。"""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def _r(value: Optional[float], digits: int = 2) -> Optional[float]:
    """四舍五入，None 透传。"""
    if value is None:
        return None
    return round(float(value), digits)


# 对外公开别名：其他模块不应依赖下划线前缀的私有名
to_float = _f
to_int = _i


def _fault_count(row: Dict[str, Any]) -> float:
    """读取故障次数，兼容两种字段名。

    历史坑：`query_device_faultsCount` 对外输出的是 `red_num`
    （SQL 里 `SUM(red_state_num) AS red_num`），但分析层早期只读
    `red_state_num`，于是每条都取到 0，导致全部设备被判成「正常」、
    count_vs_median 全为 null。两个名字都认，避免再次踩坑。
    """
    if not row:
        return 0.0
    for key in ("red_state_num", "red_num", "error_count", "count"):
        if key in row and row[key] is not None:
            return _f(row[key])
    return 0.0


# 公开别名，供 report_agent / renderer 使用
fault_count = _fault_count


# ============================================================
# Pareto：停机时长集中度（先定位大头，再谈治理）
# ============================================================
def build_pareto(
    breaking_rows: List[Dict[str, Any]],
    fault_rows: Optional[List[Dict[str, Any]]] = None,
    top_n: int = 5,
) -> Dict[str, Any]:
    """按设备聚合停机小时数，输出 Pareto 排序。

    数据源优先级：
        1. breaking_time 的 red_hour（直接是小时，最可靠）；
        2. 回退：faults_count 的 red_state_num × ave_stop_minutes / 60。
           该估算在 ave_stop_minutes 缺失时不可用，直接跳过（不补 0，
           否则会在 Pareto 里插入一堆假的 0 值条目）。
    """
    hours_by_machine: Dict[str, float] = {}

    for row in breaking_rows or []:
        mid = str(row.get("machine_id") or "").strip()
        if not mid:
            continue
        h = _f(row.get("red_hour"))
        if h > 0:
            hours_by_machine[mid] = hours_by_machine.get(mid, 0.0) + h

    if not hours_by_machine:
        for row in fault_rows or []:
            mid = str(row.get("machine_id") or "").strip()
            if not mid:
                continue
            cnt = _fault_count(row)
            ave = _f(row.get("ave_stop_minutes"))
            if cnt <= 0 or ave <= 0:
                continue
            est = cnt * ave / 60.0
            if est <= 0:
                continue
            hours_by_machine[mid] = hours_by_machine.get(mid, 0.0) + est

    total = sum(hours_by_machine.values())
    if total <= 0:
        return {
            "items": [],
            "total_red_hour": 0.0,
            "top3_share_percent": 0.0,
            "top3_hour": 0.0,
            "source": "none",
        }

    # 判定数据来源：有 breaking_time 的真实 red_hour 就用它，否则是估算值。
    # 必须在计算前判定 —— 用结果反推会把「真实值恰好为 0」的情形误判成估算。
    used_real = any(_f(r.get("red_hour")) > 0 for r in (breaking_rows or []))

    ordered = sorted(hours_by_machine.items(), key=lambda kv: kv[1], reverse=True)
    items: List[Dict[str, Any]] = []
    cum = 0.0
    for idx, (mid, h) in enumerate(ordered[:top_n], start=1):
        cum += h
        items.append({
            "rank": idx,
            "machine_id": mid,
            "red_hour": _r(h),
            "share_percent": _r(h / total * 100),
            "cumulative_percent": _r(cum / total * 100),
        })

    top3_hour = sum(h for _, h in ordered[:3])
    return {
        "items": items,
        "total_red_hour": _r(total),
        "top3_hour": _r(top3_hour),
        "top3_share_percent": _r(top3_hour / total * 100),
        "source": "breaking_time" if used_real else "faults_count_estimate",
    }


def summarize_pareto(pareto: Dict[str, Any]) -> str:
    """把 Pareto 结果转成一句可直接放进 prompt 的事实描述。"""
    items = pareto.get("items") or []
    if not items:
        return "本周期无有效停机时长记录，无法排序。"

    first = items[0]
    parts = [
        f"停机时长合计 {pareto['total_red_hour']} 小时，"
        f"Top3 设备合计 {pareto['top3_hour']} 小时，占 {pareto['top3_share_percent']}%。"
    ]
    parts.append(
        f"停机最长的是 {first['machine_id']}（{first['red_hour']} 小时，"
        f"占 {first['share_percent']}%）。"
    )
    if len(items) >= 2:
        second = items[1]
        if first["red_hour"] and second["red_hour"]:
            ratio = first["red_hour"] / second["red_hour"]
            if ratio >= 1.5:
                parts.append(
                    f"{first['machine_id']} 是第 2 名 {second['machine_id']}"
                    f"（{second['red_hour']} 小时）的 {_r(ratio)} 倍，属于明显的单点异常。"
                )
    if pareto.get("source") == "faults_count_estimate":
        parts.append("（注：时长由故障次数×平均单次时长估算，非精确值。）")
    return "".join(parts)


# ============================================================
# 设备分型：决定治理动作的方向
# ============================================================
def build_profiles(
    fault_rows: List[Dict[str, Any]],
    breaking_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """按「故障频次 × 单次时长」给设备分型。

    分型判定顺序（顺序不能换，换了会误判）：
        1. 大故障型：单次平均时长 >= 全厂中位数 × 2
           —— 次数可能不多，但每次都很久，治理方向是「缩短修复时间」；
        2. 高频小停型：次数 >= 中位数 × 2，且单次时长未超标
           —— 每次停得短但很频繁，治理方向是「减少触发次数」；
        3. 双高型：次数与单次时长双超阈值；
        4. 亚健康型：两者都在中位数以上但未达 2 倍。

    先判时长再判次数，因为大故障型的典型特征是「次数低、时长久」，
    如果先判次数就会漏判（例如示例数据里 DPD004 只有 10 次故障，
    但单次 136 分钟，是全厂最该优先处理的设备）。
    """
    rows = fault_rows or []
    if not rows:
        return []

    # 预取 breaking_time 的运行小时，用于判断设备是否在跑
    green_hour: Dict[str, float] = {}
    for b in breaking_rows or []:
        mid = str(b.get("machine_id") or "")
        if mid:
            green_hour[mid] = _f(b.get("green_hour"))

    cnts = [_fault_count(r) for r in rows]
    aves = [_f(r.get("ave_stop_minutes")) for r in rows if _f(r.get("ave_stop_minutes")) > 0]

    med_cnt = median(cnts)
    med_ave = median(aves)

    profiles: List[Dict[str, Any]] = []
    for r in rows:
        mid = str(r.get("machine_id") or "")
        cnt = _fault_count(r)
        ave = _f(r.get("ave_stop_minutes"))
        stop_h = cnt * ave / 60.0

        high_freq = med_cnt > 0 and cnt >= med_cnt * FREQ_MULTIPLE
        long_stop = med_ave > 0 and ave >= med_ave * BIG_FAILURE_MULTIPLE
        # 放宽档：用于把「明显偏高但没到 2 倍」的设备纳入观察，而不是判成正常
        watch_freq = med_cnt > 0 and cnt >= med_cnt * WATCH_MULTIPLE
        watch_stop = med_ave > 0 and ave >= med_ave * WATCH_MULTIPLE

        if long_stop and high_freq:
            ptype = "双高型"
            action = "优先停机专项整改：既降频次也降单次时长，需拆解到具体失效环节"
        elif long_stop:
            ptype = "大故障型"
            action = "缩短单次修复时间：备件前置、故障手册、维修技能培训、关键部件状态监测"
        elif high_freq:
            ptype = "高频小停型"
            action = "降低触发次数：来料/参数/传感器/程序逻辑稳定性排查，减少非计划中断"
        elif watch_freq and watch_stop:
            ptype = "亚健康型"
            action = "次数与单次时长双高，优先降频次；纳入常规巡检并跟踪单次时长趋势"
        elif watch_freq or watch_stop:
            ptype = "亚健康型"
            action = "纳入常规巡检，观察是否向高频或大故障方向恶化"
        else:
            ptype = "正常"
            action = "维持现有维护策略"

        profiles.append({
            "machine_id": mid,
            "profile": ptype,
            "action_direction": action,
            "red_state_num": cnt,
            "ave_stop_minutes": _r(ave),
            "stop_hour_est": _r(stop_h),
            "count_vs_median": _r(cnt / med_cnt) if med_cnt else None,
            "ave_vs_median": _r(ave / med_ave) if med_ave else None,
            "green_hour": _r(green_hour.get(mid)) if mid in green_hour else None,
        })

    # 异常设备排前面，正常设备排后面
    order = {"双高型": 0, "大故障型": 1, "高频小停型": 2, "亚健康型": 3, "正常": 4}
    profiles.sort(key=lambda p: (order.get(p["profile"], 9), -(p["stop_hour_est"] or 0)))
    return profiles


def summarize_profiles(profiles: List[Dict[str, Any]]) -> str:
    """把分型结果压成一句事实描述（按类型归并数量与前 3 台代表设备）。"""
    if not profiles:
        return "无设备分型数据。"

    buckets: Dict[str, List[str]] = {}
    for p in profiles:
        buckets.setdefault(p["profile"], []).append(p["machine_id"])

    parts = []
    for ptype in ("双高型", "大故障型", "高频小停型", "亚健康型"):
        ids = buckets.get(ptype) or []
        if ids:
            shown = "、".join(ids[:3])
            more = f" 等 {len(ids)} 台" if len(ids) > 3 else f" 共 {len(ids)} 台"
            parts.append(f"{ptype}：{shown}{more}")
    normal = buckets.get("正常") or []
    if normal:
        parts.append(f"其余 {len(normal)} 台运行状态正常")
    return "；".join(parts) + "。"


# ============================================================
# 组合报警：共现 → 共同上游
# ============================================================
# 业务约束：同一设备在 window 分钟内接连出现不同 error_code，视为「组合报警」。
# 这类报警往往共享同一个上游根因（气源压力、供电、通讯总线、冷却水等），
# 逐个报警去修是错的，必须先定位共同上游。窗口取 5 分钟是经验值：
#   - 太短（<2min）：抓不到「先低压 -> 后压差」这种传导滞后的因果链；
#   - 太长（>15min）：会把两次独立的偶发故障误并成一次组合事件。
CO_OCCUR_WINDOW_MIN = 5


def group_alarm_events(
    rows: List[Dict[str, Any]],
    window_min: int = CO_OCCUR_WINDOW_MIN,
    max_groups: int = 5,
) -> List[Dict[str, Any]]:
    """把报警序列切成「组合报警」事件组（纯函数，不查库，便于单测）。

    规则：
        1. 按 machine_id 分组，组内按 begin_time 排序；
        2. 相邻两条报警间隔 <= window_min 且 error_code 不同 -> 并入同一组；
        3. 只保留「组内出现 >= 2 种不同 error_code」的组；
        4. 按组内累计停机时长降序取前 max_groups 组。

    返回每个组合事件：
        machine_id, codes, symbol, sequence, event_count,
        span_min, combined_stop_min
    """
    if not rows:
        return []

    # 1) 按设备分组
    by_machine: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_machine.setdefault(str(r.get("machine_id")), []).append(r)

    groups: List[Dict[str, Any]] = []
    for mid, items in by_machine.items():
        items.sort(key=lambda x: str(x.get("begin_time") or ""))
        cur: List[Dict[str, Any]] = []
        for it in items:
            if not cur:
                cur = [it]
                continue
            gap = it.get("gap_prev_min")
            same_code = it.get("error_code") == cur[-1].get("error_code")
            # gap 为 None（时间解析失败）时不合并，避免误并成一个大组
            if gap is not None and float(gap) <= window_min and not same_code:
                cur.append(it)
            else:
                groups.append(_summarize_group(mid, cur))
                cur = [it]
        if cur:
            groups.append(_summarize_group(mid, cur))

    # 2) 过滤：至少 2 种不同编码才算组合
    groups = [g for g in groups if len(g["codes"]) >= 2]

    # 3) 排序 + 截断
    groups.sort(key=lambda g: g["combined_stop_min"], reverse=True)
    return groups[:max_groups]


def _summarize_group(machine_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把一组连续报警压成一条组合事件摘要。"""
    codes: List[str] = []
    for it in items:
        c = str(it.get("error_code") or "")
        if c and c not in codes:
            codes.append(c)

    stop = sum(_f(it.get("duration_min")) for it in items)
    span = sum(_f(it.get("gap_prev_min")) for it in items[1:])

    return {
        "machine_id": machine_id,
        "codes": codes,
        "symbol": " -> ".join(codes),
        "sequence": [
            {
                "error_code": it.get("error_code"),
                "begin_time": it.get("begin_time"),
                "duration_min": it.get("duration_min"),
                "gap_prev_min": it.get("gap_prev_min"),
            }
            for it in items
        ],
        "event_count": len(items),
        "span_min": _r(span),
        "combined_stop_min": _r(stop),
    }


def summarize_alarm_groups(groups: List[Dict[str, Any]]) -> str:
    """把组合报警事件组转成事实描述。"""
    if not groups:
        return "本周期未发现同一设备在短时间窗内连续发生多种报警的组合事件。"

    parts = []
    for g in groups[:3]:
        parts.append(
            f"{g['machine_id']} 出现组合报警 {g.get('symbol') or ' -> '.join(g['codes'])}"
            f"（{g['event_count']} 次报警，跨时 {g['span_min']} 分钟，"
            f"累计停机 {g['combined_stop_min']} 分钟）"
        )
    return "；".join(parts) + "。组合报警通常共享同一个上游根因，应优先排查共同来源，而非逐个报警单独处理。"


# ============================================================
# 月份趋势：区分「偶发峰值」与「持续恶化」
# ============================================================
def build_trend(trend_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算月份趋势的统计特征。

    输出：
        months:           逐月明细（保留原字段）
        direction:        up / down / flat / unknown
        change_percent:   最后一个完整月相对均值的偏离百分比
        peak_month:       停机最高的月份
        latest_month:     最近一个月
        is_abnormal:      最新月是否显著高于历史均值（> 均值 1.3 倍）
    """
    rows = [r for r in (trend_rows or []) if r.get("month")]
    if len(rows) < 2:
        return {
            "months": rows,
            "direction": "unknown",
            "change_percent": None,
            "peak_month": None,
            "latest_month": rows[0]["month"] if rows else None,
            "is_abnormal": False,
            "note": "月份数不足 2 个，无法判断趋势。",
        }

    rows = sorted(rows, key=lambda r: str(r.get("month")))
    hours = [_f(r.get("red_hour")) for r in rows]
    latest = hours[-1]
    history = hours[:-1]
    avg_hist = sum(history) / len(history) if history else 0.0

    change_pct = ((latest - avg_hist) / avg_hist * 100) if avg_hist > 0 else None

    if change_pct is None:
        direction = "unknown"
    elif change_pct >= 15:
        direction = "up"
    elif change_pct <= -15:
        direction = "down"
    else:
        direction = "flat"

    peak = max(rows, key=lambda r: _f(r.get("red_hour")))

    return {
        "months": rows,
        "direction": direction,
        "change_percent": _r(change_pct),
        "history_avg_hour": _r(avg_hist),
        "latest_hour": _r(latest),
        "peak_month": peak.get("month"),
        "latest_month": rows[-1].get("month"),
        "is_abnormal": bool(change_pct is not None and change_pct >= 30),
        "note": "",
    }


def summarize_trend(trend: Dict[str, Any]) -> str:
    """把趋势结果转成事实描述。"""
    rows = trend.get("months") or []
    if len(rows) < 2:
        return "月份趋势数据不足，无法判断本期是偶发还是常态。"

    label = {"up": "上升", "down": "下降", "flat": "基本持平", "unknown": "无法判断"}
    segs = "、".join(
        f"{r['month']} {_f(r.get('red_hour'))} 小时" for r in rows[-4:]
    )
    text = (
        f"近 {len(rows)} 个月停机时长（小时）：{segs}。"
        f"最近月 {trend['latest_month']} 为 {trend['latest_hour']} 小时，"
        f"历史均值 {trend['history_avg_hour']} 小时，"
        f"变化 {trend['change_percent']}%，趋势{label.get(trend['direction'], '无法判断')}。"
        f"峰值出现在 {trend['peak_month']}。"
    )
    if trend.get("is_abnormal"):
        text += "最近月显著高于历史均值，需警惕持续性恶化而非单日偶发波动。"
    elif trend["direction"] == "flat":
        text += "本期水平与历史常态接近，更可能是周期性常态而非突发异常。"
    return text


# ============================================================
# 数据缺口：诚实声明「这份数据答不了什么」
# ============================================================
def build_data_gaps(
    has_alarm_sequence: bool = True,
    has_trend: bool = False,
    has_error_catalog: bool = True,
) -> List[str]:
    """列出当前数据无法支撑的分析维度。

    作用：约束模型不要对这些维度编造结论。
    示例报告里最有价值的两个洞察（组合报警共同上游、月份趋势）
    在只有聚合数据时是做不到的，必须显式声明为缺口。
    """
    gaps: List[str] = []
    if not has_alarm_sequence:
        gaps.append("缺少报警明细序列，无法做组合报警共现分析，无法推断共同上游根因。")
    if not has_trend:
        gaps.append("缺少跨月历史数据，无法判断本期是偶发峰值还是持续恶化。")
    if not has_error_catalog:
        gaps.append("缺少故障类型明细，无法定位具体失效模式，根因只能到设备级。")

    return gaps
