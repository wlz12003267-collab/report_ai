"""主程序入口--编排

流程：
    1. 解析参数（factory_code / workshop_id 若无输入，默认取settings，report_type 驱动时间计算）
    2. analysis_agent 逐 tool 独立分析（tool -> prompt -> llm -> section）
    3. report_agent 组装日报 JSON + data_path 检查 + 持久化

用法：
    python -m main.py --report_type weekly --factory_code C --workshop_id 1689182658069925888
"""
import argparse
import time
from datetime import datetime
from typing import List, Optional

from configs.settings import get_settings
from agent_core.tools_setup import compute_time
from agent_core.report_agent import ReportAgent
from agent_core.report_save import build_and_persist_report

def run(
    report_type: str,
    factory_code: str,
    workshop_id: str,       
):
    print("Hello from fh-v3! 执行一次日报生成。")
    settings = get_settings()

    # 1) 参数兜底
    factory_code = factory_code or settings.factory_code
    workshop_id = workshop_id or settings.workshop_id

    start_at = datetime.now()
    t = time.perf_counter()  # 记录系统时间
    steps: list[dict] = []

    # 2）调用llm进行数据分析
    t_step = time.perf_counter()
    agent = ReportAgent()
    sections = agent.analyze_all(report_type, factory_code, workshop_id)
    steps.append({
        "name": "analysis",
        "status": "done",
        "duration_seconds": round(time.perf_counter() - t_step, 2),
        "section_count": len(sections),
    })

    # 3）生成report并进行持久化
    t_step = time.perf_counter()
    result = build_and_persist_report(sections = sections, report_type = report_type, factory_code = factory_code,
                                      workshop_id = workshop_id, start_at = start_at, steps = steps,)
    steps.append({
        "name": "persist",
        "status": "done",
        "duration_seconds": round(time.perf_counter() - t_step, 2),
    })
    print(f"[run_agent] 完成: status={result['status']} dir={result['report_dir']}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="设备日报生成 Agent")
    parser.add_argument("--report_type", default = "daily",
                        choices=["daily", "weekly", "monthly"])
    parser.add_argument("--factory_code", default=None)
    parser.add_argument("--workshop_id", default=None)
    args = parser.parse_args()

    run(
        report_type=args.report_type,
        factory_code=args.factory_code,
        workshop_id=args.workshop_id,
    )

# factory_code= A, C, D, G
# workshop_id= 1689182658057342976, 1689182658069925888, __unassigned__, 17014920261

