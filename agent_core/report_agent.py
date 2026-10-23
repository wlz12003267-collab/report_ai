"""Agent 构建：组装 LLM + 工具 + 输出。

数据流（严格串行，每个 section 独立完成，互不干扰）：
    tool.invoke()  ->  origin_data
                        |
    analyzer  ->  facts（代码预计算的事实）
                        |
    build_user_prompt(facts, origin_data)  ->  prompt
                        |
    LLM            ->  text  ->  parser_llm_output  ->  {conclusion, highlights}

为什么要插「facts」这一层：
    让模型只做它擅长的事（解释、归因、给建议），
    把排序、占比、倍数这类确定性计算交给代码。
    实测这能把「输出过于简单、没有根据」的问题解决掉大半 ——
    模型不需要自己去翻 JSON 找最大值，直接引用现成事实即可。
"""
import json
import logging
import traceback
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.llm_setup import get_llm
from agent_core.tools_setup import compute_period_seconds, compute_time, setup_tools
from agent_core import analyzer
from templates.prompt_temp import ROW_LIMIT, SYSTEM_PROMPT, build_user_prompt
from templates.prompt_temp import (
    CHAPTER_SYSTEM_PROMPT,
    build_chapter_facts,
    build_chapter_prompt,
)
from templates.report_temp import REPORT_SECTIONS, SectionResult

logger = logging.getLogger(__name__)

# ============================================================
# 数据源注册表：把 tool 与它的展示元信息绑定
# ============================================================
# 顺序有讲究，不是随意的：
#   breaking_time 必须排在 faults_count 之前。
#   原因：Pareto（停机时长排序）和分型都要用「真实停机小时」， 而只有 breaking_time 直接提供 red_hour；faults_count 只能给出「次数 × 平均单次时长」的估算值。
#   若顺序反了，会拿估算值当结论， 而估算值在 ave_stop_minutes 缺失的设备上会直接变成 0，排序会失真。
#   alarm_sequence / monthly_trend 放最后：前者只用自身序列， 后者查的是最近 6 个月，与报告周期无关。
ANALYSIS_REGISTRY: List[Dict[str, Any]] = [
    {
        "section": "device_breaking_time",
        "title": "设备运行与停机时长",
        "tool_name": "query_device_breakingTime",
    },
    {
        "section": "device_faults_count",
        "title": "设备故障次数与停机时长",
        "tool_name": "query_device_faultsCount",
    },
    {
        "section": "device_faults_catalog",
        "title": "故障类型统计",
        "tool_name": "query_device_faultsCatalog",
    },
    {
        # 组合报警：依赖 alarm_sequence 的明细序列，先做共现聚类再让模型归因
        "section": "alarm_sequence",
        "title": "组合报警与共现分析",
        "tool_name": "query_alarm_sequence",
    },
    {
        # 月份趋势：放最后，因为它查的是最近 N 个月，与报告周期不同
        "section": "monthly_trend",
        "title": "月份停机趋势",
        "tool_name": "query_monthly_trend",
    },
]


# ============================================================
# JSON 序列化（处理 Decimal / date 等非原生类型）
# ============================================================
def _json_default(o: Any):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"Object of type {type(o)} is not JSON serializable")


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)


# ============================================================
# LLM 输出解析
# ============================================================
def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    """尝试按 JSON 解析；不是 JSON 或结构不对时返回 None。

    容忍三种常见脏输出：
        - 前后带 ```json ... ``` 代码块
        - 前后夹着解释性文字（截取第一个 { 到最后一个 }）
        - highlights 是 list 而不是 dict
    """
    raw = text.strip()

    # 剥代码块围栏
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
        raw = raw.strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    # 截取最外层大括号
    if not (raw.startswith("{") and raw.endswith("}")):
        l, r = raw.find("{"), raw.rfind("}")
        if l == -1 or r <= l:
            return None
        raw = raw[l:r + 1]

    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    conclusion = obj.get("conclusion")
    if conclusion is None:
        conclusion = obj.get("结论") or ""
    conclusion = str(conclusion).strip()

    hl = obj.get("highlights")
    if hl is None:
        hl = obj.get("关键点")
    out: Dict[str, str] = {}
    if isinstance(hl, dict):
        for i, v in enumerate(hl.values(), start=1):
            s = str(v).strip()
            if s:
                out[f"highlight_{i}"] = s
    elif isinstance(hl, list):
        for i, v in enumerate(hl, start=1):
            s = str(v).strip()
            if s:
                out[f"highlight_{i}"] = s

    if not conclusion and not out:
        return None
    return {"conclusion": conclusion, "highlights": out}


