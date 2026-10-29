# -*- coding: utf-8 -*-
"""
core/schedule_utils.py —— 日程核心逻辑

职责（纯函数、无副作用、便于单测）：
  1. 时间字符串标准化：把 LLM / 规则解析出的各种时间写法统一成 "YYYY-MM-DD HH:MM"
  2. 事件去重：标题 + 起止时间高度重合的日程只保留一条
  3. 过期过滤：结束时间早于当前时间的日程剔除
  4. 冲突检测：同一日期内起止时间重叠的日程标记为“冲突”

本模块不依赖 streamlit，可在无 GUI 环境下直接运行与测试。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# 时间解析与标准化
# ---------------------------------------------------------------------------
def parse_dt(value: Any) -> Optional[datetime]:
    """把多种格式的时间值解析为 datetime，失败返回 None。

    支持：ISO 字符串、'YYYY-MM-DD HH:MM'、'YYYY-MM-DD HH:MM:SS'、
    datetime 对象、以及'2026-10-30'（无时间，视为当天 00:00）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # 尝试常见格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
                    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%dT%H%M%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def fmt_dt(dt: Optional[datetime]) -> str:
    """datetime -> 统一展示字符串 'YYYY-MM-DD HH:MM'，空则返回空串。"""
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def normalize_datetime(value: Any) -> str:
    """标准化为 'YYYY-MM-DD HH:MM'，无法解析返回空串。"""
    dt = parse_dt(value)
    return fmt_dt(dt) if dt else ""


# ---------------------------------------------------------------------------
# 事件结构常量（字段全集，供各模块引用）
# ---------------------------------------------------------------------------
EVENT_FIELDS = [
    "id", "title", "start", "end", "location", "notes", "priority",
    "repeat", "type", "requires_confirm", "source", "added_at",
]


def make_event(**kwargs) -> Dict[str, Any]:
    """构造一个字段齐全的事件字典（缺失字段用安全默认值补齐）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    base = {
        "id": kwargs.get("id", ""),
        "title": (kwargs.get("title") or "").strip(),
        "start": normalize_datetime(kwargs.get("start")),
        "end": normalize_datetime(kwargs.get("end")),
        "location": (kwargs.get("location") or "").strip(),
        "notes": (kwargs.get("notes") or "").strip(),
        "priority": (kwargs.get("priority") or "中"),
        "repeat": (kwargs.get("repeat") or "").strip(),
        "type": (kwargs.get("type") or "event"),
        "requires_confirm": bool(kwargs.get("requires_confirm", False)),
        "source": (kwargs.get("source") or "text"),
        "added_at": kwargs.get("added_at") or now,
    }
    # 归一化优先级，避免脏值
    if base["priority"] not in ("高", "中", "低"):
        base["priority"] = "中"
    if base["type"] not in ("event", "ddl", "recurring"):
        base["type"] = "event"
    if not base["id"]:
        base["id"] = _new_id()
    return base


def _new_id() -> str:
    """生成稳定的短 ID（基于时间戳 + 随机，无需额外依赖）。"""
    import uuid
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def _dedup_key(ev: Dict[str, Any]) -> str:
    """去重键：标题归一化 + 起止时间 + 地点。时间为空则用空串参与。"""
    title = re.sub(r"\s+", "", ev.get("title", "")).lower()
    return f"{title}|{ev.get('start', '')}|{ev.get('end', '')}|{ev.get('location', '')}"


def dedupe(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按去重键过滤，保留第一次出现的条目；返回去重后的列表。"""
    seen: Set[str] = set()
    result: List[Dict[str, Any]] = []
    for ev in events:
        key = _dedup_key(ev)
        if key in seen:
            continue
        seen.add(key)
        result.append(ev)
    return result


