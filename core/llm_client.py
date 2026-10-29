# -*- coding: utf-8 -*-
"""
core/llm_client.py —— 大模型解析客户端

职责：
  - 从环境变量读取 API 密钥与模型配置（禁止硬编码密钥）
  - 调用 OpenAI 兼容接口（支持 OpenAI / DeepSeek / 智谱 / 通义等）
  - 将“原始文本 / OCR 文本”解析为结构化日程 JSON 数组
  - 容错：网络异常 / 超时 / 返回非 JSON / 密钥缺失 均给出友好结果

环境变量（全部可选）：
  - OPENAI_API_KEY   : API 密钥
  - OPENAI_BASE_URL  : 接口地址（默认 https://api.openai.com/v1）
  - SCHEDULE_MODEL   : 模型名（默认 gpt-4o-mini）
  - LLM_TIMEOUT      : 请求超时秒数（默认 40）
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---- 配置（全部来自环境变量，未设置则用安全默认值） ----
API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY".lower()) or None
BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL: str = os.getenv("SCHEDULE_MODEL", "gpt-4o-mini")
TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "40"))


def is_configured() -> bool:
    """是否已配置大模型密钥。"""
    return bool(API_KEY)


# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是一个日程解析助手。请把用户提供的聊天文本、备忘录或 OCR 识别文字中 "
    "出现的日程信息抽取出来，输出一个严格的 JSON 数组（不要 Markdown 代码块，"
    "不要额外文字）。每个元素对应一条日程，字段如下：\n"
    "title: 标题（字符串，必填）\n"
    "start: 开始时间，格式 'YYYY-MM-DD HH:MM'，无法确定填 null\n"
    "end: 结束时间，格式 'YYYY-MM-DD HH:MM'，无法确定填 null\n"
    "location: 地点，无则空字符串\n"
    "notes: 备注，无则空字符串\n"
    "priority: '高'、'中' 或 '低'\n"
    "repeat: 重复规则的中文描述，如 '每周一'、'每天'，无则空字符串\n"
    "type: 'event'(普通活动) / 'ddl'(截止) / 'recurring'(每周循环课等固定重复)\n"
    "requires_confirm: 布尔值，当时间含模糊表达(如'明天'、'下午'、'大概'、'3点左右')"
    "或缺失时置 true，否则 false\n\n"
    "要求：\n"
    "1. 相对时间（如'明天 14:00'、'周五'）请基于我给出的当前时间推算成绝对时间。\n"
    "2. 跨天活动（如'晚上 22:00 到次日 8:00'）end 要落到下一天的日期。\n"
    "3. 只输出日程相关条目；纯闲聊、非日程内容请忽略。\n"
    "4. 若内容里完全没有任何日程，输出空数组 []。"
)


def _extract_json(text: str) -> List[Dict[str, Any]]:
    """从模型返回文本中尽力提取 JSON 数组（容忍 Markdown 代码块包裹）。"""
    if not text:
        return []
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        # 直接尝试从第一个 [ 到最后一个 ] 截取
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end > start:
            text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试修复：删除结尾逗号
        fixed = re.sub(r",\s*([\]}])", r"\1", text)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    # 只保留字典元素
    return [d for d in data if isinstance(d, dict)]


def parse_with_llm(text: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """调用大模型解析文本为日程 JSON 列表。

    任何异常都向上抛给调用方做统一兜底（避免静默失败掩盖真实错误），
    由 parser 层决定是否降级到规则解析。
    """
    if not API_KEY:
        raise RuntimeError("未配置 OPENAI_API_KEY，无法调用大模型解析。")
    if not text or not text.strip():
        return []

    try:
        from openai import OpenAI
    except ImportError as exc:  # 依赖缺失
        raise RuntimeError("缺少 openai 依赖，请先安装 requirements.txt 中的依赖。") from exc

    now = now or datetime.now()
    user_prompt = (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}（星期{now.strftime('%A')}）。\n"
        f"请解析以下内容中的日程：\n---\n{text}\n---\n"
        "输出 JSON 数组。"
    )

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=TIMEOUT)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    raw = resp.choices[0].message.content or ""
    return _extract_json(raw)


def status_line() -> str:
    """返回当前 LLM 配置状态文案（用于侧边栏展示）。"""
    if is_configured():
        return f"✅ 已配置（模型：{MODEL}）"
    return "⚠️ 未配置密钥，将使用内置规则解析（精度有限）"