def parser_llm_output(text: str) -> Dict[str, Any]:
    """把 LLM 输出解析成结构化 dict，同时兼容两种格式。

    返回：
        {
            "conclusion": "…",
            "highlights": {"highlight_1": "…", "highlight_2": "…", ...}
        }

    支持两种输出形态：
        1) JSON（章节级 prompt 要求的格式）：
           {"conclusion": "...", "highlights": {"highlight_1": "..."}}
        2) 分节文本（数据源级 prompt 要求的格式）：
           【结论】…
           【关键点】
           1. …
           2. …

    容错清洗：
        - 去掉行首的 - / * / • / ·  等列表符
        - 去掉行首的 1. 2) 3、 等编号
        - 去掉 **加粗** 和残留的【】标记
        - 丢弃空行和只有标点的行
    """
    text = (text or "").strip()
    if not text:
        return {"conclusion": "", "highlights": {}}

    parsed = _try_parse_json(text)
    if parsed is not None:
        return parsed

    conclusion = ""
    highlights: List[str] = []

    # 结论段
    if "【结论】" in text:
        after = text.split("【结论】", 1)[1]
        conclusion = after.split("【关键点】", 1)[0].strip()
    elif "【关键点】" not in text:
        # 完全没按格式输出时，整段当结论
        conclusion = text

    # 关键点段
    if "【关键点】" in text:
        after = text.split("【关键点】", 1)[1]
        for raw in after.splitlines():
            line = raw.strip()
            if not line:
                continue
            line = line.replace("**", "").replace("【", "").replace("】", "")
            line = line.lstrip("-*•·").strip()
            # 去掉 "1." / "1)" / "1、" / "1：" 这类编号
            head = line.split(" ", 1)[0]
            if head and head[0].isdigit():
                for sep in (".", ")", "、", ":", "："):
                    if head.endswith(sep):
                        line = line[len(head):].strip() or line
                        break
            if line and any(ch.isalnum() for ch in line):
                highlights.append(line)

    # 去重保序（小模型常重复同一条）
    seen = set()
    uniq: List[str] = []
    for h in highlights:
        key = h[:24]
        if key not in seen:
            seen.add(key)
            uniq.append(h)

    highlights_json: Dict[str, str] = {
        f"highlight_{i}": h for i, h in enumerate(uniq, start=1)
    }
    return {"conclusion": conclusion, "highlights": highlights_json}


# ============================================================
# 调用 LLM（延迟加载，便于离线测试时替换）
# ============================================================
def default_llm_invoker(system_prompt: str, user_prompt: str) -> str:
    """默认 LLM 调用：SystemMessage + HumanMessage -> 文本。"""
    llm = get_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    resp = llm.invoke(messages)
    if isinstance(resp, AIMessage):
        return resp.content
    return str(resp)


