# -*- coding: utf-8 -*-
"""
app.py —— AI 智能日程收纳日历导入（Streamlit 主程序）

面向大学生参赛的完整演示应用：
  - 双输入（粘贴文本 / 上传截图），支持多次追加不覆盖
  - AI 解析（LLM + OCR + 规则兜底），模糊时间打⚠️待核对
  - 去重、过期过滤、冲突检测标红
  - 网页内手动编辑 / 删除
  - 一键加载示例日程
  - 导出 ICS / CSV / Markdown（含提醒、重复规则），支持多平台导入
  - 全面容错与免责声明，全内存处理、无数据库、不落盘用户数据

运行方式：
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import os
import traceback
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

# 后端逻辑模块
from core import exporter, llm_client, ocr_engine, parser
from core.schedule_utils import (
    dedupe,
    detect_conflicts,
    event_status,
    filter_expired,
    make_event,
    sample_events,
)

# ---------------------------------------------------------------------------
# 页面基础配置（必须最先执行）
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="AI 智能日程收纳日历",
    page_icon="🗓️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 现代化简洁风格
st.markdown(
    """
    <style>
    .stApp { max-width: 1200px; margin: 0 auto; }
    div[data-testid="stSidebar"] { background: #f7f9fc; }
    .hero { padding: 1.2rem 0 0.2rem 0; }
    .hero h1 { font-size: 2rem; font-weight: 700; }
    .badge {
        display: inline-block; background: #e8f0fe; color: #1967d2;
        border-radius: 999px; padding: 4px 14px; font-size: 0.85rem;
        margin-bottom: 8px;
    }
    .footer-note {
        color: #80868b; font-size: 0.85rem; margin-top: 3rem;
        border-top: 1px solid #eee; padding-top: 1rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# 会话状态初始化（全部日程仅存于内存 session_state，不落盘）
# ---------------------------------------------------------------------------
def _init_state() -> None:
    if "events" not in st.session_state:
        st.session_state.events = []            # List[Dict]，当前全部日程
    if "source_text" not in st.session_state:
        st.session_state.source_text = ""       # 累计粘贴文本
    if "reminder" not in st.session_state:
        st.session_state.reminder = "提前15分钟"
    if "sample_loaded" not in st.session_state:
        st.session_state.sample_loaded = False
    if "expired_events" not in st.session_state:
        st.session_state.expired_events = []    # 被过滤的过期日程
    if "parse_note" not in st.session_state:
        st.session_state.parse_note = ""


_init_state()


# ===========================================================================
# 业务函数定义（必须在下方 UI 代码调用之前定义，避免 NameError）
# ===========================================================================

def _status_color(val: str) -> str:
    """状态单元格颜色：冲突标红、待核对标黄、正常标绿。"""
    if "冲突" in val:
        return "color: #c5221f; font-weight: 600;"
    if "待核对" in val:
        return "color: #b06000; font-weight: 600;"
    return "color: #1e7e34;"


def events_to_dataframe(events: List[Dict[str, Any]]) -> pd.DataFrame:
    """把事件列表转成带状态列的展示用 DataFrame。"""
    conflict_ids = detect_conflicts(events)
    rows = []
    for ev in events:
        rows.append({
            "ID": ev["id"],
            "标题": ev["title"],
            "开始": ev["start"] or "—",
            "结束": ev["end"] or "—",
            "地点": ev["location"] or "",
            "优先级": ev["priority"],
            "重复": ev["repeat"] or "",
            "类型": ev["type"],
            "状态": event_status(ev, conflict_ids),
            "备注": ev["notes"] or "",
            "来源": ev["source"],
        })
    return pd.DataFrame(rows)


def _style_status(df: pd.DataFrame):
    """返回带状态颜色的 Styler。"""
    return df.style.map(_status_color, subset=["状态"])


def _run_parse(text: str, source: str = "text") -> None:
    """解析一段文本并合并进日程列表（含去重 / 过期过滤 / 冲突重算）。"""
    try:
        with st.spinner("🤖 AI 正在解析日程…"):
            parsed = parser.parse_schedule(text, source=source)
        if not parsed:
            st.warning("未能从输入内容中识别出日程，请确认包含明确的时间/事件信息。")
            return
        _merge_and_report(parsed)
    except Exception as exc:  # 兜底：绝不因解析失败让页面崩溃
        st.error(f"解析出现异常：{exc}")
        st.caption(traceback.format_exc(limit=3))


def _run_ocr_parse(uploaded) -> None:
    """对上传的图片批量 OCR 后解析。"""
    ocr_fail = 0
    total_text = ""
    for uf in uploaded:
        data = uf.getvalue()
        try:
            res = ocr_engine.extract_text_from_image(data)
        except Exception as exc:
            st.error(f"图片「{uf.name}」处理失败：{exc}")
            ocr_fail += 1
            continue
        if not res.ok:
            st.error(f"图片「{uf.name}」：{res.error}")
            ocr_fail += 1
            continue
        total_text += ("\n" if total_text else "") + res.text

    if ocr_fail == len(uploaded):
        st.warning("所有图片均未能成功识别，请检查清晰度或格式后重试。")
        return
    if not total_text.strip():
        st.warning("未从图片中提取到文字。")
        return

    st.session_state.source_text += ("\n" if st.session_state.source_text else "") + total_text
    _run_parse(total_text, source="截图")


def _merge_and_report(parsed: List[Dict[str, Any]]) -> None:
    """把解析结果合并进现有日程，做去重、过期过滤，并给出反馈。"""
    before = len(st.session_state.events)
    merged = st.session_state.events + parsed

    # 1) 去重
    merged = dedupe(merged)
    deduped_count = before + len(parsed) - len(merged)

    # 2) 过期过滤
    merged, expired = filter_expired(merged)
    st.session_state.expired_events = expired
    st.session_state.events = merged

    # 3) 冲突检测（仅用于展示状态）
    conflicts = detect_conflicts(merged)

    st.success(
        f"✅ 本次解析到 **{len(parsed)}** 条日程"
        + (f"，过滤 **{deduped_count}** 条重复" if deduped_count else "")
        + (f"，过滤 **{len(expired)}** 条已过期" if expired else "")
        + (f"，检测到 **{len(conflicts)}** 条时间冲突" if conflicts else "")
    )
    st.session_state.parse_note = (
        f"已累计识别 **{len(merged)}** 条日程。带 🟡 待核对标记的条目时间可能不准确，"
        "请在「日程表格」中人工修正。"
    )


def _load_sample() -> None:
    """加载示例日程（追加方式，不去重覆盖用户已有内容）。"""
    samples = sample_events()
    merged = st.session_state.events + samples
    merged = dedupe(merged)
    merged, expired = filter_expired(merged)
    st.session_state.expired_events = expired
    st.session_state.events = merged
    st.session_state.sample_loaded = True
    st.session_state.parse_note = (
        f"已加载示例日程，当前共 **{len(merged)}** 条（含示例）。可前往「日程表格」与「导出」演示。"
    )


def _apply_edits(edited: pd.DataFrame) -> None:
    """把编辑表的数据写回 session_state.events。"""
    if edited is None or edited.empty:
        return
    id_to_row = {str(r["ID"]): r for _, r in edited.iterrows()}
    updated = []
    for ev in st.session_state.events:
        if ev["id"] in id_to_row:
            r = id_to_row[ev["id"]]
            ev["title"] = str(r.get("标题", ev["title"]) or ev["title"]).strip() or ev["title"]
            ev["start"] = (str(r.get("开始", "") or "").strip())
            ev["end"] = (str(r.get("结束", "") or "").strip())
            ev["location"] = str(r.get("地点", "") or "").strip()
            ev["priority"] = str(r.get("优先级", "中") or "中")
            ev["repeat"] = str(r.get("重复", "") or "").strip()
            ev["type"] = str(r.get("类型", "event") or "event")
            # 人工编辑过的时间不再强制待核对
            ev["requires_confirm"] = False
        updated.append(ev)
    st.session_state.events = updated
    # 重新去重 + 过期过滤，保持一致性
    st.session_state.events = dedupe(st.session_state.events)
    st.session_state.events, st.session_state.expired_events = filter_expired(
        st.session_state.events
    )
    st.success("修改已应用。")


# ===========================================================================
# 侧边栏：设置 + 状态
# ===========================================================================
with st.sidebar:
    st.markdown("## ⚙️ 设置")
    _reminder_options = ["提前15分钟", "提前1小时", "提前1天", "不提醒"]
    if st.session_state.reminder not in _reminder_options:
        st.session_state.reminder = "提前15分钟"
    st.session_state.reminder = st.selectbox(
        "默认日历提醒",
        _reminder_options,
        index=_reminder_options.index(st.session_state.reminder),
        help="导出 ICS 时应用到所有日程的提醒",
    )

    st.markdown("### 🤖 AI 能力状态")
    st.caption(llm_client.status_line())
    if not llm_client.is_configured():
        st.info(
            "未检测到大模型密钥。当前将使用**内置规则解析**（仍可完整演示）。\n\n"
            "配置方法见 README：设置环境变量 `OPENAI_API_KEY`。"
        )

    st.markdown("---")
    st.caption(f"当前日程：**{len(st.session_state.events)} 条**")
    st.caption(f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")


# ===========================================================================
# 顶部 Hero
# ===========================================================================
st.markdown('<div class="hero">', unsafe_allow_html=True)
st.markdown('<span class="badge">大学生竞赛演示项目 · 全内存处理 · 隐私安全</span>', unsafe_allow_html=True)
st.title("🗓️ AI 智能日程收纳日历")
st.markdown(
    "把**聊天文本、课表截图、备忘录碎文字**一键整理成结构化日程表，"
    "自动识别时间、地点、重复规则，检测冲突与重复，并导出可直接导入各大日历的 **ICS** 文件。"
)
st.markdown("</div>", unsafe_allow_html=True)


# ===========================================================================
# Tab 1：输入与解析
# ===========================================================================
tab_input, tab_table, tab_export = st.tabs([
    "📥 输入与解析", "📋 日程表格", "📤 导出"
])

with tab_input:
    st.markdown("### ✍️ 输入方式一：粘贴纯文本")
    new_text = st.text_area(
        "在这里粘贴聊天记录 / 备忘录 / 课表文字（可多次追加）",
        height=140,
        placeholder=(
            "示例：\n周三下午 14:00 数据结构小组讨论，图书馆研讨室A\n"
            "周五 19:00 社团例会，学生活动中心302\n"
            "《操作系统》大作业下周四 23:59 前提交"
        ),
        key="input_text",
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("🧠 解析文本并加入日程", width="stretch"):
            if not new_text.strip():
                st.warning("请输入需要解析的文字内容后再解析。")
            else:
                # 追加到累计来源（同时保留本次即时解析）
                st.session_state.source_text += ("\n" if st.session_state.source_text else "") + new_text
                _run_parse(new_text, source="text")
    with c2:
        if st.button("🗑️ 清空输入框", width="stretch"):
            st.session_state.input_text = ""
            st.rerun()
    with c3:
        if st.button("📚 一键加载示例日程", width="stretch"):
            _load_sample()
            st.success("已加载示例日程，可到「日程表格」查看。")

    st.markdown("### 🖼️ 输入方式二：上传截图")
    st.caption("支持 PNG / JPG / BMP / WebP，单张不超过 8MB；会先 OCR 提取文字再交给 AI 解析。")
    uploaded = st.file_uploader(
        "上传课表/聊天截图（可多选，多次追加）",
        type=["png", "jpg", "jpeg", "bmp", "webp"],
        accept_multiple_files=True,
    )
    if uploaded:
        if st.button("🔍 识别并解析截图", type="primary", width="stretch"):
            _run_ocr_parse(uploaded)

    # 解析过程提示
    if st.session_state.parse_note:
        st.info(st.session_state.parse_note)


# ===========================================================================
# Tab 2：日程表格（查看 / 编辑 / 删除）
# ===========================================================================
with tab_table:
    st.markdown("### 📋 日程总览")
    st.caption("时间冲突标红，模糊待核对标黄，正常标绿。可直接在下表编辑或删除。")

    if not st.session_state.events:
        st.info("当前还没有日程。请到「输入与解析」页签添加，或点击一键加载示例日程。")
    else:
        events = st.session_state.events

        # ---- 只读带色视图（突出冲突/待核对） ----
        df_view = events_to_dataframe(events)
        st.dataframe(
            _style_status(df_view),
            width="stretch",
            hide_index=True,
            height=320,
        )

        st.markdown("---")
        st.markdown("### ✏️ 手动编辑 / 删除")

        # 编辑：data_editor 以字符串形式编辑关键字段
        edit_df = pd.DataFrame([
            {
                "ID": ev["id"],
                "标题": ev["title"],
                "开始": ev["start"],
                "结束": ev["end"],
                "地点": ev["location"],
                "优先级": ev["priority"],
                "重复": ev["repeat"],
                "类型": ev["type"],
            }
            for ev in events
        ])
        edited = st.data_editor(
            edit_df,
            width="stretch",
            hide_index=True,
            num_rows="fixed",
            disabled=["ID"],
            column_config={
                "ID": st.column_config.TextColumn("ID", disabled=True, width="small"),
                "标题": st.column_config.TextColumn("标题", required=True),
                "开始": st.column_config.TextColumn("开始(YYYY-MM-DD HH:MM)"),
                "结束": st.column_config.TextColumn("结束(YYYY-MM-DD HH:MM)"),
                "优先级": st.column_config.SelectboxColumn("优先级", options=["高", "中", "低"]),
                "重复": st.column_config.TextColumn("重复(如:每周一)"),
                "类型": st.column_config.SelectboxColumn(
                    "类型", options=["event", "ddl", "recurring"]
                ),
            },
        )

        b_apply, b_clear = st.columns(2)
        with b_apply:
            if st.button("✅ 应用修改", width="stretch"):
                _apply_edits(edited)
        with b_clear:
            if st.button("♻️ 重置为解析结果", width="stretch"):
                st.rerun()

        # 删除：按 ID 多选删除
        st.markdown("**删除日程**")
        options = [
            f"{ev['id']}｜{ev['title']}｜{ev['start'] or '时间待定'}"
            for ev in events
        ]
        del_options = st.multiselect("选择要删除的日程（可多选）", options)
        if st.button("🗑️ 删除选中日程", type="secondary"):
            if not del_options:
                st.warning("请先勾选要删除的日程。")
            else:
                del_ids = {opt.split("｜")[0] for opt in del_options}
                before = len(st.session_state.events)
                st.session_state.events = [
                    ev for ev in st.session_state.events if ev["id"] not in del_ids
                ]
                st.success(f"已删除 {before - len(st.session_state.events)} 条日程。")
                st.rerun()


# ===========================================================================
# Tab 3：导出
# ===========================================================================
with tab_export:
    st.markdown("### 📤 导出日程")
    if not st.session_state.events:
        st.info("没有可导出的日程，请先添加日程。")
    else:
        events = st.session_state.events
        st.caption(f"将导出 **{len(events)}** 条日程，提醒方式：**{st.session_state.reminder}**")

        # 预览
        with st.expander("👁️ 导出内容预览"):
            st.markdown("**ICS 预览（前若干行）**")
            st.code(exporter.build_ics(events, st.session_state.reminder)[:1200], language="text")
            st.markdown("**Markdown 预览**")
            st.markdown(exporter.build_markdown(events))

        c_ics, c_csv, c_md = st.columns(3)
        with c_ics:
            st.download_button(
                "⬇️ 下载 .ics（导入日历）",
                data=exporter.ics_bytes(events, st.session_state.reminder),
                file_name=f"ai_schedule_{datetime.now().strftime('%Y%m%d_%H%M')}.ics",
                mime="text/calendar",
                width="stretch",
            )
        with c_csv:
            st.download_button(
                "⬇️ 下载 .csv",
                data=exporter.csv_bytes(events),
                file_name=f"ai_schedule_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                width="stretch",
            )
        with c_md:
            st.download_button(
                "⬇️ 下载 .md",
                data=exporter.markdown_bytes(events),
                file_name=f"ai_schedule_{datetime.now().strftime('%Y%m%d_%H%M')}.md",
                mime="text/markdown",
                width="stretch",
            )

        st.markdown("---")
        st.markdown("### 📱 导入到各大日历")
        platform_data = {
            "iPhone / iPad (iOS 日历)": [
                "1. 在 iPhone 上用 Safari 打开本页面，下载 .ics 文件",
                "2. 点击下载的 .ics 文件，系统会提示『添加事件』",
                "3. 选择『添加到日历』，并确认目标日历",
            ],
            "安卓 (Google 日历 / 华为日历)": [
                "1. 下载 .ics 文件到手机",
                "2. 用文件管理器点击该文件，选择用日历应用打开",
                "3. 确认导入到指定日历即可",
            ],
            "Windows (Outlook)": [
                "1. 保存 .ics 文件到电脑",
                "2. 打开 Outlook → 文件 → 打开 → 导入/导出",
                "3. 选择『导入 iCalendar (.ics) 或 vCalendar 文件』→ 选择文件 → 完成",
            ],
            "macOS / Apple 日历": [
                "1. 下载 .ics 文件",
                "2. 双击文件，日历应用会自动弹出导入预览",
                "3. 点击『添加』即可",
            ],
        }
        for title, steps in platform_data.items():
            with st.expander(f"**{title}**"):
                for i, s in enumerate(steps, 1):
                    st.markdown(f"{i}. {s}")


# ===========================================================================
# 免责声明（比赛合规）
# ===========================================================================
st.markdown(
    """
    <div class="footer-note">
    <b>隐私与免责声明：</b>
    本工具全程在浏览器内存中处理数据，<b>不启用数据库、不上传保存任何图片或日程</b>，
    刷新页面即清空。AI 解析结果可能存在误差，时间模糊的条目已标注待核对，
    请以实际安排为准。本工具仅供学习与竞赛演示使用，不构成任何正式日程建议。
    </div>
    """,
    unsafe_allow_html=True,
)
