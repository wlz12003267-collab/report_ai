"""SQL 检索工具（pymysql，返回 list[dict]）
"""
import argparse
import json
import sys
from datetime import date, timedelta, datetime
from typing import Any, List, Optional

from langchain_core.tools import tool, BaseTool

from configs.db_connection import get_db_connection_mysql
from configs.settings import get_settings

try:
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover
    DictCursor = None

# ============================================================
# 工具注册
# ============================================================
def setup_tools() -> List[BaseTool]:
    return [
        query_device_metrics,
        query_device_faultsCount,
        query_device_breakingTime,
        query_device_faultsCatalog,
        query_alarm_sequence,
        query_monthly_trend,
    ]


# ============================================================
# 时间计算
# ============================================================
TIME_FMT = "%Y-%m-%d %H:%M:%S"

def compute_time(report_type: str = "daily") -> tuple[str, str]:
    """返回 (date_start, date_end)，格式 YYYY-MM-DD 00:00:00（左闭右开区间）。

    - daily:   昨天 ~ 今天
    - weekly:  上周一 ~ 本周一
    - monthly: 上月 1 号 ~ 本月 1 号
    """
    base = date.today()

    if report_type == "daily":
        start = base - timedelta(days=1)
        end = base
    elif report_type == "weekly":
        # weekday(): 周一=0。回退到「上周一」
        offset_days = base.weekday() + 7
        start = base - timedelta(days=offset_days)
        end = start + timedelta(days=7)
    elif report_type == "monthly":
        this_month_first_day = base.replace(day=1)
        end = this_month_first_day
        last_month_last_day = this_month_first_day - timedelta(days=1)
        start = last_month_last_day.replace(day=1)
    else:
        raise ValueError(f"未知报告类型：{report_type}")

    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    return start_dt.strftime(TIME_FMT), end_dt.strftime(TIME_FMT)

def compute_period_seconds(date_start: str, date_end: str) -> float:
    """统计周期总秒数"""
    delta = datetime.strptime(date_end, TIME_FMT) - datetime.strptime(date_start, TIME_FMT)
    return max(delta.total_seconds(), 0.0)

# ============================================================
# 通用 SQL 执行封装
# ============================================================
def execute_query(sql: str, params: list) -> list[dict]:
    """执行 SQL 并返回 list[dict]。

    - 使用 pymysql 的 DictCursor，结果直接是 dict 列表
    - with conn 自动管理连接和游标
    - 异常时返回空列表，并打印错误（可换成日志）
    """
    conn = get_db_connection_mysql()
    if conn is None:
        print("✘ [execute_query] 数据库连接失败")
        return []

    try:
        with conn:                          # 自动 commit/rollback/close
            with conn.cursor(DictCursor) as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return list(rows)
    except Exception as e:
        print(f"✘ [execute_query] SQL 执行出错: {e}")
        return []

def transfer(factory_code: Optional[str], workshop_id: Optional[str]) -> tuple[str, str]:
    """入参优先，为空才回落到 settings。"""
    s = get_settings()
    return factory_code or s.factory_code, workshop_id or s.workshop_id

# ============================================================
# 基础设备计算表查询
# 所有比率指标（OEE/MTBF/MTTR/MTBI/MTBA）都从这里推导，保证口径唯一
# ============================================================
def query_base_table(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    report_type: str = "daily",
) -> list[dict]:
    """查询上一个自然日/周/月的设备计算表（已按 machine_id 聚合）。

    返回每条设备统计：
        machine_id、green_second、yellow_second、red_second、error_second、
        red_num、yellow_num、error_num、total_second
    """
    factory_code, workshop_id = transfer(factory_code, workshop_id)

    try:
        date_start, date_end = compute_time(report_type)
    except ValueError as e:
        raise ValueError(f"不支持该时间段的查询：{e}")

    # 只按 machine_id 分组；其余列全部聚合，避免把聚合结果放进 GROUP BY
    sql = """
        SELECT machine_id,
               SUM(green_state_second)  AS green_second,
               SUM(yellow_state_second) AS yellow_second,
               SUM(red_state_second)    AS red_second,
               SUM(error_state_second)  AS error_second,
               SUM(red_state_num)       AS red_num,
               SUM(yellow_state_num)    AS yellow_num,
               SUM(error_num)           AS error_num
        FROM device_state_compute
        WHERE factory_code = %s
          AND workshop_id = %s
          AND compute_start_time >= %s
          AND compute_end_time <= %s
        GROUP BY machine_id
        ORDER BY red_num DESC, red_second DESC;
    """
    params = [factory_code, workshop_id, date_start, date_end]
    rows = execute_query(sql, params) or []

    total_second = compute_period_seconds(date_start, date_end)

    for r in rows:
        for k in ("green_second", "yellow_second", "red_second", "error_second"):
            r[k] = float(r.get(k) or 0)
        for k in ("red_num", "yellow_num", "error_num"):
            r[k] = int(r.get(k) or 0)
        r["total_second"] = total_second

    return rows