# ============================================================
# 事实构造：每个 section 喂给 LLM 的「已算好」的依据
# ============================================================
def build_facts(section: str, origin_data: List[Dict[str, Any]],
                cache: Dict[str, Any]) -> str:
    """按 section 生成事实块。

    cache 用于跨 section 复用已算好的结果（例如 Pareto 在
    faults_count 与 breaking_time 都要用，但只该算一次）。
    """
    if not origin_data:
        return ""

    if section == "device_faults_count":
        breaking = cache.get("breaking_rows") or []
        pareto = cache.get("pareto")
        if pareto is None:
            pareto = analyzer.build_pareto(breaking, origin_data)
            cache["pareto"] = pareto
        profiles = analyzer.build_profiles(origin_data, breaking)
        cache["profiles"] = profiles

        blocks = [analyzer.summarize_pareto(pareto), analyzer.summarize_profiles(profiles)]

        # 单次时长最长的前 3 台，单独点名（这是分型的直接依据）
        ranked = sorted(
            [r for r in origin_data if analyzer.to_float(r.get("ave_stop_minutes")) > 0],
            key=lambda r: analyzer.to_float(r.get("ave_stop_minutes")),
            reverse=True,
        )[:3]
        if ranked:
            segs = [
                f"{r.get('machine_id')} 单次平均 {analyzer.to_float(r.get('ave_stop_minutes'))} 分钟"
                f"（{analyzer.fault_count(r)} 次）"
                for r in ranked
            ]
            blocks.append("单次停机时长最高的设备：" + "；".join(segs) + "。")

        # 通讯故障单独提示：它往往是网络/网关问题
        err_rows = [r for r in origin_data if analyzer.to_int(r.get("error_num")) > 0]
        if err_rows:
            total_err = sum(analyzer.to_int(r.get("error_num")) for r in err_rows)
            blocks.append(
                f"通讯故障共 {total_err} 次，涉及 {len(err_rows)} 台设备，"
                "该类异常通常源于网络/采集网关，而非设备本体。"
            )
        return "\n".join(blocks)

    if section == "device_breaking_time":
        pareto = cache.get("pareto")
        if pareto is None:
            pareto = analyzer.build_pareto(origin_data, cache.get("fault_rows") or [])
            cache["pareto"] = pareto

        total_green = sum(analyzer.to_float(r.get("green_hour")) for r in origin_data)
        total_red = sum(analyzer.to_float(r.get("red_hour")) for r in origin_data)
        total_yellow = sum(analyzer.to_float(r.get("yellow_hour")) for r in origin_data)
        loss = total_red + total_yellow

        blocks = [analyzer.summarize_pareto(pareto)]
        blocks.append(
            f"全车间合计运行 {round(total_green, 2)} 小时、故障 {round(total_red, 2)} 小时、"
            f"警告 {round(total_yellow, 2)} 小时；损失时间中故障占 "
            f"{round(total_red / loss * 100, 1) if loss else 0}%、"
            f"警告占 {round(total_yellow / loss * 100, 1) if loss else 0}%。"
        )

        percents = [analyzer.to_float(r.get("green_percent")) for r in origin_data
                    if analyzer.to_float(r.get("green_percent")) > 0]
        if percents:
            med = analyzer.median(percents)
            below = sum(1 for p in percents if p < med)
            blocks.append(
                f"时间稼动率中位数 {round(med, 2)}%，"
                f"最高 {round(max(percents), 2)}%，最低 {round(min(percents), 2)}%，"
                f"低于中位数的设备有 {below} 台。"
            )
        return "\n".join(blocks)

    if section == "device_faults_catalog":
        total_hour = sum(analyzer.to_float(r.get("total_hour")) for r in origin_data)
        total_cnt = sum(analyzer.to_int(r.get("error_count")) for r in origin_data)

        by_hour = sorted(origin_data, key=lambda r: analyzer.to_float(r.get("total_hour")),
                         reverse=True)
        top3_hour = sum(analyzer.to_float(r.get("total_hour")) for r in by_hour[:3])
        share = (top3_hour / total_hour * 100) if total_hour else 0

        # 按次数排，和按时长排往往是两批不同的故障
        by_cnt = sorted(origin_data, key=lambda r: analyzer.to_int(r.get("error_count")),
                        reverse=True)

        blocks = [
            f"共 {len(origin_data)} 条（设备×故障类型）记录，累计停机 "
            f"{round(total_hour, 2)} 小时，故障发生合计 {total_cnt} 次。",
            f"停机时长 Top3 占全部停机的 {round(share, 1)}%；"
            "时长第一为 "
            + (
                f"{by_hour[0].get('machine_id')} / {by_hour[0].get('error_code')}"
                f"（{analyzer.to_float(by_hour[0].get('total_hour'))} 小时）"
                if by_hour else "无"
            )
            + "。",
        ]
        if by_cnt:
            blocks.append(
                "发生次数最多为 "
                f"{by_cnt[0].get('machine_id')} / {by_cnt[0].get('error_code')}"
                f"（{analyzer.to_int(by_cnt[0].get('error_count'))} 次，"
                f"单次平均 {analyzer.to_float(by_cnt[0].get('avg_stop_min'))} 分钟）。"
            )

        # 单次时长异常长的故障类型：这类是「一次停很久」的主要原因
        long_types = sorted(
            [r for r in origin_data if analyzer.to_float(r.get("avg_stop_min")) > 0],
            key=lambda r: analyzer.to_float(r.get("avg_stop_min")),
            reverse=True,
        )[:3]
        if long_types:
            segs = [
                f"{r.get('error_code')} 单次 {analyzer.to_float(r.get('avg_stop_min'))} 分钟"
                for r in long_types
            ]
            blocks.append("单次处理时长最长的故障类型：" + "；".join(segs) + "。")
        return "\n".join(blocks)

    if section == "alarm_sequence":
        groups = analyzer.group_alarm_events(origin_data)
        cache["alarm_groups"] = groups
        blocks = [analyzer.summarize_alarm_groups(groups)]
        machines = {str(r.get("machine_id")) for r in origin_data}
        blocks.append(
            f"本周期共 {len(origin_data)} 条报警明细，涉及 {len(machines)} 台设备。"
        )
        return "\n".join(blocks)

    if section == "monthly_trend":
        trend = analyzer.build_trend(origin_data)
        cache["trend"] = trend
        return analyzer.summarize_trend(trend)

    return ""