# ---------------------------------------------------------------------------
# 过期过滤
# ---------------------------------------------------------------------------
def filter_expired(events: List[Dict[str, Any]], now: Optional[datetime] = None,
                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按时间过滤过期日程。

    - 有明确 end 且 end < now：过期
    - 无 end 但有 start 且 start < now（且非每日重复）：过期
    - 重复日程（repeat 非空）默认不过期（视为持续性的每周课程等）
    - 时间缺失的日程不判过期（需人工核对）
    返回 (保留列表, 过期列表)。
    """
    now = now or datetime.now()
    keep: List[Dict[str, Any]] = []
    expired: List[Dict[str, Any]] = []
    for ev in events:
        if ev.get("repeat"):
            keep.append(ev)
            continue
        end = parse_dt(ev.get("end"))
        start = parse_dt(ev.get("start"))
        if end and end < now:
            expired.append(ev)
        elif end is None and start and start < now:
            expired.append(ev)
        else:
            keep.append(ev)
    return keep, expired


# ---------------------------------------------------------------------------
# 冲突检测
# ---------------------------------------------------------------------------
def _event_range(ev: Dict[str, Any]):
    """返回事件的 (start_dt, end_dt)，缺时间返回 (None, None)。

    规则：有 start 无 end -> 视为 start 起 1 小时的临时占用；都无 -> 不参与冲突。
    """
    start = parse_dt(ev.get("start"))
    end = parse_dt(ev.get("end"))
    if start is None:
        return None, None
    if end is None or end <= start:
        end = start + timedelta(hours=1)
    return start, end


def detect_conflicts(events: List[Dict[str, Any]]) -> Set[str]:
    """检测同一自然日内起止时间重叠的事件，返回冲突事件 id 集合。

    只比较非空时间段；跨天事件以各自 [start,end] 区间直接比较。
    注：重复日程暂只按其首条（即模板）与其它事件比较，作为提示。
    """
    conflict_ids: Set[str] = set()
    n = len(events)
    for i in range(n):
        s1, e1 = _event_range(events[i])
        if s1 is None:
            continue
        for j in range(i + 1, n):
            s2, e2 = _event_range(events[j])
            if s2 is None:
                continue
            # 区间重叠判断：s1 < e2 and s2 < e1
            if s1 < e2 and s2 < e1:
                conflict_ids.add(events[i]["id"])
                conflict_ids.add(events[j]["id"])
    return conflict_ids


# ---------------------------------------------------------------------------
# 事件状态标注（用于展示“正常 / 待核对 / 时间冲突”）
# ---------------------------------------------------------------------------
def event_status(ev: Dict[str, Any], conflict_ids: Set[str]) -> str:
    """返回单条事件的状态文案。"""
    if ev["id"] in conflict_ids:
        return "⚠️ 时间冲突"
    if ev.get("requires_confirm"):
        return "🟡 待核对"
    return "🟢 正常"


# ---------------------------------------------------------------------------
# 示例日程（供“一键加载示例”演示按钮使用）
# ---------------------------------------------------------------------------
def sample_events() -> List[Dict[str, Any]]:
    """内置一组贴近学生生活的示例日程，方便比赛现场演示。"""
    base = datetime.now()
    # 本周一 0 点作为基准，便于生成“未来”的示例时间
    monday = (base - timedelta(days=base.weekday())).replace(hour=0, minute=0, second=0)
    d1 = (monday + timedelta(days=1)).strftime("%Y-%m-%d")   # 周二
    d2 = (monday + timedelta(days=2)).strftime("%Y-%m-%d")   # 周三
    d3 = (monday + timedelta(days=3)).strftime("%Y-%m-%d")   # 周四
    d4 = (monday + timedelta(days=4)).strftime("%Y-%m-%d")   # 周五

    return [
        make_event(
            title="高等数学（每周一）", start=f"{d1} 08:00", end=f"{d1} 09:40",
            location="三教 101", notes="每周循环课", priority="高",
            repeat="每周一", type="recurring", source="示例",
        ),
        make_event(
            title="数据结构小组讨论", start=f"{d1} 14:00", end=f"{d1} 16:00",
            location="图书馆研讨室 A", notes="讨论项目进度", priority="中",
            type="event", source="示例",
        ),
        make_event(
            title="课程设计中期答辩", start=f"{d2} 10:00", end=f"{d2} 11:30",
            location="学院楼 205", notes="准备 PPT 和演示", priority="高",
            type="event", source="示例",
        ),
        make_event(
            title="《操作系统》大作业 DDL", start=f"{d3} 23:59", end=f"{d3} 23:59",
            location="线上提交", notes="上传到教务系统", priority="高",
            type="ddl", source="示例",
        ),
        make_event(
            title="社团例会", start=f"{d4} 19:00", end=f"{d4} 20:30",
            location="学生活动中心 302", notes="", priority="低",
            type="event", source="示例",
        ),
        make_event(
            title="体测补测（待确认时间）", start=f"{d4} 15:00", end=f"{d4} 16:00",
            location="田径场", notes="时间模糊，需人工核对", priority="中",
            requires_confirm=True, type="event", source="示例",
        ),
    ]