# ============================================================
# 常用指标计算
# ============================================================
def safe_div(num: float, den: float, scale: float = 1.0,
             digits: int = 4) -> Optional[float]:
    """安全除法：分母为 0 或 None 返回 None。"""
    if not den:
        return None
    return round(num / den * scale, digits)

def compute_metrics(base: dict) -> dict:
    """由基础量推导业务指标。

    口径：
        MTBI = green_second / red_num                     → 平均中断间隔
        MTBA = green_second / (red_num + yellow_num)      → 平均援助间隔
    分母为 0 时对应字段返回 None。
    """
    g = float(base.get("green_second") or 0)
    rn = int(base.get("red_num") or 0)
    yn = int(base.get("yellow_num") or 0)

    return {
        "MTBI_min": safe_div(g, rn, scale=1 / 60),                  # 分钟
        "MTBA_min": safe_div(g, rn + yn, scale=1 / 60),             # 分钟
    }

# ============================================================
# Tool 0: 设备综合指标（OEE / MTBF / MTTR / MTBI / MTBA）
# ============================================================
@tool
def query_device_metrics(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    report_type: str = "daily",
) -> list[dict]:
    """
    查询上一个自然日/周/月的设备综合指标（MTBI / MTBA）。

    Args:
        factory_code: 工厂编码，不传读取系统默认工厂
        workshop_id: 车间ID，不传读取系统默认车间
        report_type: 统计周期，支持枚举：daily(昨日), weekly(上周), monthly(上月)

    返回每条设备统计：
        machine_id:     设备编号
        green_hour:     运行时长(小时)
        red_hour:       故障时长(小时)
        red_num:        故障次数
        yellow_num:     警告/援助次数
        MTBI_min:           平均中断间隔(小时)，green_hour / red_num
        MTBA_min:           平均援助间隔(小时)，green_hour / （red_num + yellow_num）
        （任一指标分母为 0 时该字段为 None）
    """
    base_rows = query_base_table(factory_code, workshop_id, report_type)
    if not base_rows:
        return []

    out: list[dict] = []
    for b in base_rows:
        metrics = compute_metrics(b)
        total_hour = (b["total_second"] / 3600) if b.get("total_second") else None
        out.append({
            "machine_id": b["machine_id"],
            "green_hour": round(b["green_second"] / 3600, 2),
            "red_hour": round(b["red_second"] / 3600, 2),
            "red_num": b["red_num"],
            "yellow_num": b["yellow_num"],
            "total_hour": round(total_hour, 2) if total_hour is not None else None,
            **metrics,
        })
    return out

