"""report -- 组装与持久化。

职责：
    1. 汇总多个数据源的分析结果，组装成日报 JSON；
    2. 负责 data_path 检查与目录创建；
    3. 负责文件持久化（**两个文件**）：
         reports/{report_type}/{factory_code}/{workshop_id}/{YYYY-MM-DD}/
             ├── report.json        报告本体（含预计算分析结论）
             └── report_data.json   各 tool 的原始数据

为什么不再写 diagnostics.json
------------------------------------------------------------
一次日报生成的执行诊断（status / 耗时 / errors / steps）没有存储价值：
    - status 正常恒为 success，耗时恒为个位数秒，steps 恒为固定几步，
      每份文件内容几乎一样，没人会去翻历史；
    - 它污染日期目录，数据同步、备份、前端遍历都要单独跳过它；
    - 真正的失败信息混在 errors 数组里，不像日志那样能被采集和告警。

现在改成：
    - 诊断信息只走 logging（带 run_id），交给日志系统采集、检索、告警；
    - report.json 的 meta 里保留 status / failed_sections / run_id，
      消费者要判断「这份报告可不可信」时看这里就够了；
    - 全部小节失败时抛 ReportBuildError，不写任何报告文件 ——
      失败不该被写成一份看起来正常的 JSON。
"""
import json
import logging
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core.report_agent import ReportAgent, _json_default
from agent_core.tools_setup import compute_period_seconds, compute_time
from agent_core import analyzer
from configs.settings import get_settings
from templates.report_temp import Report, ReportBuildError, ReportMeta, SectionResult

logger = logging.getLogger(__name__)


# ============================================================
# 路径生成与检测：data_path 检查（确认目录可用、可写）
# ============================================================
def mk_report_dir(
    factory_code: str,
    workshop_id: str,
    report_date: str,
    report_type: str,
    create: bool = True,
) -> Path:
    """构造并（可选）创建报告目录。

    路径规则: {report_root}/report_type/{factory_code}/{workshop_id}/{YYYY-MM-DD}/
    report_root 取自 settings，避免相对路径在不同工作目录下漂移。
    """
    settings = get_settings()
    reports_dir = (
        Path(settings.report_root)
        / report_type
        / factory_code
        / workshop_id
        / report_date
    )
    if create:
        reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir


def check_path(path: Path) -> Dict[str, Any]:
    """检查目录是否存在、是否可写。返回结构同时用于日志与返回给调用方。"""
    info: Dict[str, Any] = {
        "path": str(path),
        "exist": path.exists(),
        "is_dir": path.is_dir(),
        "writable": False,
        "check_at": datetime.now().isoformat(timespec="seconds"),
    }
    # 只有目录存在，才测试写入权限
    if info["exist"] and info["is_dir"]:
        probe = path / ".write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            info["writable"] = True
        except Exception as e:
            info["error"] = str(e)
            logger.error("报告目录不可写: %s (%s)", path, e)
    return info


# ============================================================
# 文件保存
# ============================================================
def write_json(path: Path, payload: Any) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
        logger.info("写入文件成功: %s", path)
    except Exception as e:
        logger.error("写入 JSON 失败 %s: %s", path, e)
        raise


def save_report(report_dir: Path, report: Report) -> Dict[str, str]:
    """写出两个 JSON 文件，返回路径字典。

    只写 report.json 与 report_data.json。
    诊断信息不进文件，改由调用方通过返回值 / 日志获取。
    """
    report_path = report_dir / "report.json"
    data_path = report_dir / "report_data.json"

    # 1) report.json -- 报告本体（含预计算结论）
    write_json(report_path, report.to_dict())

    # 2) report_data.json -- 实际原始数据（检索到的全部数据）
    write_json(
        data_path,
        {
            "meta": report.meta.to_dict(),
            "record_count": {s.section: s.record_count for s in report.sections},
            "data": {s.section: s.origin_data for s in report.sections},
        },
    )

    return {"report": str(report_path), "report_data": str(data_path)}


