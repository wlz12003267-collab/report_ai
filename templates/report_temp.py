"""报告数据结构。

落盘文件（**只有两个**）：
    1. report.json       -> 报告本体（meta + 各 section 分析 + 预计算分析结论）
    2. report_data.json  -> 各 tool 的原始数据（actual data）

目录规则：
    reports/{report_type}/{factory_code}/{workshop_id}/{YYYY-MM-DD}/

关于 diagnostics 为什么不再落盘
------------------------------------------------------------
旧设计每次生成都会写 diagnostics.json（status / 耗时 / errors / steps）。
这个文件的问题在于：
    1. 价值为 0：一次日报的 status 恒为 success，耗时恒为个位数秒，
       steps 恒为固定三步 —— 每份文件内容 99% 相同，没人会去翻历史诊断；
    2. 污染目录：按日期分目录后，每个日期目录都多一个无用文件，
       数据同步/备份/前端遍历时都要单独跳过它；
    3. 掩盖真问题：真正的失败信息（哪个工具挂了、哪条 SQL 报错）
       混在 errors 数组里，和正常情况下的空数组长得一样，
       不像日志那样能被采集和告警。

现在的做法：
    - 运行期诊断**只走日志**（logging），带 run_id，可被日志系统采集告警；
    - 报告本体里只保留「这次报告可不可信」这一个消费者真正需要的信息，
      即 meta.status / meta.failed_sections，随 report.json 一起走；
    - 失败时不再生成空壳 report.json，而是抛出 ReportBuildError，
      由调用方（调度器）通过异常感知失败 —— 失败不该被写成一个文件。
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "2.0"


# ============================================================
# 报告章节：报告正文的固定骨架（与报告 PDF 的编号一一对应）
# ============================================================
# 每个章节由「代码预计算的结构化结果」+「LLM 针对该章节写的一段分析」组成。
# 注意 sources：同一个章节可能同时消费多个数据源的分析结论
# （例如 Pareto 章节同时用到 breaking_time 与 faults_count），
# 所以这里存的是数据源名，而不是一对一映射。
REPORT_SECTIONS: List[Dict[str, str]] = [
    {"key": "pareto", "num": "01", "title": "停机集中度（Pareto）",
     "sources": ["device_breaking_time", "device_faults_count"]},
    {"key": "alarm", "num": "02", "title": "组合报警与共同上游",
     "sources": ["alarm_sequence"]},
    {"key": "profile", "num": "03", "title": "设备分型与治理方向",
     "sources": ["device_faults_count"]},
    {"key": "actions", "num": "04", "title": "治理行动清单",
     "sources": ["device_faults_count", "device_faults_catalog", "alarm_sequence"]},
    {"key": "trend", "num": "05", "title": "月份趋势",
     "sources": ["monthly_trend"]},
]

SECTION_KEY_BY_NAME = {x["key"]: x for x in REPORT_SECTIONS}


# ============================================================
# 单个数据源的分析结果（一个 tool 对应一条）
# ============================================================
@dataclass
class SectionResult:
    """一个 tool 的「数据 + 大模型分析」结果。"""
    section: str
    title: str                           # 中文标题，如「设备故障次数与停机时长」
    tool_name: str                       # tool 函数名
    status: str = "ok"                   # ok / empty / error
    error: Optional[str] = None          # 出错信息（不落盘，只进日志）
    record_count: int = 0                # 原始记录数
    origin_data: List[Dict[str, Any]] = field(default_factory=list)   # 原始数据
    analysis: str = ""                   # LLM 对该数据的自然语言分析（结论）
    highlights: Dict[str, str] = field(default_factory=dict)  # 关键点
    facts: str = ""                      # 代码预计算的事实块（喂给 LLM 的依据）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# 报告元信息
# ============================================================
@dataclass
class ReportMeta:
    report_type: str                     # daily / weekly / monthly
    date_start: str                      # 统计窗口起（含）
    date_end: str                        # 统计窗口止（不含）
    factory_code: str
    workshop_id: str
    generated_at: str = ""               # 生成时间戳
    duration_seconds: float = 0.0        # 总耗时
    period_hours: float = 0.0            # 统计窗口时长（小时）
    status: str = "success"              # success / partial
    failed_sections: List[str] = field(default_factory=list)   # 失败的小节名
    run_id: str = ""                     # 关联日志，便于按 run_id 反查

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# 报告整体结构（report.json）
# ============================================================
@dataclass
class Report:
    meta: ReportMeta
    schema_version: str = SCHEMA_VERSION
    sections: List[SectionResult] = field(default_factory=list)
    # 以下为代码预计算的分析结果，模型只负责解释，不负责算
    pareto: Dict[str, Any] = field(default_factory=dict)
    profiles: List[Dict[str, Any]] = field(default_factory=list)
    alarm_groups: List[Dict[str, Any]] = field(default_factory=list)
    trend: Dict[str, Any] = field(default_factory=dict)
    data_gaps: List[str] = field(default_factory=list)
    # 按报告章节聚合的 LLM 分析：{章节key: {"analysis": str, "highlights": {...}}}
    chapter_analysis: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """
        报告落盘视图。
        """
        section_list = []
        for s in self.sections:
            section_list.append({
                "section": s.section,
                "title": s.title,
                "tool_name": s.tool_name,
                "status": s.status,
                "record_count": s.record_count,
                "origin_data": s.origin_data[:10],
                "analysis": s.analysis,
                "highlight": s.highlights,
            })
        return {
            "schema_version": self.schema_version,
            "meta": self.meta.to_dict(),
            "pareto": self.pareto,
            "profiles": self.profiles,
            "alarm_groups": self.alarm_groups,
            "trend": self.trend,
            "data_gaps": self.data_gaps,
            "chapter_analysis": self.chapter_analysis,
            "sections": section_list,
        }


# ============================================================
# 构建失败时抛出（替代写一个失败态的 diagnostics.json）
# ============================================================
class ReportBuildError(RuntimeError):
    """报告构建失败。由调度器捕获并告警，不落盘任何报告文件。"""

    def __init__(self, message: str, run_id: str = "",
                 failed_sections: Optional[List[str]] = None):
        super().__init__(message)
        self.run_id = run_id
        self.failed_sections = failed_sections or []