# ============================================================
# Tool 1: 设备故障次数 / 停机时长
# ============================================================
@tool
def query_device_faultsCount(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    report_type: str = "daily",
) -> list[dict]:
    """
    查询上一个自然日/周/月的设备故障次数与停机时长。

    Args:
        factory_code: 工厂编码，不传则读取系统默认工厂
        workshop_id: 车间ID，不传则读取系统默认车间
        report_type: 统计周期，支持枚举：daily(昨日), weekly(上周), monthly(上月)

    返回每条设备统计：
        machine_id:       设备编号
        red_state_num:    故障次数
        yellow_state_num: 警告次数
        error_num:        通讯故障次数
        ave_stop_minutes: 平均单次故障停机分钟
    """
    factory_code, workshop_id = transfer(factory_code, workshop_id)

    try:
        date_start, date_end = compute_time(report_type)
    except ValueError as e:
        raise ValueError(f"不支持该时间查询: {e}")

    sql = """
        SELECT machine_id,
            SUM(red_state_num)    AS red_num,
            SUM(yellow_state_num) AS yellow_num,
            SUM(error_num)        AS error_num,
            ROUND(
                COALESCE(
                    SUM(red_state_second) / 60 / NULLIF(SUM(red_state_num), 0),
                    0
                ),
                2
            ) AS ave_stop_minutes
        FROM device_state_compute
        WHERE factory_code = %s
            AND workshop_id = %s
            AND compute_start_time >= %s
            AND compute_end_time <= %s
        GROUP BY machine_id
        ORDER BY red_num DESC;
    """
    params = [factory_code, workshop_id, date_start, date_end]
    return execute_query(sql, params)

# ============================================================
# Tool 2: 设备运行 / 停机时长
# ============================================================
@tool
def query_device_breakingTime(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    report_type: str = "daily",
) -> list[dict]:
    """
    查询上一个自然日/周/月的设备运行时长与停机时长。

    Args:
        factory_code: 工厂编码，不传读取系统默认工厂
        workshop_id: 车间ID，不传读取系统默认车间
        report_type: 统计周期，支持枚举：daily(昨日), weekly(上周), monthly(上月)

    返回每条设备统计：
        machine_id:    设备编号
        green_hour:    设备运行时间(小时)
        yellow_hour:   设备警告时间(小时)
        red_hour:      设备故障时间(小时)
        green_percent: 时间稼动率(%)
    """
    factory_code, workshop_id = transfer(factory_code, workshop_id)

    try:
        date_start, date_end = compute_time(report_type)
    except ValueError as e:
        raise ValueError(f"不支持该时间查询: {e}")

    sql = """
        SELECT machine_id,
            ROUND(SUM(green_state_second) / 3600, 2) AS green_hour,
            ROUND(SUM(yellow_state_second) / 3600, 2) AS yellow_hour,
            ROUND(SUM(red_state_second) / 3600, 2) AS red_hour,
            ROUND(
                COALESCE(
                    SUM(green_state_second) / NULLIF(DATEDIFF(%s, %s) * 86400, 0) * 100,
                    0
                ),
                2
            ) AS green_percent
        FROM device_state_compute
        WHERE factory_code = %s
            AND workshop_id = %s
            AND compute_start_time >= %s
            AND compute_end_time <= %s
        GROUP BY machine_id
        ORDER BY red_hour DESC;
    """
    params = [
        date_end, date_start,
        factory_code, workshop_id, date_start, date_end,
    ]
    return execute_query(sql, params)