# ============================================================
# 分析结论汇总（从 agent 缓存里取，避免重复计算）
# ============================================================
def collect_analytics(
    sections: List[SectionResult],
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """汇总 code 预计算的分析结论，供写入 report.json。

    优先用 agent 跑分析时缓存下来的结果（同一批数据只算一次）；
    缓存缺失时（例如离线测试直接传 sections）就地重算。

    失败不阻断报告生成：分析增强块坏了，正文仍应能产出。
    """
    cache = cache or {}
    raw: Dict[str, List[Dict[str, Any]]] = cache.get("raw") or {
        s.section: s.origin_data for s in sections
    }

    fault_rows = raw.get("device_faults_count") or []
    breaking_rows = raw.get("device_breaking_time") or []
    alarm_rows = raw.get("alarm_sequence") or []
    trend_rows = raw.get("monthly_trend") or []

    analytics: Dict[str, Any] = {
        "pareto": cache.get("pareto") or {},
        "profiles": cache.get("profiles") or [],
        "alarm_groups": cache.get("alarm_groups") or [],
        "trend": cache.get("trend") or {},
        "data_gaps": [],
    }

    if not analytics["pareto"] and (breaking_rows or fault_rows):
        analytics["pareto"] = analyzer.build_pareto(breaking_rows, fault_rows)
    if not analytics["profiles"] and fault_rows:
        analytics["profiles"] = analyzer.build_profiles(fault_rows, breaking_rows)
    if not analytics["alarm_groups"] and alarm_rows:
        analytics["alarm_groups"] = analyzer.group_alarm_events(alarm_rows)
    if not analytics["trend"] and trend_rows:
        analytics["trend"] = analyzer.build_trend(trend_rows)

    analytics["data_gaps"] = analyzer.build_data_gaps(
        has_alarm_sequence=bool(alarm_rows),
        has_trend=len(trend_rows) >= 2,
        has_error_catalog=bool(raw.get("device_faults_catalog")),
    )
    return analytics


def build_section_map(analytics: Dict[str, Any],
                      sections: List[SectionResult]) -> Dict[str, Any]:
    """把预计算结果按「报告章节」重新组织，喂给章节级 LLM 分析。

    注意这里刻意复用了渲染层的 derive_actions —— 行动清单既要被模型
    解释，也要被画成表格，两边必须是同一份，否则会出现
    「正文说有 3 条 P0，表格里只有 2 条」这种前后不一致。
    """
    from report_pdf.renderer import derive_actions

    view = {
        "pareto": analytics.get("pareto") or {},
        "profiles": analytics.get("profiles") or [],
        "alarm_groups": analytics.get("alarm_groups") or [],
        "trend": analytics.get("trend") or {},
    }
    view["actions"] = {"actions": derive_actions(view)}

    return {
        "pareto": view["pareto"],
        "alarm": view["alarm_groups"],
        "profile": view["profiles"],
        "actions": view["actions"],
        "trend": view["trend"],
    }


# ============================================================
# 主入口：组装 + 检查 + 落盘
# ============================================================
def build_and_persist_report(
    sections: List[SectionResult],
    report_type: str,
    factory_code: str,
    workshop_id: str,
    start_at: datetime,
    analytics_cache: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    agent: Optional[ReportAgent] = None,
) -> Dict[str, Any]:
    """组装报告并落盘。

    Args:
        sections: 各数据源分析结果
        report_type: daily / weekly / monthly
        factory_code / workshop_id: 车间范围
        start_at: 流程开始时间，用于计算总耗时
        analytics_cache: ReportAgent.last_cache，复用预计算结果
        run_id: 本次运行的追踪 ID，默认自动生成
        agent: 传入 ReportAgent 则额外做章节级 LLM 分析（每节一段结论）；
               不传则报告只有结构化结果与图表，正文各节不放分析段落

    Returns:
        {
            "status": "success" | "partial",
            "run_id": ...,
            "report_dir": ...,
            "paths": {"report": ..., "report_data": ...},
            "section_status": {...},
            "path_check": {...},
            "diagnostics": {...},     # 仅返回给调用方，不落盘
        }

    Raises:
        ReportBuildError: 全部小节失败（没有可用内容），此时不写任何文件。
    """
    finish_at = datetime.now()
    run_id = run_id or uuid.uuid4().hex[:12]
    date_start, date_end = compute_time(report_type)
    period_hours = round(compute_period_seconds(date_start, date_end) / 3600, 2)

    # ---- 1) 状态判定：全部失败时直接抛错，不产生空壳报告 ----
    failed = [s.section for s in sections if s.status == "error"]
    if sections and len(failed) == len(sections):
        msg = f"全部 {len(failed)} 个数据源分析失败，不生成报告: {failed}"
        logger.error("[run_id=%s] %s", run_id, msg)
        raise ReportBuildError(msg, run_id=run_id, failed_sections=failed)

    status = "partial" if failed else "success"

    # ---- 2) meta ----
    meta = ReportMeta(
        report_type=report_type,
        date_start=date_start,
        date_end=date_end,
        factory_code=factory_code,
        workshop_id=workshop_id,
        generated_at=finish_at.isoformat(timespec="seconds"),
        duration_seconds=round((finish_at - start_at).total_seconds(), 2),
        period_hours=period_hours,
        status=status,
        failed_sections=failed,
        run_id=run_id,
    )

    # ---- 3) 分析结论汇总 ----
    analytics = collect_analytics(sections, analytics_cache)

    report = Report(
        meta=meta,
        sections=sections,
        pareto=analytics["pareto"],
        profiles=analytics["profiles"],
        alarm_groups=analytics["alarm_groups"],
        trend=analytics["trend"],
        data_gaps=analytics["data_gaps"],
    )

    # ---- 3.5) 章节级分析：报告正文每一节各一段 LLM 结论 ----
    # 放在 path 检查之前：分析失败不应阻断落盘（正文还有图表和表格），
    # 但必须在组装 Report 之前完成，否则 chapter_analysis 是空的。
    chapter_analysis: Dict[str, Any] = {}
    if agent is not None:
        try:
            section_map = build_section_map(analytics, sections)
            chapter_analysis = agent.analyze_chapters(
                results=sections,
                section_map=section_map,
                report_type=report_type,
                factory_code=factory_code,
                workshop_id=workshop_id,
            )
            logger.info("[run_id=%s] 章节分析完成: %s", run_id, list(chapter_analysis))
        except Exception as e:
            logger.error("[run_id=%s] 章节分析失败，正文将不含分析段落: %s", run_id, e)
    report.chapter_analysis = chapter_analysis

    # ---- 4) path 检查 ----
    report_date = date_start.split(" ")[0]
    report_dir = mk_report_dir(factory_code, workshop_id, report_date,
                               report_type, create=True)
    path_info = check_path(report_dir)
    if not path_info["writable"]:
        msg = f"报告目录不可写: {path_info}"
        logger.error("[run_id=%s] %s", run_id, msg)
        raise ReportBuildError(msg, run_id=run_id, failed_sections=failed)

    # ---- 5) 保存文件 ----
    paths = save_report(report_dir=report_dir, report=report)

    # ---- 6) 诊断信息：只返回 + 记日志，不落盘 ----
    diagnostics = {
        "run_id": run_id,
        "status": status,
        "started_at": start_at.isoformat(timespec="seconds"),
        "finished_at": finish_at.isoformat(timespec="seconds"),
        "duration_seconds": meta.duration_seconds,
        "period_hours": period_hours,
        "report_dir": str(report_dir),
        "failed_sections": failed,
        "errors": [s.error for s in sections if s.status == "error" and s.error],
        "section_status": {s.section: s.status for s in sections},
        "record_count": {s.section: s.record_count for s in sections},
    }

    log_fn = logger.info if status == "success" else logger.warning
    log_fn(
        "[run_id=%s] 报告生成完成 status=%s 耗时=%.2fs 小节=%s 目录=%s",
        run_id, status, meta.duration_seconds, diagnostics["section_status"], report_dir,
    )
    if failed:
        logger.warning("[run_id=%s] 失败小节 %s 详情: %s",
                       run_id, failed, diagnostics["errors"])

    return {
        "status": status,
        "run_id": run_id,
        "report_dir": str(report_dir),
        "paths": paths,
        "section_status": diagnostics["section_status"],
        "path_check": path_info,
        "diagnostics": diagnostics,
    }


# ============================================================
# 一站式入口：取数 + 分析 + 组装 + 落盘
# ============================================================
def run_report(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    report_type: Optional[str] = None,
) -> Dict[str, Any]:
    """前端只需提供三个参数，其余全部自动完成。"""
    s = get_settings()
    factory_code = factory_code or s.factory_code
    workshop_id = workshop_id or s.workshop_id
    report_type = report_type or s.report_type

    run_id = uuid.uuid4().hex[:12]
    start_at = datetime.now()
    logger.info("[run_id=%s] 开始生成报告: %s/%s %s",
                run_id, factory_code, workshop_id, report_type)

    agent = ReportAgent()
    sections = agent.analyze_all(
        report_type=report_type,
        factory_code=factory_code,
        workshop_id=workshop_id,
    )
    return build_and_persist_report(
        sections=sections,
        report_type=report_type,
        factory_code=factory_code,
        workshop_id=workshop_id,
        start_at=start_at,
        analytics_cache=agent.last_cache,
        run_id=run_id,
        agent=agent,
    )


# ============================================================
# 调试入口
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    results = run_report(
        factory_code="C",
        workshop_id="1689182658069925888",
        report_type="daily",
    )
    print("==== 报告生成结果 ====")
    print(f"整体状态: {results['status']}")
    print(f"run_id: {results['run_id']}")
    print(f"报告目录: {results['report_dir']}")
    print(f"小节状态: {results['section_status']}")
    print(f"目录可写: {results['path_check']['writable']}")
    print("文件列表：")
    for name, file_path in results["paths"].items():
        print(f"  {name}: {file_path}")
