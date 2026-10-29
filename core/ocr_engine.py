# -*- coding: utf-8 -*-
"""
core/ocr_engine.py —— OCR 文字识别引擎

职责：
  - 校验上传图片：格式、损坏、尺寸、文件大小
  - 调用离线 OCR（rapidocr_onnxruntime，无需联网与密钥）
  - 提取图中文字并做初步拼接
  - 各类失败（损坏/过大/模糊/OCR 失败）给出明确、可读的错误信息

设计说明：
  - OCR 引擎采用「懒加载」：首次用到时才初始化模型，避免启动卡顿。
  - 为降低内存峰值，超长边会先等比缩放到上限，再送入 OCR。
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image, UnidentifiedImageError

# 允许的图片大小与尺寸上限
MAX_FILE_BYTES = 8 * 1024 * 1024        # 8MB
MAX_LONG_EDGE = 1600                     # OCR 前缩放的最长边像素


class OCRResult:
    """OCR 结果封装：成功时 text 非空、error 为空。"""

    def __init__(self, text: str = "", error: str = ""):
        self.text = text
        self.error = error

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


def _validate_image(data: bytes) -> Optional[str]:
    """图片基础校验，返回错误信息（无错误返回 None）。"""
    if not data:
        return "未收到图片数据，请重新上传。"
    if len(data) > MAX_FILE_BYTES:
        return f"图片超过 {MAX_FILE_BYTES // (1024 * 1024)}MB，请压缩后再试。"
    try:
        Image.open(io.BytesIO(data)).verify()  # 校验完整性，会抛出异常表示损坏
    except UnidentifiedImageError:
        return "无法识别的图片格式（仅支持 PNG/JPG/JPEG/BMP/WebP），请更换图片。"
    except Exception:
        return "图片文件已损坏或无法读取，请重新上传。"
    return None


def _lazy_ocr():
    """懒加载 OCR 引擎实例。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError("缺少 rapidocr-onnxruntime 依赖，请先安装 requirements.txt。") from exc
    return RapidOCR()


def extract_text_from_image(data: bytes) -> OCRResult:
    """从图片字节流中识别文字。

    返回 OCRResult：
      - 成功：result.ok 为 True，result.text 为拼接后的文字
      - 失败：result.error 给出友好原因
    """
    err = _validate_image(data)
    if err:
        return OCRResult(error=err)

    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return OCRResult(error="图片无法解码，请更换图片后重试。")

    # 尺寸过大时等比缩放，兼顾识别质量与内存
    w, h = image.size
    long_edge = max(w, h)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    # 缩小前统计：避免过小图片被直接识别为空白
    if min(w, h) < 20:
        return OCRResult(error="图片过小，难以识别文字，请上传更清晰的图片。")

    try:
        engine = _lazy_ocr()
        import numpy as np
        result, _ = engine(np.array(image))
    except RuntimeError as exc:  # 依赖缺失
        return OCRResult(error=str(exc))
    except Exception:
        return OCRResult(error="OCR 识别失败，请换一张更清晰、文字更工整的截图。")

    if not result:
        return OCRResult(error="未在图片中识别到文字，可能图片过于模糊或未包含文字。")

    # result 结构：[[ [box坐标], 文字, 置信度 ], ...]
    lines = []
    for item in result:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            text = str(item[1]).strip()
            if text:
                lines.append(text)
    joined = "\n".join(lines)
    if not joined:
        return OCRResult(error="OCR 未能提取到有效文字，请检查图片清晰度。")
    return OCRResult(text=joined)
