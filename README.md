# 🗓️ AI 智能日程收纳日历导入（Streamlit）

面向大学生参赛的一键运行 Web 应用：上传**课表截图**或粘贴**聊天文本 / 备忘录碎文字**，
AI 自动整理成结构化日程表，检测**冲突 / 重复 / 过期**，并导出可直接导入
**iPhone / 安卓 / Windows / Outlook** 的 **ICS 日历文件**，同时支持 CSV、Markdown 导出。

全程**在内存中处理**，不使用数据库，不保存用户图片与日程，刷新即清空 —— 隐私安全，适合比赛合规演示。

---

## ✨ 功能总览

| 模块 | 功能 |
| --- | --- |
| 📥 双输入 | 粘贴纯文本 + 上传截图（OCR），支持**多次追加不覆盖**已有日程 |
| 🤖 AI 解析 | 自动提取标题 / 起止时间 / 地点 / 备注 / 优先级 / 重复规则；识别普通活动、DDL、会议、每周循环课 |
| ⏰ 模糊时间 | 无法确定的时间自动推测并加 **⚠️ 待核对** 标记，提醒人工核实 |
| 🧹 智能清洗 | 自动去重、过滤过期日程、检测时间冲突并**标红高亮** |
| ✏️ 网页交互 | 可视化表格展示；网页内**手动编辑 / 删除**每条日程 |
| 🎯 一键示例 | 内置示例日程按钮，方便比赛现场演示 |
| 🔔 日历提醒 | 导出时自定义提醒：提前 15 分钟 / 1 小时 / 1 天 / 不提醒 |
| 📤 多格式导出 | 核心 **.ics** + **CSV + Markdown**；附四大平台导入教程 |
| 🛡️ 容错 | 空输入 / 损坏 / 过大 / 模糊图片 / OCR 失败 / 大模型失败 / 超时，均有友好兜底 |
| 🔒 隐私 | 全内存处理、无数据库、不落盘；页脚免责声明 |

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 若安装 `rapidocr-onnxruntime`（含 onnxruntime）网络受限，OCR 功能会降级提示，其余功能不受影响。

### 2.（可选）配置大模型密钥

工具通过**环境变量**读取密钥，**不会硬编码**。支持任意 OpenAI 兼容接口
（OpenAI、DeepSeek、智谱 GLM、阿里通义 Qwen、Moonshot 等）。

```bash
# Linux / macOS
export OPENAI_API_KEY="sk-xxxxxxxxxxxx"
export OPENAI_BASE_URL="https://api.openai.com/v1"   # 可选，第三方平台填对应地址
export SCHEDULE_MODEL="gpt-4o-mini"                  # 可选，默认 gpt-4o-mini
export LLM_TIMEOUT="40"                              # 可选，请求超时秒数

# Windows (PowerShell)
$env:OPENAI_API_KEY="sk-xxxxxxxxxxxx"
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:SCHEDULE_MODEL="gpt-4o-mini"
```

> **未配置密钥也能运行**：自动降级到内置规则解析（精度有限，但流程完整，适合演示与评测）。

### 3. 启动

```bash
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`。

---

## 🧩 项目结构

```
ai-schedule-import/
├── app.py                      # Streamlit 主程序（前端 + 交互 + 容错）
├── requirements.txt            # 依赖清单
├── README.md                   # 本文档
├── 公网部署指南.md              # 部署到手机可访问的教程
├── 自查报告与部署清单.md        # 开发自查与部署操作清单
└── core/                       # 后端逻辑模块（纯函数，可独立测试）
    ├── __init__.py
    ├── schedule_utils.py       # 时间标准化 / 去重 / 过期过滤 / 冲突检测 / 示例
    ├── llm_client.py           # 大模型解析（环境变量密钥 + 超时 + JSON 兜底）
    ├── ocr_engine.py           # 离线 OCR + 图片校验（懒加载）
    ├── parser.py               # 解析编排（LLM 优先 + 规则兜底 + 清洗）
    └── exporter.py             # ICS / CSV / Markdown 导出
```

---

## 🧠 技术实现要点

- **OCR**：`rapidocr_onnxruntime` 离线识别，懒加载避免启动卡顿；图片先校验（格式 / 损坏 / 8MB 上限），超长边自动缩放后再识别。
- **大模型**：OpenAI 兼容接口，严格 JSON 输出；相对时间基于当前时间推算绝对时间；跨天活动自动修正结束日期。
- **降级链**：大模型不可用/失败 → 内置规则解析 → 友好提示；任一层失败都不让页面崩溃。
- **冲突检测**：同一自然日时间段重叠标红；单条事件无结束时间按 1 小时占位判断。
- **去重**：标题 + 起止时间 + 地点归一化后去重。
- **过期过滤**：结束时间早于当前时间剔除；重复日程默认保留。
- **ICS**：标准 RFC 5545，含 RRULE 重复规则、VALARM 提醒、PRIORITY 优先级、文本转义与折行。

---

## 🛡️ 安全与隐私

- 密钥仅从环境变量读取，代码内**无任何硬编码密钥**。
- 日程仅存于 `st.session_state`（浏览器内存），**无数据库、无落盘**，刷新页面即清空。
- 用户上传的图片只在内存中完成校验与 OCR，不保存副本。
- 页面底部含免责声明，满足比赛合规要求。
- 所有外部调用均有异常捕获与超时保护。

---

## 🌐 部署到公网（手机随时访问）

见 `公网部署指南.md`：推荐 **Streamlit Community Cloud（免费）**，推代码到 GitHub 后即可获得公网网址，手机无需同一 WiFi。

---

## ✅ 部署 / 运行手动操作清单

见 `自查报告与部署清单.md`，要点如下：

1. `python3 -m venv .venv && source .venv/bin/activate`（可选，推荐）
2. `pip install -r requirements.txt`
3. 配置环境变量 `OPENAI_API_KEY`（可选）
4. `streamlit run app.py`
5. 浏览器访问 `http://localhost:8501`
6. 演示路径：一键加载示例 → 日程表格查看冲突/待核对 → 编辑 → 导出 .ics → 按教程导入手机日历

---

*仅供学习与竞赛演示使用，不构成正式日程建议。*