# ============================================================
# Tool 3: 故障类型统计
# ============================================================
@tool
def query_device_faultsCatalog(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    report_type: str = "daily",
) -> list[dict]:
    """
    查询上一个自然日/周/月的故障类型统计。

    Args:
        factory_code: 工厂编码，不传读取系统默认工厂
        workshop_id: 车间ID，不传读取系统默认车间
        report_type: 统计周期，支持枚举：daily(昨日), weekly(上周), monthly(上月)

    返回每条故障分类统计：
        machine_id:      设备编号
        error_code:      故障类型编码
        error_count:     该故障发生次数
        avg_stop_min:    该类故障单次平均停机分钟
        total_hour:      该类故障累计停机小时

    口径说明：
        - 只统计 state = 4（设备故障）。待机(2)与人为停止(3)是计划性停机，不属于设备问题。
        - 只统计 is_deleted = 0 的记录，排除已删除的脏数据。
        - 时长按「与统计窗口的交集」计算，跨窗口的故障只计窗内部分。
    """
    factory_code, workshop_id = transfer(factory_code, workshop_id)

    try:
        date_start, date_end = compute_time(report_type)
    except ValueError as e:
        raise ValueError(f"不支持该时间查询: {e}")

    sql = f"""
        SELECT
            machine_id,
            error_code,
            COUNT(*) AS error_count,
            ROUND(
                COALESCE(
                    SUM(
                        GREATEST(
                            TIMESTAMPDIFF(
                                SECOND,
                                GREATEST(begin_time, %s),
                                LEAST(COALESCE(end_time, %s), %s)
                            ),
                            0
                        )
                    ), 0
                ) / 60 / NULLIF(COUNT(*), 0),
            2) AS avg_stop_min,
            ROUND(
                COALESCE(
                    SUM(
                        GREATEST(
                            TIMESTAMPDIFF(
                                SECOND,
                                GREATEST(begin_time, %s),
                                LEAST(COALESCE(end_time, %s), %s)
                            ),
                            0
                        )
                    ), 0
                ) / 3600,
            2) AS total_hour
        FROM device_state
        WHERE error_code IS NOT NULL
        AND factory_code = %s
        AND workshop_id = %s
        AND state IN (3,4)
        AND is_deleted = 0
        AND begin_time < %s
        AND (end_time IS NULL OR end_time > %s)
        GROUP BY machine_id, error_code
        -- 剔除在窗口内时长为 0 的分组（如紧贴窗口边界结束的记录），否则会虚增故障次数并拉低平均单次时长
        HAVING SUM(
            GREATEST(
                TIMESTAMPDIFF(
                    SECOND,
                    GREATEST(begin_time, %s),
                    LEAST(COALESCE(end_time, %s), %s)
                ),
                0
            )
        ) > 0
        ORDER BY total_hour DESC;
    """
    params = [
        date_start, date_end, date_end,
        date_start, date_end, date_end,
        factory_code, workshop_id, date_end, date_start,
        date_start, date_end, date_end,
    ]
    return execute_query(sql, params)


# ============================================================
# Tool 4: 报警时间序列（用于组合报警 / 共现分析）
# ============================================================
# 业务约束：同一设备在 CO_OCCUR_WINDOW_MIN 分钟内接连出现不同 error_code，
# 视为「组合报警」。这类报警往往共享同一个上游根因（气源压力、供电、通讯总线等），
# 逐个报警去修是错的，必须定位共同上游。窗口取 5 分钟是经验值：
#   - 太短（<2min）：抓不到「先低压 → 后压差」这种传导滞后的因果链；
#   - 太长（>15min）：会把两次独立的偶发故障误并成一次组合事件。
CO_OCCUR_WINDOW_MIN = 5


@tool
def query_alarm_sequence(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    report_type: str = "daily",
) -> list[dict]:
    """
    查询上一个自然日/周/月的报警明细序列，按设备 + 开始时间升序排列。

    用途：识别「组合报警」。单看聚合次数只能知道哪个报警多，
    看序列才能知道哪些报警是**连续发生**的，从而推断共同上游根因。

    Args:
        factory_code: 工厂编码，不传读取系统默认工厂
        workshop_id: 车间ID，不传读取系统默认车间
        report_type: 统计周期，支持枚举：daily(昨日), weekly(上周), monthly(上月)

    返回每条报警事件：
        machine_id:       设备编号
        error_code:       报警/故障编码
        state:        原始上报状态（2=待机 / 3=人为停止 / 4=设备故障）
        begin_time:       报警开始时间
        end_time:         报警结束时间（未恢复为 None）
        duration_min:     本次持续时间(分钟)
        gap_prev_min:     与同设备上一条报警的间隔(分钟)；首条为 None
    """
    factory_code, workshop_id = transfer(factory_code, workshop_id)

    try:
        date_start, date_end = compute_time(report_type)
    except ValueError as e:
        raise ValueError(f"不支持该时间查询: {e}")

    # 只取必要字段，按设备 + 时间排序；序列顺序是后续共现分析的前提
    sql = f"""
        SELECT machine_id,
               error_code,
               state,
               begin_time,
               end_time,
               ROUND(
                   GREATEST(
                       TIMESTAMPDIFF(
                           SECOND,
                           GREATEST(begin_time, %s),
                           LEAST(COALESCE(end_time, %s), %s)
                       ),
                       0
                   ) / 60,
               2) AS duration_min
        FROM device_state
        WHERE error_code IS NOT NULL
          AND factory_code = %s
          AND workshop_id = %s
          AND state IN (3,4)
          AND is_deleted = 0
          AND begin_time < %s
          AND (end_time IS NULL OR end_time > %s)
        ORDER BY machine_id ASC, begin_time ASC;
    """
    params = [
        date_start, date_end, date_end,
        factory_code, workshop_id, date_end, date_start,
    ]
    rows = execute_query(sql, params) or []

    # 补 gap_prev_min：逐设备计算相邻两条报警的时间差
    prev_machine: Optional[str] = None
    prev_begin: Optional[datetime] = None
    for r in rows:
        mid = r.get("machine_id")
        bt = r.get("begin_time")
        if not isinstance(bt, datetime):
            bt = _parse_dt(bt)

        gap = None
        if mid == prev_machine and prev_begin is not None and bt is not None:
            gap = round((bt - prev_begin).total_seconds() / 60, 2)
        r["gap_prev_min"] = gap

        # 为序列化做准备：时间统一成字符串
        r["begin_time"] = bt.strftime(TIME_FMT) if bt else None
        r["end_time"] = (
            r["end_time"].strftime(TIME_FMT)
            if isinstance(r.get("end_time"), datetime)
            else (str(r["end_time"]) if r.get("end_time") else None)
        )

        prev_machine, prev_begin = mid, bt

    return rows


