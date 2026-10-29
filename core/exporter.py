# -*- coding: utf-8 -*-
"""
core/exporter.py —— 多格式导出

职责：
  - ICS：生成标准 RFC 5545 日历文件（含重复规则 RRULE 与提醒 VALARM），
    可导入 iPhone / 安卓 / Windows / Outlook
  - CSV：通用表格格式
  - Markdown：便于查看/分享的日程表格
  - ICS 文本做 RFC 转义，避免逗号/分号/换行破坏格式
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Any, Dict, List

from .schedule_utils import parse_dt


# ---------------------------------------------------------------------------
# ICS 辅助
# ---------------------------------------------------------------------------
def _ics_escape(text: str) -> str:
    """RFC 5545 文本转义：逗号、分号、反斜杠、换行。"""
    if not text:
        return ""
    return (text.replace("\\", "\\\\")
                .replace(",", "\\,")
                .replace(";", "\\;")
                .replace("\n", "\\n"))


def _fold(text: str) -> str:
    """ICS 规定每行不超过 75 字节，超长需折行（续行以空格开头）。"""
    # 先按 utf-8 字节计算；简单实现按字符折，足够大多数场景
    max_len = 70
    if len(text) <= max_len:
        return text
    parts = []
    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]
        parts.append(chunk if i == 0 else " " + chunk)
    return "\r\n".join(parts)


def _dt_ics(value: Any) -> str:
    """datetime -> ICS 时间字符串（本地时间、无时区后缀，便于各平台按本地导入）。"""
    dt = parse_dt(value)
    if dt is None:
        return ""
    return dt.strftime("%Y%m%dT%H%M%S")


# 重复规则：中文“每周一”等 -> RRULE
_WEEKDAY_MAP = {
    "一": "MO", "二": "TU", "三": "WE", "四": "TH",
    "五": "FR", "六": "SA", "日": "SU", "天": "SU",
}


def repeat_to_rrule(repeat: str) -> str:
    """把中文重复规则描述转成 ICS RRULE；无法识别返回空串。"""
    rep = (repeat or "").strip()
    if not rep:
        return ""
    if "每" in rep and "周" in rep and ("一" in rep or "二" in rep or "三" in rep or
                                        "四" in rep or "五" in rep or "六" in rep or
                                        "日" in rep or "天" in rep or "1" in rep or
                                        "2" in rep or "3" in rep or "4" in rep or
                                        "5" in rep or "6" in rep or "7" in rep):
        # 每周 X
        day = None
        for ch in rep:
            if ch in _WEEKDAY_MAP:
                day = _WEEKDAY_MAP[ch]
                break
        if day is None:
            for d in "一二三四五六日天":
                if d in rep:
                    day = _WEEKDAY_MAP[d]
                    break
        if day:
            return f"RRULE:FREQ=WEEKLY;BYDAY={day}"
    if "每" in rep and ("天" in rep or "日" in rep):
        return "RRULE:FREQ=DAILY"
    if "每" in rep and "月" in rep:
        return "RRULE:FREQ=MONTHLY"
    return ""


def reminder_to_trigger(reminder: str) -> str:
    """提醒选项 -> VALARM TRIGGER 的负偏移（返回空串表示不提醒）。"""
    mapping = {
        "提前15分钟": "-PT15M",
        "提前1小时": "-PT1H",
        "提前1天": "-P1D",
        "不提醒": "",
    }
    return mapping.get(reminder, "")


# ---------------------------------------------------------------------------
# ICS 导出
# ---------------------------------------------------------------------------
def build_ics(events: List[Dict[str, Any]], reminder: str = "不提醒",
              calendar_name: str = "AI日程整理") -> str:
    """生成完整 ICS 日历文本。"""
    trigger = reminder_to_trigger(reminder)
    lines: List[str] = []
    lines.append("BEGIN:VCALENDAR")
    lines.append("VERSION:2.0")
    lines.append("PRODID:-//AI Schedule Import//CN//EN")
    lines.append("CALSCALE:GREGORIAN")
    lines.append("METHOD:PUBLISH")
    lines.append(f"X-WR-CALNAME:{_ics_escape(calendar_name)}")

    dtstamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")

    for ev in events:
        start = _dt_ics(ev.get("start"))
        end = _dt_ics(ev.get("end"))
        if not start:
            continue  # 无开始时间的日程无法进入 ICS，跳过

        uid = str(uuid.uuid4())
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{dtstamp}")
        lines.append(f"DTSTART:{start}")
        if end:
            lines.append(f"DTEND:{end}")
        lines.append(f"SUMMARY:{_ics_escape(ev.get('title', ''))}")
        if ev.get("location"):
            lines.append(f"LOCATION:{_ics_escape(ev.get('location', ''))}")
        if ev.get("notes"):
            lines.append(f"DESCRIPTION:{_ics_escape(ev.get('notes', ''))}")
        # 优先级映射：高=1，中=5，低=9（RFC 5545）
        pri = {"高": 1, "中": 5, "低": 9}.get(ev.get("priority"), 5)
        lines.append(f"PRIORITY:{pri}")
        # 重复规则
        rrule = repeat_to_rrule(ev.get("repeat", ""))
        if rrule:
            lines.append(rrule)
        # 提醒
        if trigger:
            lines.append("BEGIN:VALARM")
            lines.append("ACTION:DISPLAY")
            lines.append(f"DESCRIPTION:{_ics_escape(ev.get('title', ''))} 提醒")
            lines.append(f"TRIGGER:{trigger}")
            lines.append("END:VALARM")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in lines)


def ics_bytes(events: List[Dict[str, Any]], reminder: str = "不提醒") -> bytes:
    """返回可直接下载的 ICS 字节流。"""
    return build_ics(events, reminder).encode("utf-8")


# ---------------------------------------------------------------------------
# CSV 导出
# ---------------------------------------------------------------------------
CSV_HEADERS = ["标题", "开始时间", "结束时间", "地点", "备注", "优先级", "重复规则", "类型"]


def build_csv(events: List[Dict[str, Any]]) -> str:
    """生成 CSV 文本（UTF-8 with BOM，便于 Excel 直接打开不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for ev in events:
        writer.writerow([
            ev.get("title", ""),
            ev.get("start", ""),
            ev.get("end", ""),
            ev.get("location", ""),
            ev.get("notes", ""),
            ev.get("priority", "中"),
            ev.get("repeat", ""),
            ev.get("type", "event"),
        ])
    data = buf.getvalue()
    # 加 BOM
    return "\ufeff" + data


def csv_bytes(events: List[Dict[str, Any]]) -> bytes:
    return build_csv(events).encode("utf-8-sig")


# ---------------------------------------------------------------------------
# Markdown 导出
# ---------------------------------------------------------------------------
def build_markdown(events: List[Dict[str, Any]]) -> str:
    """生成 Markdown 日程表。"""
    events = sorted(events, key=lambda e: (e.get("start", ""), e.get("title", "")))
    md = ["# AI 日程整理导出", "", f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
          "| 标题 | 开始 | 结束 | 地点 | 优先级 | 重复 | 类型 |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for ev in events:
        md.append(
            f"| {ev.get('title','')} | {ev.get('start','')} | {ev.get('end','')} "
            f"| {ev.get('location','')} | {ev.get('priority','中')} "
            f"| {ev.get('repeat','')} | {ev.get('type','event')} |"
        )
    md.append("")
    md.append("> 说明：本表格由 AI 日程整理工具生成，请以实际安排为准。")
    return "\n".join(md)


def markdown_bytes(events: List[Dict[str, Any]]) -> bytes:
    return build_markdown(events).encode("utf-8")
