# -*- coding: utf-8 -*-
"""
core/parser.py —— 日程解析编排层

职责：
  - 统一入口：给定一段原始文本（聊天/备忘录/OCR结果），产出结构化日程列表
  - 优先使用大模型解析（精度高）；LLM 不可用/失败时自动降级到内置规则解析
  - 对 LLM 返回结果做清洗、字段校验、时间标准化，并打上「待核对」标记
  - 合并 LLM 与规则解析结果，避免遗漏
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List

from . import llm_client
from .schedule_utils import make_event, normalize_datetime, parse_dt


# ---------------------------------------------------------------------------
# 内置规则解析（无大模型密钥时的兜底方案）
# ---------------------------------------------------------------------------
def _rule_parse(text: str, now: datetime) -> List[Dict[str, Any]]:
    """基于正则的轻量解析：识别常见日期时间表达，构造简单日程。

    精度有限，主要用于演示与容错兜底；所有产出统一打 requires_confirm=True，
    提醒用户人工核对。
    """
    events: List[Dict[str, Any]] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # 识别“标题 + 时间”的行，如“高数课 周三 08:00-09:40 三教101”
    date_map = {
        "今天": 0, "明天": 1, "后天": 2,
        "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6,
        "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3, "星期五": 4, "星期六": 5, "星期日": 6,
        "周1": 0, "周2": 1, "周3": 2, "周4": 3, "周5": 4, "周6": 5, "周7": 6,
    }
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    # 时间区间：08:00-09:40 或 8:00~9:40
    time_range_re = re.compile(
        r"(\d{1,2}[:：]\d{2})\s*[-~至]\s*(\d{1,2}[:：]\d{2})"
    )
    single_time_re = re.compile(r"(\d{1,2}[:：]\d{2})")
    date_re = re.compile(r"(今天|明天|后天|周[一二三四五六日1-7]|星期[一二三四五六日])")

    for line in lines:
        m_date = date_re.search(line)
        m_range = time_range_re.search(line)
        m_time = single_time_re.search(line) if not m_range else None
        if not (m_date or m_range or m_time):
            continue  # 该行没有明显时间，跳过

        # 计算基准日期
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        target_day = base
        if m_date:
            token = m_date.group(1)
            if token in ("今天", "明天", "后天"):
                target_day = base + timedelta(days=date_map[token])
            else:
                wd = date_map[token]
                delta = (wd - now.weekday()) % 7
                target_day = base + timedelta(days=delta)

        # 时间
        start_str = end_str = None
        if m_range:
            start_str = m_range.group(1).replace("：", ":")
            end_str = m_range.group(2).replace("：", ":")
        elif m_time:
            start_str = m_time.group(1).replace("：", ":")

        start = f"{target_day.strftime('%Y-%m-%d')} {start_str or '09:00'}"
        end = f"{target_day.strftime('%Y-%m-%d')} {end_str or start_str or '10:00'}"

        # 标题 = 行首去掉时间后的剩余文字（取前面非时间部分）
        title = re.sub(time_range_re, " ", line)
        title = re.sub(single_time_re, " ", title)
        title = re.sub(date_re, " ", title)
        title = re.sub(r"\s+", " ", title).strip(" -~–—")
        if not title:
            title = "（未命名日程）"

        events.append({
            "title": title,
            "start": start,
            "end": end,
            "location": "",
            "notes": "由内置规则解析，请人工核对",
            "priority": "中",
            "repeat": "",
            "type": "event",
            "requires_confirm": True,
        })
    return events


# ---------------------------------------------------------------------------
# LLM 结果清洗：字段校验 + 时间标准化 + 待核对标记
# ---------------------------------------------------------------------------
def _clean_llm_events(raw_list: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in raw_list:
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        start_raw = item.get("start")
        end_raw = item.get("end")
        start = normalize_datetime(start_raw)
        end = normalize_datetime(end_raw)

        # 待核对标记：模型标记 / 时间缺失 / 时间仍带“今天”“明天”等字样
        requires_confirm = bool(item.get("requires_confirm", False))
        if not start or not end:
            requires_confirm = True
        if isinstance(start_raw, str) and re.search(r"(今天|明天|后天|周[一二三四五六日]|下午|晚上|早上|左右|大概|约)", start_raw):
            requires_confirm = True

        # 跨天兜底：end 早于 start 且都在同一天，视为次日凌晨（如 22:00 -> 次日 8:00）
        s_dt, e_dt = parse_dt(start), parse_dt(end)
        if s_dt and e_dt and e_dt <= s_dt and e_dt.date() == s_dt.date():
            e_dt = e_dt + timedelta(days=1)
            end = e_dt.strftime("%Y-%m-%d %H:%M")
            requires_confirm = True

        ev = make_event(
            title=title,
            start=start,
            end=end,
            location=str(item.get("location", "")).strip(),
            notes=str(item.get("notes", "")).strip(),
            priority=str(item.get("priority", "中")),
            repeat=str(item.get("repeat", "")).strip(),
            type=str(item.get("type", "event")),
            requires_confirm=requires_confirm,
            source=source,
        )
        cleaned.append(ev)
    return cleaned


# ---------------------------------------------------------------------------
# 对外统一入口
# ---------------------------------------------------------------------------
def parse_schedule(text: str, source: str = "text") -> List[Dict[str, Any]]:
    """解析一段原始文本为结构化日程列表（已清洗、已标准化）。

    返回事件字典列表；如文本无任何日程则返回 []。
    本函数内部已做 LLM 失败降级，正常路径不会抛异常。
    """
    text = (text or "").strip()
    if not text:
        return []

    now = datetime.now()
    events: List[Dict[str, Any]] = []

    # 1) 优先大模型
    if llm_client.is_configured():
        try:
            raw = llm_client.parse_with_llm(text, now)
            events = _clean_llm_events(raw, source)
        except Exception:
            # LLM 失败（超时/网络/密钥失效等）→ 降级规则解析，不中断流程
            events = _rule_parse(text, now)
    else:
        events = _rule_parse(text, now)

    # 2) 规则兜底结果补充（仅当 LLM 结果为空或很少时追加，避免重复）
    if len(events) < 3:
        rule_events = _rule_parse(text, now)
        existing_keys = {(
            re.sub(r"\s+", "", ev.get("title", "")),
            ev.get("start", ""),
        ) for ev in events}
        for r_ev in rule_events:
            key = (re.sub(r"\s+", "", r_ev.get("title", "")), r_ev.get("start", ""))
            if key not in existing_keys:
                events.append(r_ev)
                existing_keys.add(key)

    # 3) 统一归一化：补齐 id 及全部字段，保证后续冲突检测 / 展示不越界
    events = [make_event(**ev, source=ev.get("source") or source) for ev in events]
    return events