def _parse_dt(value: Any) -> Optional[datetime]:
    """把 DB 返回的时间值（datetime / date / str）统一成 datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    for fmt in (TIME_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return None


# ============================================================
# Tool 5: 近 N 个月停机趋势
# ============================================================
@tool
def query_monthly_trend(
    factory_code: Optional[str] = None,
    workshop_id: Optional[str] = None,
    months: int = 6,
) -> list[dict]:
    """
    查询最近 N 个完整自然月的停机趋势（不含当月，避免当期不完整导致趋势失真）。

    用途：判断本期数据是「偶发峰值」还是「持续恶化」。
    单看一天的数据无法区分这两种情况，而两者的治理动作完全不同。

    Args:
        factory_code: 工厂编码，不传读取系统默认工厂
        workshop_id: 车间ID，不传读取系统默认车间
        months: 回溯月份数，默认 6，范围 6~12

   返回每个月的汇总（按月份升序）：
        month:           YYYY-MM
        red_num:         该月故障总次数
        red_hour:        该月故障总停机小时
        yellow_num:      该月警告总次数
        error_num:       该月通讯故障总次数
        ave_stop_min:    该月平均单次故障停机分钟
        machine_count:   该月有数据的设备台数
    """
    factory_code, workshop_id = transfer(factory_code, workshop_id)
    months = max(6, min(int(months or 6), 12))

    # 统计窗口：从 months 个月前的 1 号 到 本月 1 号（左闭右开）
    first_this_month = date.today().replace(day=1)
    anchor = first_this_month
    for _ in range(months):
        anchor = (anchor - timedelta(days=1)).replace(day=1)
    date_start = datetime.combine(anchor, datetime.min.time()).strftime(TIME_FMT)
    date_end = datetime.combine(first_this_month, datetime.min.time()).strftime(TIME_FMT)

    # DATE_FORMAT 按月分组：跨月边界的记录按月归属由 compute_start_time 决定
    sql = """
        SELECT DATE_FORMAT(compute_start_time, '%%Y-%%m') AS month,
               SUM(red_state_num)    AS red_num,
               ROUND(SUM(red_state_second) / 3600, 2) AS red_hour,
               SUM(yellow_state_num) AS yellow_num,
               SUM(error_num)        AS error_num,
               ROUND(
                   COALESCE(
                       SUM(red_state_second) / 60 / NULLIF(SUM(red_state_num), 0),
                       0
                   ),
               2) AS ave_stop_min,
               COUNT(DISTINCT machine_id) AS machine_count
        FROM device_state_compute
        WHERE factory_code = %s
          AND workshop_id = %s
          AND compute_start_time >= %s
          AND compute_start_time < %s
        GROUP BY DATE_FORMAT(compute_start_time, '%%Y-%%m')
        ORDER BY month ASC;
    """
    params = [factory_code, workshop_id, date_start, date_end]
    rows = execute_query(sql, params) or []

    for r in rows:
        r["red_num"] = int(r.get("red_num") or 0)
        r["yellow_num"] = int(r.get("yellow_num") or 0)
        r["error_num"] = int(r.get("error_num") or 0)
        r["machine_count"] = int(r.get("machine_count") or 0)
        r["red_hour"] = float(r.get("red_hour") or 0)
        r["ave_stop_min"] = float(r.get("ave_stop_min") or 0)

    return rows


# ============================================================
# 测试入口
# ============================================================
# TOOL_REGISTRY = {
#     "metrics": query_device_metrics,
#     "faults_count": query_device_faultsCount,
#     "breaking_time": query_device_breakingTime,
#     "faults_catalog": query_device_faultsCatalog,
#     "alarm_sequence": query_alarm_sequence,
#     "monthly_trend": query_monthly_trend,
# }


# def _print_result(name: str, rows: Any) -> None:
#     print(f"\n=== {name} ===")
#     if not rows:
#         print("（无数据）")
#         return
#     print(f"共 {len(rows)} 条")
#     print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


# # 各工具的入参构造：不同工具参数签名不同，不能统一塞 report_type
# def _build_params(name: str, args: argparse.Namespace,
#                   factory_code: str, workshop_id: str) -> dict:
#     base = {"factory_code": factory_code, "workshop_id": workshop_id}
#     if name == "monthly_trend":
#         base["months"] = args.months
#     else:
#         base["report_type"] = args.report_type
#     return base


# def main(argv: Optional[List[str]] = None) -> int:
#     """命令行测试入口。

