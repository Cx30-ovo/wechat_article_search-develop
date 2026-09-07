"""搜一搜结果和微信公众号资料窗口的本地 OCR 定位。"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

import cv2
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value or "").split())


def _strip_ocr_edge_punctuation(value: str) -> str:
    """移除 OCR 在账号名称首尾偶发识别出的标点，不改变名称正文。"""
    normalized = _normalize(value)
    # 仅处理首尾标点，例如截图中的“Kimi智能助手?”。中间字符不会被删除，
    # 因此“账号A”和“账号-A”等相似名称仍会按不同账号处理。
    while normalized and unicodedata.category(normalized[0]).startswith("P"):
        normalized = normalized[1:]
    while normalized and unicodedata.category(normalized[-1]).startswith("P"):
        normalized = normalized[:-1]
    return normalized


def _account_name_match(expected_name: str, observed_name: str) -> tuple[bool, str]:
    """判断搜一搜名称是否可安全地映射为目标公众号。

    默认只接受完全一致；少数公众号会在搜索结果中附加“媒体”“事业单位”等
    账号身份后缀。这类受限后缀可接受，其他相似名一律拒绝，避免误点同名账号。
    """
    expected = _normalize(expected_name)
    observed = _normalize(observed_name)
    if observed == expected:
        return True, "exact"

    # 英文品牌名在不同页面的大小写并不稳定，例如 ComfyUI / ComfyUi。
    # 仅忽略 ASCII 大小写，中文和其他字符仍必须逐字一致。
    if observed.casefold() == expected.casefold():
        return True, "ascii-case-ignored"

    # RapidOCR 偶尔会把大写 I 识别成小写 l（AI -> Al）。只接受长度相同、
    # 且除此之外逐字一致的 I/l 单字符差异，避免使用宽泛的模糊匹配误点账号。
    if len(expected) >= 6 and len(observed) == len(expected):
        differences = [
            (left, right)
            for left, right in zip(expected.casefold(), observed.casefold())
            if left != right
        ]
        if differences and all({left, right} == {"i", "l"} for left, right in differences):
            return True, "ocr-i-l-confusion"

    # 账号名前缀 AI 在小字号窗口标题中偶尔会漏掉 I（AI新工匠 -> A新工匠）。
    # 只允许这个固定前缀的单字符缺失，且余下正文完全一致，不采用泛化的编辑距离。
    if (
        len(expected) >= 5
        and expected.startswith("AI")
        and observed == "A" + expected[2:]
    ):
        return True, "ocr-ai-prefix-i-omitted"

    # 少数中文字符形近会被 OCR 稳定混淆。这里只放行本项目截图验证过的
    # “哔/哗”和“千/干”，并且仍要求其余字符、账号筛选和后续资料窗口全部一致。
    if len(expected) >= 4 and len(observed) == len(expected):
        differences = [
            (left, right)
            for left, right in zip(expected, observed)
            if left != right
        ]
        accepted_pairs = ({"哔", "哗"}, {"千", "干"})
        if differences and all({left, right} in accepted_pairs for left, right in differences):
            return True, "ocr-chinese-confusion"
    # 账号结果行偶尔会在末尾多出 ?、！等 OCR 噪声；只忽略首尾标点后再比较。
    # 这一步不会接受正文不同的近似名称，仍由后续“公众号/原创内容”证据二次约束。
    expected_without_edge_punctuation = _strip_ocr_edge_punctuation(expected)
    observed_without_edge_punctuation = _strip_ocr_edge_punctuation(observed)
    if (
        expected_without_edge_punctuation
        and observed_without_edge_punctuation == expected_without_edge_punctuation
    ):
        return True, "edge-punctuation-ignored"
    suffix = observed[len(expected) :] if observed.startswith(expected) else ""
    # 微信搜一搜会把认证账号的主体类型拼在名称 OCR 行末，例如“书生Intern 事业单位”。
    # 仅放行固定身份标签，且仍须在后续卡片中找到“原创内容”等证据，避免扩大误匹配范围。
    safe_suffixes = {
        "媒体",
        "官方",
        "公众号",
        "社区",
        "频道",
        "服务号",
        "事业单位",
        "企业",
        "学校",
        "机构",
        "政府",
    }
    if len(expected) >= 2 and suffix in safe_suffixes:
        return True, "safe-suffix-alias"
    return False, ""


EMPTY_FEED_MARKERS = (
    "已无更多消息",
    "没有更多消息",
    "暂无更多消息",
    "没有更多内容",
    "暂无更多内容",
    "已经到底了",
    "暂无内容",
    "没有内容",
)


def _empty_feed_marker(value: str) -> str | None:
    """识别公众号资料页无文章可展示的底部文案。"""
    normalized = _normalize(value or "")
    if not normalized:
        return None
    for marker in EMPTY_FEED_MARKERS:
        if marker in normalized:
            return marker
    return None


def _rows_empty_feed_marker(rows: list[dict[str, Any]], height: float) -> str | None:
    """从 OCR 行中找内容区底部的无更多消息文案。"""
    for row in rows:
        center_y = float(row.get("center_y") or 0)
        if not height * 0.35 <= center_y <= height * 0.98:
            continue
        marker = _empty_feed_marker(str(row.get("normalized") or row.get("text") or ""))
        if marker:
            return marker
    return None


def _bounds(box: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return min(xs), min(ys), max(xs), max(ys)


def _number(value: str) -> int | None:
    text = unicodedata.normalize("NFKC", value or "").strip().rstrip("+")
    match = re.fullmatch(r"([\d.]+)(万)?", text)
    if not match:
        return None
    multiplier = 10000 if match.group(2) else 1
    return round(float(match.group(1)) * multiplier)


def _is_numeric_or_punctuation_only(value: str) -> bool:
    """过滤封面、图片和徽章中的纯数字 OCR 噪声。"""
    normalized = _normalize(value)
    return bool(normalized) and bool(
        re.fullmatch(r"[0-9０-９.,，。:：/／\\\\+＋%％()（）\-—_＿]+", normalized)
    )


TIME_LABEL_RE = re.compile(
    r"^(?:置顶|今天|昨天|(?:星期|周)[一二三四五六日天1-7]|"
    r"(?:\d{4}年)?\d{1,2}月\d{1,2}日)$"
)
NON_ARTICLE_SECTION_LABELS = {"贴图", "视频号"}
METRIC_FRIEND_SUFFIX = r"(?:\s*[\d.]+万?\+?个朋友(?:看过|转发))?"
METRICS_RE = re.compile(
    rf"^阅读\s*([\d.]+万?\+?)\s*赞\s*([\d.]+万?\+?){METRIC_FRIEND_SUFFIX}$"
)
COMBINED_METRICS_RE = re.compile(
    rf"^(.+?)阅读\s*([\d.]+万?\+?)\s*赞\s*([\d.]+万?\+?){METRIC_FRIEND_SUFFIX}$"
)


class WeChatProfileOCR:
    def __init__(self) -> None:
        self.ocr = RapidOCR()
        self._cached_screenshot: Image.Image | None = None
        self._cached_rows: list[dict[str, Any]] = []

    @staticmethod
    def _expand_navigation_terms(rows: list[dict[str, Any]]) -> list[tuple[float, str]]:
        """把 OCR 行展开为可对齐的导航词。

        RapidOCR 会把资料页同一导航行识别成“贴图文章•视频号”等合并文本，
        如果只接受独立的“贴图”“文章”“视频号”词，这类帧会被误判成缺少
        资料页结构。合并行中同时出现至少两个导航词时按同 y 展开计数。
        """
        expanded: list[tuple[float, str]] = []
        for row in rows:
            normalized = str(row.get("normalized") or row.get("text") or "").strip()
            if normalized in {"全部", "贴图", "文章", "视频号"}:
                expanded.append((float(row["center_y"]), normalized))
                continue
            if len(normalized) <= 24:
                found = [term for term in ("全部", "贴图", "文章", "视频号") if term in normalized]
                if len(found) >= 2:
                    expanded.extend((float(row["center_y"]), term) for term in found)
        return expanded

    def _rows(self, screenshot: Image.Image) -> list[dict[str, Any]]:
        # 一个决策阶段会对同一张截图做多项校验，复用 OCR 结果可明显减少等待时间。
        if self._cached_screenshot is screenshot:
            return self._cached_rows
        result, _ = self.ocr(screenshot)
        rows = []
        for box, text, confidence in result or []:
            left, top, right, bottom = _bounds(box)
            rows.append(
                {
                    "text": str(text).strip(),
                    "normalized": _normalize(str(text)),
                    "confidence": float(confidence),
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "center_x": (left + right) / 2,
                    "center_y": (top + bottom) / 2,
                }
            )
        self._cached_screenshot = screenshot
        self._cached_rows = rows
        return rows

    def locate_copy_link_action(self, screenshot: Image.Image) -> dict[str, Any]:
        """在文章浏览器菜单中定位“复制链接”，避免依赖不同电脑上的固定坐标。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "复制" in row["normalized"]
            and "链接" in row["normalized"]
            # 浏览器菜单位于窗口上半部分；排除正文中偶然出现的同名文字。
            and row["center_y"] < height * 0.45
            and row["center_x"] > width * 0.30
        ]
        if not candidates:
            return {"found": False, "reason": "浏览器菜单中未识别到复制链接"}
        row = min(candidates, key=lambda item: (item["center_y"], -item["confidence"]))
        return {
            "found": True,
            "text": row["text"],
            "center_x_1000": round(row["center_x"] * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            "confidence": row["confidence"],
            "method": "rapidocr-browser-copy-link",
        }

    def locate_browser_menu_button(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过标题栏中的三个横向圆点定位浏览器“更多”菜单按钮。

        这里只分析截图顶部右半区，并要求三个小连通域水平对齐、间距接近，
        避免把最小化、最大化或正文中的省略号当成菜单按钮。
        """
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        if width < 120 or height < 80:
            return {"found": False, "reason": "文章窗口截图过小"}

        # 只检查浏览器顶部工具栏。文章正文和网页工具栏也可能出现“...”，
        # 如果把识别区域放到页面内容区，会误点网页自身的分享/更多按钮。
        title_bottom = max(44, min(round(height * 0.06), 72))
        search_left = round(width * 0.45)
        search_right = round(width * 0.96)
        gray = cv2.cvtColor(rgb[:title_bottom, search_left:search_right], cv2.COLOR_RGB2GRAY)
        # 标题栏通常是浅色背景；较严格的深色阈值可排除大部分抗锯齿文字边缘。
        mask = cv2.threshold(gray, 105, 255, cv2.THRESH_BINARY_INV)[1]
        component_count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        dots: list[dict[str, float]] = []
        max_dot_size = max(9, round(title_bottom * 0.18))
        for index in range(1, component_count):
            left, top, box_width, box_height, area = stats[index]
            if not (1 <= area <= max_dot_size * max_dot_size):
                continue
            if not (1 <= box_width <= max_dot_size and 1 <= box_height <= max_dot_size):
                continue
            aspect = box_width / max(box_height, 1)
            if not 0.45 <= aspect <= 2.2:
                continue
            center_x, center_y = centroids[index]
            dots.append(
                {
                    "x": float(center_x),
                    "y": float(center_y),
                    "width": float(box_width),
                    "height": float(box_height),
                    "area": float(area),
                }
            )

        best: tuple[float, dict[str, float], dict[str, float], dict[str, float]] | None = None
        dots.sort(key=lambda item: item["x"])
        for first_index in range(len(dots)):
            for second_index in range(first_index + 1, len(dots)):
                for third_index in range(second_index + 1, len(dots)):
                    first, second, third = dots[first_index], dots[second_index], dots[third_index]
                    gap_one = second["x"] - first["x"]
                    gap_two = third["x"] - second["x"]
                    average_size = max(
                        1.0,
                        (first["width"] + second["width"] + third["width"]) / 3,
                    )
                    if not (average_size * 0.8 <= gap_one <= average_size * 5.5):
                        continue
                    if not (average_size * 0.8 <= gap_two <= average_size * 5.5):
                        continue
                    gap_similarity = min(gap_one, gap_two) / max(gap_one, gap_two)
                    y_spread = max(first["y"], second["y"], third["y"]) - min(
                        first["y"], second["y"], third["y"]
                    )
                    if gap_similarity < 0.65 or y_spread > max(2.5, average_size * 0.9):
                        continue
                    size_similarity = min(first["area"], second["area"], third["area"]) / max(
                        first["area"], second["area"], third["area"]
                    )
                    if size_similarity < 0.45:
                        continue
                    # 优先选择标题栏更靠右、也更靠上的候选；浏览器菜单通常位于
                    # 标签栏/工具栏右侧，而不是网页正文区域。
                    rightness = third["x"] / max(search_right - search_left, 1)
                    topness = 1.0 - min(1.0, max(first["y"], second["y"], third["y"]) / title_bottom)
                    score = (
                        gap_similarity * 0.40
                        + size_similarity * 0.20
                        + rightness * 0.25
                        + topness * 0.15
                    )
                    if best is None or score > best[0]:
                        best = (score, first, second, third)

        if best is None:
            return {"found": False, "reason": "标题栏未识别到三点菜单按钮"}
        score, first, second, third = best
        center_x = search_left + (first["x"] + second["x"] + third["x"]) / 3
        center_y = (first["y"] + second["y"] + third["y"]) / 3
        return {
            "found": True,
            "center_x_1000": round(center_x * 1000 / width),
            "center_y_1000": round(center_y * 1000 / height),
            "confidence": round(min(0.99, score), 3),
            "method": "opencv-browser-ellipsis",
        }

    def locate_wechat_main_search_box(self, screenshot: Image.Image) -> dict[str, Any]:
        """定位 Win11 新版微信主界面左上角的全局搜索框。

        新版主界面没有搜一搜页面的绿色“搜索”按钮，因此不能复用
        ``locate_search_box``。截图中的搜索框位于左侧会话栏顶部，输入“搜一搜”
        后由键盘回车打开内置搜一搜页面。
        """
        width, height = screenshot.size
        rows = self._rows(screenshot)
        candidates = [
            row
            for row in rows
            if row["normalized"] == "搜索"
            and row["center_x"] < width * 0.40
            and height * 0.03 < row["center_y"] < height * 0.18
        ]
        if candidates:
            row = min(candidates, key=lambda item: (item["center_y"], -item["confidence"]))
            return {
                "found": True,
                "center_x_1000": round(row["center_x"] * 1000 / width),
                "center_y_1000": round(row["center_y"] * 1000 / height),
                "confidence": row["confidence"],
                "method": "rapidocr-win11-main-search-bar",
            }

        # OCR 可能读不到浅灰色占位文字，但 Win11 新版主界面布局固定：搜索框
        # 位于左侧会话栏顶部约 x=18.5%、y=8.0%。只在足够大的微信主窗口中
        # 启用该布局兜底，避免把搜一搜网页误当成主界面。
        if width >= 900 and height >= 700:
            return {
                "found": True,
                "center_x_1000": 185,
                "center_y_1000": 80,
                "confidence": 0.68,
                "method": "win11-main-search-bar-layout-v1",
            }
        return {"found": False, "reason": "未找到 Win11 微信主界面搜索框"}

    def locate_search_box(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过顶部绿色“搜索”按钮定位其左侧输入框。"""
        width, height = screenshot.size
        buttons = [
            row
            for row in self._rows(screenshot)
            if row["normalized"] == "搜索"
            # 搜索结果页在顶部，搜一搜首页则位于页面中央，两种布局都要兼容。
            and row["center_y"] < height * 0.55
            and row["center_x"] > width * 0.55
        ]
        if buttons:
            button = min(buttons, key=lambda row: row["center_y"])
            return {
                "found": True,
                "center_x_1000": 430,
                "center_y_1000": round(button["center_y"] * 1000 / height),
                "button_x_1000": round(button["center_x"] * 1000 / width),
                "button_y_1000": round(button["center_y"] * 1000 / height),
                "confidence": button["confidence"],
                "method": "rapidocr-sogou-search-box",
            }

        # 首页或页面动画期间，RapidOCR 可能漏掉绿色按钮上的“搜索”二字；
        # 绿色按钮本身仍是稳定的视觉锚点，使用颜色和连通区域做保守兜底。
        rgb = np.asarray(screenshot.convert("RGB"))
        red = rgb[:, :, 0].astype(np.int16)
        green = rgb[:, :, 1].astype(np.int16)
        blue = rgb[:, :, 2].astype(np.int16)
        green_mask = (
            (green > 120)
            & (green > red * 1.18)
            & (green > blue * 1.08)
        ).astype(np.uint8) * 255
        green_mask[: max(1, round(height * 0.15)), :] = 0
        green_mask[round(height * 0.62) :, :] = 0
        green_mask[:, : round(width * 0.50)] = 0
        component_count, _labels, stats, centroids = cv2.connectedComponentsWithStats(
            green_mask, 8
        )
        candidates: list[tuple[int, float, float, int, int]] = []
        for index in range(1, component_count):
            left, top, box_width, box_height, area = stats[index]
            if area < max(400, round(width * height * 0.0001)):
                continue
            if box_width < 70 or box_height < 24 or box_width / max(box_height, 1) < 2.0:
                continue
            center_x, center_y = centroids[index]
            candidates.append((int(area), float(center_x), float(center_y), box_width, box_height))
        if not candidates:
            return {"found": False, "reason": "未找到搜一搜顶部搜索框"}
        area, center_x, center_y, _box_width, _box_height = max(
            candidates, key=lambda item: item[0]
        )
        return {
            "found": True,
            "center_x_1000": 430,
            "center_y_1000": round(center_y * 1000 / height),
            "button_x_1000": round(center_x * 1000 / width),
            "button_y_1000": round(center_y * 1000 / height),
            "confidence": min(0.98, 0.72 + area / max(width * height, 1)),
            "method": "opencv-sogou-green-search-button",
        }

    def inspect_search_submit_state(self, screenshot: Image.Image) -> dict[str, Any]:
        """用 OpenCV 快速判断搜索结果布局和输入框焦点状态。

        这个检查不调用 OCR。新版搜一搜提交后会先切换到顶部搜索栏布局，
        随后可能再次自动聚焦输入框并弹出联想层。绿色搜索按钮用于定位，
        按钮左侧是否仍存在绿色输入框描边用于判断焦点是否已经释放。
        """
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        red = rgb[:, :, 0].astype(np.int16)
        green = rgb[:, :, 1].astype(np.int16)
        blue = rgb[:, :, 2].astype(np.int16)
        green_mask = (
            (green > 120)
            & (green > red * 1.18)
            & (green > blue * 1.08)
        ).astype(np.uint8) * 255
        # 先腐蚀掉输入框的一像素/三像素描边，避免描边与按钮偶然连通后形成
        # 一个低填充率大组件，导致真正的实心搜索按钮无法被定位。
        filled_green_mask = cv2.erode(
            green_mask,
            np.ones((3, 3), dtype=np.uint8),
            iterations=1,
        )
        component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            filled_green_mask, 8
        )
        button_candidates: list[tuple[int, int, int, int, int]] = []
        for index in range(1, component_count):
            left, top, box_width, box_height, area = [int(value) for value in stats[index]]
            if left < width * 0.50 or top > height * 0.60:
                continue
            if box_width < width * 0.06 or box_height < height * 0.025:
                continue
            fill_ratio = area / max(box_width * box_height, 1)
            if fill_ratio < 0.45:
                continue
            button_candidates.append((area, left, top, box_width, box_height))
        if not button_candidates:
            return {
                "found": False,
                "result_layout_ready": False,
                "input_focused": False,
                "reason": "未找到绿色搜索按钮",
                "method": "opencv-search-submit-state",
            }

        _area, left, top, box_width, box_height = max(
            button_candidates, key=lambda item: item[0]
        )
        center_y = top + box_height / 2
        focus_left = max(0, round(width * 0.02))
        focus_right = max(focus_left + 1, left - 4)
        focus_top = max(0, top - round(box_height * 0.30))
        focus_bottom = min(height, top + box_height + round(box_height * 0.30))
        focus_crop = green_mask[focus_top:focus_bottom, focus_left:focus_right]
        focus_green_pixels = int(np.count_nonzero(focus_crop))
        # 聚焦描边横跨输入框，绿色像素数量远高于文字抗锯齿噪声。
        focus_threshold = max(40, round((focus_right - focus_left) * 0.35))
        input_focused = focus_green_pixels >= focus_threshold
        return {
            "found": True,
            "result_layout_ready": center_y < height * 0.24,
            "input_focused": input_focused,
            "button_bbox": [left, top, left + box_width, top + box_height],
            "button_center_y_1000": round(center_y * 1000 / max(height, 1)),
            "focus_green_pixels": focus_green_pixels,
            "focus_threshold": focus_threshold,
            "method": "opencv-search-submit-state",
        }

    def locate_search_home(self, screenshot: Image.Image) -> dict[str, Any]:
        """识别搜一搜首页，供关闭资料页后的标签恢复使用。"""
        width, height = screenshot.size
        navigation_terms = {
            "全部",
            "视频号",
            "文章",
            "表情",
            "公众号",
            "小程序",
            "朋友圈",
        }
        rows = self._rows(screenshot)
        navigation_rows = [
            row
            for row in rows
            if row["normalized"] in navigation_terms
            and height * 0.20 < row["center_y"] < height * 0.62
        ]
        distinct_terms = {row["normalized"] for row in navigation_rows}
        if len(distinct_terms) < 3:
            return {"found": False, "reason": "未找到搜一搜首页分类导航"}
        return {
            "found": True,
            "method": "rapidocr-sogou-home-navigation",
            "navigation_terms": sorted(distinct_terms),
            "confidence": min(0.99, 0.55 + len(distinct_terms) * 0.06),
        }

    def locate_search_result(self, screenshot: Image.Image, expected_name: str) -> dict[str, Any]:
        """定位搜一搜“账号/公众号”结果中的精确名称，排除顶部输入框和相似账号。"""
        width, height = screenshot.size
        expected = _normalize(expected_name)
        rows = self._rows(screenshot)
        navigation_rows = [
            row
            for row in rows
            if "账号" in row["normalized"]
            and height * 0.08 <= row["center_y"] <= height * 0.30
            and row["center_x"] < width * 0.55
        ]
        navigation_bottom = min(
            (row["bottom"] for row in navigation_rows),
            default=height * 0.14,
        )
        filter_labels = {"不限", "小程序", "公众号", "服务号", "视频号"}
        filter_rows = [
            row
            for row in rows
            if row["normalized"] in filter_labels
            # 只认一级导航紧下方的二级筛选栏，排除结果卡片里的“公众号”类型文字。
            and navigation_bottom < row["center_y"] < navigation_bottom + height * 0.13
        ]
        # 以二级账号筛选栏的真实底边为锚点，不再依赖窗口高度百分比。
        # 如果某次 OCR 没读到筛选栏，再退回顶部导航底边作为结果区起点。
        if filter_rows:
            result_top = max(row["bottom"] for row in filter_rows)
        else:
            fallback_navigation_rows = [
                row
                for row in rows
                if row["normalized"] in {"全部", "账号", "文章", "视频"}
                and row["center_y"] < height * 0.35
            ]
            result_top = max(
                (row["bottom"] for row in fallback_navigation_rows),
                default=height * 0.15,
            )
        text_height = max(
            (row["bottom"] - row["top"] for row in filter_rows),
            default=max(12, height * 0.015),
        )
        result_top += max(4, text_height * 0.35)
        # 左侧头像/Logo 常被 OCR 识别成同名文字，只接受结果主内容列的名称行。
        exact_matches = [
            row
            for row in rows
            if row["normalized"] == expected
            and row["center_y"] > result_top
            and row["left"] < width * 0.55
            and row["center_x"] >= width * 0.10
        ]
        # 例如库内名称“腾讯技术工程”在微信中实际展示为“腾讯技术工程媒体”。
        # 仅接受白名单后缀，不能把任意包含关系当作同一公众号。
        alias_matches = [
            row
            for row in rows
            if row["center_y"] > result_top
            and row["left"] < width * 0.55
            and row["center_x"] >= width * 0.10
            and _account_name_match(expected_name, row["text"])[0]
        ]
        # 完全同名的视频号和带“媒体”等安全后缀的公众号可能同时存在。
        # 不能用 ``exact_matches or alias_matches``，否则任意完全同名视频号都会
        # 把真正的“公众号名 + 媒体”候选从集合中整体丢弃。
        matches: list[dict[str, Any]] = []
        seen_match_ids: set[int] = set()
        for candidate in exact_matches + alias_matches:
            candidate_id = id(candidate)
            if candidate_id in seen_match_ids:
                continue
            seen_match_ids.add(candidate_id)
            matches.append(candidate)
        if not matches:
            return {"found": False, "reason": "搜一搜结果中没有精确匹配名称"}
        # 同名结果可能同时包含视频号、公众号和服务号。必须按每张卡片自己的
        # 垂直边界提取类型，不能让上方视频号借用下方公众号的类型证据。
        ordered_matches = sorted(matches, key=lambda item: item["center_y"])
        candidate_records: list[dict[str, Any]] = []
        for index, candidate_name in enumerate(ordered_matches):
            next_top = (
                ordered_matches[index + 1].get("top", ordered_matches[index + 1]["center_y"])
                if index + 1 < len(ordered_matches)
                else height
            )
            card_bottom = min(
                candidate_name["bottom"] + height * 0.16,
                float(next_top) - 2,
            )
            card_rows = [
                item
                for item in rows
                if candidate_name["bottom"] <= item["center_y"] <= card_bottom
                and abs(item["left"] - candidate_name["left"]) < width * 0.16
                and item is not candidate_name
            ]
            def normalized_account_type(item: dict[str, Any]) -> str:
                normalized = item["normalized"]
                if normalized == "公众号":
                    return "公众号"
                if normalized == "服务号":
                    return "服务号"
                if normalized == "小程序" or normalized.startswith("小程序"):
                    return "小程序"
                # 视频号类型后面常带蝴蝶结/无限符号等图标，OCR 可能合并成
                # “视频号∞”。它仍必须被识别为视频号并明确排除。
                if normalized.startswith("视频号"):
                    return "视频号"
                return ""

            explicit_types = [
                (item, normalized_account_type(item))
                for item in card_rows
                if normalized_account_type(item)
            ]
            nearest_type = min(
                explicit_types,
                key=lambda pair: pair[0]["center_y"],
                default=None,
            )
            explicit_type = nearest_type[1] if nearest_type else ""
            original_content_evidence = [
                item["text"]
                for item in card_rows
                if "篇原创内容" in item["normalized"]
            ]
            # 旧版公众号筛选结果可能显示主体类型“个人”，此时“篇原创内容”仍可
            # 作为公众号证据；但明确写着“视频号”的卡片永远不能降级接受。
            account_type = explicit_type
            if not account_type and original_content_evidence:
                account_type = "公众号"
            organization_identity_evidence = [
                item["text"]
                for item in card_rows
                if any(
                    marker in item["normalized"]
                    for marker in {
                        "公司", "集团", "报社", "中心", "委员会", "政府",
                        "管理局", "商务局", "发改委", "协会", "学校", "大学", "研究院",
                    }
                )
            ]
            candidate_records.append(
                {
                    "row": candidate_name,
                    "name_match_method": _account_name_match(
                        expected_name, candidate_name["text"]
                    )[1],
                    "account_type": account_type,
                    "explicit_type": explicit_type,
                    "type_evidence": [nearest_type[0]["text"]] if nearest_type else [],
                    "original_content_evidence": original_content_evidence,
                    "organization_identity_evidence": organization_identity_evidence,
                    "personal_evidence": [
                        item["text"]
                        for item in card_rows
                        if item["normalized"] == "个人"
                    ],
                    "card_bottom": round(card_bottom, 1),
                }
            )

        eligible_candidates = [
            item
            for item in candidate_records
            if item["account_type"] in {"公众号", "服务号"}
            and item["explicit_type"] != "视频号"
        ]
        if not eligible_candidates:
            return {
                "found": False,
                "reason": "同名结果中没有可确认的公众号或服务号，视频号不会被打开",
                "candidate_types": [item["explicit_type"] for item in candidate_records],
            }
        # 固定业务优先级：同一次搜索先公众号；完全没有公众号候选时才服务号。
        selected = min(
            eligible_candidates,
            key=lambda item: (
                0 if item["account_type"] == "公众号" else 1,
                0 if item["name_match_method"] == "exact" else 1,
                item["row"]["center_y"],
            ),
        )
        row = selected["row"]
        account_type = selected["account_type"]
        _, name_match_method = _account_name_match(expected_name, row["text"])
        original_content_evidence = selected["original_content_evidence"]
        organization_identity_evidence = selected["organization_identity_evidence"]
        official_evidence = selected["type_evidence"] + original_content_evidence
        account_identity_evidence = official_evidence + organization_identity_evidence
        company = organization_identity_evidence[0] if organization_identity_evidence else ""
        return {
            "found": True,
            "name": row["text"],
            "matched_name": row["text"],
            "name_match_method": name_match_method,
            "company": company,
            "account_type": account_type,
            "official_evidence": official_evidence,
            "organization_identity_evidence": organization_identity_evidence,
            "account_identity_evidence": account_identity_evidence,
            "original_content_evidence": original_content_evidence,
            "candidate_types": [item["explicit_type"] for item in candidate_records],
            "selection_policy": "prefer-official-account-then-service-account-never-video-account",
            "card_bottom": selected["card_bottom"],
            "is_official_account": True,
            "center_x_1000": round(row["center_x"] * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            # 名称点击未触发时改点同一结果左侧头像，兼容不同搜一搜版本的点击热区。
            "avatar_x_1000": round(max(0, row["left"] - width * 0.04) * 1000 / width),
            "avatar_y_1000": round(min(height, row["center_y"] + height * 0.015) * 1000 / height),
            "result_top": round(result_top, 1),
            "confidence": row["confidence"],
            "method": "rapidocr-sogou-result-structural-anchor",
        }

    def locate_all_page_account_result(
        self, screenshot: Image.Image, expected_name: str
    ) -> dict[str, Any]:
        """定位新版“全部”页面中的公众号账号区块。

        Win11 新版搜一搜会在默认“全部”页直接展示“关键词 - 账号”区块，
        不一定需要先点击顶部“账号”。只有同时看到账号区块或顶部导航、精确名称
        和公众号内容证据时，才允许调用方跳过旧版分类筛选。
        """
        width, height = screenshot.size
        rows = self._rows(screenshot)
        expected = _normalize(expected_name)
        section_rows = [
            row
            for row in rows
            if row["center_y"] < height * 0.55
            and row["left"] < width * 0.80
            and "账号" in row["normalized"]
            and (
                row["normalized"].endswith("-账号")
                or row["normalized"].endswith("—账号")
                or row["normalized"].endswith("账号")
            )
            and (expected in row["normalized"] or row["normalized"] == "账号")
        ]
        navigation_rows = [
            row
            for row in rows
            if row["center_y"] < height * 0.35
            and row["left"] < width * 0.80
            and row["normalized"] in {"全部", "账号", "文章", "划线", "视频", "百科", "新闻", "直播"}
        ]
        target = self.locate_search_result(screenshot, expected_name)
        if not target.get("found"):
            return {
                "found": False,
                "reason": target.get("reason") or "新版全部页未找到可确认的公众号卡片",
                "section_evidence": [row["text"] for row in section_rows],
            }
        if not section_rows and not navigation_rows:
            return {
                "found": False,
                "reason": "公众号卡片缺少新版全部页账号区块证据",
                "section_evidence": [],
            }
        return {
            **target,
            "layout": "all-account-section",
            "method": "rapidocr-sogou-all-page-account-card",
            "section_evidence": [row["text"] for row in section_rows],
        }

    def locate_account_tab(self, screenshot: Image.Image) -> dict[str, Any]:
        """定位搜一搜结果页的一级“账号”分类，不依赖上一次选中的分类。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "账号" in row["normalized"]
            and height * 0.08 <= row["center_y"] <= height * 0.30
            and row["center_x"] < width * 0.55
        ]
        if not candidates:
            return {"found": False, "reason": "搜一搜结果页未找到一级账号分类"}
        row = min(candidates, key=lambda item: item["center_y"])
        # 导航间距较小时，RapidOCR 会把“账号 文章 划线”合并为一行。
        # 按字符位置还原“账号”子区域，避免误点相邻的文章分类。
        normalized = row["normalized"]
        account_index = normalized.index("账号")
        char_width = (row["right"] - row["left"]) / max(len(normalized), 1)
        account_left = row["left"] + char_width * account_index
        account_right = account_left + char_width * 2
        return {
            "found": True,
            "center_x_1000": round((account_left + account_right) / 2 * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            "confidence": row["confidence"],
            "ocr_text": row["text"],
            "method": "rapidocr-sogou-account-tab-subregion",
        }

    def locate_official_account_filter(self, screenshot: Image.Image) -> dict[str, Any]:
        """定位“账号”分类下的二级“公众号”筛选项。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "公众号" in row["normalized"]
            and height * 0.12 <= row["center_y"] <= height * 0.38
            and row["center_x"] < width * 0.55
        ]
        if not candidates:
            return {"found": False, "reason": "账号结果页未找到二级公众号筛选项"}

        row = min(candidates, key=lambda item: item["center_y"])
        normalized = row["normalized"]
        official_index = normalized.index("公众号")
        char_width = (row["right"] - row["left"]) / max(len(normalized), 1)
        official_left = row["left"] + char_width * official_index
        official_right = official_left + char_width * 3
        return {
            "found": True,
            "center_x_1000": round((official_left + official_right) / 2 * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            "confidence": row["confidence"],
            "ocr_text": row["text"],
            "method": "rapidocr-sogou-official-account-filter-subregion",
        }

    def validate_official_account_filter_selected(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过文字颜色确认二级“公众号”已选中；选中项明显深于其他筛选项。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "公众号" in row["normalized"]
            and height * 0.12 <= row["center_y"] <= height * 0.38
            and row["center_x"] < width * 0.55
        ]
        if not candidates:
            return {"selected": False, "reason": "未找到二级公众号筛选文字"}
        row = min(candidates, key=lambda item: item["center_y"])
        normalized = row["normalized"]
        official_index = normalized.index("公众号")
        char_width = (row["right"] - row["left"]) / max(len(normalized), 1)
        left = max(0, round(row["left"] + char_width * official_index))
        right = min(width, round(left + char_width * 3))
        top = max(0, round(row["top"]))
        bottom = min(height, round(row["bottom"]))
        crop = np.asarray(screenshot.convert("L").crop((left, top, right, bottom)))
        foreground = crop[crop < 220]
        median = float(np.median(foreground)) if foreground.size else 255.0
        selected = median <= 100
        return {
            "selected": selected,
            "foreground_median": round(median, 2),
            "reason": "" if selected else "公众号文字仍为未选中的浅色",
            "method": "opencv-secondary-filter-text-darkness",
        }

    def validate_account_tab_selected(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过账号下划线和二级账号筛选项确认点击确实生效。"""
        width, height = screenshot.size
        rows = self._rows(screenshot)
        exact_account_rows = [
            row
            for row in rows
            if row["normalized"] == "账号"
            and height * 0.08 <= row["center_y"] <= height * 0.30
            and row["center_x"] < width * 0.55
        ]
        if exact_account_rows:
            account = min(exact_account_rows, key=lambda item: item["center_y"])
        else:
            # RapidOCR 偶尔会把相邻的“全部”“账号”合并，按末尾两个汉字估算账号区域。
            merged_rows = [
                row
                for row in rows
                if "账号" in row["normalized"]
                and height * 0.08 <= row["center_y"] <= height * 0.30
                and row["center_x"] < width * 0.55
            ]
            if not merged_rows:
                return {"selected": False, "reason": "点击后未找到账号分类文字"}
            merged = min(merged_rows, key=lambda item: item["center_y"])
            normalized = merged["normalized"]
            account_index = normalized.index("账号")
            char_width = (merged["right"] - merged["left"]) / max(len(normalized), 1)
            account = {
                **merged,
                "left": merged["left"] + char_width * account_index,
                "right": merged["left"] + char_width * (account_index + 2),
            }

        # 选中状态会在文字正下方显示一条较长的深色横线。
        left = max(0, round(account["left"] - width * 0.02))
        right = min(width, round(account["right"] + width * 0.02))
        top = max(0, round(account["bottom"] + 2))
        bottom = min(height, round(account["bottom"] + height * 0.035))
        underline_crop = np.asarray(
            screenshot.convert("L").crop((left, top, right, bottom))
        )
        if underline_crop.size:
            dark_ratio_by_row = (underline_crop < 100).mean(axis=1)
            underline_ratio = float(dark_ratio_by_row.max())
        else:
            underline_ratio = 0.0

        filter_names = {"不限", "小程序", "公众号", "服务号", "视频号"}
        visible_filters = sorted(
            {
                row["normalized"]
                for row in rows
                if row["normalized"] in filter_names
                and account["bottom"] < row["center_y"] < account["bottom"] + height * 0.13
            }
        )
        browser_title_confirmed = any(
            "账号" in row["normalized"] and "搜一搜" in row["normalized"]
            for row in rows
            if row["center_y"] < height * 0.07
        )
        # 新版搜一搜账号页不一定展示“不限/公众号/服务号”等二级筛选项，
        # 但会出现“搜索词-账号”的结果区标题或明确的“公众号”类型文字。
        account_result_confirmed = any(
            (
                row["normalized"].endswith("-账号")
                or row["normalized"] == "公众号"
            )
            and account["bottom"] < row["center_y"] < height * 0.55
            for row in rows
        )
        # 浏览器标签标题可能被截断，或被 OCR 识别成“搜”而不是“搜一搜”。
        # 它只能作为辅助证据；页面上的账号下划线、筛选项和结果区才是主判断依据。
        selected = (
            underline_ratio >= 0.45
            and (len(visible_filters) >= 3 or account_result_confirmed)
        )
        return {
            "selected": selected,
            "underline_ratio": round(underline_ratio, 4),
            "visible_account_filters": visible_filters,
            "browser_title_confirmed": browser_title_confirmed,
            "account_result_confirmed": account_result_confirmed,
            "reason": "" if selected else "账号下划线或账号结果证据未确认",
            "method": "opencv-underline-plus-account-result-evidence",
        }

    def validate_profile_header(self, screenshot: Image.Image, expected_name: str) -> dict[str, Any]:
        """用整页 OCR 身份和资料页结构确认账号。

        方法名为兼容既有调用方保留；顶部名称不再是唯一证据。搜索结果页和文章
        正文也可能出现公众号名称，因此必须同时满足资料页结构，才能判定匹配。
        """
        width, height = screenshot.size
        rows = self._rows(screenshot)
        header_rows = [
            row
            for row in rows
            # Win11 窗口移动、缩放和浏览器工具栏高度变化后，资料页名称可能
            # 落在原 65% 横向范围之外；扩大到左侧 85% 仍保留顶部区域约束。
            if row["center_y"] < height * 0.28 and row["center_x"] < width * 0.85
        ]
        matches = [
            row
            for row in rows
            if row["center_y"] > height * 0.055
            and row["center_y"] < height * 0.72
            and row["center_x"] < width * 0.90
            if _account_name_match(expected_name, row["text"])[0]
        ]
        action_terms = sorted(
            {
                row["normalized"]
                for row in rows
                if row["normalized"] in {"关注", "私信"}
                and row["center_y"] < height * 0.55
            }
        )
        expanded_navigation = WeChatProfileOCR._expand_navigation_terms(
            [row for row in rows if row["center_y"] < height * 0.60]
        )
        navigation_terms = sorted({term for _center_y, term in expanded_navigation})
        action_terms = sorted(
            {
                row["normalized"]
                for row in rows
                if row["normalized"] in {"关注", "私信"}
                and row["center_y"] < height * 0.55
            }
        )
        aligned_navigation_count = max(
            (
                len(
                    {
                        candidate_term
                        for candidate_y, candidate_term in expanded_navigation
                        if abs(candidate_y - anchor_y)
                        <= height * 0.035
                    }
                )
                for anchor_y, _anchor_term in expanded_navigation
            ),
            default=0,
        )
        identity_terms = sorted(
            {
                row["normalized"]
                for row in rows
                if row["normalized"] in {"公众号", "服务号"}
                and row["center_y"] < height * 0.60
            }
        )
        action_evidence = {"关注", "私信"}.issubset(action_terms)
        search_page_evidence = sorted(
            {
                row["normalized"]
                for row in rows
                if row["center_y"] < height * 0.55
                and (
                    row["normalized"] == "搜索"
                    or row["normalized"].endswith("-账号")
                    or row["normalized"].endswith("—账号")
                )
            }
        )
        empty_feed_marker = _rows_empty_feed_marker(rows, height)
        # Win11 公众号/服务号资料页在滚动、未关注以及不同账号类型下，可能不
        # 展示“关注/私信/公众号/服务号”。稳定的页面类型证据是同一导航行中
        # 出现至少两个内容标签；账号身份仍由下面的名称精确匹配单独确认。
        # 个别公众号的导航行会被 OCR 合并/误读成单个“文章”，此时“关注/私信”
        # 同时存在且没有搜一搜证据仍可确认资料页身份。
        structural_match = bool(
            not search_page_evidence
            and (
                aligned_navigation_count >= 2
                or (action_evidence and bool(navigation_terms))
            )
        )
        if not matches:
            return {
                "matched": False,
                "reason": "公众号资料页整屏未找到名称匹配",
                # 记录实际读到的顶部候选，便于区分 OCR 偏差、旧窗口残留和误点。
                "observed_header_candidates": [row["text"] for row in header_rows[:8]],
                "structural_terms": action_terms + navigation_terms + identity_terms,
                "profile_structure_found": structural_match,
                "aligned_navigation_count": aligned_navigation_count,
                "search_page_evidence": search_page_evidence,
            }
        if not structural_match:
            if matches and empty_feed_marker and not search_page_evidence:
                row = min(matches, key=lambda item: item["center_y"])
                _, name_match_method = _account_name_match(expected_name, row["text"])
                return {
                    "matched": True,
                    "name": row["text"],
                    "confidence": row["confidence"],
                    "method": f"rapidocr-profile-viewport-empty-feed-{name_match_method}",
                    "empty_feed_evidence": True,
                    "empty_feed_marker": empty_feed_marker,
                    "structural_terms": action_terms + navigation_terms + identity_terms,
                    "profile_structure_found": False,
                    "aligned_navigation_count": aligned_navigation_count,
                    "search_page_evidence": search_page_evidence,
                }
            return {
                "matched": False,
                "reason": (
                    "找到账号名称但当前仍是搜一搜页面"
                    if search_page_evidence
                    else "找到账号名称但缺少公众号资料页结构证据"
                ),
                "name_candidates": [row["text"] for row in matches[:5]],
                "structural_terms": action_terms + navigation_terms + identity_terms,
                "profile_structure_found": structural_match,
                "aligned_navigation_count": aligned_navigation_count,
                "search_page_evidence": search_page_evidence,
            }
        row = min(matches, key=lambda item: item["center_y"])
        _, name_match_method = _account_name_match(expected_name, row["text"])
        return {
            "matched": True,
            "name": row["text"],
            "confidence": row["confidence"],
            "method": f"rapidocr-profile-viewport-{name_match_method}",
            "structural_terms": action_terms + navigation_terms + identity_terms,
            "profile_structure_found": True,
            "aligned_navigation_count": aligned_navigation_count,
            "search_page_evidence": search_page_evidence,
        }

    def identify_profile_account(
        self,
        screenshot: Image.Image,
        expected_names: list[str],
    ) -> dict[str, Any]:
        """一次 OCR 识别资料页结构及其所属监听账号。

        热更新附着不能为十个账号分别对同一标签重复 OCR。本方法先只做一次
        ``_rows``，再将资料页头部候选与全部监听账号进行安全精确匹配。只有
        资料页结构和账号名称同时成立时才返回账号；其他资料页保留候选文本供
        日志和截图审计，不会被误登记或自动关闭。
        """
        width, height = screenshot.size
        rows = self._rows(screenshot)
        accounts = list(dict.fromkeys(name.strip() for name in expected_names if name.strip()))
        expanded_navigation = WeChatProfileOCR._expand_navigation_terms(
            [row for row in rows if row["center_y"] < height * 0.60]
        )
        navigation_terms = sorted({term for _center_y, term in expanded_navigation})
        action_terms = sorted(
            {
                row["normalized"]
                for row in rows
                if row["normalized"] in {"关注", "私信"}
                and row["center_y"] < height * 0.55
            }
        )
        aligned_navigation_count = max(
            (
                len(
                    {
                        candidate_term
                        for candidate_y, candidate_term in expanded_navigation
                        if abs(candidate_y - anchor_y)
                        <= height * 0.035
                    }
                )
                for anchor_y, _anchor_term in expanded_navigation
            ),
            default=0,
        )
        search_page_evidence = sorted(
            {
                row["normalized"]
                for row in rows
                if row["center_y"] < height * 0.55
                and (
                    row["normalized"] == "搜索"
                    or row["normalized"].endswith("-账号")
                    or row["normalized"].endswith("—账号")
                )
            }
        )
        empty_feed_marker = _rows_empty_feed_marker(rows, height)
        action_evidence = {"关注", "私信"}.issubset(action_terms)
        structural_match = bool(
            not search_page_evidence
            and (
                aligned_navigation_count >= 2
                or (action_evidence and bool(navigation_terms))
            )
        )
        header_rows = [
            row
            for row in rows
            if height * 0.055 < row["center_y"] < height * 0.30
            and row["center_x"] < width * 0.90
        ]
        ignored_header_terms = {
            "关注", "私信", "全部", "贴图", "文章", "视频号",
            "公众号", "服务号", "展开", "置顶", "今天", "昨天",
        }
        observed_header_candidates = [
            row["text"]
            for row in header_rows
            if row["normalized"] not in ignored_header_terms
        ][:12]

        matches: list[tuple[int, dict[str, Any], str, str]] = []
        for account_index, account in enumerate(accounts):
            for row in header_rows:
                matched, method = _account_name_match(account, row["text"])
                if matched:
                    matches.append((account_index, row, account, method))
        if (
            matches
            and not structural_match
            and not search_page_evidence
            and empty_feed_marker
        ):
            account_index, row, account, method = min(
                matches,
                key=lambda item: (item[1]["center_y"], item[0]),
            )
            return {
                "matched": True,
                "account": account,
                "name": row["text"],
                "confidence": row["confidence"],
                "method": f"rapidocr-profile-inventory-empty-feed-{method}",
                "empty_feed_evidence": True,
                "empty_feed_marker": empty_feed_marker,
                "observed_header_candidates": observed_header_candidates,
                "header_identity_visible": True,
                "structural_terms": navigation_terms,
                "profile_structure_found": False,
                "aligned_navigation_count": aligned_navigation_count,
                "search_page_evidence": search_page_evidence,
            }
        if not structural_match or not matches:
            return {
                "matched": False,
                "account": None,
                "name": None,
                "reason": (
                    "资料页结构成立但未识别到监听账号名称"
                    if structural_match
                    else "当前页面缺少资料页结构证据"
                ),
                "observed_header_candidates": observed_header_candidates,
                "header_identity_visible": bool(observed_header_candidates),
                "structural_terms": navigation_terms,
                "profile_structure_found": structural_match,
                "aligned_navigation_count": aligned_navigation_count,
                "search_page_evidence": search_page_evidence,
            }

        account_index, row, account, method = min(
            matches,
            key=lambda item: (item[1]["center_y"], item[0]),
        )
        return {
            "matched": True,
            "account": account,
            "name": row["text"],
            "confidence": row["confidence"],
            "method": f"rapidocr-profile-inventory-{method}",
            "observed_header_candidates": observed_header_candidates,
            "header_identity_visible": True,
            "structural_terms": navigation_terms,
            "profile_structure_found": True,
            "aligned_navigation_count": aligned_navigation_count,
            "search_page_evidence": search_page_evidence,
        }

    def inspect_profile_layout(self, screenshot: Image.Image) -> dict[str, Any]:
        """提取资料页结构证据，仅用于诊断和 Qwen-VL 触发，不替代账号校验。"""
        width, height = screenshot.size
        rows = self._rows(screenshot)
        action_terms = {
            row["normalized"]
            for row in rows
            if row["normalized"] in {"关注", "私信"}
            and row["center_y"] < height * 0.55
        }
        navigation_terms = {
            term
            for _center_y, term in WeChatProfileOCR._expand_navigation_terms(
                [row for row in rows if row["center_y"] < height * 0.55]
            )
        }
        terms = [
            term
            for term in ("全部", "贴图", "文章", "视频号", "关注", "私信")
            if term in navigation_terms or term in action_terms
        ]
        # 页面类型不依赖“关注/私信”等可选控件；至少两个内容导航项即可。
        # 最终账号身份仍必须由名称精确匹配确认。
        found = len(navigation_terms.intersection({"全部", "贴图", "文章", "视频号"})) >= 2
        return {
            "found": found,
            "terms": terms,
            "method": "rapidocr-profile-structural-evidence",
        }

    def inspect_profile_feed(self, screenshot: Image.Image) -> dict[str, Any]:
        """识别资料窗口中的时间分组、文章标题、阅读数和点赞数。"""
        started = time.perf_counter()
        width, height = screenshot.size
        rows = self._rows(screenshot)
        empty_feed_marker = _rows_empty_feed_marker(rows, height)
        labels = []
        label_ids: set[int] = set()
        metric_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            normalized = row["normalized"]
            combined = COMBINED_METRICS_RE.fullmatch(normalized)
            if combined:
                # OCR 偶尔会把标题末行和“阅读/赞”合成一个文本框，先拆开再参与分组。
                metric = dict(row)
                metric["normalized"] = f"阅读{combined.group(2)}赞{combined.group(3)}"
                metric["combined_title"] = combined.group(1)
                metric_rows.append(metric)
                row = dict(row)
                row["text"] = combined.group(1)
                row["normalized"] = combined.group(1)
                rows[index] = row
                normalized = row["normalized"]
            is_time_label = bool(TIME_LABEL_RE.fullmatch(normalized))
            # 新版微信常把“昨天 20:23”合并成一个时间标签，兼容这种形式。
            is_time_label = is_time_label or bool(
                re.fullmatch(r"(?:今天|昨天)\s+\d{1,2}:\d{2}", normalized)
            )
            is_section_label = normalized in NON_ARTICLE_SECTION_LABELS
            if is_time_label or is_section_label:
                # 翻页后日期标签可能已经靠近窗口顶部，不能只检查首屏下半区。
                # 顶部导航也有“贴图/视频号”，专区标签只接受内容区中的同名文本。
                minimum_y = height * (0.20 if is_section_label else 0.08)
                if row["center_y"] > minimum_y and row["left"] < width * 0.45:
                    label_ids.add(index)
                    labels.append(
                        {
                            "text": normalized,
                            "center_y_1000": round(row["center_y"] * 1000 / height),
                            "confidence": row["confidence"],
                        }
                    )
            if METRICS_RE.fullmatch(normalized):
                metric_rows.append(row)

        excluded = {
            "全部", "贴图", "文章", "视频号", "私信", "已关注",
            "AI开源项目", "AI研究前沿", "AI产业动态",
        }
        title_rows = []
        for index, row in enumerate(rows):
            normalized = row["normalized"]
            if index in label_ids or normalized in excluded or METRICS_RE.fullmatch(normalized):
                continue
            # 公众号头部的账号主体名不属于文章；它和封面里的文字都可能被自由文本
            # 回退分支误当成标题。真实文章标题优先由“阅读/赞”锚点反向定位。
            if row["center_y"] < height * 0.20:
                continue
            if _is_numeric_or_punctuation_only(normalized):
                continue
            if row["left"] < width * 0.15 or row["left"] > width * 0.62:
                continue
            # 资料页滚动后第一张卡片可能紧贴顶部，后续仍会通过时间分组约束是否采集。
            if not height * 0.08 <= row["center_y"] <= height * 0.97:
                continue
            # 短标题在有“阅读/赞”锚点时同样可能是真实文章，例如“图解政策”。
            # 此处只排除单字符噪声；无指标锚点的自由文本回退仍保持 5 字门槛。
            if len(normalized) < 2:
                continue
            title_rows.append(row)

        title_rows.sort(key=lambda row: (row["center_y"], row["left"]))

        # 公众号卡片的真实标题位于“阅读/赞”指标正上方；封面图内部可能包含
        # Logo、榜单、表格和海报文案，不能把所有 OCR 行都视为文章标题。
        # 优先以指标行为锚点反向寻找相邻标题，只在整页完全没有指标时才使用
        # 旧的自由文本分组作为兼容回退。
        anchored_groups: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
        for metric in sorted(metric_rows, key=lambda row: (row["center_y"], row["left"])):
            combined_title = str(metric.get("combined_title") or "").strip()
            if combined_title:
                title_row = dict(metric)
                title_row["text"] = combined_title
                title_row["normalized"] = _normalize(combined_title)
                anchored_groups.append(([title_row], metric))
                continue

            candidates = [
                row
                for row in title_rows
                if row["bottom"] <= metric["top"] + height * 0.008
                and 0 <= metric["top"] - row["bottom"] <= height * 0.12
                # 标题和指标左边缘基本对齐。适度放宽以兼容 OCR 框偏移，
                # 但不能跨到双列布局中的相邻卡片。
                and abs(row["left"] - metric["left"]) <= width * 0.12
                and metric["left"] - width * 0.05
                <= row["center_x"]
                <= metric["left"] + width * 0.36
            ]
            if not candidates:
                continue
            nearest = max(candidates, key=lambda row: (row["bottom"], row["confidence"]))
            group = [nearest]
            # 只取最靠近指标的一行。卡片标题被截断时，后续标题校验支持可靠的
            # 8 字以上前缀；向上盲目合并反而会吞入封面底部的表格或海报文字。
            anchored_groups.append((group, metric))

        if anchored_groups:
            articles = []
            seen_anchors: set[tuple[str, int, int]] = set()
            for group, metric in anchored_groups:
                title = "".join(row["text"] for row in group).strip()
                top = min(row["top"] for row in group)
                bottom = max(row["bottom"] for row in group)
                left = min(row["left"] for row in group)
                right = max(row["right"] for row in group)
                match = METRICS_RE.fullmatch(metric["normalized"])
                read_count = _number(match.group(1)) if match else None
                like_count = _number(match.group(2)) if match else None
                anchor_key = (_normalize(title), round(metric["center_x"]), round(metric["center_y"]))
                if not title or anchor_key in seen_anchors:
                    continue
                seen_anchors.add(anchor_key)
                articles.append(
                    {
                        "title": title,
                        "center_x_1000": round(((left + right) / 2) * 1000 / width),
                        "center_y_1000": round(((top + bottom) / 2) * 1000 / height),
                        "confidence": round(min(row["confidence"] for row in group), 4),
                        "list_read_count": read_count,
                        "list_like_count": like_count,
                    }
                )
            return {
                "time_labels": sorted(labels, key=lambda item: item["center_y_1000"]),
                "articles": articles,
                "recognition_method": "rapidocr-profile-feed-metric-anchored",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }

        if empty_feed_marker and not anchored_groups:
            return {
                "time_labels": [],
                "articles": [],
                "empty_feed_reason": "no_more_messages",
                "empty_feed_marker": empty_feed_marker,
                "recognition_method": "rapidocr-profile-feed-empty-message",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }

        groups: list[list[dict[str, Any]]] = []
        for row in title_rows:
            if len(row["normalized"]) < 5:
                continue
            if not groups:
                groups.append([row])
                continue
            previous = groups[-1][-1]
            gap = row["top"] - previous["bottom"]
            if gap <= height * 0.035 and abs(row["left"] - previous["left"]) <= width * 0.06:
                groups[-1].append(row)
            else:
                groups.append([row])

        articles = []
        for group in groups:
            title = "".join(row["text"] for row in group).strip()
            top = min(row["top"] for row in group)
            bottom = max(row["bottom"] for row in group)
            left = min(row["left"] for row in group)
            right = max(row["right"] for row in group)
            metric = next(
                (
                    row
                    for row in metric_rows
                    if bottom <= row["center_y"] <= bottom + height * 0.055
                ),
                None,
            )
            read_count = like_count = None
            if metric:
                match = METRICS_RE.fullmatch(metric["normalized"])
                if match:
                    read_count = _number(match.group(1))
                    like_count = _number(match.group(2))
            articles.append(
                {
                    "title": title,
                    "center_x_1000": round(((left + right) / 2) * 1000 / width),
                    "center_y_1000": round(((top + bottom) / 2) * 1000 / height),
                    "confidence": round(min(row["confidence"] for row in group), 4),
                    "list_read_count": read_count,
                    "list_like_count": like_count,
                }
            )

        return {
            "time_labels": sorted(labels, key=lambda item: item["center_y_1000"]),
            "articles": articles,
            "recognition_method": "rapidocr-profile-feed",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