# ============================================================
# tool调用 -> llm分析 -> report_agent构建
# ============================================================
class ReportAgent:
    """日报生成 Agent：逐个数据源取数 + 单独 LLM 分析。"""

    def __init__(self, llm_invoker: Optional[Callable[[str, str], str]] = None) -> None:
        self.registry = ANALYSIS_REGISTRY
        self.llm_invoker = llm_invoker or default_llm_invoker
        self.tool_map = {t.name: t for t in setup_tools()}
        self._last_cache: Dict[str, Any] = {}

    # 数据获取
    def fetch_data(
        self,
        section: str,
        tool_name: str,
        report_type: str,
        factory_code: str,
        workshop_id: str,
    ) -> List[Dict[str, Any]]:
        tool = self.tool_map.get(tool_name)
        if tool is None:
            raise ValueError(f"未注册的工具: {tool_name}")

        params: Dict[str, Any] = {
            "factory_code": factory_code,
            "workshop_id": workshop_id,
        }
        # monthly_trend 不接 report_type，接了会报参数错误
        if section == "monthly_trend":
            params["months"] = 6
        else:
            params["report_type"] = report_type

        data = tool.invoke(params)
        return data or []

    # 单数据源分析
    def analyze_single_section(
        self,
        section: Dict[str, Any],
        report_type: str,
        factory_code: str,
        workshop_id: str,
        cache: Optional[Dict[str, Any]] = None,
    ) -> SectionResult:
        """对单个数据源：调用 tool 取数 -> 构造 prompt -> LLM 分析 -> SectionResult。"""

        part = section["section"]
        title = section["title"]
        tool_name = section["tool_name"]
        cache = cache if cache is not None else {}

        result = SectionResult(section=part, title=title, tool_name=tool_name)

        # ---- 1) 调用该工具取数 ----
        try:
            origin_data = self.fetch_data(
                section=part,
                tool_name=tool_name,
                report_type=report_type,
                factory_code=factory_code,
                workshop_id=workshop_id,
            )
        except Exception as e:
            logger.error("[%s] 工具 %s 调用失败: %s", part, tool_name, e)
            result.status = "error"
            result.error = f"工具 {tool_name} 调用失败: {e}"
            result.origin_data = []
            return result

        result.origin_data = origin_data
        result.record_count = len(origin_data)

        # 缓存原始行，供后续 section 的交叉分析复用
        cache.setdefault("raw", {})[part] = origin_data
        if part == "device_breaking_time":
            cache["breaking_rows"] = origin_data
        elif part == "device_faults_count":
            cache["fault_rows"] = origin_data

        # ---- 2) 空数据：跳过 LLM ----
        if not origin_data:
            result.status = "empty"
            result.analysis = "本周期无相关记录。"
            result.highlights = {"highlight_1": "本周期该数据源无记录"}
            return result

        # ---- 3) 代码预计算事实 ----
        try:
            facts = build_facts(part, origin_data, cache)
        except Exception as e:
            logger.warning("[%s] 事实计算失败，降级为纯数据分析: %s", part, e)
            facts = ""
        result.facts = facts

        # ---- 4) 构造 prompt ----
        date_start, date_end = compute_time(report_type)
        period_hours = compute_period_seconds(date_start, date_end) / 3600

        # 只送前 N 条：全量 JSON 会稀释重点，且小模型上下文有限
        visible = origin_data[:ROW_LIMIT]
        user_prompt = build_user_prompt(
            section=part,
            title=title,
            report_type=report_type,
            date_start=date_start,
            date_end=date_end,
            factory_code=factory_code,
            workshop_id=workshop_id,
            origin_data=dumps(visible),
            facts=facts,
            period_hours=period_hours,
            row_limit=len(visible),
        )

        # ---- 5) 单独调用 LLM ----
        try:
            llm_text = self.llm_invoker(SYSTEM_PROMPT, user_prompt)
            parsed = parser_llm_output(llm_text)
            result.analysis = parsed["conclusion"]
            result.highlights = parsed["highlights"]
            if parsed["conclusion"] or parsed["highlights"]:
                result.status = "ok"
            else:
                result.status = "error"
                result.error = f"LLM 输出为空或无法解析: {llm_text[:200]}"
        except Exception as e:
            logger.error("[%s] LLM 分析失败: %s", part, e)
            result.status = "error"
            result.error = f"LLM 分析失败: {e}\n{traceback.format_exc(limit=2)}"

        return result

    # 遍历所有数据源
    def analyze_all(
        self,
        report_type: str,
        factory_code: str,
        workshop_id: str,
    ) -> List[SectionResult]:
        """自动调用注册表中所有工具，逐个独立分析。

        返回的 SectionResult 列表顺序与 ANALYSIS_REGISTRY 一致。
        跨 section 的预计算结果放在 cache 里，通过 last_cache 暴露给report_pipeline，避免重复计算 Pareto。
        """
        cache: Dict[str, Any] = {}
        results: List[SectionResult] = []
        for r in self.registry:
            logger.info("[Agent] 分析数据源: %s", r["section"])
            res = self.analyze_single_section(
                section=r,
                report_type=report_type,
                factory_code=factory_code,
                workshop_id=workshop_id,
                cache=cache,
            )
            results.append(res)

        self._last_cache = cache
        return results

    # 章节级分析：把「预计算结构 + 相关数据源结论」交给 LLM，写该章节的分析
    def analyze_chapters(
        self,
        results: List[SectionResult],
        section_map: Dict[str, Any],
        report_type: str,
        factory_code: str,
        workshop_id: str,
    ) -> Dict[str, Dict[str, Any]]:
        """为报告正文的每个章节单独生成一段 LLM 分析。

        与 analyze_all 的分工：
            analyze_all     —— 一个 data source 一次分析，回答「这张表说明了什么」
            analyze_chapters—— 一个报告章节一次分析，回答「这一节该给管理层讲什么」

        两者共用同一批原始数据，但 prompt 的视角和约束不同：
        章节分析拿到的是该章节的**结构化结果**（Pareto 排序、分型清单、报警链、趋势序列），并要求模型只解释这些结果，不重复罗列数字。

        section_map 的键是报告章节 key（pareto/alarm/profile/actions/trend），值是该章节的结构化数据，由 report_pipeline 计算好后传入 ——
        这样渲染层与生成层用的是同一份数字，不会各算各的。
        """
        date_start, date_end = compute_time(report_type)
        period_hours = compute_period_seconds(date_start, date_end) / 3600

        # 数据源结论按名字索引，章节可以直接引用上游分析
        by_name = {r.section: r for r in results}

        out: Dict[str, Dict[str, Any]] = {}
        for chapter in REPORT_SECTIONS:
            key = chapter["key"]
            payload = section_map.get(key)
            if payload is None:
                continue

            facts = build_chapter_facts(key, payload, by_name)
            if not facts:
                continue

            user_prompt = build_chapter_prompt(
                chapter=key,
                title=chapter["title"],
                report_type=report_type,
                date_start=date_start,
                date_end=date_end,
                factory_code=factory_code,
                workshop_id=workshop_id,
                facts=facts,
                period_hours=period_hours,
            )

            try:
                llm_text = self.llm_invoker(CHAPTER_SYSTEM_PROMPT, user_prompt)
                parsed = parser_llm_output(llm_text)
                out[key] = {
                    "title": chapter["title"],
                    "analysis": parsed["conclusion"],
                    "highlights": parsed["highlights"],
                    "status": "ok" if (parsed["conclusion"] or parsed["highlights"]) else "error",
                }
            except Exception as e:
                logger.error("[chapter:%s] LLM 分析失败: %s", key, e)
                out[key] = {
                    "title": chapter["title"],
                    "analysis": "",
                    "highlights": {},
                    "status": "error",
                    "error": str(e),
                }

        return out

    @property
    def last_cache(self) -> Dict[str, Any]:
        """最近一次 analyze_all 的预计算缓存（pareto / profiles / trend / alarm_groups）。"""
        return self._last_cache


# ============================================================
# 调试入口
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    agent = ReportAgent()
    results = agent.analyze_all(
        report_type="daily",
        factory_code="C",
        workshop_id="1689182658069925888",
    )

    for r in results:
        print(f"\n=== {r.section} ({r.status}) 共 {r.record_count} 条 ===")
        print("事实:", r.facts[:300])
        print("结论:", r.analysis)
        print("关键点:", r.highlights)