#     用法：
#         python -m agent_core.tools_setup                    # 跑全部工具，daily
#         python -m agent_core.tools_setup -t metrics         # 只跑指标
#         python -m agent_core.tools_setup -r weekly -f C -w 1689182658069925888
#         python -m agent_core.tools_setup -t monthly_trend --months 12
#         python -m agent_core.tools_setup --time             # 只看时间窗口
#     """
#     parser = argparse.ArgumentParser(description="SQL 检索工具测试入口")
#     parser.add_argument("-t", "--tool", default="all",
#                         choices=["all", *TOOL_REGISTRY.keys()],
#                         help="要执行的工具")
#     parser.add_argument("-r", "--report_type", default="daily",
#                         choices=["daily", "weekly", "monthly"])
#     parser.add_argument("-f", "--factory_code", default=None)
#     parser.add_argument("-w", "--workshop_id", default=None)
#     parser.add_argument("-m", "--months", type=int, default=6,
#                         help="monthly_trend 回溯月份数")
#     parser.add_argument("--time", action="store_true", help="只打印时间窗口")
#     args = parser.parse_args(argv)

#     if args.time:
#         start, end = compute_time(args.report_type)
#         hours = compute_period_seconds(start, end) / 3600
#         print(f"report_type = {args.report_type}")
#         print(f"窗口: {start} ~ {end}  ({hours:.0f} 小时)")
#         return 0

#     s = get_settings()
#     factory_code = args.factory_code or s.factory_code
#     workshop_id = args.workshop_id or s.workshop_id

#     print(f"report_type = {args.report_type}")
#     print(f"factory_code = {factory_code}  workshop_id = {workshop_id}")
#     if args.tool in ("all", "monthly_trend"):
#         print(f"trend months = {args.months}")
#     else:
#         start, end = compute_time(args.report_type)
#         print(f"窗口: {start} ~ {end}")

#     names = list(TOOL_REGISTRY.keys()) if args.tool == "all" else [args.tool]
#     failed = 0

#     for name in names:
#         fn = TOOL_REGISTRY[name]
#         try:
#             rows = fn.invoke(_build_params(name, args, factory_code, workshop_id))
#             _print_result(name, rows)
#         except Exception as e:
#             failed += 1
#             print(f"\n=== {name} ===\n执行失败: {type(e).__name__}: {e}")

#     return 1 if failed else 0


# if __name__ == "__main__":
#     sys.exit(main())
