"""微信电脑版公众号视觉采集器。

默认仅截图和识别，不点击界面。只有显式传入 ``--live`` 才允许鼠标操作。
"""

from __future__ import annotations

import argparse
import ctypes
import difflib
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageGrab, ImageStat
from pymongo import MongoClient

from wechat_rpa.paths import PROJECT_ROOT
from wechat_rpa.settings import load_project_env


# 命令行直接启动采集器时也自动加载项目配置，不依赖 PowerShell 会话变量。
load_project_env()

from wechat_rpa.vision.qwen_client import QwenVisionClient, QwenVisionConfig
from wechat_rpa.vision.article_evidence import ArticleEvidenceOCR
from wechat_rpa.content.article_ingest import (
    append_local_exports,
    ingest,
    load_account_article_urls,
    load_cached_page,
    normalize_article_url,
    parse_page,
    parse_publish_time,
    shanghai_timezone,
)
from wechat_rpa.vision.interaction import InteractionOCR
from wechat_rpa.vision.feed import WeChatFeedOCR
from wechat_rpa.vision.wechat import WeChatOCR
from wechat_rpa.vision.profile import WeChatProfileOCR
from wechat_rpa.desktop.win11_adapter import Win11WeChatAdapter


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

RPA_DIR = PROJECT_ROOT
DEFAULT_OUTPUT_DIR = RPA_DIR / "output"
ACCOUNT_ALIASES_PATH = RPA_DIR / "config" / "account_aliases.json"
COPY_LINK_POSITION_CACHE_PATH = Path(
    os.getenv("RPA_COPY_LINK_CACHE_PATH", str(RPA_DIR / "config" / "ui_position_cache.json"))
)
FEED_OCR = WeChatFeedOCR()
INTERACTION_OCR = InteractionOCR()
PROFILE_OCR = WeChatProfileOCR()
ARTICLE_EVIDENCE_OCR = ArticleEvidenceOCR()
RUN_LOGGER = logging.getLogger("wechat_rpa")
WINDOW_LAYOUT_MODE = "auto"
# 仅在上一个账号完成闭环后启用的进程内热状态。首次启动或恢复流程仍走完整确认。
_SEARCH_WINDOW_HOT = False
# 多账号监听的进程内资料页缓存。缓存只保存当前进程中的 WindowInfo；
# HWND 和标签位置不跨微信/虚拟机重启复用，重启后必须重新搜索并验证。
_WATCH_PROFILE_CACHE: dict[str, "WindowInfo"] = {}
_WATCH_PROFILE_TRANSITIONS: dict[tuple[str, str], int] = {}
_WATCH_ACTIVE_PROFILE_ACCOUNT: str | None = None
_WATCH_PROFILE_SCAN_STATE: dict[str, dict[str, Any]] = {}
_WATCH_PROFILE_REBUILD_LOCKS: set[str] = set()
_SEARCH_SCAN_RECOVERED_PROFILE: dict[str, "WindowInfo"] = {}
# Win11 微信资料页和搜一搜共享 Chromium 标签栏。探测次数是从当前活动标签
# 开始的相对次数，不是绝对标签序号；96 只作为异常防死循环上限。正常失败
# 必须以“从同一搜一搜工作面出发并再次返回该工作面”证明完整绕行一圈，不能
# 再把固定次数用作“目标标签不存在”的事实依据。
WIN11_MAX_TAB_SCAN = 96


class ProfileTemporarilyUnverifiedError(RuntimeError):
    """资料页暂时无法确认，但尚未完成可靠整圈扫描，禁止重建。"""


def mark_watch_profile_active(account_name: str | None) -> None:
    """记录当前活动资料页；搜索或未知页面传入 ``None`` 使路由失效。"""
    global _WATCH_ACTIVE_PROFILE_ACCOUNT
    _WATCH_ACTIVE_PROFILE_ACCOUNT = account_name
# 微信 3.x/4.x 及其内置 Chromium 子进程可能使用不同可执行文件名。
# 普通 chrome.exe/msedge.exe 即使窗口标题恰好为“微信”，也绝不能进入自动化范围。
WECHAT_PROCESS_NAMES = frozenset(
    {
        "wechat.exe",
        "wechatappex.exe",
        "wechatbrowser.exe",
        "weixin.exe",
        "weixinappex.exe",
    }
)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _read_ui_position_cache() -> dict[str, Any]:
    """读取界面坐标缓存；损坏或不存在时按空缓存处理。"""
    try:
        raw = json.loads(COPY_LINK_POSITION_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": 1}
    return raw if isinstance(raw, dict) else {"version": 1}


def _write_ui_position_cache(payload: dict[str, Any]) -> None:
    """原子写入界面坐标缓存，避免进程退出时留下半个 JSON 文件。"""
    COPY_LINK_POSITION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = COPY_LINK_POSITION_CACHE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(COPY_LINK_POSITION_CACHE_PATH)


def _compatible_cached_position(key: str, rect: "Rect", dpi: int) -> dict[str, Any] | None:
    """读取与当前窗口尺寸、DPI 相容的归一化坐标。"""
    cached = _read_ui_position_cache().get(key)
    if not isinstance(cached, dict):
        return None
    try:
        x_1000 = int(cached["center_x_1000"])
        y_1000 = int(cached["center_y_1000"])
        cached_dpi = int(cached["dpi"])
        cached_width = int(cached["window_width"])
        cached_height = int(cached["window_height"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x_1000 <= 1000 and 0 <= y_1000 <= 1000):
        return None
    width_ratio = rect.width / max(cached_width, 1)
    height_ratio = rect.height / max(cached_height, 1)
    if abs(cached_dpi - dpi) > 24 or not (0.8 <= width_ratio <= 1.25) or not (0.8 <= height_ratio <= 1.25):
        return None
    return cached


def _save_ui_position(key: str, action: dict[str, Any], rect: "Rect", dpi: int, source: str) -> None:
    """保存一个已经通过后续行为验证的界面坐标，同时保留其他坐标。"""
    payload = _read_ui_position_cache()
    payload["version"] = 1
    payload[key] = {
        "center_x_1000": int(action["center_x_1000"]),
        "center_y_1000": int(action["center_y_1000"]),
        "dpi": dpi,
        "window_width": rect.width,
        "window_height": rect.height,
        "source": source,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_ui_position_cache(payload)


def _clear_ui_position(key: str, reason: str) -> None:
    """只删除指定坐标，避免一个控件失效时连带清除其他有效缓存。"""
    try:
        payload = _read_ui_position_cache()
        removed = payload.pop(key, None)
        if removed is not None:
            _write_ui_position_cache(payload)
    except OSError as exc:
        log_event("ui_position_cache_clear_failed", key=key, reason=reason, error=str(exc))
        return
    log_event("ui_position_cache_cleared", key=key, reason=reason)


def load_copy_link_position_cache(rect: "Rect", dpi: int) -> dict[str, Any] | None:
    """读取上一次验证成功的“复制链接”归一化坐标。

    缓存只提供一个候选区域，后续仍需用小区域 OCR 验证；窗口尺寸或 DPI
    变化过大时直接放弃缓存，避免把另一台电脑上的坐标当成当前坐标使用。
    """
    return _compatible_cached_position("copy_link", rect, dpi)


def save_copy_link_position_cache(action: dict[str, Any], rect: "Rect", dpi: int, source: str) -> None:
    """持久化已经通过剪贴板 URL 校验的菜单坐标。"""
    try:
        _save_ui_position("copy_link", action, rect, dpi, source)
    except OSError as exc:
        # 缓存不可写不能阻塞采集，当前文章仍然可以依靠本次 OCR 结果继续。
        log_event("copy_link_position_cache_write_failed", error=str(exc))


def clear_copy_link_position_cache(reason: str) -> None:
    """缓存失效后立即移除，避免下一篇文章再次落入同一个错误坐标。"""
    _clear_ui_position("copy_link", reason)


def load_menu_button_position_cache(rect: "Rect", dpi: int) -> dict[str, Any] | None:
    """读取上一次完成合法 URL 复制时使用的浏览器菜单按钮坐标。"""
    return _compatible_cached_position("menu_button", rect, dpi)


def save_menu_button_position_cache(action: dict[str, Any], rect: "Rect", dpi: int, source: str) -> None:
    """保存已由完整复制链接流程验证成功的浏览器菜单按钮坐标。"""
    try:
        _save_ui_position("menu_button", action, rect, dpi, source)
    except OSError as exc:
        log_event("menu_button_position_cache_write_failed", error=str(exc))


def clear_menu_button_position_cache(reason: str) -> None:
    """动态菜单按钮失效后只清除该按钮缓存。"""
    _clear_ui_position("menu_button", reason)


def validate_cached_copy_link_action(
    screenshot: Image.Image,
    cached: dict[str, Any],
) -> dict[str, Any]:
    """仅 OCR 缓存坐标附近的小区域，确认文字仍然是“复制链接”。"""
    width, height = screenshot.size
    center_x = width * int(cached["center_x_1000"]) / 1000
    center_y = height * int(cached["center_y_1000"]) / 1000
    # 让候选文字位于裁剪区域上半部，兼容 locate_copy_link_action 的菜单区域约束。
    left = max(0, round(center_x - width * 0.14))
    top = max(0, round(center_y - height * 0.05))
    right = min(width, round(center_x + width * 0.14))
    bottom = min(height, round(center_y + height * 0.13))
    if right - left < 20 or bottom - top < 20:
        return {"found": False, "reason": "缓存坐标附近区域过小"}
    region = screenshot.crop((left, top, right, bottom))
    action = PROFILE_OCR.locate_copy_link_action(region)
    if not action.get("found"):
        return {"found": False, "reason": str(action.get("reason") or "缓存区域未识别到复制链接")}
    region_width, region_height = region.size
    full_x = left + region_width * int(action["center_x_1000"]) / 1000
    full_y = top + region_height * int(action["center_y_1000"]) / 1000
    return {
        **action,
        "center_x_1000": round(full_x * 1000 / width),
        "center_y_1000": round(full_y * 1000 / height),
        "method": "cached-position-roi-rapidocr",
    }


def validate_cached_menu_button_action(
    screenshot: Image.Image,
    cached: dict[str, Any],
) -> dict[str, Any]:
    """校验已成功复制过公众号 URL 的浏览器菜单坐标。

    缓存本身已经经过“复制链接 + 合法公众号 URL”验证。当前页面可能还包含
    文章自身的三点按钮，因此实时识别到另一个三点时，不能反过来淘汰可靠缓存；
    真正的失效由后续菜单文字与剪贴板 URL 双重校验确认。
    """
    try:
        cached_x = int(cached["center_x_1000"])
        cached_y = int(cached["center_y_1000"])
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "菜单按钮缓存坐标格式错误"}
    if not (450 <= cached_x <= 980 and 0 <= cached_y <= 120):
        return {"found": False, "reason": "菜单按钮缓存坐标不在浏览器顶部工具栏内"}

    detected = PROFILE_OCR.locate_browser_menu_button(screenshot)
    if detected.get("found"):
        try:
            distance_x = abs(int(detected["center_x_1000"]) - cached_x)
            distance_y = abs(int(detected["center_y_1000"]) - cached_y)
        except (KeyError, TypeError, ValueError):
            distance_x = distance_y = 1000
        if distance_x <= 55 and distance_y <= 35:
            return {**detected, "method": "cached-menu-button-opencv"}

    # OCR/OpenCV 没找到缓存附近的按钮，或找到了网页里的另一个三点。
    # 先按已验证缓存尝试；若菜单文字/URL 校验失败，调用方会清缓存并降级识别。
    return {
        "found": True,
        "center_x_1000": cached_x,
        "center_y_1000": cached_y,
        "confidence": float(cached.get("confidence") or 1.0),
        "method": "cached-menu-button-verified-position",
        "live_detection_found": bool(detected.get("found")),
    }


def normalize_qwen_copy_link_action(result: dict[str, Any]) -> dict[str, Any]:
    """把 Qwen-VL 返回值收紧为可点击的“复制链接”动作。"""
    if not result.get("found") or str(result.get("label") or "").replace(" ", "") != "复制链接":
        return {"found": False, "reason": "Qwen-VL 未确认精确的复制链接菜单项"}
    try:
        x_1000 = int(result["center_x_1000"])
        y_1000 = int(result["center_y_1000"])
        confidence = float(result.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "Qwen-VL 返回坐标格式错误"}
    if not (0 <= x_1000 <= 1000 and 0 <= y_1000 <= 1000) or confidence < 0.75:
        return {"found": False, "reason": "Qwen-VL 坐标越界或置信度不足"}
    return {
        "found": True,
        "text": "复制链接",
        "center_x_1000": x_1000,
        "center_y_1000": y_1000,
        "confidence": confidence,
        "method": "qwen-vl-copy-link-fallback",
    }


def normalize_qwen_menu_button_action(result: dict[str, Any]) -> dict[str, Any]:
    """只接受 Qwen-VL 明确认出的浏览器标题栏三点菜单按钮。"""
    label = str(result.get("label") or "").replace(" ", "")
    if not result.get("found") or label not in {"...", "…", "⋯", "更多", "三点菜单"}:
        return {"found": False, "reason": "Qwen-VL 未确认浏览器三点菜单按钮"}
    try:
        x_1000 = int(result["center_x_1000"])
        y_1000 = int(result["center_y_1000"])
        confidence = float(result.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "Qwen-VL 菜单按钮坐标格式错误"}
    # 菜单按钮必须位于文章窗口标题栏右侧，防止选择正文或页面内的省略号。
    if not (450 <= x_1000 <= 950 and 0 <= y_1000 <= 140) or confidence < 0.80:
        return {"found": False, "reason": "Qwen-VL 菜单按钮位置越界或置信度不足"}
    return {
        "found": True,
        "center_x_1000": x_1000,
        "center_y_1000": y_1000,
        "confidence": confidence,
        "method": "qwen-vl-browser-menu-button",
    }


def normalize_qwen_account_tab_action(
    result: dict[str, Any], *, require_selected: bool = False
) -> dict[str, Any]:
    """只接受搜一搜顶部一级“账号”分类的高置信度模型坐标。"""
    label = "".join(
        unicodedata.normalize("NFKC", str(result.get("label") or "")).split()
    )
    if not result.get("found") or label != "账号":
        return {"found": False, "reason": "Qwen-VL 未确认顶部一级账号分类"}
    try:
        raw_x = result["center_x_1000"]
        raw_y = result["center_y_1000"]
        if isinstance(raw_x, bool) or isinstance(raw_y, bool):
            raise TypeError("布尔值不是有效坐标")
        x_1000 = int(round(float(raw_x)))
        y_1000 = int(round(float(raw_y)))
        confidence = float(result.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "Qwen-VL 账号分类坐标格式错误"}
    # 一级分类必须位于搜索框下方、结果区上方的左半屏导航栏。该约束会排除
    # “公众号名 - 账号”结果标题和卡片内的账号类型文字。
    if not (80 <= x_1000 <= 600 and 70 <= y_1000 <= 300) or confidence < 0.80:
        return {"found": False, "reason": "Qwen-VL 账号分类位置越界或置信度不足"}
    selected = result.get("selected") is True
    if require_selected and not selected:
        return {"found": False, "reason": "Qwen-VL 未确认账号分类已选中"}
    return {
        "found": True,
        "selected": selected,
        "label": "账号",
        "center_x_1000": x_1000,
        "center_y_1000": y_1000,
        "confidence": confidence,
        "method": "qwen-vl-sogou-account-tab",
    }


def resolve_search_account_name(account_name: str) -> str:
    """返回搜一搜使用的名称；别名只改变检索词，不改变 MongoDB 中的来源账号名。"""
    try:
        raw = json.loads(ACCOUNT_ALIASES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return account_name
    except (OSError, json.JSONDecodeError) as exc:
        # 配置损坏时继续使用原名，避免一个别名配置阻断全部账号采集。
        log_event("account_alias_config_ignored", error=str(exc))
        return account_name
    if not isinstance(raw, dict):
        log_event("account_alias_config_ignored", error="根节点必须是 JSON 对象")
        return account_name
    alias = raw.get(account_name)
    return alias.strip() if isinstance(alias, str) and alias.strip() else account_name


def configure_run_logging(output_dir: Path) -> Path:
    """同时记录控制台和 UTF-8 文件日志，便于还原每一次界面决策。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"
    RUN_LOGGER.setLevel(logging.INFO)
    RUN_LOGGER.handlers.clear()
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    RUN_LOGGER.addHandler(file_handler)
    RUN_LOGGER.addHandler(stream_handler)
    return log_path


def log_event(event: str, **details: Any) -> None:
    """使用单行 JSON 记录事件，既方便人工查看，也方便后续程序统计。"""
    payload = {"event": event, **details}
    RUN_LOGGER.info(json.dumps(payload, ensure_ascii=False, default=str))


def source_fingerprint() -> str:
    """生成本次运行实际加载的核心源码指纹，便于 VM 冒烟测试确认版本。"""
    digest = hashlib.sha256()
    for path in (
        Path(__file__),
        RPA_DIR / "src" / "wechat_rpa" / "vision" / "profile.py",
        RPA_DIR / "src" / "wechat_rpa" / "desktop" / "win11_adapter.py",
    ):
        try:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            digest.update(f"missing:{path.name}".encode("utf-8"))
    return digest.hexdigest()[:16]
# 必须在首次读取窗口坐标前启用 DPI 感知，否则 150% 缩放下截图与点击坐标不一致。
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    rect: Rect
    process_name: str = ""
    page_kind: str = ""
    empty_article_profile: bool = False


@dataclass(frozen=True)
class CropRegion:
    left_ratio: float
    top_ratio: float
    right_ratio: float
    bottom_ratio: float

    def pixel_box(self, image: Image.Image) -> tuple[int, int, int, int]:
        width, height = image.size
        return (
            round(width * self.left_ratio),
            round(height * self.top_ratio),
            round(width * self.right_ratio),
            round(height * self.bottom_ratio),
        )


def window_process_name(hwnd: int) -> str:
    """返回窗口所属进程名；无法确认时返回空串并按非微信窗口处理。"""
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return ""
    process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value
    )
    if not process:
        return ""
    try:
        size = wintypes.DWORD(32768)
        executable = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, executable, ctypes.byref(size)
        ):
            return ""
        return Path(executable.value).name.lower()
    finally:
        kernel32.CloseHandle(process)


def is_wechat_owned_window(hwnd: int) -> bool:
    """只信任明确属于微信进程的窗口，未知归属一律安全拒绝。"""
    return window_process_name(hwnd) in WECHAT_PROCESS_NAMES


def enumerate_wechat_windows() -> list[WindowInfo]:
    """枚举微信主窗口、公众号消息窗口和文章浏览器窗口。"""
    windows: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        class_name = class_buffer.value
        # 微信不同版本会改变 Qt/Chromium 窗口类名中的版本号或末尾序号。
        is_qt_window = class_name.startswith("Qt") and class_name.endswith("QWindowIcon")
        is_chrome_window = class_name.startswith("Chrome_WidgetWin_")
        if not (is_qt_window or is_chrome_window):
            return True
        process_name = window_process_name(hwnd)
        if process_name not in WECHAT_PROCESS_NAMES:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        raw = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            return True
        rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
        if rect.width > 500 and rect.height > 500:
            windows.append(
                WindowInfo(
                    hwnd,
                    title_buffer.value,
                    class_buffer.value,
                    rect,
                    process_name,
                )
            )
        return True

    user32.EnumWindows(callback, 0)
    return windows


def find_search_window() -> WindowInfo:
    candidates = [
        item for item in enumerate_wechat_windows()
        if item.class_name.startswith("Qt")
        and item.class_name.endswith("QWindowIcon")
        and item.title.strip() == "微信"
    ]
    if not candidates:
        raise RuntimeError("没有找到微信服务号搜索主窗口")
    return max(candidates, key=lambda item: item.rect.width * item.rect.height)


def is_sogou_search_window(window: WindowInfo) -> bool:
    """判断窗口是否为搜一搜浏览器，兼容新版“公众号名 - 公众号搜一搜”标题。"""
    title = window.title.strip()
    return (
        window.process_name in WECHAT_PROCESS_NAMES
        and window.class_name.startswith("Chrome_WidgetWin_")
        and (
            title == "微信" or "搜一搜" in title
        )
    )


def find_sogou_search_window(
    excluded_hwnds: set[int] | frozenset[int] | None = None,
) -> WindowInfo:
    """查找微信搜一搜浏览器窗口；文章会在该窗口的新标签页中打开。"""
    candidates = [
        item for item in enumerate_wechat_windows()
        if is_sogou_search_window(item)
        and item.hwnd not in (excluded_hwnds or set())
    ]
    if not candidates:
        raise RuntimeError("没有找到微信搜一搜窗口，请先在微信中打开搜一搜")
    # 新版窗口标题会直接包含“搜一搜”，优先级高于旧版仅显示“微信”的兼容候选。
    return max(
        candidates,
        key=lambda item: (
            int("搜一搜" in item.title),
            item.rect.width * item.rect.height,
        ),
    )


def find_profile_pool_browser_window(profile_pool_state: dict[str, Any]) -> WindowInfo:
    """热更新时优先找搜一搜窗口，找不到则回退到预热阶段登记的 HWND。"""
    try:
        return find_sogou_search_window()
    except RuntimeError as exc:
        known_hwnds = {
            int(item.get("hwnd") or 0)
            for item in (profile_pool_state.get("profile_registry") or {}).values()
            if int(item.get("hwnd") or 0) > 0
        }
        candidates = [
            item for item in enumerate_wechat_windows() if item.hwnd in known_hwnds
        ]
        if candidates:
            candidate = max(
                candidates,
                key=lambda item: item.rect.width * item.rect.height,
            )
            log_event(
                "watch_profile_pool_browser_registered_hwnd_reused",
                hwnd=candidate.hwnd,
                title=candidate.title,
                reason=str(exc),
            )
            return candidate
        raise


def open_sogou_from_wechat_main(
    account_name: str,
    *,
    excluded_hwnds: set[int] | frozenset[int] | None = None,
) -> WindowInfo:
    """搜一搜窗口缺失时，从已登录的微信主窗口自动恢复。"""
    # 主窗口可能是 Qt，也可能是新版 Chromium 窗口，统一走管理窗口探测。
    main_hwnd, main_rect = find_wechat_manager_window()
    main_window = WindowInfo(
        main_hwnd,
        "微信",
        "Chrome_WidgetWin_0",
        main_rect,
        window_process_name(main_hwnd),
    )
    activate_window(main_window.hwnd)
    screenshot = capture_window(main_window.rect)
    # Win11 新版微信主界面左上角是灰色全局搜索框，不包含搜一搜网页的绿色
    # “搜索”按钮；这里必须使用主界面专用定位，不能复用搜一搜页面定位规则。
    search_box = PROFILE_OCR.locate_wechat_main_search_box(screenshot)
    if not search_box.get("found"):
        # OCR 失败时使用新版 Win11 主界面布局坐标兜底：搜索框中心约为
        # x=18.5%、y=8.0%。点击后按回车提交“搜一搜”。
        search_box = {
            "found": True,
            "center_x_1000": 185,
            "center_y_1000": 80,
            "confidence": 0.60,
            "method": "win11-main-search-bar-layout-v1-fallback",
        }
        log_event("wechat_main_search_box_fallback", account=account_name)
    # 先打开微信内置的“搜一搜”浏览器，具体公众号名称由后续网页流程输入。
    set_clipboard_text("搜一搜")
    click(
        main_window.rect.left
        + round(main_window.rect.width * int(search_box["center_x_1000"]) / 1000),
        main_window.rect.top
        + round(main_window.rect.height * int(search_box["center_y_1000"]) / 1000),
    )
    press_ctrl_a()
    press_ctrl_v()
    if "button_x_1000" in search_box:
        click(
            main_window.rect.left
            + round(main_window.rect.width * int(search_box["button_x_1000"]) / 1000),
            main_window.rect.top
            + round(main_window.rect.height * int(search_box["button_y_1000"]) / 1000),
        )
    else:
        # 微信主窗口的候选下拉框第一项就是“搜一搜”。优先用键盘确认，
        # 后面再由窗口探测结果决定是否需要鼠标点击兜底。
        press_enter()
    log_event(
        "sogou_recovery_submitted",
        account=account_name,
        query="搜一搜",
        source="wechat-main-window",
    )
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            window = find_sogou_search_window(excluded_hwnds)
            log_event("sogou_recovery_succeeded", account=account_name, hwnd=window.hwnd)
            return window
        except RuntimeError:
            continue

    # 某些微信版本回车只关闭候选框，不会打开搜一搜；重新聚焦搜索框，
    # 用“向下+回车”明确选中第一条候选项，再等待浏览器窗口出现。
    activate_window(main_window.hwnd)
    click(
        main_window.rect.left
        + round(main_window.rect.width * int(search_box["center_x_1000"]) / 1000),
        main_window.rect.top
        + round(main_window.rect.height * int(search_box["center_y_1000"]) / 1000),
    )
    press_ctrl_a()
    press_ctrl_v()
    press_down()
    press_enter()
    log_event("sogou_recovery_keyboard_fallback", account=account_name, query="搜一搜")
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            window = find_sogou_search_window(excluded_hwnds)
            log_event("sogou_recovery_succeeded", account=account_name, hwnd=window.hwnd, method="down-enter")
            return window
        except RuntimeError:
            continue
    raise RuntimeError("已从微信主窗口提交搜索，但未出现搜一搜浏览器窗口")


def recreate_sogou_search_window(
    stale_window: WindowInfo,
    account_name: str,
    reason: str,
) -> WindowInfo:
    """无损新建搜一搜窗口；旧窗口无论是否失效都不得由恢复流程关闭。"""
    log_event(
        "search_page_recovery_started",
        account=account_name,
        reason=reason,
        stale_window={
            "hwnd": stale_window.hwnd,
            "title": stale_window.title,
            "class_name": stale_window.class_name,
        },
        action="preserve_stale_window_and_open_new",
    )
    # 页面 OCR 失败不能证明窗口可以安全销毁。排除旧 HWND，只接受新出现的微信窗口；
    # 若微信复用旧窗口，则让任务失败并保留用户现场，等待人工处理。
    recovered = open_sogou_from_wechat_main(
        account_name,
        excluded_hwnds={stale_window.hwnd},
    )
    recovered = arrange_automation_window(recovered, "browser")
    activate_window(recovered.hwnd)
    # 新窗口通常只有一个标签；不再依赖 Ctrl+1，也不尝试调整标签顺序。
    # 后续由页面 OCR 确认当前标签，避免快捷键失效时把页面切走。
    time.sleep(0.3)
    log_event(
        "search_page_recovery_finished",
        account=account_name,
        recovered_hwnd=recovered.hwnd,
    )
    return recovered


def find_official_profile_window(
    expected_name: str | None = None,
    *,
    excluded_hwnds: set[int] | frozenset[int] | None = None,
    search_window: WindowInfo | None = None,
) -> WindowInfo:
    """按窗口或活动标签内容识别公众号资料页，不依赖微信版本提供的窗口标题。"""
    excluded = excluded_hwnds or set()

    # 新版微信把公众号资料页打开为搜一搜浏览器中的新标签；顶层窗口仍是
    # Chrome_WidgetWin_0 且标题仍为“微信”。必须先检查传入的搜一搜窗口当前
    # 活动标签，再检查独立的 Qt/Chromium 资料窗口。
    if expected_name and search_window is not None:
        try:
            validation = PROFILE_OCR.validate_profile_header(
                capture_window(search_window.rect), expected_name
            )
        except Exception:
            validation = {"matched": False}
        if validation.get("matched"):
            return WindowInfo(
                search_window.hwnd,
                search_window.title,
                search_window.class_name,
                search_window.rect,
                search_window.process_name,
                "embedded_profile_tab",
            )

    candidates = [
        item for item in enumerate_wechat_windows()
        if (
            item.class_name.startswith("Chrome_WidgetWin_")
            or (item.class_name.startswith("Qt") and item.class_name.endswith("QWindowIcon"))
        )
        and item.hwnd not in excluded
        # “公众号名 - 公众号搜一搜”是左侧搜索浏览器，不是右侧公众号资料页。
        and not is_sogou_search_window(item)
    ]
    if not candidates:
        raise RuntimeError("没有找到微信公众号资料窗口")

    if expected_name:
        matched: list[WindowInfo] = []
        for candidate in candidates:
            try:
                validation = PROFILE_OCR.validate_profile_header(
                    capture_window(candidate.rect), expected_name
                )
            except Exception:
                continue
            if validation.get("matched"):
                matched.append(candidate)
        if not matched:
            raise RuntimeError(
                f"没有找到名称匹配的微信公众号资料窗口：{expected_name}"
            )
        return max(matched, key=lambda item: item.rect.width * item.rect.height)

    # 兼容旧调用方：没有目标名称时仍只接受显式标题线索，避免把微信主窗口误当资料页。
    titled = [item for item in candidates if "公众号" in item.title.strip()]
    if not titled:
        raise RuntimeError("没有找到微信公众号资料窗口")
    return max(titled, key=lambda item: item.rect.width * item.rect.height)


def find_account_message_window(account_name: str) -> WindowInfo:
    expected = normalize_title(account_name)
    candidates = [
        item for item in enumerate_wechat_windows()
        if item.class_name.startswith("Qt")
        and item.class_name.endswith("QWindowIcon")
        and normalize_title(item.title) == expected
    ]
    if not candidates:
        raise RuntimeError(f"没有找到公众号消息窗口：{account_name}")
    return max(candidates, key=lambda item: item.rect.width * item.rect.height)


def close_window(hwnd: int, timeout_seconds: float = 3.0) -> None:
    """只关闭当前仍明确属于微信进程的窗口句柄。"""
    process_name = window_process_name(hwnd)
    if process_name not in WECHAT_PROCESS_NAMES:
        raise RuntimeError(
            "拒绝关闭非微信窗口："
            f"hwnd={hwnd}, process={process_name or 'unknown'}"
        )
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    deadline = time.time() + timeout_seconds
    while time.time() < deadline and user32.IsWindow(hwnd):
        time.sleep(0.1)


def normalized_bbox_to_pixels(values: list[int], image: Image.Image) -> tuple[int, int, int, int]:
    if len(values) != 4 or any(not 0 <= int(value) <= 1000 for value in values):
        raise ValueError(f"模型返回了无效区域：{values}")
    width, height = image.size
    left, top, right, bottom = (int(value) for value in values)
    box = (
        round(width * left / 1000),
        round(height * top / 1000),
        round(width * right / 1000),
        round(height * bottom / 1000),
    )
    if box[2] - box[0] < 120 or box[3] - box[1] < 250:
        raise ValueError(f"模型定位区域过小：{box}")
    return box


def find_wechat_manager_window() -> tuple[int, Rect]:
    candidates: list[tuple[int, str, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        is_qt_window = class_name.value.startswith("Qt") and class_name.value.endswith("QWindowIcon")
        is_chrome_window = class_name.value.startswith("Chrome_WidgetWin_")
        if title.value.strip() != "微信" or not (is_qt_window or is_chrome_window):
            return True
        if not is_wechat_owned_window(hwnd):
            return True
        raw = wintypes.RECT()
        initial_area = 0
        if user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            initial_area = max(0, raw.right - raw.left) * max(0, raw.bottom - raw.top)
        # 最小化窗口的当前矩形可能非常小，不能在恢复前用尺寸把它过滤掉。
        candidates.append((hwnd, class_name.value, initial_area))
        return True

    user32.EnumWindows(callback, 0)
    # 传统 Qt 主窗口优先；新版 Chromium 微信则按恢复前面积排序。
    candidates.sort(
        key=lambda item: (item[1].startswith("Qt"), item[2]),
        reverse=True,
    )
    for hwnd, class_name, _initial_area in candidates:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.15)
        raw = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            continue
        rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
        if rect.width > 700 and rect.height > 600:
            log_event(
                "wechat_manager_window_recovered",
                hwnd=hwnd,
                class_name=class_name,
                width=rect.width,
                height=rect.height,
            )
            return hwnd, rect
    raise RuntimeError("没有找到已打开的微信公众号管理窗口（已尝试恢复最小化窗口）")


def find_article_window() -> tuple[int, Rect]:
    candidates: list[tuple[int, Rect, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(64)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 64)
        user32.GetClassNameW(hwnd, class_name, 256)
        if title.value.strip() != "微信" or not class_name.value.startswith("Chrome_WidgetWin_"):
            return True
        if not is_wechat_owned_window(hwnd):
            return True
        raw = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
            if rect.width > 700 and rect.height > 600:
                candidates.append((hwnd, rect, rect.width * rect.height))
        return True

    user32.EnumWindows(callback, 0)
    if not candidates:
        raise RuntimeError("没有找到已打开的微信文章窗口")
    hwnd, rect, _ = max(candidates, key=lambda item: item[2])
    return hwnd, rect


def capture_window(rect: Rect) -> Image.Image:
    # ImageGrab 只读屏幕像素；窗口不能被其他窗口遮挡。
    return ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)


def activate_window(hwnd: int, max_settle_seconds: float = 0.8) -> None:
    # 截图前恢复并置前，避免文章窗口或其他应用遮挡公众号列表。
    # 不再无条件睡满 0.8 秒：窗口已经处于前台时快速返回，只有 Windows
    # 尚未完成置前时才轮询等待，保留原有最大等待上限。
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + max(0.0, max_settle_seconds)
    while time.monotonic() < deadline:
        try:
            if int(user32.GetForegroundWindow()) == int(hwnd):
                time.sleep(min(0.08, max(0.0, deadline - time.monotonic())))
                return
        except Exception:
            # 某些测试替身或旧版 Windows API 不提供前台句柄查询，回退到短等待。
            break
        time.sleep(0.04)
    if max_settle_seconds > 0:
        time.sleep(0.05)


def _tab_switch_difference(before: Image.Image, after: Image.Image) -> float:
    """比较浏览器标签栏和页面主体，判断 Ctrl+Tab 是否真的切换了标签。"""
    if before.size != after.size:
        return 255.0
    width, height = before.size
    # 标签栏变化最直接，同时保留少量页面区域来区分两个标题相近的页面。
    crop_height = max(1, round(height * 0.32))
    difference = ImageChops.difference(
        before.crop((0, 0, width, crop_height)).convert("L"),
        after.crop((0, 0, width, crop_height)).convert("L"),
    )
    return float(ImageStat.Stat(difference).mean[0])


def wait_for_visual_change(
    rect: Rect,
    before: Image.Image,
    *,
    timeout_seconds: float = 2.5,
    threshold: float = 3.0,
) -> float:
    """等待标签或页面发生可见变化，避免导航后固定睡眠。"""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    latest_difference = 0.0
    while time.monotonic() < deadline:
        after = capture_window(rect)
        latest_difference = _tab_switch_difference(before, after)
        if latest_difference >= threshold:
            break
        time.sleep(0.08)
    return latest_difference


def wait_for_stable_frames(
    rect: Rect,
    *,
    timeout_seconds: float = 2.8,
    interval_seconds: float = 0.22,
    threshold: float = 1.6,
) -> Image.Image:
    """等待连续两帧基本稳定并返回后一帧。

    标签切换、刷新和 Ctrl+Home 后都可能短暂出现空白或加载动画。这里用整页
    灰度差异确认两帧稳定；到达上限时返回最后一帧，由 OCR 决定是否属于加载
    中间态，不能仅因等待超时就宣称标签不存在。
    """
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    previous = capture_window(rect)
    while time.monotonic() < deadline:
        time.sleep(max(0.05, interval_seconds))
        current = capture_window(rect)
        if previous.size == current.size:
            difference = ImageChops.difference(
                previous.convert("L"), current.convert("L")
            )
            if float(ImageStat.Stat(difference).mean[0]) <= threshold:
                return current
        previous = current
    return previous


def same_sogou_search_workspace(first: Image.Image, current: Image.Image) -> bool:
    """确认两帧属于同一个搜一搜工作面，而非仅仅都是搜索类页面。"""
    if not _inspect_sogou_search_results(first).get("found"):
        return False
    if not _inspect_sogou_search_results(current).get("found"):
        return False
    # 查询词和顶部导航集中在窗口上部；主体动态内容不参与绕圈判断。
    return _tab_switch_difference(first, current) <= 10.0


def _inspect_sogou_search_results(screenshot: Image.Image) -> dict[str, Any]:
    """验证搜一搜结果页或首页，避免关闭资料页后把首页误判为失效标签。"""
    search_box = PROFILE_OCR.locate_search_box(screenshot)
    account_tab = PROFILE_OCR.locate_account_tab(screenshot)
    search_home = PROFILE_OCR.locate_search_home(screenshot)
    return {
        "found": bool(
            search_box.get("found")
            and (account_tab.get("found") or search_home.get("found"))
        ),
        "search_box": search_box,
        "account_tab": account_tab,
        "search_home": search_home,
    }


def find_and_pin_search_tab(
    search_window: WindowInfo,
    account_name: str,
    *,
    max_tabs: int = WIN11_MAX_TAB_SCAN,
    stop_on_target_profile: bool = True,
) -> bool:
    """遍历现有标签找到真正的搜一搜页，并停留在已确认的当前标签。

    函数名保留是为了兼容已有调用方，但 Win11 微信环境不再尝试使用
    Ctrl+Shift+PageUp 移动标签。该快捷键在当前键盘/微信组合下不可可靠验证，
    强行发送会产生“已归位”的假成功，随后可能把资料页或文章页误当成搜索页。
    """
    activate_window(search_window.hwnd)
    for index in range(max_tabs):
        screenshot = wait_for_stable_frames(search_window.rect)
        profile_validation = PROFILE_OCR.validate_profile_header(
            screenshot, account_name
        )
        if profile_validation.get("profile_structure_found") and not profile_validation.get("matched"):
            press_ctrl_home()
            screenshot = wait_for_stable_frames(search_window.rect)
            profile_validation = PROFILE_OCR.validate_profile_header(
                screenshot, account_name
            )
        if profile_validation.get("matched") and stop_on_target_profile:
            recovered = WindowInfo(
                search_window.hwnd,
                search_window.title,
                search_window.class_name,
                search_window.rect,
                search_window.process_name,
                "embedded_profile_tab",
            )
            _SEARCH_SCAN_RECOVERED_PROFILE[account_name] = recovered
            _WATCH_PROFILE_CACHE[account_name] = recovered
            mark_watch_profile_active(account_name)
            log_event(
                "search_recovery_target_profile_reused",
                account=account_name,
                tab_index=index + 1,
                validation=profile_validation,
                action="cancel_search_rebuild",
            )
            return False
        evidence = _inspect_sogou_search_results(screenshot)
        log_event(
            "sogou_search_tab_probe",
            account=account_name,
            tab_index=index + 1,
            found=bool(evidence["found"]),
            search_box_found=bool(evidence["search_box"].get("found")),
            account_tab_found=bool(evidence["account_tab"].get("found")),
        )
        if evidence["found"]:
            log_event(
                "sogou_search_tab_selected",
                account=account_name,
                observed_tab_index=index + 1,
                strategy="preserve-current-tab",
                tab_reorder="disabled",
            )
            return True
        activate_window(search_window.hwnd)
        press_ctrl_tab()
        time.sleep(0.35)
    log_event(
        "sogou_search_tab_not_found",
        account=account_name,
        inspected_tabs=max_tabs,
    )
    return False


def deduplicate_account_profile_tabs(
    browser_window: WindowInfo,
    account_name: str,
    *,
    max_tabs: int = WIN11_MAX_TAB_SCAN,
) -> dict[str, Any]:
    """从搜一搜工作面绕行一圈，只关闭同账号的精确重复资料页。"""
    if not find_and_pin_search_tab(
        browser_window,
        account_name,
        max_tabs=max_tabs,
        stop_on_target_profile=False,
    ):
        result = {
            "completed_cycle": False,
            "primary_found": False,
            "duplicates_closed": 0,
            "reason": "confirmed_search_workspace_not_found",
        }
        log_event("profile_account_dedup_skipped", account=account_name, **result)
        return result

    search_anchor = wait_for_stable_frames(browser_window.rect)
    primary_found = False
    duplicates_closed = 0
    press_ctrl_tab()
    time.sleep(0.35)
    for probe_index in range(max_tabs):
        screenshot = wait_for_stable_frames(browser_window.rect)
        if (
            _inspect_sogou_search_results(screenshot).get("found")
            and same_sogou_search_workspace(search_anchor, screenshot)
        ):
            result = {
                "completed_cycle": True,
                "primary_found": primary_found,
                "duplicates_closed": duplicates_closed,
                "reason": "returned_to_confirmed_search_workspace",
            }
            mark_watch_profile_active(None)
            log_event("profile_account_dedup_finished", account=account_name, **result)
            return result

        validation = PROFILE_OCR.validate_profile_header(screenshot, account_name)
        if validation.get("profile_structure_found") and not validation.get("matched"):
            press_ctrl_home()
            screenshot = wait_for_stable_frames(browser_window.rect)
            validation = PROFILE_OCR.validate_profile_header(screenshot, account_name)
        if validation.get("matched"):
            if not primary_found:
                primary_found = True
                _WATCH_PROFILE_CACHE[account_name] = WindowInfo(
                    browser_window.hwnd,
                    browser_window.title,
                    browser_window.class_name,
                    browser_window.rect,
                    browser_window.process_name,
                    "embedded_profile_tab",
                )
                log_event(
                    "profile_account_primary_selected",
                    account=account_name,
                    probe_index=probe_index + 1,
                    validation=validation,
                )
            else:
                # validate_profile_header 已同时证明：名称精确匹配、资料页结构
                # 成立、不是搜一搜页。文章页不具备该导航结构，因此不会被关闭。
                press_ctrl_w()
                duplicates_closed += 1
                time.sleep(0.35)
                log_event(
                    "profile_account_duplicate_closed",
                    account=account_name,
                    probe_index=probe_index + 1,
                    duplicates_closed=duplicates_closed,
                )
                # 关闭后浏览器激活哪个相邻标签不稳定。立即结束本轮，下一次从
                # 已确认搜一搜工作面重新开始，避免把主标签误当成下一重复页。
                result = {
                    "completed_cycle": False,
                    "primary_found": True,
                    "duplicates_closed": duplicates_closed,
                    "reason": "one_duplicate_closed_restart_from_search_required",
                }
                log_event("profile_account_dedup_partial", account=account_name, **result)
                return result
        press_ctrl_tab()
        time.sleep(0.35)

    result = {
        "completed_cycle": False,
        "primary_found": primary_found,
        "duplicates_closed": duplicates_closed,
        "reason": "safety_limit_before_returning_to_search_workspace",
    }
    log_event("profile_account_dedup_incomplete", account=account_name, **result)
    return result


def keep_only_search_tab(
    search_window: WindowInfo,
    account_name: str,
    output_dir: Path | None = None,
    *,
    close_non_search_tabs: bool = True,
    preserve_current_search_tab: bool = False,
) -> int:
    """确认当前搜一搜标签；默认不改变标签顺序。

    ``preserve_current_search_tab`` 用于前一步已经通过标签扫描选中的搜一搜页。
    这时禁止 Ctrl+1，避免把当前搜一搜切换成资料页/文章页。生产流程目前关闭
    ``close_non_search_tabs``，未知标签统一保留，文章标签由逐篇安全关闭负责。
    """
    activate_window(search_window.hwnd)
    if not preserve_current_search_tab:
        press_ctrl_1()
        time.sleep(0.35)
    time.sleep(0.35)
    baseline = capture_window(search_window.rect)
    if not _inspect_sogou_search_results(baseline)["found"]:
        return 0
    # 先测量当前电脑、远程桌面压缩和页面动画带来的自然波动，避免使用某台电脑的固定阈值。
    time.sleep(0.2)
    stable_baseline = capture_window(search_window.rect)
    idle_difference = _tab_switch_difference(baseline, stable_baseline)
    baseline = stable_baseline
    if not _inspect_sogou_search_results(baseline).get("found"):
        # 长时间运行后 Chromium/远程桌面偶尔会把短暂的全黑帧当成稳定帧。
        # 这种情况不是标签被关闭，仍交给调用方做有界等待和恢复。
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            baseline.save(output_dir / "browser-tabs-drift.png")
        log_event(
            "browser_tab_state_drifted",
            account=account_name,
            idle_difference=round(idle_difference, 3),
            reason="search_workbench_missing_after_stabilization",
            action="caller_recovery_required",
        )
        return 0
    single_tab_threshold = max(0.35, min(12.0, idle_difference * 3.0 + 0.5))
    log_event(
        "browser_tab_cleanup_calibrated",
        account=account_name,
        idle_difference=round(idle_difference, 3),
        single_tab_threshold=round(single_tab_threshold, 3),
        close_non_search_tabs=close_non_search_tabs,
    )
    if not close_non_search_tabs:
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            baseline.save(output_dir / "browser-tabs-preserved.png")
        log_event(
            "browser_tabs_preserved",
            account=account_name,
            reason="search_initialization_preserves_profile_and_unknown_tabs",
        )
        return 0
    if preserve_current_search_tab:
        # 已知搜索页不是首标签时，不能在清理未知标签的过程中用 Ctrl+1
        # 重新定位；这会把当前页面切走。生产恢复路径宁可保留现场。
        log_event(
            "browser_tabs_preserved",
            account=account_name,
            reason="current_search_tab_preserved_without_reordering",
        )
        return 0
    removed = 0
    for index in range(20):
        # 每次按键前重新激活同一个 HWND，防止远程桌面或公众号资料窗口抢走焦点。
        activate_window(search_window.hwnd)
        press_ctrl_9()
        time.sleep(0.35)
        candidate = capture_window(search_window.rect)
        difference = _tab_switch_difference(baseline, candidate)
        candidate_evidence = _inspect_sogou_search_results(candidate)
        candidate_has_search_box = bool(candidate_evidence["search_box"].get("found"))
        candidate_is_search_page = bool(candidate_evidence["found"])
        log_event(
            "browser_tab_probe",
            account=account_name,
            probe=index + 1,
            difference=round(difference, 3),
            single_tab_threshold=round(single_tab_threshold, 3),
            strategy="last_tab",
            candidate_has_search_box=candidate_has_search_box,
            candidate_is_search_page=candidate_is_search_page,
        )
        # 只有一个标签时 Ctrl+9 不会切页，截图差异仅来自光标或轻微动画。
        if difference < 0.35:
            break
        # 搜索结果页的动态内容会造成中等截图差异；只有差异较小且仍识别到搜索框时才保护当前搜索标签。
        # 文章分享弹窗同样含搜索框，但与搜一搜基准差异通常显著，不能据此放弃清理。
        if candidate_is_search_page and difference <= single_tab_threshold:
            log_event(
                "browser_tab_cleanup_stopped",
                account=account_name,
                probe=index + 1,
                reason="single_search_page_with_dynamic_content",
                difference=round(difference, 3),
            )
            break
        activate_window(search_window.hwnd)
        press_ctrl_w()
        removed += 1
        time.sleep(0.35)
        if not user32.IsWindow(search_window.hwnd):
            raise RuntimeError("清理浏览器标签时搜一搜窗口被意外关闭")
        # 关闭候选标签后可能落在另一个文章标签，必须显式回到搜索标签再校验。
        activate_window(search_window.hwnd)
        press_ctrl_1()
        time.sleep(0.35)
        baseline = capture_window(search_window.rect)
        if not _inspect_sogou_search_results(baseline)["found"]:
            raise RuntimeError("清理浏览器标签后没有回到搜一搜页面")
    else:
        raise RuntimeError("已清理20个历史标签但仍检测到其他标签，请人工检查浏览器")
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        baseline.save(output_dir / "browser-tabs-normalized.png")
    log_event("browser_tabs_normalized", account=account_name, removed=removed, remaining=1)
    return removed


def recover_search_workbench(
    search_window: WindowInfo,
    account_name: str,
    output_dir: Path,
    *,
    reason: str,
    poll_attempts: int = 4,
) -> tuple[WindowInfo, Image.Image, dict[str, Any]]:
    """清理后搜索框短暂消失时先等待，再扫描现有标签，最后无损重建。

    长时间连续采集会让微信 Chromium 偶发输出全黑帧或短暂切换到非搜索状态；
    此时不能把“当前帧不是搜一搜”等同于“窗口失效”。
    """
    last_reason = str(reason or "标签清理后未识别到搜一搜搜索框")
    for attempt in range(1, poll_attempts + 1):
        time.sleep(0.4)
        activate_window(search_window.hwnd)
        image = capture_window(search_window.rect)
        evidence = _inspect_sogou_search_results(image)
        log_event(
            "search_workbench_drift_retry",
            account=account_name,
            attempt=attempt,
            found=bool(evidence["found"]),
            search_box_found=bool(evidence["search_box"].get("found")),
            account_tab_found=bool(evidence["account_tab"].get("found")),
            reason=last_reason,
        )
        if evidence["found"]:
            log_event(
                "search_workbench_recovered_by_wait",
                account=account_name,
                attempt=attempt,
            )
            return search_window, image, evidence
        last_reason = str(
            evidence["search_box"].get("reason")
            or evidence.get("reason")
            or "搜索框仍处于漂移或全黑状态"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    image = capture_window(search_window.rect)
    image.save(output_dir / "search-workbench-drift.png")
    log_event(
        "search_workbench_poll_exhausted",
        account=account_name,
        reason=last_reason,
        action="scan_existing_tabs_then_recreate_if_needed",
    )
    if find_and_pin_search_tab(
        search_window,
        account_name,
        max_tabs=3,
        stop_on_target_profile=False,
    ):
        image = capture_window(search_window.rect)
        evidence = _inspect_sogou_search_results(image)
        if evidence["found"]:
            log_event(
                "search_workbench_recovered_by_tab_scan",
                account=account_name,
            )
            return search_window, image, evidence
    log_event(
        "search_workbench_recreated",
        account=account_name,
        reason=last_reason,
        action="preserve_stale_window_and_open_new",
    )
    search_window = recreate_sogou_search_window(
        search_window,
        account_name,
        last_reason,
    )
    image = capture_window(search_window.rect)
    evidence = _inspect_sogou_search_results(image)
    if not evidence["found"]:
        raise RuntimeError(
            "标签清理后无法重新定位搜一搜搜索框，重建窗口后仍处于漂移或全黑状态"
        )
    log_event(
        "search_workbench_recovered_by_recreate",
        account=account_name,
        recovered_hwnd=search_window.hwnd,
    )
    return search_window, image, evidence


def close_article_tabs_until_search(account_name: str) -> None:
    """定位并保留已确认的搜索标签，不依赖标签移动或首标签快捷键。"""
    # 只能从明确识别为搜一搜的窗口开始清理，禁止把任意微信窗口当作搜索窗口。
    search_window = find_sogou_search_window()
    if not find_and_pin_search_tab(search_window, account_name):
        recovered_profile = _SEARCH_SCAN_RECOVERED_PROFILE.pop(account_name, None)
        if recovered_profile is not None:
            log_event(
                "article_tab_cleanup_stopped_at_target_profile",
                account=account_name,
                action="preserve_profile_and_cancel_search_recovery",
            )
            return
        # 搜索标签可能已被异常流程关闭或替换。此时不在旧窗口里继续盲目 Ctrl+W，
        # 也不销毁旧窗口；只尝试从微信主窗口无损打开一个新的搜一搜页。
        search_window = recreate_sogou_search_window(
            search_window,
            account_name,
            "遍历现有标签后未找到真正的搜一搜结果页",
        )
        if not find_and_pin_search_tab(search_window, account_name, max_tabs=3):
            recovered_profile = _SEARCH_SCAN_RECOVERED_PROFILE.pop(account_name, None)
            if recovered_profile is not None:
                log_event(
                    "article_tab_cleanup_stopped_at_target_profile",
                    account=account_name,
                    action="preserve_profile_and_cancel_search_recovery",
                )
                return
            raise RuntimeError("重新创建搜一搜窗口后仍无法确认搜索页，为保护页面拒绝自动关闭标签")
        log_event(
            "article_tab_cleanup_search_recovered",
            account=account_name,
            recovered_hwnd=search_window.hwnd,
        )
    # 搜索标签可能不是第一个。当前环境不可靠支持标签移动，因此恢复阶段
    # 只保留当前已确认页面，未知标签不做批量关闭，避免误关资料页。
    closed = keep_only_search_tab(
        search_window,
        account_name,
        close_non_search_tabs=False,
        preserve_current_search_tab=True,
    )
    log_event(
        "article_tabs_cleanup_finished",
        account=account_name,
        closed=closed,
        search_tab_preserved=True,
        unknown_tabs_preserved=True,
        tab_reorder="disabled",
    )


def close_current_article_tab(
    account_name: str,
    title: str = "",
    return_window: WindowInfo | None = None,
) -> bool:
    """正常路径直接关闭当前文章标签，避免每篇文章都轮询全部标签。

    文章采集结束时，前台应当仍是刚打开的文章标签。先确认它不是搜一搜
    页面，再发送一次 Ctrl+W；关闭后优先在当前标签确认父资料页并保持滚动
    位置，只有当前返回页无法确认时才进入全量标签恢复流程。
    """
    try:
        article_hwnd, article_rect = find_article_window()
        foreground_hwnd = int(user32.GetForegroundWindow())
        if foreground_hwnd != article_hwnd:
            log_event(
                "article_tab_direct_close_skipped",
                account=account_name,
                title=title,
                reason="article_browser_not_foreground",
                foreground_hwnd=foreground_hwnd,
                article_hwnd=article_hwnd,
            )
            return False

        # 防止异常流程没有真正打开文章时误关搜一搜标签或公众号资料页标签。
        evidence = _inspect_sogou_search_results(capture_window(article_rect))
        if evidence.get("found"):
            log_event(
                "article_tab_direct_close_skipped",
                account=account_name,
                title=title,
                reason="current_tab_is_search_page",
            )
            return False
        if return_window and return_window.page_kind == "embedded_profile_tab":
            profile_validation = PROFILE_OCR.validate_profile_header(
                capture_window(article_rect), account_name
            )
            if profile_validation.get("matched"):
                log_event(
                    "article_tab_direct_close_skipped",
                    account=account_name,
                    title=title,
                    reason="current_tab_is_profile_page",
                )
                return False

        press_ctrl_w()
        time.sleep(0.45)
        if return_window and return_window.page_kind == "embedded_profile_tab":
            # Ctrl+W 正常会回到打开文章时的父资料页。此时资料页通常仍停留在
            # 文章卡片所在的滚动位置，账号名称可能不在视口内；不能为了精确
            # 名称校验调用全标签扫描或 Ctrl+Home，否则会破坏剩余卡片坐标。
            immediate_evidence: dict[str, Any] = {}
            for immediate_attempt in range(1, 4):
                immediate_screenshot = capture_window(return_window.rect)
                immediate_evidence = PROFILE_OCR.validate_profile_header(
                    immediate_screenshot, account_name
                )
                immediate_structure = bool(
                    immediate_evidence.get("profile_structure_found")
                    and not immediate_evidence.get("search_page_evidence")
                )
                if immediate_evidence.get("matched") or immediate_structure:
                    log_event(
                        "article_tab_closed_directly",
                        account=account_name,
                        title=title,
                        return_page="embedded_profile_tab",
                        return_validation=(
                            "exact_account_and_profile_structure"
                            if immediate_evidence.get("matched")
                            else "immediate_profile_structure_preserve_scroll"
                        ),
                        immediate_attempt=immediate_attempt,
                        global_profile_scan_skipped=True,
                    )
                    return True
                if immediate_attempt < 3:
                    time.sleep(0.35)
            log_event(
                "article_tab_immediate_return_unconfirmed",
                account=account_name,
                title=title,
                validation=immediate_evidence,
                action="fallback_to_bounded_profile_scan",
            )
            if not activate_embedded_profile_tab(return_window, account_name):
                log_event(
                    "article_tab_direct_close_failed",
                    account=account_name,
                    title=title,
                    reason="profile_tab_not_found_after_close",
                )
                return False
            profile_evidence = PROFILE_OCR.validate_profile_header(
                capture_window(return_window.rect), account_name
            )
            if profile_evidence.get("matched"):
                log_event(
                    "article_tab_closed_directly",
                    account=account_name,
                    title=title,
                    return_page="embedded_profile_tab",
                )
                return True
            log_event(
                "article_tab_direct_close_failed",
                account=account_name,
                title=title,
                reason="profile_page_not_confirmed_after_close",
            )
            return False
        search_window = find_sogou_search_window()
        search_evidence = _inspect_sogou_search_results(
            capture_window(search_window.rect)
        )
        if not search_evidence.get("found"):
            log_event(
                "article_tab_direct_close_failed",
                account=account_name,
                title=title,
                reason="search_page_not_confirmed_after_close",
            )
            return False
        log_event(
            "article_tab_closed_directly",
            account=account_name,
            title=title,
            search_tab_preserved=True,
        )
        return True
    except Exception as exc:
        # 直接关闭只是一条快速路径，任何不确定都交给安全恢复流程。
        log_event(
            "article_tab_direct_close_failed",
            account=account_name,
            title=title,
            reason="direct_close_exception",
            error=str(exc),
        )
        return False


def close_article_after_attempt(
    account_name: str,
    title: str = "",
    return_window: WindowInfo | None = None,
) -> None:
    """优先关闭当前文章，失败时才执行全量标签恢复。"""
    if close_current_article_tab(account_name, title, return_window=return_window):
        return
    log_event(
        "article_tab_cleanup_recovery_started",
        account=account_name,
        title=title,
        reason="direct_close_not_confirmed",
    )
    if return_window and return_window.page_kind == "embedded_profile_tab":
        raise RuntimeError(
            "无法安全关闭文章标签并恢复公众号资料页，为避免误关资料页已停止当前公众号"
        )
    close_article_tabs_until_search(account_name)


def arrange_automation_window(window: WindowInfo, role: str) -> WindowInfo:
    """固定搜一搜浏览器和公众号资料窗口，移动后返回新的真实坐标。"""
    if WINDOW_LAYOUT_MODE == "off":
        return window

    work_area = wintypes.RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):  # SPI_GETWORKAREA
        return window
    work = Rect(work_area.left, work_area.top, work_area.right, work_area.bottom)
    if role == "browser":
        # 搜一搜在超宽窗口会切换成聚合布局并隐藏“公众号”二级筛选，限制宽度保证结构稳定。
        browser_width = max(900, min(round(work.width * 0.58), 1600))
        target = Rect(
            work.left,
            work.top,
            work.left + browser_width,
            work.bottom,
        )
    elif role == "profile":
        profile_width = max(620, min(round(work.width * 0.38), 1100))
        vertical_margin = max(0, round(work.height * 0.04))
        target = Rect(
            work.right - profile_width,
            work.top + vertical_margin,
            work.right,
            work.bottom - vertical_margin,
        )
    else:
        raise ValueError(f"未知窗口布局角色：{role}")

    if window.rect == target:
        # 热窗口已经处于目标布局时，不重复 MoveWindow，也不等待动画完成。
        log_event(
            "window_arranged",
            role=role,
            title=window.title,
            class_name=window.class_name,
            moved=False,
            rect={
                "left": window.rect.left,
                "top": window.rect.top,
                "width": window.rect.width,
                "height": window.rect.height,
            },
        )
        return window

    user32.ShowWindow(window.hwnd, 9)  # SW_RESTORE
    moved = bool(user32.MoveWindow(
        window.hwnd,
        target.left,
        target.top,
        target.width,
        target.height,
        True,
    ))
    raw = wintypes.RECT()
    actual = window.rect
    deadline = time.monotonic() + 0.4
    while moved and time.monotonic() < deadline:
        if user32.GetWindowRect(window.hwnd, ctypes.byref(raw)):
            actual = Rect(raw.left, raw.top, raw.right, raw.bottom)
            if actual == target:
                break
        time.sleep(0.04)
    arranged = WindowInfo(
        window.hwnd,
        window.title,
        window.class_name,
        actual,
        window.process_name,
        window.page_kind,
        window.empty_article_profile,
    )
    log_event(
        "window_arranged",
        role=role,
        title=window.title,
        class_name=window.class_name,
        moved=moved,
        rect={
            "left": actual.left,
            "top": actual.top,
            "width": actual.width,
            "height": actual.height,
        },
    )
    return arranged


def normalized_to_screen(
    item: dict[str, Any], crop_box: tuple[int, int, int, int], window_rect: Rect
) -> tuple[int, int]:
    left, top, right, bottom = crop_box
    x = left + (right - left) * int(item["center_x_1000"]) / 1000
    y = top + (bottom - top) * int(item["center_y_1000"]) / 1000
    return round(window_rect.left + x), round(window_rect.top + y)


def click(screen_x: int, screen_y: int) -> None:
    user32.SetCursorPos(screen_x, screen_y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def press_ctrl_end() -> None:
    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    user32.keybd_event(0x23, 0, 0, 0)  # End down
    user32.keybd_event(0x23, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_home() -> None:
    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    user32.keybd_event(0x24, 0, 0, 0)  # Home down
    user32.keybd_event(0x24, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_r() -> None:
    """刷新当前已确认的微信内置浏览器标签。"""
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x52, 0, 0, 0)
    user32.keybd_event(0x52, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_w() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x57, 0, 0, 0)
    user32.keybd_event(0x57, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_tab() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x09, 0, 0, 0)
    user32.keybd_event(0x09, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_1() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x31, 0, 0, 0)
    user32.keybd_event(0x31, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_9() -> None:
    """切换到浏览器最右侧标签，用于从尾部逐个清理文章页。"""
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x39, 0, 0, 0)
    user32.keybd_event(0x39, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def validate_profile_with_home_recovery(
    window: WindowInfo,
    expected_name: str,
) -> tuple[Image.Image, dict[str, Any]]:
    """先识别页面结构；名称或导航因滚动不可见时回到顶部再精确确认。"""
    screenshot = wait_for_stable_frames(window.rect)
    validation = PROFILE_OCR.validate_profile_header(screenshot, expected_name)
    # 名称或导航可能因为页面停留在置顶/列表内容区域而不可见；只要已经
    # 出现账号名称但缺少结构证据，或结构成立但名称不可见，都先回顶一次。
    if (
        not validation.get("matched")
        and not validation.get("search_page_evidence")
        and (
            validation.get("profile_structure_found")
            or validation.get("name_candidates")
        )
    ):
        home_trigger = (
            "partial_structure"
            if validation.get("name_candidates")
            else "structure_without_name"
        )
        press_ctrl_home()
        screenshot = wait_for_stable_frames(window.rect)
        validation = PROFILE_OCR.validate_profile_header(screenshot, expected_name)
        log_event(
            "profile_identity_retried_after_home",
            account=expected_name,
            home_trigger=home_trigger,
            matched=bool(validation.get("matched")),
            reason=validation.get("reason"),
        )
    return screenshot, validation


def activate_embedded_profile_tab(
    window: WindowInfo,
    expected_name: str,
    *,
    max_tabs: int = WIN11_MAX_TAB_SCAN,
    allow_name_only_profile: bool = False,
) -> bool:
    """优先复用已学习的标签切换步数，失败时按整页身份有界恢复。"""
    global _WATCH_ACTIVE_PROFILE_ACCOUNT
    if window.empty_article_profile:
        allow_name_only_profile = True
    source_account = _WATCH_ACTIVE_PROFILE_ACCOUNT
    learned_steps = (
        _WATCH_PROFILE_TRANSITIONS.get((source_account, expected_name))
        if source_account and source_account != expected_name
        else None
    )
    if source_account == expected_name:
        activate_window(window.hwnd)
        _image, validation = validate_profile_with_home_recovery(window, expected_name)
        if validation.get("matched"):
            _WATCH_PROFILE_SCAN_STATE[expected_name] = {
                "matched": True,
                "completed_cycle": False,
                "search_seen": False,
                "method": "current-tab",
            }
            log_event(
                "profile_tab_switch_reused_current",
                account=expected_name,
                validation=validation,
            )
            return True
        _WATCH_ACTIVE_PROFILE_ACCOUNT = None
        source_account = None
    elif learned_steps is not None and learned_steps > 0:
        activate_window(window.hwnd)
        log_event(
            "profile_tab_switch_requested",
            account=expected_name,
            source_account=source_account,
            learned_steps=learned_steps,
        )
        for _ in range(learned_steps):
            press_ctrl_tab()
            time.sleep(0.12)
        _image, validation = validate_profile_with_home_recovery(window, expected_name)
        if validation.get("matched"):
            _WATCH_PROFILE_SCAN_STATE[expected_name] = {
                "matched": True,
                "completed_cycle": False,
                "search_seen": False,
                "method": "learned-route",
            }
            _WATCH_ACTIVE_PROFILE_ACCOUNT = expected_name
            log_event(
                "profile_identity_confirmed",
                account=expected_name,
                method="learned-relative-tab-route",
                validation=validation,
            )
            return True
        log_event(
            "profile_slot_recovery",
            account=expected_name,
            source_account=source_account,
            reason="learned_route_destination_identity_mismatch",
            validation=validation,
        )
        _WATCH_PROFILE_TRANSITIONS.pop((source_account, expected_name), None)
        _WATCH_ACTIVE_PROFILE_ACCOUNT = None

    adapter = Win11WeChatAdapter(
        activate_window=activate_window,
        capture_window=capture_window,
        validate_profile_header=PROFILE_OCR.validate_profile_header,
        press_ctrl_tab=press_ctrl_tab,
        press_ctrl_w=press_ctrl_w,
        log_event=log_event,
        press_ctrl_home=press_ctrl_home,
        wait_for_stable_frames=wait_for_stable_frames,
        inspect_search_page=lambda image: bool(
            _inspect_sogou_search_results(image).get("found")
        ),
        same_search_page=same_sogou_search_workspace,
        allow_name_only_profile=allow_name_only_profile,
    )
    matched = adapter.activate_profile_tab(
        window,
        expected_name,
        max_tabs=max_tabs,
    )
    if matched:
        if source_account and source_account != expected_name:
            recovered_steps = (
                (learned_steps or 0) + adapter.last_switch_steps
                if learned_steps is not None
                else adapter.last_switch_steps
            )
            if 0 < recovered_steps <= max_tabs:
                _WATCH_PROFILE_TRANSITIONS[(source_account, expected_name)] = recovered_steps
        _WATCH_ACTIVE_PROFILE_ACCOUNT = expected_name
        log_event(
            "profile_identity_confirmed",
            account=expected_name,
            method="bounded-viewport-ocr-scan",
            switch_steps=adapter.last_switch_steps,
        )
    _WATCH_PROFILE_SCAN_STATE[expected_name] = {
        "matched": bool(matched),
        "completed_cycle": bool(adapter.last_scan_completed_cycle),
        "search_seen": bool(adapter.last_scan_saw_search),
        "method": "content-cycle-scan",
        "empty_profile_name_only": bool(
            adapter.last_match_mode == "empty_profile_name_only"
        ),
    }
    return matched


def confirm_embedded_profile_preserved(
    profile_window: WindowInfo,
    account_name: str,
) -> bool:
    """轻量确认当前资料页仍属于本次采集会话，且不破坏滚动位置。

    文章标签关闭或翻页后，资料页通常停留在卡片列表的滚动位置，账号名称
    可能不在视口内。此时只要整页仍具备资料页结构且没有搜一搜证据，就允许
    继续 OCR 和滚动；只有结构证据也缺失时才进入回顶或全标签扫描。账号身份
    的强校验仍由资料页打开、文章关闭返回和文章详情页账号校验共同保证。
    """
    try:
        activate_window(profile_window.hwnd)
        validation = PROFILE_OCR.validate_profile_header(
            capture_window(profile_window.rect), account_name
        )
    except Exception:
        return False
    return bool(
        validation.get("matched")
        or (
            validation.get("profile_structure_found")
            and not validation.get("search_page_evidence")
        )
    )


def inventory_existing_profile_pool(
    browser_window: WindowInfo,
    accounts: list[str],
    evidence_root: Path,
    *,
    max_tabs: int = WIN11_MAX_TAB_SCAN,
) -> tuple[dict[str, WindowInfo], dict[str, Any]]:
    """一次绕圈盘点现有标签，并批量登记全部监听账号资料页。"""
    global _WATCH_ACTIVE_PROFILE_ACCOUNT
    evidence_dir = evidence_root / datetime.now().strftime("%Y%m%d-%H%M%S")

    def save_unresolved_evidence(
        probe_index: int,
        screenshot: Image.Image,
        identity: dict[str, Any],
    ) -> None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / f"probe-{probe_index:03d}-unresolved-profile.png"
        screenshot.save(path)
        log_event(
            "watch_profile_inventory_evidence_saved",
            probe_index=probe_index,
            path=str(path),
            observed_header_candidates=identity.get("observed_header_candidates") or [],
            reason=identity.get("reason"),
        )

    adapter = Win11WeChatAdapter(
        activate_window=activate_window,
        capture_window=capture_window,
        validate_profile_header=PROFILE_OCR.validate_profile_header,
        press_ctrl_tab=press_ctrl_tab,
        press_ctrl_w=press_ctrl_w,
        log_event=log_event,
        press_ctrl_home=press_ctrl_home,
        wait_for_stable_frames=wait_for_stable_frames,
        inspect_search_page=lambda image: bool(
            _inspect_sogou_search_results(image).get("found")
        ),
        same_search_page=same_sogou_search_workspace,
        identify_profile_account=PROFILE_OCR.identify_profile_account,
    )
    log_event(
        "watch_profile_inventory_started",
        accounts=accounts,
        accounts_total=len(accounts),
        max_tabs=max_tabs,
    )
    result = adapter.inventory_profile_tabs(
        browser_window,
        accounts,
        max_tabs=max_tabs,
        evidence_callback=save_unresolved_evidence,
    )
    profiles = {
        account: WindowInfo(
            browser_window.hwnd,
            browser_window.title,
            browser_window.class_name,
            browser_window.rect,
            browser_window.process_name,
            "embedded_profile_tab",
        )
        for account in result.get("profiles_found") or []
    }

    positions = result.get("profile_positions") or {}
    cycle_length = result.get("cycle_length")
    for source_account, source_position in positions.items():
        for target_account, target_position in positions.items():
            if source_account == target_account:
                continue
            if cycle_length:
                steps = (int(target_position) - int(source_position)) % int(cycle_length)
            else:
                steps = int(target_position) - int(source_position)
            if steps > 0:
                _WATCH_PROFILE_TRANSITIONS[(source_account, target_account)] = steps

    for account, duplicate_positions in (result.get("duplicate_positions") or {}).items():
        log_event(
            "watch_profile_inventory_duplicate_observed",
            account=account,
            duplicate_positions=duplicate_positions,
            action="preserve_without_automatic_cleanup",
        )
    _WATCH_ACTIVE_PROFILE_ACCOUNT = None
    for account in accounts:
        _WATCH_PROFILE_CACHE.pop(account, None)
    _WATCH_PROFILE_CACHE.update(profiles)
    return profiles, result


def refresh_verified_profile_window(
    window: WindowInfo,
    account_name: str,
    *,
    timeout_seconds: float = 12.0,
    output_dir: Path | None = None,
) -> bool:
    """刷新已验证的资料页，并等待账号头部重新出现。

    Ctrl+R 只能作用于当前活动标签；刷新前先按页面类型激活并校验，刷新后
    再用资料页头部 OCR 做闸门。页面加载期间允许出现空白或旧截图，不能立即
    把一次截图失败当作资料页丢失。
    """
    if not user32.IsWindow(window.hwnd):
        log_event(
            "profile_refresh_failed",
            account=account_name,
            reason="profile_window_invalid",
        )
        return False
    if window.page_kind == "embedded_profile_tab":
        if not activate_embedded_profile_tab(window, account_name):
            log_event(
                "profile_refresh_failed",
                account=account_name,
                reason="profile_tab_not_found_before_refresh",
            )
            return False
    else:
        activate_window(window.hwnd)
    baseline = capture_window(window.rect)
    before_validation = PROFILE_OCR.validate_profile_header(baseline, account_name)
    if not before_validation.get("matched"):
        log_event(
            "profile_refresh_failed",
            account=account_name,
            reason="profile_identity_not_confirmed_before_refresh",
            validation=before_validation,
        )
        return False
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        baseline.save(output_dir / "profile-before-refresh.png")
    log_event("profile_refresh_requested", account=account_name, method="ctrl-r")
    refresh_started = time.monotonic()
    press_ctrl_r()
    deadline = refresh_started + max(1.0, timeout_seconds)
    first_match_at: float | None = None
    last_screenshot = baseline
    last_validation: dict[str, Any] = before_validation
    while time.monotonic() < deadline:
        time.sleep(0.35)
        screenshot = capture_window(window.rect)
        validation = PROFILE_OCR.validate_profile_header(screenshot, account_name)
        last_screenshot = screenshot
        last_validation = validation
        if not validation.get("matched"):
            first_match_at = None
            continue
        if first_match_at is None:
            first_match_at = time.monotonic()
            continue
        # 连续两次通过头部校验，避免刚刷新时残留旧画面或加载动画误判完成。
        if time.monotonic() - first_match_at < 0.35:
            continue
        if output_dir is not None:
            screenshot.save(output_dir / "profile-after-refresh.png")
        log_event(
            "profile_refresh_completed",
            account=account_name,
            method="ctrl-r",
            elapsed_seconds=round(max(0.0, time.monotonic() - refresh_started), 3),
            validation=validation,
        )
        return True
    if output_dir is not None:
        last_screenshot.save(output_dir / "profile-refresh-timeout.png")
        (output_dir / "profile-refresh-timeout.json").write_text(
            json.dumps(last_validation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    log_event(
        "profile_refresh_failed",
        account=account_name,
        reason="profile_identity_not_restored_after_refresh",
        validation=last_validation,
    )
    return False


def press_ctrl_shift_pageup() -> None:
    """历史兼容函数：当前 Win11 流程不调用标签移动快捷键。"""
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x10, 0, 0, 0)
    user32.keybd_event(0x21, 0, 0, 0)
    user32.keybd_event(0x21, 0, 0x0002, 0)
    user32.keybd_event(0x10, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_a() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x41, 0, 0, 0)
    user32.keybd_event(0x41, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_v() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x56, 0, 0, 0)
    user32.keybd_event(0x56, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_enter() -> None:
    """向当前微信搜索框发送回车，触发搜索。"""
    user32.keybd_event(0x0D, 0, 0, 0)
    user32.keybd_event(0x0D, 0, 0x0002, 0)


def press_ctrl_f() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x46, 0, 0, 0)
    user32.keybd_event(0x46, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_enter() -> None:
    user32.keybd_event(0x0D, 0, 0, 0)
    user32.keybd_event(0x0D, 0, 0x0002, 0)


def press_down() -> None:
    """选中微信搜索候选列表中的下一项。"""
    user32.keybd_event(0x28, 0, 0, 0)
    user32.keybd_event(0x28, 0, 0x0002, 0)


def press_escape() -> None:
    user32.keybd_event(0x1B, 0, 0, 0)
    user32.keybd_event(0x1B, 0, 0x0002, 0)


def scroll_window_up(rect: Rect, wheel_notches: int = 2) -> None:
    """在公众号内容区域向上翻页，正滚轮值表示查看更早的消息。"""
    user32.SetCursorPos(rect.left + rect.width // 2, rect.top + rect.height // 2)
    # Qt 会把超大的单次 delta 仍按一次滚轮处理，因此必须逐次发送标准 120 delta。
    for _ in range(wheel_notches):
        user32.mouse_event(0x0800, 0, 0, 120, 0)  # MOUSEEVENTF_WHEEL
        time.sleep(0.02)


def scroll_window_down(rect: Rect, wheel_notches: int = 2) -> None:
    """在公众号资料窗口向下滚动，查看更早的文章。"""
    user32.SetCursorPos(rect.left + rect.width // 2, rect.top + rect.height * 3 // 4)
    for _ in range(wheel_notches):
        user32.mouse_event(0x0800, 0, 0, -120, 0)
        time.sleep(0.02)


def set_clipboard_text(value: str) -> None:
    """写入 Unicode 剪贴板且不创建窗口，避免抢走微信输入焦点。"""
    import pyperclip

    pyperclip.copy(value)


def read_clipboard_text() -> str:
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        raise RuntimeError("无法打开系统剪贴板")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = ctypes.windll.kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            ctypes.windll.kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def copy_article_url(
    hwnd: int,
    rect: Rect,
    output_dir: Path | None = None,
    phase: str = "before",
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
) -> str:
    """动态定位浏览器菜单与“复制链接”，并用剪贴板 URL 验证整条操作链。"""
    clipboard_sentinel = f"__WECHAT_RPA_COPY_PENDING_{phase}__"
    # 标题栏按钮的物理像素会随 Windows DPI 缩放：100% 时菜单距右侧约 124px，
    # 150% 时约 186px。使用窗口真实 DPI，避免把 150% 坐标误用到 100% 电脑。
    get_dpi_for_window = getattr(user32, "GetDpiForWindow", None)
    dpi = int(get_dpi_for_window(hwnd)) if get_dpi_for_window else 96
    dpi = dpi if dpi > 0 else 96
    scale = dpi / 96
    fallback_menu_action = {
        "found": True,
        "center_x_1000": round((rect.width - 124 * scale) * 1000 / rect.width),
        "center_y_1000": round(21 * scale * 1000 / rect.height),
        "confidence": 0.35,
        "method": "dpi-relative-menu-button-fallback",
    }

    # 先在菜单关闭状态下识别标题栏按钮，避免菜单遮罩干扰三点图标检测。
    press_escape()
    time.sleep(0.2)
    titlebar_screenshot = capture_window(rect)
    menu_button_action: dict[str, Any] | None = None
    cached_menu_button = load_menu_button_position_cache(rect, dpi)
    if cached_menu_button is not None:
        validated_menu_button = validate_cached_menu_button_action(
            titlebar_screenshot,
            cached_menu_button,
        )
        log_event("menu_button_cached_position_validation", phase=phase, **validated_menu_button)
        if validated_menu_button.get("found"):
            menu_button_action = validated_menu_button
        else:
            clear_menu_button_position_cache(
                str(validated_menu_button.get("reason") or "缓存菜单按钮验证失败")
            )

    if menu_button_action is None:
        local_menu_button = PROFILE_OCR.locate_browser_menu_button(titlebar_screenshot)
        log_event("menu_button_local_detection", phase=phase, **local_menu_button)
        if local_menu_button.get("found"):
            menu_button_action = local_menu_button

    if menu_button_action is None and allow_vl and client is not None:
        try:
            qwen_menu_button = normalize_qwen_menu_button_action(
                client.detect_browser_menu_button(titlebar_screenshot)
            )
        except Exception as exc:
            qwen_menu_button = {"found": False, "reason": f"Qwen-VL 菜单按钮定位失败：{exc}"}
        log_event("menu_button_qwen_fallback", phase=phase, **qwen_menu_button)
        if qwen_menu_button.get("found"):
            menu_button_action = qwen_menu_button

    if menu_button_action is None:
        # 最终兜底仍是 DPI 相对位置，但只有后续成功复制出公众号 URL 时才会写入缓存。
        menu_button_action = fallback_menu_action
        log_event("menu_button_dpi_fallback", phase=phase, **fallback_menu_action)

    def open_menu(stage: str) -> Image.Image:
        # Esc 同时负责收起旧菜单和误弹出的“发送给”窗口，再打开干净菜单。
        press_escape()
        time.sleep(0.2)
        menu_x = rect.left + round(
            rect.width * int(menu_button_action["center_x_1000"]) / 1000
        )
        menu_y = rect.top + round(
            rect.height * int(menu_button_action["center_y_1000"]) / 1000
        )
        click(menu_x, menu_y)
        log_event(
            "copy_link_menu_button_clicked",
            phase=phase,
            stage=stage,
            dpi=dpi,
            scale=round(scale, 3),
            screen_x=menu_x,
            screen_y=menu_y,
            method=str(menu_button_action.get("method") or "unknown"),
        )
        time.sleep(0.8)
        screenshot = capture_window(rect)
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            screenshot.save(output_dir / f"copy-menu-{phase}-{stage}.png")
        return screenshot

    def click_action(action: dict[str, Any], stage: str) -> str:
        set_clipboard_text(clipboard_sentinel)
        action_x = rect.left + round(rect.width * int(action["center_x_1000"]) / 1000)
        action_y = rect.top + round(rect.height * int(action["center_y_1000"]) / 1000)
        click(action_x, action_y)
        log_event(
            "copy_link_action_clicked",
            phase=phase,
            stage=stage,
            screen_x=action_x,
            screen_y=action_y,
            method=str(action.get("method") or stage),
        )
        deadline = time.monotonic() + 2.5
        observed = clipboard_sentinel
        while time.monotonic() < deadline:
            time.sleep(0.2)
            observed = read_clipboard_text().strip()
            if observed != clipboard_sentinel:
                break
        log_event(
            "copy_link_clipboard_validation",
            phase=phase,
            stage=stage,
            valid=observed.startswith("https://mp.weixin.qq.com/"),
        )
        return observed

    def remember_success(action: dict[str, Any], source: str) -> None:
        """只有剪贴板 URL 已验证成功，才同时学习按钮与菜单项坐标。"""
        save_copy_link_position_cache(action, rect, dpi, source)
        save_menu_button_position_cache(
            menu_button_action,
            rect,
            dpi,
            str(menu_button_action.get("method") or "unknown"),
        )

    last_action: dict[str, Any] = {"found": False, "reason": "尚未识别菜单"}
    last_url = clipboard_sentinel
    menu_screenshot = open_menu("cache")

    cached = load_copy_link_position_cache(rect, dpi)
    if cached is not None:
        cached_action = validate_cached_copy_link_action(menu_screenshot, cached)
        last_action = cached_action
        log_event("copy_link_cached_position_validation", phase=phase, **cached_action)
        if cached_action.get("found"):
            last_url = click_action(cached_action, "cache")
            if last_url.startswith("https://mp.weixin.qq.com/"):
                remember_success(cached_action, "cache-validated")
                return last_url
            clear_copy_link_position_cache("缓存坐标点击后剪贴板未得到公众号 URL")
            menu_screenshot = open_menu("local-ocr")
        else:
            clear_copy_link_position_cache("缓存坐标附近未识别到复制链接")
    else:
        log_event("copy_link_cached_position_missed", phase=phase)

    local_action = PROFILE_OCR.locate_copy_link_action(menu_screenshot)
    last_action = local_action
    log_event("copy_link_menu_detection", phase=phase, stage="local-ocr", **local_action)
    if local_action.get("found"):
        last_url = click_action(local_action, "local-ocr")
        if last_url.startswith("https://mp.weixin.qq.com/"):
            remember_success(local_action, "local-ocr")
            return last_url
        menu_screenshot = open_menu("qwen-vl")
    else:
        log_event(
            "copy_link_menu_action_skipped",
            phase=phase,
            stage="local-ocr",
            reason=str(local_action.get("reason") or "未找到复制链接菜单项"),
        )

    if allow_vl and client is not None:
        try:
            qwen_action = normalize_qwen_copy_link_action(
                client.detect_copy_link_action(menu_screenshot)
            )
        except Exception as exc:
            qwen_action = {"found": False, "reason": f"Qwen-VL 调用失败：{exc}"}
        last_action = qwen_action
        log_event("copy_link_qwen_fallback", phase=phase, **qwen_action)
        if qwen_action.get("found"):
            last_url = click_action(qwen_action, "qwen-vl")
            if last_url.startswith("https://mp.weixin.qq.com/"):
                remember_success(qwen_action, "qwen-vl")
                return last_url
    else:
        log_event(
            "copy_link_qwen_fallback_skipped",
            phase=phase,
            reason="VL 已禁用或客户端未配置",
        )

    clear_copy_link_position_cache("复制链接三层识别全部失败")
    clear_menu_button_position_cache("未能通过合法公众号 URL 验证菜单按钮")
    press_escape()
    raise RuntimeError(
        "复制链接失败，缓存坐标、本地 OCR 与 Qwen-VL 均未写入公众号URL："
        f"menu_found={bool(last_action.get('found'))}，clipboard={last_url[:80]!r}"
    )


class ArticleMismatchError(RuntimeError):
    pass


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip()
    normalized = normalized.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    # 资料页 OCR 偶尔会把标题和其下方的阅读、点赞指标合并成一行。
    normalized = re.sub(r"阅读\s*[\d.万亿+]+.*$", "", normalized)
    normalized = re.sub(r"赞\s*\d+.*$", "", normalized)
    # 中文书名/引号的右半边常被 OCR 识别为 ASCII 方括号。
    normalized = normalized.replace("]", "」")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def compact_title(value: str) -> str:
    """去掉标题标点，降低 OCR 对引号、竖线和中英文标点差异的影响。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_title(value))


def canonical_title_for_match(value: str) -> str:
    """生成用于比对的标题文本，兼容本地 OCR 的少量高频字符误识别。"""
    compact = compact_title(value).replace("丨", "").replace("｜", "").replace("|", "")
    # 在公众号标题中，OCR 常把 AI 末尾的大写 I 读成小写 l。
    # 这里只处理已知品牌词，避免对普通中文标题做过宽的替换。
    return (
        compact.replace("OpenAl", "OpenAI")
        .replace("ChatGPt", "ChatGPT")
        .replace("AlAgent", "AIAgent")
        .replace("PhysicalAl", "PhysicalAI")
    )


def titles_match(expected: str, actual: str) -> bool:
    expected_value = normalize_title(expected)
    actual_value = normalize_title(actual)
    expected_canonical = canonical_title_for_match(expected_value)
    actual_canonical = canonical_title_for_match(actual_value)
    truncated = expected_value.rstrip(".…")
    if expected_value.endswith(("...", "…")):
        truncated_canonical = canonical_title_for_match(truncated)
        # 卡片文本带省略号时，卡片只提供标题前缀；以去标点后的前缀比较，
        # 能兼容“丨 / |”以及极少量 OCR 错字；公众号名和文章 URL 仍会独立校验。
        if len(truncated_canonical) < 8:
            return False
        if actual_canonical.startswith(truncated_canonical):
            return True
        actual_prefix = actual_canonical[: len(truncated_canonical)]
        return difflib.SequenceMatcher(
            None, truncated_canonical, actual_prefix
        ).ratio() >= 0.90
    if expected_value == actual_value:
        return True
    if expected_canonical == actual_canonical:
        return True
    # 卡片 OCR 可能漏掉末尾或错读一个字符；公众号名称仍会另行严格校验。
    # 浏览器标签受窗口宽度限制时不会显示省略号，只保留标题前缀。
    # 同时还有网页大标题、公众号名称及前后 URL 校验，因此 8 字以上前缀可安全接受。
    if len(expected_canonical) >= 8 and actual_canonical.startswith(expected_canonical):
        return True
    # 指标锚定模式优先读取紧贴“阅读/赞”的最后一行；多行标题因此可能只留下
    # 末行。公众号归属和复制前后 URL 仍会独立校验，6 字以上的完整后缀可接受。
    if len(expected_canonical) >= 6 and actual_canonical.endswith(expected_canonical):
        return True
    # OCR 的窗口标签经常只保留前半段，并把 AI/Al、O/0 等单字符读错。
    # 比较较短标题与真实标题等长前缀；公众号名称和 URL 仍会独立严格校验。
    shorter, longer = sorted((expected_canonical, actual_canonical), key=len)
    if len(shorter) >= 8:
        prefix_similarity = difflib.SequenceMatcher(
            None, shorter, longer[: len(shorter)]
        ).ratio()
        if prefix_similarity >= 0.84:
            return True
    length_ratio = min(len(expected_value), len(actual_value)) / max(
        len(expected_value), len(actual_value), 1
    )
    similarity = difflib.SequenceMatcher(None, expected_canonical, actual_canonical).ratio()
    return length_ratio >= 0.75 and similarity >= 0.92


def title_similarity_score(expected: str, actual: str) -> float:
    """返回卡片 OCR 标题与文章页标题的辅助相似度，不作为唯一放行条件。"""
    expected_canonical = canonical_title_for_match(expected)
    actual_canonical = canonical_title_for_match(actual)
    if not expected_canonical or not actual_canonical:
        return 0.0
    return round(
        difflib.SequenceMatcher(None, expected_canonical, actual_canonical).ratio(),
        4,
    )


def extract_local_interaction_metrics(
    screenshot: Image.Image, metric_mode: str, *, allow_partial: bool = True
) -> tuple[dict[str, Any], str, str | None]:
    """识别本地互动指标，并在非关键图标失败时保住已验证的转发数。

    转发数是当前采集的核心指标。全部指标模式下，收藏或评论图标可能随
    微信版本变化而匹配失败；此时不能让一篇已经确认链接、标题和转发数的
    文章被整体丢弃。函数会明确标记为部分采集，未确认的指标保持 ``None``。
    """
    if metric_mode == "share":
        return INTERACTION_OCR.extract_share(screenshot), "template-ocr-share-only", None

    try:
        metrics = INTERACTION_OCR.extract(screenshot)
        required = ("share_count", "favorite_count", "comment_count")
        if any(metrics.get(name) is None for name in required):
            raise ValueError(f"本地互动数识别不完整：{metrics}")
        return metrics, "template-ocr", None
    except Exception as full_error:
        # 已启用 VL 时仍交给视觉模型补齐全部指标；局部降级仅服务于禁用 VL 的本地运行。
        if not allow_partial:
            raise
        # 只对转发图标进行一次独立识别；它成功时允许以“部分指标”继续入库。
        # 若转发本身也无法确认，仍把原始异常向上抛出，避免写入不可靠数据。
        try:
            share_metrics = INTERACTION_OCR.extract_share(screenshot)
        except Exception:
            raise full_error
        if share_metrics.get("share_count") is None:
            raise full_error
        details = dict(share_metrics.get("details") or {})
        details["partial_reason"] = str(full_error)
        return (
            {
                "share_count": share_metrics["share_count"],
                "like_count": None,
                "favorite_count": None,
                "comment_count": None,
                "details": details,
            },
            "template-ocr-partial-share",
            str(full_error),
        )


def collect_open_article(
    client: QwenVisionClient,
    output_dir: Path,
    write_mongo: bool,
    export_jsonl: str | None,
    export_csv: str | None,
    expected_title: str | None = None,
    expected_account: str | None = None,
    storage_account: str | None = None,
    allow_vl: bool = True,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    list_read_count: int | None = None,
    list_like_count: int | None = None,
    successful_urls_in_run: set[str] | None = None,
    metric_mode: str = "all",
    scan_range: str | None = None,
) -> dict[str, Any]:
    log_event(
        "article_collect_started",
        expected_title=expected_title,
        expected_account=expected_account,
        metric_mode=metric_mode,
    )
    hwnd, rect = find_article_window()
    activate_window(hwnd)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(raw))
    rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
    log_event(
        "article_window_selected",
        hwnd=hwnd,
        rect={"left": rect.left, "top": rect.top, "width": rect.width, "height": rect.height},
    )
    # 先复制链接并解析真实标题；账号、正文和发布时间校验通过后再识别互动栏。
    url = copy_article_url(
        hwnd,
        rect,
        output_dir,
        "before",
        client=client,
        allow_vl=allow_vl,
    )
    log_event("article_url_copied_before", url=url)
    normalized_url = normalize_article_url(url)
    if (
        successful_urls_in_run is not None
        and normalized_url
        and normalized_url in successful_urls_in_run
    ):
        # URL 是文章的确定标识；卡片 OCR 标题只作辅助，不能替代 URL 去重。
        log_event(
            "article_skipped_duplicate_url",
            url=url,
            url_normalized=normalized_url,
            expected_title=expected_title,
        )
        return {
            "url": url,
            "title": expected_title or "",
            "account_name": storage_account or expected_account or "",
            "status": "skipped_duplicate_in_run",
        }
    # 先查 MongoDB；已成功采集且正文完整的文章不再重复下载正文。
    cached_page = None
    if write_mongo:
        try:
            cached_page = load_cached_page(
                url,
                mongo_uri or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
                mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
                mongo_collection or os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
            )
            if cached_page:
                log_event("article_page_reused_from_mongo", url=url)
        except Exception as exc:
            log_event("article_cache_lookup_failed", url=url, error=str(exc))
    page = cached_page or parse_page(url)
    log_event(
        "article_page_parsed",
        url=url,
        title=page.get("title"),
        account_name=page.get("account_name"),
        publish_time=page.get("publish_time") or page.get("publishDate"),
        content_length=len(str(page.get("content") or "")),
    )
    title_matched = not expected_title or titles_match(expected_title, page["title"])
    title_similarity = (
        title_similarity_score(expected_title, page["title"])
        if expected_title
        else None
    )
    if expected_title and not title_matched:
        # 资料页卡片标题可能被截断、错位或混入互动数字；文章页标题和 URL 才是
        # 最终身份。保留告警供诊断，但不因卡片 OCR 误差丢弃已验证文章。
        log_event(
            "article_card_title_mismatch_warning",
            url=url,
            url_normalized=normalized_url,
            card_title=expected_title,
            parsed_title=page["title"],
            similarity=title_similarity,
            action="continue_after_url_account_content_validation",
        )
    if expected_account and normalize_title(expected_account) != normalize_title(page["account_name"]):
        raise ArticleMismatchError(
            f"公众号不匹配：目标={expected_account!r}，实际={page['account_name']!r}"
        )
    publish_time = page.get("publish_time") or page.get("publishDate")
    if scan_range and not publish_time_matches_scan_range(publish_time, scan_range):
        # 资料页时间分组只用于初筛；真正写库前必须以文章页面的发布时间为准。
        # 这样即使 OCR 把旧卡片错误归到“今天”，也不会更新历史文章互动数。
        log_event(
            "article_skipped_outside_scan_range",
            url=url,
            title=page.get("title"),
            publish_time=publish_time,
            scan_range=scan_range,
        )
        return {
            "url": url,
            "title": page.get("title") or expected_title or "",
            "account_name": storage_account or expected_account or "",
            "publish_time": publish_time,
            "status": "skipped_outside_scan_range",
            "skip_reason": f"真实发布时间 {publish_time} 不属于扫描范围 {scan_range}",
        }
    evidence_screenshot = capture_window(rect)
    evidence_screenshot.save(output_dir / "article_evidence.png")
    evidence = ARTICLE_EVIDENCE_OCR.inspect(evidence_screenshot, page["title"])
    viewport_title = str((evidence.get("viewport_title") or {}).get("text") or "")
    tab_title = str((evidence.get("tab_title") or {}).get("text") or "")
    log_event(
        "article_title_evidence",
        parsed_title=page.get("title"),
        card_title=expected_title,
        tab_title=tab_title,
        viewport_title=viewport_title,
    )
    # 正文 OCR 可能把首段引文识别成标题；网页标题、公众号名和 URL 已严格校验，
    # 因此正文/标签页 OCR 只记录辅助证据，不因误识别而重复打开文章。
    viewport_matched = bool(viewport_title) and titles_match(viewport_title, page["title"])
    tab_matched = bool(tab_title) and titles_match(tab_title, page["title"])
    if not viewport_matched or not tab_matched:
        log_event(
            "article_title_evidence_warning",
            parsed_title=page.get("title"),
            viewport_title=viewport_title,
            tab_title=tab_title,
            viewport_matched=viewport_matched,
            tab_matched=tab_matched,
            action="continue_after_url_page_account_validation",
        )
    if False and (not viewport_title or not titles_match(viewport_title, page["title"])):
        raise ArticleMismatchError(
            f"同屏正文标题不匹配：OCR={viewport_title!r}，网页={page['title']!r}"
        )
    if False and (not tab_title or not titles_match(tab_title, page["title"])):
        raise ArticleMismatchError(
            f"活动标签标题不匹配：OCR={tab_title!r}，网页={page['title']!r}"
        )

    # 正文标题和互动栏必须来自同一张完整窗口截图。
    footer_top = round(evidence_screenshot.height * 0.70)
    article_footer = evidence_screenshot.crop(
        (0, footer_top, evidence_screenshot.width, evidence_screenshot.height)
    )
    article_footer.save(output_dir / "article_footer.png")
    metric_source = "template-ocr-share-only" if metric_mode == "share" else "template-ocr"
    try:
        bottom_metrics, metric_source, partial_reason = extract_local_interaction_metrics(
            evidence_screenshot, metric_mode, allow_partial=not allow_vl
        )
        if partial_reason:
            log_event(
                "article_metrics_partial",
                url=url,
                metric_source=metric_source,
                retained_metrics={"share_count": bottom_metrics.get("share_count")},
                unavailable_metrics=["like_count", "favorite_count", "comment_count"],
                reason=partial_reason,
            )
    except Exception as exc:
        if not allow_vl:
            raise RuntimeError(f"本地互动数识别失败且已禁用VL：{exc}") from exc
        # 窗口缩放、主题或微信版本变化导致模板失效时，保留视觉模型兜底。
        metric_source = "qwen-vl-share-fallback" if metric_mode == "share" else "qwen-vl-fallback"
        bottom_metrics = client.extract_interaction_counts(article_footer)
        bottom_metrics["fallback_reason"] = str(exc)
    metrics = {
        "read_count": None if metric_mode == "share" else list_read_count,
        "like_count": None if metric_mode == "share" else (
            bottom_metrics.get("like_count")
            if bottom_metrics.get("like_count") is not None
            else list_like_count
        ),
        "share_count": bottom_metrics.get("share_count"),
        "favorite_count": None if metric_mode == "share" else bottom_metrics.get("favorite_count"),
        "comment_count": None if metric_mode == "share" else bottom_metrics.get("comment_count"),
        "metric_source": metric_source,
    }
    log_event("article_metrics_extracted", url=url, **metrics)
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    url_after = copy_article_url(
        hwnd,
        rect,
        output_dir,
        "after",
        client=client,
        allow_vl=allow_vl,
    )
    log_event("article_url_copied_after", url_before=url, url_after=url_after, stable=url_after == url)
    if url_after != url:
        raise ArticleMismatchError(
            f"互动数采集期间活动标签发生变化：before={url!r}, after={url_after!r}"
        )
    verification = {
        "url_before": url,
        "url_after": url_after,
        "url_stable": True,
        "expected_card_title": expected_title or "",
        "parsed_title": page["title"],
        "parsed_account": page["account_name"],
        "tab_title": evidence.get("tab_title"),
        "viewport_title": evidence.get("viewport_title"),
        "same_frame_evidence": "article_evidence.png",
        "title_matched": title_matched,
        "title_similarity": title_similarity,
        "title_validation": "advisory_card_ocr",
        "account_matched": True,
    }
    (output_dir / "verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    storage_page = _prepare_storage_page(page, storage_account)
    if storage_page is not page:
        log_event(
            "article_account_stored_as_standard_name",
            account=storage_account,
            observed_account=page.get("account_name"),
            action="keep_mongo_name_standard",
        )
    result = ingest(
        url=url,
        metrics=metrics,
        mongo_uri=mongo_uri or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
        database_name=mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
        collection_name=mongo_collection or os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
        dry_run=not write_mongo,
        page=storage_page,
        target_collection_name=mongo_target_collection
        or os.getenv("MONGO_TARGET_COLLECTION", "collection_target"),
        expected_account_name=storage_account or expected_account,
    )
    log_event(
        "article_ingest_finished",
        url=url,
        title=page.get("title"),
        status=result.get("status"),
        write_mongo=write_mongo,
    )
    result["verification"] = verification
    append_local_exports(result, export_jsonl, export_csv)
    (output_dir / "collection.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return result


def _prepare_storage_page(
    page: dict[str, str],
    storage_account: str | None,
) -> dict[str, str]:
    """文章页实际公众号名只用于校验；入库时统一写回数据库标准名。"""
    if not storage_account or storage_account == str(page.get("account_name") or ""):
        return page
    return {**page, "account_name": storage_account}


def safe_path_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:60] or "unknown"


def _default_watch_state(account_name: str) -> dict[str, Any]:
    return {
        "version": 1,
        "account": account_name,
        "known_urls": [],
        "cycle_count": 0,
        "last_cycle_started_at": None,
        "last_cycle_finished_at": None,
        "last_error": None,
        "last_summary": None,
        "last_window_closed_at": None,
        "schedule": None,
    }


def load_watch_state(path: Path, account_name: str) -> dict[str, Any]:
    """加载监听状态；文件损坏或账号不一致时从空状态安全恢复。"""
    state = _default_watch_state(account_name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return state
    if not isinstance(raw, dict) or str(raw.get("account") or "").strip() != account_name:
        return state
    known_urls = {
        normalized
        for item in raw.get("known_urls") or []
        if (normalized := normalize_article_url(str(item)))
    }
    state.update(raw)
    # 资料页标签登记属于微信 UI 资源状态，不属于文章增量状态。旧版本曾把
    # profile 复制到每账号 watch-state.json；读取时主动丢弃，避免代码热更新
    # 后把旧 HWND、旧标签位置误当成可复用资源。
    state.pop("profile", None)
    state["account"] = account_name
    state["known_urls"] = list(dict.fromkeys(known_urls))
    try:
        state["cycle_count"] = max(0, int(raw.get("cycle_count") or 0))
    except (TypeError, ValueError):
        state["cycle_count"] = 0
    return state


def load_profile_pool_state(
    path: Path,
    accounts: list[str],
    accounts_per_vm: int,
) -> dict[str, Any]:
    """加载独立资料页池状态；标签身份必须在当前进程重新 OCR 确认。"""
    normalized_accounts = list(dict.fromkeys(item.strip() for item in accounts if item.strip()))
    state: dict[str, Any] = {
        "version": 1,
        "accounts": normalized_accounts,
        "accounts_per_vm": accounts_per_vm,
        "status": "unverified",
        "profile_registry": {},
        "profiles_ready": [],
        "profiles_unavailable": normalized_accounts,
        "last_bootstrap_started_at": None,
        "last_bootstrap_finished_at": None,
        "last_attach_started_at": None,
        "last_attach_finished_at": None,
    }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return state
    if not isinstance(raw, dict) or raw.get("accounts") != normalized_accounts:
        return state
    state.update(raw)
    state["version"] = 1
    state["accounts"] = normalized_accounts
    state["accounts_per_vm"] = accounts_per_vm
    state.setdefault("profile_registry", {})
    return state


def save_watch_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temporary_path.replace(path)


def _summary_urls(summary: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for key in ("collected", "skipped"):
        for item in summary.get(key) or []:
            if isinstance(item, dict):
                normalized = normalize_article_url(str(item.get("url") or ""))
                if normalized:
                    urls.add(normalized)
    return urls


def _sleep_in_chunks(seconds: float) -> None:
    """分段休眠，避免常驻监听无法及时响应 Ctrl-C。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(30.0, remaining))


def parse_watch_clock(value: str) -> int:
    """解析监听窗口时间，返回当天分钟数；允许用 24:00 表示次日零点。"""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise argparse.ArgumentTypeError("时间必须使用 HH:MM 格式，例如 07:30 或 24:00")
    hour, minute = int(match.group(1)), int(match.group(2))
    if minute > 59 or hour > 24 or (hour == 24 and minute != 0):
        raise argparse.ArgumentTypeError("时间必须在 00:00 到 24:00 之间")
    return hour * 60 + minute


def _watch_clock_datetime(now: datetime, day: date, minutes: int) -> datetime:
    base = now.replace(
        year=day.year,
        month=day.month,
        day=day.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return base + timedelta(minutes=minutes)


def watch_window_state(
    now: datetime,
    start_minutes: int,
    end_minutes: int,
) -> tuple[bool, datetime, datetime]:
    """返回当前是否在窗口内，以及当前/下一窗口的开始和结束时间。"""
    today = now.date()
    for day_offset in (-1, 0, 1):
        candidate_day = today + timedelta(days=day_offset)
        start_at = _watch_clock_datetime(now, candidate_day, start_minutes)
        end_at = _watch_clock_datetime(now, candidate_day, end_minutes)
        if end_at <= start_at:
            end_at += timedelta(days=1)
        if start_at <= now < end_at:
            return True, start_at, end_at
    today_start = _watch_clock_datetime(now, today, start_minutes)
    today_end = _watch_clock_datetime(now, today, end_minutes)
    if today_end <= today_start:
        today_end += timedelta(days=1)
    if now < today_start:
        return False, today_start, today_end
    next_start = _watch_clock_datetime(now, today + timedelta(days=1), start_minutes)
    next_end = _watch_clock_datetime(now, today + timedelta(days=1), end_minutes)
    if next_end <= next_start:
        next_end += timedelta(days=1)
    return False, next_start, next_end


def watch_single_account(
    client: QwenVisionClient,
    account_name: str,
    output_dir: Path,
    poll_interval_seconds: float,
    recent_card_limit: int,
    export_jsonl: str | None,
    export_csv: str | None,
    allow_vl: bool = True,
    write_mongo: bool = False,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    metric_mode: str = "share",
    task_timeout_minutes: float | None = None,
    watch_cycles: int = 0,
    manual_search_fallback_seconds: float = 0.0,
    schedule_start_minutes: int | None = None,
    schedule_end_minutes: int | None = None,
) -> dict[str, Any]:
    """持续监听单个公众号；0 轮表示直到 Ctrl-C，正数用于有限测试。"""
    state_path = output_dir / "watch-state.json"
    if (schedule_start_minutes is None) != (schedule_end_minutes is None):
        raise ValueError("监听时间窗口必须同时设置开始时间和结束时间")
    if schedule_start_minutes is not None and schedule_end_minutes is not None:
        if not (0 <= schedule_start_minutes <= 1440 and 0 <= schedule_end_minutes <= 1440):
            raise ValueError("监听时间窗口必须在 00:00 到 24:00 之间")
    state = load_watch_state(state_path, account_name)
    known_urls = set(state["known_urls"])
    if write_mongo:
        try:
            mongo_urls = load_account_article_urls(
                mongo_uri or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
                mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
                mongo_collection or os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
                account_name,
            )
            known_urls.update(mongo_urls)
            log_event(
                "watch_known_urls_loaded_from_mongo",
                account=account_name,
                count=len(mongo_urls),
            )
        except Exception as exc:
            log_event("watch_known_urls_mongo_load_failed", account=account_name, error=str(exc))
    state["known_urls"] = list(dict.fromkeys(known_urls))
    save_watch_state(state_path, state)
    log_event(
        "watch_started",
        account=account_name,
        poll_interval_seconds=poll_interval_seconds,
        recent_card_limit=recent_card_limit,
        known_urls=len(known_urls),
        watch_cycles=watch_cycles,
        state_path=str(state_path),
        schedule_start=(
            f"{schedule_start_minutes // 60:02d}:{schedule_start_minutes % 60:02d}"
            if schedule_start_minutes is not None
            else None
        ),
        schedule_end=(
            "24:00"
            if schedule_end_minutes == 1440
            else (
                f"{schedule_end_minutes // 60:02d}:{schedule_end_minutes % 60:02d}"
                if schedule_end_minutes is not None
                else None
            )
        ),
    )
    last_summary: dict[str, Any] | None = None
    waiting_for_start: str | None = None
    active_window_key: str | None = None
    try:
        while watch_cycles == 0 or state["cycle_count"] < watch_cycles:
            window_end: datetime | None = None
            if schedule_start_minutes is not None and schedule_end_minutes is not None:
                now = datetime.now(shanghai_timezone())
                in_window, window_start, window_end = watch_window_state(
                    now, schedule_start_minutes, schedule_end_minutes
                )
                window_key = window_start.isoformat()
                if not in_window:
                    if waiting_for_start != window_key:
                        waiting_for_start = window_key
                        log_event(
                            "watch_outside_schedule",
                            account=account_name,
                            now=now.isoformat(),
                            next_start=window_start.isoformat(),
                            next_end=window_end.isoformat(),
                        )
                    _sleep_in_chunks(min(30.0, max(1.0, (window_start - now).total_seconds())))
                    continue
                waiting_for_start = None
                if active_window_key != window_key:
                    active_window_key = window_key
                    state["schedule"] = {
                        "start": schedule_start_minutes,
                        "end": schedule_end_minutes,
                        "window_started_at": window_start.isoformat(),
                    }
                    save_watch_state(state_path, state)
                    log_event(
                        "watch_window_opened",
                        account=account_name,
                        window_start=window_start.isoformat(),
                        window_end=window_end.isoformat(),
                    )
            state["cycle_count"] += 1
            cycle_number = state["cycle_count"]
            cycle_started = datetime.now(timezone.utc)
            state["last_cycle_started_at"] = cycle_started.isoformat()
            state["last_error"] = None
            cycle_dir = output_dir / "cycles" / f"cycle-{cycle_number:05d}"
            log_event(
                "watch_cycle_started",
                account=account_name,
                cycle=cycle_number,
                known_urls=len(known_urls),
                output_dir=str(cycle_dir),
            )
            try:
                last_summary = collect_profile_account(
                    client,
                    account_name,
                    cycle_dir,
                    recent_card_limit,
                    export_jsonl,
                    export_csv,
                    allow_vl=allow_vl,
                    write_mongo=write_mongo,
                    mongo_uri=mongo_uri,
                    mongo_database=mongo_database,
                    mongo_collection=mongo_collection,
                    mongo_target_collection=mongo_target_collection,
                    metric_mode=metric_mode,
                    task_timeout_minutes=task_timeout_minutes,
                    scan_range="today",
                    recent_card_limit=recent_card_limit,
                    known_urls=known_urls,
                    stop_after_known_url=True,
                    manual_search_fallback_seconds=manual_search_fallback_seconds,
                )
                cycle_urls = _summary_urls(last_summary)
                new_urls = cycle_urls - known_urls
                known_urls.update(cycle_urls)
                state["known_urls"] = list(dict.fromkeys(known_urls))
                state["last_summary"] = {
                    "cycle": cycle_number,
                    "detected_articles": last_summary.get("detected_articles", 0),
                    "stop_reason": last_summary.get("stop_reason"),
                    "new_articles": len(new_urls),
                    "failures": len(last_summary.get("failures") or []),
                }
                state["last_cycle_finished_at"] = datetime.now(timezone.utc).isoformat()
                save_watch_state(state_path, state)
                log_event(
                    "watch_cycle_finished",
                    account=account_name,
                    cycle=cycle_number,
                    duration_seconds=round(
                        (datetime.now(timezone.utc) - cycle_started).total_seconds(), 3
                    ),
                    new_articles=len(new_urls),
                    known_url_stop=bool(last_summary.get("dedupe", {}).get("known_url_stop")),
                    failures=len(last_summary.get("failures") or []),
                    stop_reason=last_summary.get("stop_reason"),
                )
            except Exception as exc:
                state["last_error"] = str(exc)
                state["last_cycle_finished_at"] = datetime.now(timezone.utc).isoformat()
                save_watch_state(state_path, state)
                log_event(
                    "watch_cycle_failed",
                    account=account_name,
                    cycle=cycle_number,
                    error=str(exc),
                    category=classify_collection_error(exc),
                )
            if watch_cycles and state["cycle_count"] >= watch_cycles:
                break
            if window_end is not None and datetime.now(shanghai_timezone()) >= window_end:
                state["last_window_closed_at"] = datetime.now(shanghai_timezone()).isoformat()
                save_watch_state(state_path, state)
                log_event(
                    "watch_window_closed",
                    account=account_name,
                    cycle=cycle_number,
                    reason="当前采集轮次已完成，达到日终时间",
                    window_end=window_end.isoformat(),
                )
                active_window_key = None
                continue
            log_event(
                "watch_sleeping",
                account=account_name,
                cycle=cycle_number,
                seconds=poll_interval_seconds,
            )
            _sleep_in_chunks(poll_interval_seconds)
    except KeyboardInterrupt:
        log_event("watch_stopped", account=account_name, reason="keyboard_interrupt")
    save_watch_state(state_path, state)
    log_event(
        "watch_finished",
        account=account_name,
        cycles=state["cycle_count"],
        known_urls=len(known_urls),
        last_error=state.get("last_error"),
    )
    return {
        "account": account_name,
        "mode": "watch",
        "cycles": state["cycle_count"],
        "known_urls": len(known_urls),
        "state_path": str(state_path),
        "last_error": state.get("last_error"),
        "last_summary": state.get("last_summary"),
    }


def watch_multiple_accounts(
    client: QwenVisionClient,
    account_names: list[str],
    output_dir: Path,
    poll_interval_seconds: float,
    recent_card_limit: int,
    export_jsonl: str | None,
    export_csv: str | None,
    *,
    allow_vl: bool = True,
    write_mongo: bool = False,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    metric_mode: str = "share",
    task_timeout_minutes: float | None = None,
    watch_cycles: int = 0,
    manual_search_fallback_seconds: float = 0.0,
    schedule_start_minutes: int | None = None,
    schedule_end_minutes: int | None = None,
    max_accounts_per_vm: int = 10,
    profile_pool_mode: str = "bootstrap_and_watch",
) -> dict[str, Any]:
    """多账号串行轮询；账号列表只有一项时等价于单账号监听。"""
    global _WATCH_PROFILE_CACHE, _WATCH_ACTIVE_PROFILE_ACCOUNT
    # 所有标签热状态只对当前监听进程和本次预热建立的页面池有效。
    _WATCH_PROFILE_CACHE = {}
    _WATCH_PROFILE_TRANSITIONS.clear()
    _WATCH_PROFILE_SCAN_STATE.clear()
    _WATCH_PROFILE_REBUILD_LOCKS.clear()
    _SEARCH_SCAN_RECOVERED_PROFILE.clear()
    _WATCH_ACTIVE_PROFILE_ACCOUNT = None
    accounts = list(dict.fromkeys(name.strip() for name in account_names if name.strip()))
    if not accounts:
        raise ValueError("多账号监听至少需要一个公众号")
    if len(accounts) > max_accounts_per_vm:
        raise ValueError(
            f"当前虚拟机最多监听 {max_accounts_per_vm} 个账号，实际收到 {len(accounts)} 个；"
            f"若确认单机运行全部账号，请显式传入 --accounts-per-vm {len(accounts)}"
        )
    if (schedule_start_minutes is None) != (schedule_end_minutes is None):
        raise ValueError("监听时间窗口必须同时设置开始时间和结束时间")
    if profile_pool_mode not in {
        "bootstrap_and_watch",
        "bootstrap_only",
        "existing_only",
    }:
        raise ValueError(f"未知资料页池运行模式：{profile_pool_mode}")

    output_dir.mkdir(parents=True, exist_ok=True)
    multi_account = len(accounts) > 1
    account_dirs = {
        account: output_dir / safe_path_name(account) if multi_account else output_dir
        for account in accounts
    }
    states: dict[str, dict[str, Any]] = {}
    known_urls: dict[str, set[str]] = {}
    for account in accounts:
        account_dir = account_dirs[account]
        state = (
            _default_watch_state(account)
            if profile_pool_mode == "bootstrap_only"
            else load_watch_state(account_dir / "watch-state.json", account)
        )
        urls = set(state.get("known_urls") or [])
        if write_mongo and profile_pool_mode != "bootstrap_only":
            try:
                mongo_urls = load_account_article_urls(
                    mongo_uri or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
                    mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
                    mongo_collection or os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
                    account,
                )
                urls.update(mongo_urls)
                log_event("watch_known_urls_loaded_from_mongo", account=account, count=len(mongo_urls))
            except Exception as exc:
                log_event("watch_known_urls_mongo_load_failed", account=account, error=str(exc))
        state["known_urls"] = list(dict.fromkeys(urls))
        state.setdefault("consecutive_failures", 0)
        state.pop("profile", None)
        states[account] = state
        known_urls[account] = urls
        if profile_pool_mode != "bootstrap_only":
            save_watch_state(account_dir / "watch-state.json", state)

    scheduler_path = output_dir / "scheduler-state.json"
    scheduler_state: dict[str, Any] = {
        "version": 3,
        "accounts": accounts,
        "accounts_per_vm": max_accounts_per_vm,
        "phase": "waiting_for_profile_pool",
        "round_count": 0,
        "last_error": None,
    }
    if profile_pool_mode != "bootstrap_only":
        try:
            previous = json.loads(scheduler_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("accounts") == accounts:
                scheduler_state.update(previous)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
    # v2 曾把资料页登记混在调度状态中。v3 调度文件只保存轮询进度；资料页池
    # 独立保存，进程重启后仍必须通过 OCR 重新附着，不能直接信任旧 HWND。
    scheduler_state.pop("profile_registry", None)
    scheduler_state.pop("profiles_ready", None)
    scheduler_state.pop("profiles_unavailable", None)
    scheduler_state["version"] = 3
    scheduler_state["phase"] = "waiting_for_profile_pool"
    if profile_pool_mode != "bootstrap_only":
        save_watch_state(scheduler_path, scheduler_state)
    profile_pool_path = output_dir / "profile-pool-state.json"
    profile_pool_state = load_profile_pool_state(
        profile_pool_path, accounts, max_accounts_per_vm
    )
    reuse_existing_profile_tabs = bool(profile_pool_state.get("profile_registry"))
    save_watch_state(profile_pool_path, profile_pool_state)
    log_event(
        "watch_profile_pool_process_started"
        if profile_pool_mode == "bootstrap_only"
        else "watch_scheduler_started",
        accounts=accounts,
        accounts_total=len(accounts),
        accounts_per_vm=max_accounts_per_vm,
        poll_interval_seconds=poll_interval_seconds,
        recent_card_limit=recent_card_limit,
        state_path=str(scheduler_path),
        profile_pool_state_path=str(profile_pool_path),
        profile_pool_mode=profile_pool_mode,
    )

    # 保持账号文件中的顺序。多个账号在 Windows 上可能获得完全相同的到期
    # 时间；使用 set 会让首轮随机从中间账号开始。
    pending = list(accounts)
    account_order = {account: index for index, account in enumerate(accounts)}
    next_due = {account: datetime.now(timezone.utc) for account in accounts}
    completed_rounds = 0
    initial_round_count = max(0, int(scheduler_state.get("round_count") or 0))
    waiting_for_start: str | None = None
    active_window_key: str | None = None
    profiles_bootstrapped = False

    def bootstrap_profiles(reason: str, *, existing_only: bool = False) -> None:
        """启动监听前逐个建立并验证资料页缓存。

        资料页预热与文章采集分离：这里不打开文章，只把搜一搜窗口和已确认的
        公众号资料页标签注册到内存缓存。独立预热模式允许搜索建池；热更新附着
        模式只扫描现有标签，某个账号失败时保留其他账号且禁止自动搜索重建。
        """
        nonlocal profiles_bootstrapped
        phase = "attach_existing_profile_pool" if existing_only else "bootstrap_profiles"
        scheduler_state["phase"] = phase
        timestamp_key = "last_attach_started_at" if existing_only else "last_bootstrap_started_at"
        profile_pool_state[timestamp_key] = datetime.now(timezone.utc).isoformat()
        profile_pool_state["status"] = "attaching" if existing_only else "bootstrapping"
        profile_pool_state["reason"] = reason
        if profile_pool_mode != "bootstrap_only":
            save_watch_state(scheduler_path, scheduler_state)
        save_watch_state(profile_pool_path, profile_pool_state)
        log_event(
            "watch_profile_pool_attach_started" if existing_only else "watch_profile_bootstrap_started",
            accounts=accounts,
            accounts_total=len(accounts),
            reason=reason,
        )

        attached_profiles: dict[str, WindowInfo] = {}
        if existing_only:
            try:
                browser_window = (
                    next(iter(_WATCH_PROFILE_CACHE.values()))
                    if _WATCH_PROFILE_CACHE
                    else find_profile_pool_browser_window(profile_pool_state)
                )
                browser_window = arrange_automation_window(browser_window, "profile")
                attached_profiles, inventory_result = inventory_existing_profile_pool(
                    browser_window,
                    accounts,
                    output_dir / "profile-pool-inventory",
                )
                log_event(
                    "watch_profile_pool_inventory_applied",
                    profiles_found=inventory_result.get("profiles_found") or [],
                    profiles_missing=inventory_result.get("profiles_missing") or [],
                    completed_cycle=bool(inventory_result.get("completed_cycle")),
                )
            except Exception as inventory_exc:
                log_event(
                    "watch_profile_pool_inventory_failed",
                    error=str(inventory_exc),
                    action="mark_accounts_temporarily_unverified_without_search",
                )

        for account in accounts:
            account_dir = account_dirs[account]
            bootstrap_dir = account_dir / "bootstrap"
            bootstrap_error = ""
            profile_window: WindowInfo | None = None
            observed_name = account
            max_attempts = 1 if existing_only else 3
            for attempt in range(1, max_attempts + 1):
                try:
                    log_event(
                        "watch_profile_bootstrap_attempt",
                        account=account,
                        attempt=attempt,
                        output_dir=str(bootstrap_dir / f"attempt-{attempt}"),
                    )
                    if attempt == 1 and existing_only:
                        profile_window = attached_profiles.get(account)
                        if profile_window is not None:
                            observed_name = account
                            log_event(
                                "watch_profile_bootstrap_existing_tab_reused",
                                account=account,
                                hwnd=profile_window.hwnd,
                                method="single-pass-profile-pool-inventory",
                            )
                        elif (
                            profile_pool_state.get("profile_registry", {})
                            .get(account, {})
                            .get("empty_article_profile")
                        ):
                            try:
                                browser_window = find_profile_pool_browser_window(
                                    profile_pool_state
                                )
                                if activate_embedded_profile_tab(
                                    browser_window,
                                    account,
                                    max_tabs=WIN11_MAX_TAB_SCAN,
                                    allow_name_only_profile=True,
                                ):
                                    profile_window = WindowInfo(
                                        browser_window.hwnd,
                                        browser_window.title,
                                        browser_window.class_name,
                                        browser_window.rect,
                                        browser_window.process_name,
                                        "embedded_profile_tab",
                                        empty_article_profile=True,
                                    )
                                    observed_name = account
                                    log_event(
                                        "watch_profile_bootstrap_existing_tab_reused",
                                        account=account,
                                        hwnd=profile_window.hwnd,
                                        method="empty-service-name-only-fallback",
                                    )
                            except Exception as empty_exc:
                                log_event(
                                    "watch_profile_bootstrap_existing_tab_probe_failed",
                                    account=account,
                                    error=str(empty_exc),
                                    action="continue_without_search",
                                )
                    elif attempt == 1 and reuse_existing_profile_tabs:
                        try:
                            if _WATCH_PROFILE_CACHE:
                                browser_window = next(iter(_WATCH_PROFILE_CACHE.values()))
                            else:
                                browser_window = find_profile_pool_browser_window(profile_pool_state)
                            if activate_embedded_profile_tab(
                                browser_window,
                                account,
                                max_tabs=WIN11_MAX_TAB_SCAN,
                            ):
                                profile_window = WindowInfo(
                                    browser_window.hwnd,
                                    browser_window.title,
                                    browser_window.class_name,
                                    browser_window.rect,
                                    browser_window.process_name,
                                    "embedded_profile_tab",
                                )
                                profile_window = arrange_automation_window(
                                    profile_window, "profile"
                                )
                                observed_name = account
                                log_event(
                                    "watch_profile_bootstrap_existing_tab_reused",
                                    account=account,
                                    hwnd=profile_window.hwnd,
                                )
                        except Exception as reuse_exc:
                            log_event(
                                "watch_profile_bootstrap_existing_tab_probe_failed",
                                account=account,
                                error=str(reuse_exc),
                                action="continue_with_search",
                            )
                    if profile_window is None and existing_only:
                        raise ProfileTemporarilyUnverifiedError(
                            "现有资料页池中未确认目标账号；热更新附着模式禁止自动搜索重建"
                        )
                    if profile_window is None:
                        profile_window, observed_name = search_and_open_profile(
                            account,
                            bootstrap_dir / f"attempt-{attempt}",
                            client=client,
                            allow_vl=allow_vl,
                            manual_fallback_seconds=manual_search_fallback_seconds,
                        )
                    _WATCH_PROFILE_CACHE[account] = profile_window
                    if existing_only:
                        # 热更新附着只负责把已经精确确认的资料页重新登记到内存。
                        # 不在这里寻找搜一搜或扫描全标签去重，否则每成功附着一个
                        # 账号都会再次绕行整个标签池，并把“当前不是搜一搜”记录成
                        # 容易误解的 found=false。重复页只允许在已经获得同账号重复
                        # 证据后，通过账号级定向维护流程处理。
                        log_event(
                            "watch_profile_attach_registered_without_dedup",
                            account=account,
                            hwnd=profile_window.hwnd,
                            reason="hot_attach_only_registers_exact_profile_match",
                        )
                    registry_item = {
                        "status": "ready",
                        "account": account,
                        "observed_name": observed_name,
                        "page_kind": profile_window.page_kind,
                        "empty_article_profile": bool(profile_window.empty_article_profile),
                        "hwnd": profile_window.hwnd,
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                        "bootstrap_attempt": attempt,
                    }
                    profile_pool_state.setdefault("profile_registry", {})[account] = registry_item
                    states[account]["last_error"] = None
                    if profile_pool_mode != "bootstrap_only":
                        save_watch_state(account_dir / "watch-state.json", states[account])
                    save_watch_state(profile_pool_path, profile_pool_state)
                    log_event(
                        "watch_profile_pool_attach_succeeded" if existing_only else "watch_profile_bootstrap_succeeded",
                        account=account,
                        observed_name=observed_name,
                        page_kind=profile_window.page_kind,
                        hwnd=profile_window.hwnd,
                        attempt=attempt,
                    )
                    break
                except Exception as exc:
                    bootstrap_error = str(exc)
                    log_event(
                        "watch_profile_bootstrap_attempt_failed",
                        account=account,
                        attempt=attempt,
                        error=bootstrap_error,
                        category=classify_collection_error(exc),
                    )
                    time.sleep(0.8)
            else:
                registry_item = {
                    "status": "temporarily_unverified",
                    "account": account,
                    "verified_at": None,
                    "last_error": bootstrap_error,
                }
                profile_pool_state.setdefault("profile_registry", {})[account] = registry_item
                states[account]["last_error"] = bootstrap_error
                states[account]["consecutive_failures"] = int(
                    states[account].get("consecutive_failures") or 0
                ) + 1
                if profile_pool_mode != "bootstrap_only":
                    save_watch_state(account_dir / "watch-state.json", states[account])
                save_watch_state(profile_pool_path, profile_pool_state)
                log_event(
                    "watch_profile_pool_attach_failed" if existing_only else "watch_profile_bootstrap_failed",
                    account=account,
                    error=bootstrap_error,
                    category=classify_collection_error(RuntimeError(bootstrap_error)),
                    action="defer_without_declaring_profile_absent",
                )

        scheduler_state["phase"] = "profile_pool_ready"
        finished_key = "last_attach_finished_at" if existing_only else "last_bootstrap_finished_at"
        profile_pool_state[finished_key] = datetime.now(timezone.utc).isoformat()
        profile_pool_state["profiles_ready"] = [
            account
            for account in accounts
            if account in _WATCH_PROFILE_CACHE
        ]
        profile_pool_state["profiles_unavailable"] = [
            account
            for account in accounts
            if account not in _WATCH_PROFILE_CACHE
        ]
        profile_pool_state["status"] = (
            "ready" if not profile_pool_state["profiles_unavailable"] else "partially_ready"
        )
        if profile_pool_mode != "bootstrap_only":
            save_watch_state(scheduler_path, scheduler_state)
        save_watch_state(profile_pool_path, profile_pool_state)
        profiles_bootstrapped = True
        log_event(
            "watch_profile_pool_attach_finished" if existing_only else "watch_profile_bootstrap_finished",
            profiles_ready=profile_pool_state["profiles_ready"],
            profiles_unavailable=profile_pool_state["profiles_unavailable"],
            action=(
                "exit_after_profile_pool_ready"
                if profile_pool_mode == "bootstrap_only"
                else "start_serial_profile_polling"
            ),
        )

    if profile_pool_mode == "bootstrap_only":
        bootstrap_profiles("explicit_profile_pool_bootstrap")
        return {
            "mode": "bootstrap_profile_pool",
            "accounts_total": len(accounts),
            "profiles_ready": profile_pool_state.get("profiles_ready", []),
            "profiles_unavailable": profile_pool_state.get("profiles_unavailable", []),
            "state_path": str(profile_pool_path),
            "last_error": None,
        }

    try:
        while watch_cycles == 0 or completed_rounds < watch_cycles:
            if schedule_start_minutes is not None and schedule_end_minutes is not None:
                now_shanghai = datetime.now(shanghai_timezone())
                in_window, window_start, window_end = watch_window_state(
                    now_shanghai, schedule_start_minutes, schedule_end_minutes
                )
                window_key = window_start.isoformat()
                if not in_window:
                    if waiting_for_start != window_key:
                        waiting_for_start = window_key
                        log_event(
                            "watch_outside_schedule",
                            accounts=accounts,
                            now=now_shanghai.isoformat(),
                            next_start=window_start.isoformat(),
                            next_end=window_end.isoformat(),
                        )
                    _sleep_in_chunks(min(30.0, max(1.0, (window_start - now_shanghai).total_seconds())))
                    continue
                waiting_for_start = None
                if active_window_key != window_key:
                    active_window_key = window_key
                    scheduler_state["schedule"] = {
                        "start": schedule_start_minutes,
                        "end": schedule_end_minutes,
                        "window_started_at": window_start.isoformat(),
                    }
                    save_watch_state(scheduler_path, scheduler_state)
                    log_event(
                        "watch_window_opened",
                        accounts=accounts,
                        window_start=window_start.isoformat(),
                        window_end=window_end.isoformat(),
                    )

            if not profiles_bootstrapped:
                bootstrap_profiles(
                    "schedule_window_opened"
                    if schedule_start_minutes is not None
                    else "listener_start",
                    existing_only=profile_pool_mode == "existing_only",
                )
                scheduler_state["phase"] = "poll_profiles"
                save_watch_state(scheduler_path, scheduler_state)

            if not pending:
                completed_rounds += 1
                scheduler_state["round_count"] = initial_round_count + completed_rounds
                scheduler_state["last_round_finished_at"] = datetime.now(timezone.utc).isoformat()
                save_watch_state(scheduler_path, scheduler_state)
                if watch_cycles and completed_rounds >= watch_cycles:
                    break
                pending = list(accounts)
                due_at = datetime.now(timezone.utc) + timedelta(seconds=poll_interval_seconds)
                next_due = {account: due_at for account in accounts}

            now = datetime.now(timezone.utc)
            ready = [account for account in pending if next_due[account] <= now]
            if not ready:
                wait_seconds = min((next_due[account] - now).total_seconds() for account in pending)
                log_event("watch_scheduler_sleeping", seconds=round(max(0.0, wait_seconds), 3), pending_accounts=len(pending))
                _sleep_in_chunks(min(30.0, max(1.0, wait_seconds)))
                continue

            account = min(
                ready,
                key=lambda item: (next_due[item], account_order[item]),
            )
            state = states[account]
            state["cycle_count"] = max(0, int(state.get("cycle_count") or 0)) + 1
            cycle_number = state["cycle_count"]
            account_dir = account_dirs[account]
            cycle_dir = account_dir / "cycles" / f"cycle-{cycle_number:05d}"
            cycle_started = datetime.now(timezone.utc)
            state["last_cycle_started_at"] = cycle_started.isoformat()
            state["last_error"] = None
            save_watch_state(account_dir / "watch-state.json", state)
            log_event(
                "watch_account_cycle_started",
                account=account,
                cycle=cycle_number,
                pending_accounts=len(pending),
                known_urls=len(known_urls[account]),
                output_dir=str(cycle_dir),
            )

            cached_profile = _WATCH_PROFILE_CACHE.get(account)
            try:
                if cached_profile is None and profile_pool_mode == "existing_only":
                    try:
                        browser_window = (
                            next(iter(_WATCH_PROFILE_CACHE.values()))
                            if _WATCH_PROFILE_CACHE
                            else find_profile_pool_browser_window(profile_pool_state)
                        )
                        allow_empty_name_only = bool(
                            profile_pool_state.get("profile_registry", {})
                            .get(account, {})
                            .get("empty_article_profile")
                        )
                        if activate_embedded_profile_tab(
                            browser_window,
                            account,
                            max_tabs=WIN11_MAX_TAB_SCAN,
                            allow_name_only_profile=allow_empty_name_only,
                        ):
                            cached_profile = arrange_automation_window(
                                WindowInfo(
                                    browser_window.hwnd,
                                    browser_window.title,
                                    browser_window.class_name,
                                    browser_window.rect,
                                    browser_window.process_name,
                                    "embedded_profile_tab",
                                    empty_article_profile=allow_empty_name_only,
                                ),
                                "profile",
                            )
                            _WATCH_PROFILE_CACHE[account] = cached_profile
                            log_event(
                                "watch_profile_pool_reattached",
                                account=account,
                                cycle=cycle_number,
                                hwnd=cached_profile.hwnd,
                            )
                    except Exception as attach_exc:
                        log_event(
                            "watch_profile_pool_reattach_failed",
                            account=account,
                            cycle=cycle_number,
                            error=str(attach_exc),
                            action="skip_without_search_rebuild",
                        )
                    if cached_profile is None:
                        raise ProfileTemporarilyUnverifiedError(
                            "热更新采集模式未附着到现有资料页，本轮跳过且不自动搜索重建"
                        )
                summary = collect_profile_account(
                    client,
                    account,
                    cycle_dir,
                    recent_card_limit,
                    export_jsonl,
                    export_csv,
                    allow_vl=allow_vl,
                    write_mongo=write_mongo,
                    mongo_uri=mongo_uri,
                    mongo_database=mongo_database,
                    mongo_collection=mongo_collection,
                    mongo_target_collection=mongo_target_collection,
                    metric_mode=metric_mode,
                    task_timeout_minutes=task_timeout_minutes,
                    scan_range="today",
                    recent_card_limit=recent_card_limit,
                    known_urls=known_urls[account],
                    stop_after_known_url=True,
                    manual_search_fallback_seconds=manual_search_fallback_seconds,
                    reuse_profile_window=cached_profile,
                    preserve_profile=True,
                    refresh_profile=cached_profile is not None,
                )
                cycle_urls = _summary_urls(summary)
                new_urls = cycle_urls - known_urls[account]
                known_urls[account].update(cycle_urls)
                state["known_urls"] = list(dict.fromkeys(known_urls[account]))
                state["last_summary"] = {
                    "cycle": cycle_number,
                    "detected_articles": summary.get("detected_articles", 0),
                    "stop_reason": summary.get("stop_reason"),
                    "new_articles": len(new_urls),
                    "failures": len(summary.get("failures") or []),
                }
                state["consecutive_failures"] = 0
                cached_profile = _WATCH_PROFILE_CACHE.get(account)
                if cached_profile is not None:
                    registry_item = profile_pool_state.setdefault("profile_registry", {}).get(account, {})
                    registry_item.update(
                        {
                            "status": "ready",
                            "account": account,
                            "page_kind": cached_profile.page_kind,
                            "empty_article_profile": bool(
                                cached_profile.empty_article_profile
                            ),
                            "hwnd": cached_profile.hwnd,
                            "last_ready_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    profile_pool_state["profile_registry"][account] = registry_item
                log_event(
                    "watch_account_cycle_finished",
                    account=account,
                    cycle=cycle_number,
                    duration_seconds=round((datetime.now(timezone.utc) - cycle_started).total_seconds(), 3),
                    new_articles=len(new_urls),
                    known_url_stop=bool(summary.get("dedupe", {}).get("known_url_stop")),
                    failures=len(summary.get("failures") or []),
                    stop_reason=summary.get("stop_reason"),
                )
            except Exception as exc:
                scan_state = _WATCH_PROFILE_SCAN_STATE.get(account, {})
                completed_cycle = bool(scan_state.get("completed_cycle"))
                temporary = isinstance(exc, ProfileTemporarilyUnverifiedError) or not completed_cycle
                if not temporary:
                    _WATCH_PROFILE_CACHE.pop(account, None)
                registry_item = profile_pool_state.setdefault("profile_registry", {}).get(account, {})
                registry_item.update(
                    {
                        "status": (
                            "temporarily_unverified" if temporary else "rebuild_required"
                        ),
                        "last_error": str(exc),
                        "last_failed_at": datetime.now(timezone.utc).isoformat(),
                        "last_scan_state": scan_state,
                    }
                )
                profile_pool_state["profile_registry"][account] = registry_item
                state["last_error"] = str(exc)
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                log_event(
                    "watch_account_cycle_failed",
                    account=account,
                    cycle=cycle_number,
                    error=str(exc),
                    category=classify_collection_error(exc),
                    profile_status=registry_item["status"],
                    action=(
                        "retry_later_without_rebuild"
                        if temporary
                        else "allow_account_scoped_rebuild"
                    ),
                )
            state["last_cycle_finished_at"] = datetime.now(timezone.utc).isoformat()
            save_watch_state(account_dir / "watch-state.json", state)
            profile_pool_state["profiles_ready"] = [
                item for item in accounts if item in _WATCH_PROFILE_CACHE
            ]
            profile_pool_state["profiles_unavailable"] = [
                item for item in accounts if item not in _WATCH_PROFILE_CACHE
            ]
            profile_pool_state["status"] = (
                "ready" if not profile_pool_state["profiles_unavailable"] else "partially_ready"
            )
            save_watch_state(scheduler_path, scheduler_state)
            save_watch_state(profile_pool_path, profile_pool_state)
            pending.remove(account)
            next_due[account] = datetime.now(timezone.utc) + timedelta(seconds=poll_interval_seconds)
    except KeyboardInterrupt:
        log_event("watch_scheduler_stopped", accounts=accounts, reason="keyboard_interrupt")
    finally:
        save_watch_state(scheduler_path, scheduler_state)
        save_watch_state(profile_pool_path, profile_pool_state)

    account_results = []
    total_known_urls = 0
    last_error = None
    for account in accounts:
        state = states[account]
        total_known_urls += len(state.get("known_urls") or [])
        last_error = state.get("last_error") or last_error
        account_results.append(
            {
                "account": account,
                "cycles": state.get("cycle_count", 0),
                "known_urls": len(state.get("known_urls") or []),
                "state_path": str(account_dirs[account] / "watch-state.json"),
                "last_error": state.get("last_error"),
                "last_summary": state.get("last_summary"),
            }
        )
    return {
        "mode": "watch",
        "accounts": account_results,
        "accounts_total": len(accounts),
        "rounds": scheduler_state.get("round_count", initial_round_count + completed_rounds),
        "known_urls": total_known_urls,
        "state_path": str(scheduler_path),
        "profile_pool_state_path": str(profile_pool_path),
        "last_error": last_error,
    }


def classify_collection_error(error: BaseException) -> str:
    """把采集异常归类，便于失败队列按类型重试和统计。"""
    text = str(error).lower()
    if "未找到可确认的同名公众号" in text or "没有精确匹配名称" in text:
        return "account_not_found"
    if "筛选未确认选中" in text or "二级公众号筛选" in text:
        return "account_filter"
    if (
        "资料窗口顶部名称不匹配" in text
        or "资料页整屏未找到名称匹配" in text
        or "缺少公众号资料页结构证据" in text
        or "当前仍是搜一搜页面" in text
    ):
        return "profile_validation"
    if "ocr" in text or "识别" in text or "模板" in text:
        return "interaction_ocr"
    if "复制链接" in text or "clipboard" in text or "url" in text:
        return "copy_link"
    if "窗口" in text or "window" in text or "标签页" in text or "tab" in text:
        return "window"
    if "http" in text or "网络" in text or "timeout" in text or "timed out" in text:
        return "network"
    if "mongodb" in text or "mongo" in text or "入库" in text:
        return "mongodb"
    return "unknown"


def append_failure_queue(output_dir: Path, item: dict[str, Any]) -> None:
    """追加失败文章队列，下一次任务可据此优先补采。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "failure-queue.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")


def recent_visible_articles(
    articles: list[dict[str, Any]], scan_range: str = "today_yesterday"
) -> list[dict[str, Any]]:
    """继承时间分组标签，并按选择的日期范围筛选文章。"""
    recent: list[dict[str, Any]] = []
    current_group = ""
    for article in sorted(articles, key=lambda item: item["screen_point"][1]):
        visible_time = str(article.get("visible_time") or "").strip()
        if visible_time:
            current_group = visible_time
        article["effective_visible_time"] = current_group
        if is_recent_time_group(current_group, scan_range):
            recent.append(article)
    return recent


def run_one_account(
    client: QwenVisionClient,
    output_dir: Path,
    account_index: int,
    max_articles: int,
    export_jsonl: str | None,
    export_csv: str | None,
    metric_mode: str = "all",
    scan_range: str = "today_yesterday",
) -> dict[str, Any]:
    manager_result = analyze_current_window(client, output_dir / "manager-before")
    accounts = manager_result["accounts"]
    if not 0 <= account_index < len(accounts):
        raise IndexError("公众号序号超出当前屏识别结果范围")
    account = accounts[account_index]
    click(*account["screen_point"])
    time.sleep(2)

    selected_result = analyze_current_window(client, output_dir / "account-selected")
    articles = recent_visible_articles(
        selected_result["articles"], scan_range
    )[:max_articles]
    collected: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for index, article in enumerate(articles, start=1):
        try:
            click(*article["screen_point"])
            time.sleep(2.5)
            article_dir = output_dir / f"article-{index:02d}-{safe_path_name(article['title'])}"
            record = collect_open_article(
                client,
                article_dir,
                write_mongo=False,
                export_jsonl=export_jsonl,
                export_csv=export_csv,
                metric_mode=metric_mode,
                scan_range=scan_range,
            )
            if record.get("status") == "skipped_outside_scan_range":
                skipped.append(
                    {
                        "title": article.get("title", ""),
                        "reason": str(record.get("skip_reason") or "真实发布时间不在扫描范围"),
                    }
                )
            else:
                collected.append({key: value for key, value in record.items() if key != "content"})
        except Exception as exc:
            failures.append({"title": article.get("title", ""), "error": str(exc)})
        finally:
            try:
                article_hwnd, _ = find_article_window()
                activate_window(article_hwnd)
                press_ctrl_w()
                time.sleep(0.8)
            except Exception:
                pass
            manager_hwnd, _ = find_wechat_manager_window()
            activate_window(manager_hwnd)

    summary = {
        "account": account.get("name"),
        "recognized_recent_articles": len(articles),
        "collected": collected,
        "skipped": skipped,
        "failures": failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return summary


def analyze_current_window(client: QwenVisionClient, output_dir: Path) -> dict[str, Any]:
    hwnd, rect = find_wechat_manager_window()
    activate_window(hwnd)
    # 恢复窗口后位置可能变化，重新读取矩形。
    raw = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(raw))
    rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
    screenshot = capture_window(rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot.save(output_dir / "wechat_window.png")

    layout = client.detect_manager_layout(screenshot)
    if not layout.get("is_manager_visible"):
        raise RuntimeError("当前微信窗口没有显示公众号管理页面")
    sidebar_box = normalized_bbox_to_pixels(layout["account_sidebar_bbox_1000"], screenshot)
    content_box = normalized_bbox_to_pixels(layout["article_content_bbox_1000"], screenshot)
    sidebar = screenshot.crop(sidebar_box)
    content = screenshot.crop(content_box)
    sidebar.save(output_dir / "sidebar.png")
    content.save(output_dir / "content.png")

    accounts = client.detect_accounts(sidebar)
    articles = client.detect_articles(content)
    for account in accounts:
        account["screen_point"] = normalized_to_screen(account, sidebar_box, rect)
    for article in articles:
        article["screen_point"] = normalized_to_screen(article, content_box, rect)

    result = {
        "window": rect.__dict__,
        "layout": layout,
        "sidebar_box": sidebar_box,
        "content_box": content_box,
        "accounts": accounts,
        "articles": articles,
    }
    (output_dir / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def search_and_open_account(
    client: QwenVisionClient,
    account_name: str,
    output_dir: Path,
    allow_vl: bool = True,
) -> WindowInfo:
    search_window = find_search_window()
    # 先准备查询文本，再激活并操作微信搜索框。
    set_clipboard_text(account_name)
    activate_window(search_window.hwnd)
    search_ocr = WeChatOCR()
    # Ctrl+F 唤起微信全局搜索，再由 OCR 定位输入框，兼容窗口尺寸及当前页面变化。
    press_ctrl_f()
    time.sleep(0.5)
    before_search = capture_window(search_window.rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    before_search.save(output_dir / "before-search.png")
    search_box = search_ocr.locate_search_box(before_search, account_name)
    if not search_box.get("found"):
        raise RuntimeError(str(search_box.get("reason") or "无法定位微信搜索框"))
    click(
        search_window.rect.left
        + round(search_window.rect.width * int(search_box["center_x_1000"]) / 1000),
        search_window.rect.top
        + round(search_window.rect.height * int(search_box["center_y_1000"]) / 1000),
    )
    time.sleep(0.2)
    press_ctrl_a()
    press_ctrl_v()
    time.sleep(2.0)

    screenshot = capture_window(search_window.rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot.save(output_dir / "search-result.png")
    try:
        target = search_ocr.locate_official_account_result(screenshot, account_name)
        if not target.get("found"):
            raise ValueError(str(target.get("reason") or "本地OCR没有定位到公众号"))
    except Exception as exc:
        if not allow_vl:
            raise RuntimeError(f"本地公众号搜索失败且已禁用VL：{exc}") from exc
        # 窗口主题或版面变化时保留 VL 兜底，但正常搜索不再消耗 VL。
        target = client.detect_search_account(screenshot, account_name)
        target["method"] = "qwen-vl-fallback"
        target["fallback_reason"] = str(exc)
    (output_dir / "search-detection.json").write_text(
        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not target.get("found") or normalize_title(str(target.get("name") or "")) != normalize_title(account_name):
        raise RuntimeError(f"搜索结果中没有公众号精确匹配项：{account_name}")
    click(
        search_window.rect.left + round(search_window.rect.width * int(target["center_x_1000"]) / 1000),
        search_window.rect.top + round(search_window.rect.height * int(target["center_y_1000"]) / 1000),
    )

    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            account_window = find_account_message_window(account_name)
            activate_window(account_window.hwnd)
            return account_window
        except RuntimeError:
            time.sleep(0.3)
    raise RuntimeError(f"点击搜索结果后未打开公众号窗口：{account_name}")


def _normalize_account_name_for_confirmation(value: object) -> str:
    """标准化用于公众号精确确认的名称。

    这里故意不使用文章标题的模糊匹配逻辑：公众号名称一旦点错，后面的文章、
    链接和互动数就会全部归属错误。仅忽略 Unicode 形式、空白和 ASCII 大小写差异。
    """
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _qwen_profile_header_confirmed(
    validation: dict[str, Any], expected_name: str
) -> bool:
    """只依据模型读到的精确名称和置信度确认资料页。

    部分兼容网关会返回正确的 ``name``，但把派生字段 ``matched`` 错置为 false。
    名称仍必须精确一致，并要求足够置信度；因此不会放宽到相似公众号。
    """
    observed_name = str(validation.get("name") or "").strip()
    try:
        confidence = float(validation.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        bool(observed_name)
        and confidence >= 0.80
        and _normalize_account_name_for_confirmation(observed_name)
        == _normalize_account_name_for_confirmation(expected_name)
    )


def _qwen_search_target(
    client: QwenVisionClient,
    screenshot: Image.Image,
    expected_name: str,
) -> dict[str, Any]:
    """把 Qwen-VL 的定位结果转为搜索卡片。

    调用时已经由本地 OCR 确认了“账号 → 公众号”筛选。即使如此，仍在这里再做名称与坐标检查，不接受模型的猜测结果。
    """
    result = client.detect_search_account(screenshot, expected_name)
    observed_name = str(result.get("name") or "").strip()
    if not result.get("found"):
        raise ValueError("Qwen-VL 未确认目标公众号")
    if not observed_name or (
        _normalize_account_name_for_confirmation(observed_name)
        != _normalize_account_name_for_confirmation(expected_name)
    ):
        raise ValueError(
            f"Qwen-VL 公众号名称不匹配：预期={expected_name!r}，识别={observed_name!r}"
        )

    def coordinate(key: str) -> int:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Qwen-VL 缺少有效坐标：{key}")
        normalized = int(round(value))
        if not 0 <= normalized <= 1000:
            raise ValueError(f"Qwen-VL 坐标超出范围：{key}={normalized}")
        return normalized

    center_x = coordinate("center_x_1000")
    center_y = coordinate("center_y_1000")
    avatar_x = result.get("avatar_x_1000")
    avatar_y = result.get("avatar_y_1000")
    # 旧版模型或缓存响应没有头像坐标时，只允许退回到同一张卡片的名称坐标，
    # 绝不会使用一个未校验的默认点。
    if isinstance(avatar_x, bool) or not isinstance(avatar_x, (int, float)):
        avatar_x = center_x
    if isinstance(avatar_y, bool) or not isinstance(avatar_y, (int, float)):
        avatar_y = center_y
    return {
        "found": True,
        "is_official_account": True,
        "name": observed_name,
        "matched_name": observed_name,
        "name_match_method": "qwen-vl-exact-after-local-filter",
        "official_evidence": "local_filter_confirmed + qwen-vl-exact-name",
        "center_x_1000": center_x,
        "center_y_1000": center_y,
        "avatar_x_1000": int(round(avatar_x)),
        "avatar_y_1000": int(round(avatar_y)),
        "confidence": result.get("confidence"),
    }


def wait_for_search_account_tab(
    search_window: WindowInfo,
    output_dir: Path,
    account_name: str,
    *,
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
    # 第一轮立即检查；如果页面仍在加载，再按递增间隔重试。
    wait_intervals: tuple[float, ...] = (0.0, 0.8, 1.2, 2.0, 2.5),
) -> tuple[dict[str, Any], Image.Image]:
    """等待搜索结果加载，并在本地 OCR 多次失败后安全降级到 Qwen-VL。"""
    screenshot: Image.Image | None = None
    local_result: dict[str, Any] = {
        "found": False,
        "reason": "尚未检查搜一搜账号分类",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for attempt, wait_seconds in enumerate(wait_intervals, start=1):
        time.sleep(wait_seconds)
        activate_window(search_window.hwnd)
        screenshot = capture_window(search_window.rect)
        screenshot.save(output_dir / f"search-result-account-tab-wait-{attempt}.png")
        try:
            # Win11 新版默认停在“全部”页，公众号卡片直接位于“关键词 - 账号”区块。
            # 先尝试识别这个完整卡片；只有未命中时才回退到旧版顶部“账号”分类。
            direct_account = PROFILE_OCR.locate_all_page_account_result(
                screenshot, account_name
            )
            if direct_account.get("found"):
                local_result = {
                    "found": True,
                    "mode": "all-account-section",
                    "target": direct_account,
                    "center_x_1000": direct_account["center_x_1000"],
                    "center_y_1000": direct_account["center_y_1000"],
                    "confidence": direct_account.get("confidence"),
                    "method": "rapidocr-sogou-all-page-account-card",
                }
            else:
                local_result = PROFILE_OCR.locate_account_tab(screenshot)
        except Exception as exc:
            local_result = {"found": False, "reason": f"本地 OCR 异常：{exc}"}
        log_event(
            "account_tab_detection_attempt",
            account=account_name,
            attempt=attempt,
            found=bool(local_result.get("found")),
            reason=local_result.get("reason"),
            method=local_result.get("method"),
            mode=local_result.get("mode"),
        )
        if local_result.get("found"):
            screenshot.save(output_dir / "search-result-before-account-tab.png")
            (output_dir / "account-tab-detection.json").write_text(
                json.dumps(local_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log_event(
                "search_result_page_confirmed",
                account=account_name,
                mode=local_result.get("mode") or "legacy-account-tab",
                method=local_result.get("method"),
                matched_name=(local_result.get("target") or {}).get("matched_name"),
            )
            return local_result, screenshot

    assert screenshot is not None
    screenshot.save(output_dir / "search-result-before-account-tab.png")
    if allow_vl and client is not None:
        local_reason = str(local_result.get("reason") or "本地 OCR 未识别账号分类")
        log_event(
            "vl_fallback_requested",
            stage="search_account_tab",
            account=account_name,
            local_attempts=len(wait_intervals),
            reason=local_reason,
        )
        try:
            raw_result = client.detect_search_account_tab(screenshot)
            account_tab = normalize_qwen_account_tab_action(raw_result)
            (output_dir / "account-tab-qwen-raw.json").write_text(
                json.dumps(raw_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if account_tab.get("found"):
                (output_dir / "account-tab-detection.json").write_text(
                    json.dumps(account_tab, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                log_event(
                    "vl_fallback_succeeded",
                    stage="search_account_tab",
                    account=account_name,
                    label=account_tab.get("label"),
                    confidence=account_tab.get("confidence"),
                )
                log_event(
                    "search_result_page_confirmed",
                    account=account_name,
                    mode="legacy-account-tab",
                    method=account_tab.get("method") or "qwen-vl-sogou-account-tab",
                )
                return account_tab, screenshot
            qwen_reason = str(account_tab.get("reason") or "Qwen-VL 未识别账号分类")
        except Exception as exc:
            qwen_reason = str(exc)
        log_event(
            "vl_fallback_failed",
            stage="search_account_tab",
            account=account_name,
            local_reason=local_reason,
            error=qwen_reason,
        )
        local_result = {
            **local_result,
            "qwen_fallback_reason": qwen_reason,
        }

    (output_dir / "account-tab-detection.json").write_text(
        json.dumps(local_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    raise RuntimeError(
        "搜索结果加载后连续多次未找到一级账号分类："
        f"{local_result.get('reason') or '未提供原因'}"
        + (
            f"；Qwen-VL：{local_result.get('qwen_fallback_reason')}"
            if local_result.get("qwen_fallback_reason")
            else ""
        )
    )


def settle_search_result_after_submit(
    search_window: WindowInfo,
    output_dir: Path,
    account_name: str,
    submit_attempt: int,
    *,
    timeout_seconds: float = 6.0,
) -> Image.Image:
    """等待结果页布局稳定，关闭并验证提交后重新出现的联想层。

    这里仅使用 OpenCV 状态检查，不运行整屏 OCR。只有结果页顶部搜索栏已经出现、
    且输入框连续两次处于失焦状态，调用方才可以开始公众号结果 OCR。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    transition_probe = 0
    last_state: dict[str, Any] = {
        "found": False,
        "result_layout_ready": False,
        "input_focused": False,
        "reason": "尚未检查提交后的搜索页面",
    }
    result_image: Image.Image | None = None
    while time.monotonic() < deadline:
        transition_probe += 1
        activate_window(search_window.hwnd)
        result_image = capture_window(search_window.rect)
        last_state = PROFILE_OCR.inspect_search_submit_state(result_image)
        if last_state.get("result_layout_ready"):
            result_image.save(
                output_dir
                / f"search-after-submit-before-escape-{submit_attempt}.png"
            )
            log_event(
                "search_result_layout_ready",
                account=account_name,
                attempt=submit_attempt,
                transition_probes=transition_probe,
                state=last_state,
            )
            break
        time.sleep(0.20)
    else:
        if result_image is not None:
            result_image.save(
                output_dir / f"search-submit-layout-timeout-{submit_attempt}.png"
            )
        raise RuntimeError(
            "提交搜索后未进入顶部结果页布局："
            + str(last_state.get("reason") or last_state)
        )

    assert result_image is not None
    for dismiss_attempt in range(1, 4):
        state = PROFILE_OCR.inspect_search_submit_state(result_image)
        if state.get("input_focused"):
            log_event(
                "search_suggestion_dismiss_requested",
                account=account_name,
                attempt=submit_attempt,
                dismiss_attempt=dismiss_attempt,
                method="escape_after_result_layout_ready",
                state=state,
            )
            press_escape()
            time.sleep(0.20)
            result_image = capture_window(search_window.rect)
            state = PROFILE_OCR.inspect_search_submit_state(result_image)
            if state.get("input_focused"):
                # Escape 可能只关闭刚完成导航的旧层。点击网页顶部右侧安全空白区
                # 释放搜索框焦点，不点击搜索结果、账号卡片或浏览器标题栏。
                click(
                    search_window.rect.left + round(search_window.rect.width * 0.92),
                    search_window.rect.top + round(search_window.rect.height * 0.20),
                )
                log_event(
                    "search_suggestion_neutral_blur_requested",
                    account=account_name,
                    attempt=submit_attempt,
                    dismiss_attempt=dismiss_attempt,
                    method="safe_page_blank_click",
                )

        # 给结果页自动聚焦逻辑留出时间；必须连续两次确认失焦，不能发送按键后
        # 立刻把“请求关闭”误记成“已经关闭”。
        time.sleep(0.45)
        first_verify = capture_window(search_window.rect)
        first_state = PROFILE_OCR.inspect_search_submit_state(first_verify)
        if first_state.get("input_focused"):
            result_image = first_verify
            continue
        time.sleep(0.35)
        second_verify = capture_window(search_window.rect)
        second_state = PROFILE_OCR.inspect_search_submit_state(second_verify)
        if second_state.get("input_focused"):
            result_image = second_verify
            continue

        second_verify.save(output_dir / f"search-after-submit-{submit_attempt}.png")
        second_verify.save(output_dir / "search-after-submit.png")
        log_event(
            "search_suggestion_dismiss_confirmed",
            account=account_name,
            attempt=submit_attempt,
            dismiss_attempt=dismiss_attempt,
            state=second_state,
        )
        return second_verify

    result_image.save(output_dir / f"search-suggestion-dismiss-failed-{submit_attempt}.png")
    log_event(
        "search_suggestion_dismiss_failed",
        account=account_name,
        attempt=submit_attempt,
        state=PROFILE_OCR.inspect_search_submit_state(result_image),
    )
    raise RuntimeError("搜索结果页联想下拉层连续关闭失败")


def search_and_open_profile(
    account_name: str,
    output_dir: Path,
    *,
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
    manual_fallback_seconds: float = 0.0,
) -> tuple[WindowInfo, str]:
    """通过搜一搜精确名称打开公众号资料窗口，不要求当前微信账号关注公众号。"""
    global _SEARCH_WINDOW_HOT
    # 搜索页激活后，上一条资料页相对路由不再可信。成功打开资料页时重新登记。
    mark_watch_profile_active(None)
    search_name = resolve_search_account_name(account_name)
    try:
        search_window = find_sogou_search_window()
    except RuntimeError as exc:
        log_event(
            "sogou_window_missing",
            account=account_name,
            reason=str(exc),
            action="recover_from_wechat_main_window",
        )
        search_window = open_sogou_from_wechat_main(account_name)
        # 明确记录自动恢复已完成，控制台日志可区分“已自愈”与“仍然缺少搜一搜窗口”。
        log_event(
            "sogou_window_recovered",
            account=account_name,
            hwnd=search_window.hwnd,
            method="wechat_main_window",
        )
    search_window = arrange_automation_window(search_window, "browser")
    activate_window(search_window.hwnd)
    # 恢复搜索前先检查当前标签是否已经是目标资料页。轮询阶段经常只是资料页
    # 被滚动或短暂加载，不能先报“没有搜索框”再无谓重建同账号标签。
    current_image, current_profile_validation = validate_profile_with_home_recovery(
        search_window, search_name
    )
    if current_profile_validation.get("matched"):
        profile_window = WindowInfo(
            search_window.hwnd,
            search_window.title,
            search_window.class_name,
            search_window.rect,
            search_window.process_name,
            "embedded_profile_tab",
        )
        _WATCH_PROFILE_CACHE[account_name] = profile_window
        mark_watch_profile_active(account_name)
        output_dir.mkdir(parents=True, exist_ok=True)
        current_image.save(output_dir / "profile-recovered-before-search.png")
        log_event(
            "search_recovery_target_profile_reused",
            account=account_name,
            validation=current_profile_validation,
            action="cancel_search_rebuild",
        )
        return profile_window, account_name
    # 不再假设搜一搜位于首标签，也不发送标签移动快捷键。Win11 微信中资料页、
    # 文章页和搜一搜可能共存于同一窗口，后续通过页面 OCR 扫描并停留在正确标签。
    output_dir.mkdir(parents=True, exist_ok=True)
    log_event(
        "account_search_started",
        account=account_name,
        search_name=search_name,
        alias_applied=search_name != account_name,
        reason="direct_search",
    )
    search_box: dict[str, Any] = {"found": False}
    before: Image.Image | None = None
    search_page_recreated = False
    search_page_ready_without_recovery = False
    search_tab_is_current = False
    for recovery_index in range(9):
        before = capture_window(search_window.rect)
        before.save(output_dir / f"before-search-{recovery_index:02d}.png")
        search_box = PROFILE_OCR.locate_search_box(before)
        log_event(
            "search_box_detection",
            account=account_name,
            recovery_index=recovery_index,
            found=bool(search_box.get("found")),
            reason=search_box.get("reason"),
        )
        if search_box.get("found"):
            search_page_ready_without_recovery = _SEARCH_WINDOW_HOT and recovery_index == 0
            search_tab_is_current = True
            break
        if recovery_index == 1 and not search_page_recreated:
            if find_and_pin_search_tab(search_window, account_name):
                log_event(
                    "search_page_recovery_finished",
                    account=account_name,
                    recovered_hwnd=search_window.hwnd,
                    method="existing-tab-scan",
                )
                search_tab_is_current = True
                continue
            recovered_profile = _SEARCH_SCAN_RECOVERED_PROFILE.pop(account_name, None)
            if recovered_profile is not None:
                recovered_profile = arrange_automation_window(recovered_profile, "profile")
                log_event(
                    "search_recovery_cancelled_by_target_profile",
                    account=account_name,
                    hwnd=recovered_profile.hwnd,
                )
                return recovered_profile, account_name
            # 所有现有标签都不是搜一搜，才从微信主窗口重建，避免无谓关闭整个浏览器。
            search_window = recreate_sogou_search_window(
                search_window,
                account_name,
                str(search_box.get("reason") or "当前标签不是搜一搜页面"),
            )
            _SEARCH_WINDOW_HOT = False
            # 页面标签被文章窗口替换时会重新拉起搜一搜；记录这一步便于定位后续失败发生在哪个阶段。
            log_event(
                "sogou_search_page_recreated",
                account=account_name,
                hwnd=search_window.hwnd,
                recovery_index=recovery_index,
            )
            search_page_recreated = True
            search_tab_is_current = True
            continue
        # 只等待当前标签稳定；不再用 Ctrl+1 假设搜索页位于首标签。
        activate_window(search_window.hwnd)
        press_escape()
        time.sleep(0.5)
    if before is not None:
        before.save(output_dir / "before-search.png")
    if not search_box.get("found"):
        raise RuntimeError(str(search_box.get("reason") or "无法定位搜一搜搜索框"))
    # 搜索初始化阶段只确认当前页面是搜一搜，不关闭已有公众号资料页或未知标签。
    # 上一账号已安全回到当前搜一搜标签时，直接复用热窗口，跳过重复的双截图校准；
    # 只有发生标签恢复或当前标签不确定时才执行完整确认。
    if search_page_ready_without_recovery:
        log_event(
            "search_window_hot_reused",
            account=account_name,
            reason="当前标签已确认包含搜一搜搜索框，保留现有标签状态",
        )
    else:
        keep_only_search_tab(
            search_window,
            account_name,
            output_dir,
            close_non_search_tabs=False,
            preserve_current_search_tab=search_tab_is_current,
        )
    before = capture_window(search_window.rect)
    search_box = PROFILE_OCR.locate_search_box(before)
    if not search_box.get("found"):
        search_window, before, search_evidence = recover_search_workbench(
            search_window,
            account_name,
            output_dir,
            reason=search_box.get("reason") or "未找到搜一搜顶部搜索框",
        )
        search_box = search_evidence["search_box"]
    last_submit_error = ""
    account_tab: dict[str, Any] | None = None
    screenshot: Image.Image | None = None
    for submit_attempt in range(1, 4):
        # 每次重试都重新定位输入框；第一次点击后页面可能仍停留在联想下拉框，
        # 不能继续复用旧坐标或假设焦点仍在输入框。
        current_image = capture_window(search_window.rect)
        current_box = PROFILE_OCR.locate_search_box(current_image)
        if not current_box.get("found"):
            current_box = search_box
        activate_window(search_window.hwnd)
        click(
            search_window.rect.left
            + round(search_window.rect.width * int(current_box["center_x_1000"]) / 1000),
            search_window.rect.top
            + round(search_window.rect.height * int(current_box["center_y_1000"]) / 1000),
        )
        press_ctrl_a()
        set_clipboard_text(search_name)
        press_ctrl_v()
        # Win11 新版搜一搜是在提交后切换布局，并可能在结果页再次自动聚焦。
        # 提交函数不直接发送 Escape；后续状态机会等待结果布局并验证焦点释放。
        time.sleep(0.20)
        button_x = search_window.rect.left + round(
            search_window.rect.width * int(current_box["button_x_1000"]) / 1000
        )
        button_y = search_window.rect.top + round(
            search_window.rect.height * int(current_box["button_y_1000"]) / 1000
        )
        if submit_attempt == 2:
            press_enter()
            submit_method = "enter"
        else:
            click(button_x, button_y)
            submit_method = "search-button"
        log_event(
            "search_submitted",
            account=account_name,
            search_name=search_name,
            attempt=submit_attempt,
            method=submit_method,
        )
        try:
            settle_search_result_after_submit(
                search_window,
                output_dir,
                account_name,
                submit_attempt,
            )
            account_tab, screenshot = wait_for_search_account_tab(
                search_window,
                output_dir / f"submit-attempt-{submit_attempt}",
                account_name,
                client=client,
                # 最后一轮才允许 Qwen-VL，避免联想下拉框未关闭时浪费模型调用。
                allow_vl=allow_vl and submit_attempt == 3,
            )
            break
        except RuntimeError as exc:
            last_submit_error = str(exc)
            log_event(
                "search_submit_attempt_failed",
                account=account_name,
                search_name=search_name,
                attempt=submit_attempt,
                error=last_submit_error,
            )
    else:
        if manual_fallback_seconds > 0:
            log_event(
                "search_manual_fallback_requested",
                account=account_name,
                search_name=search_name,
                timeout_seconds=manual_fallback_seconds,
                reason=last_submit_error or "自动提交后未确认搜索结果页",
            )
            manual_deadline = time.time() + manual_fallback_seconds
            manual_poll = 0
            while time.time() < manual_deadline:
                manual_poll += 1
                time.sleep(1.0)
                activate_window(search_window.hwnd)
                screenshot = capture_window(search_window.rect)
                direct_target = PROFILE_OCR.locate_all_page_account_result(
                    screenshot, search_name
                )
                legacy_target = PROFILE_OCR.locate_account_tab(screenshot)
                if direct_target.get("found"):
                    account_tab = {
                        "found": True,
                        "mode": "all-account-section",
                        "target": direct_target,
                        "center_x_1000": direct_target["center_x_1000"],
                        "center_y_1000": direct_target["center_y_1000"],
                        "confidence": direct_target.get("confidence"),
                        "method": "manual-fallback-local-ocr",
                    }
                elif legacy_target.get("found"):
                    account_tab = legacy_target
                else:
                    continue
                screenshot.save(output_dir / "search-result-manual-fallback.png")
                log_event(
                    "search_manual_fallback_succeeded",
                    account=account_name,
                    search_name=search_name,
                    poll=manual_poll,
                    mode=account_tab.get("mode") or "legacy-account-tab",
                )
                break
            else:
                log_event(
                    "search_manual_fallback_failed",
                    account=account_name,
                    search_name=search_name,
                    timeout_seconds=manual_fallback_seconds,
                )
        if account_tab is None or screenshot is None:
            raise RuntimeError(
                "自动提交后仍未确认搜一搜结果页。可在微信中手工完成该账号搜索后，"
                "使用 --manual-search-fallback 让程序接管；最后原因："
                f"{last_submit_error or '未提供原因'}"
            )
    direct_target = (
        account_tab.get("target")
        if account_tab.get("mode") == "all-account-section"
        else None
    )
    # Win11 的“全部”页面已经把公众号卡片展示在“关键词 - 账号”区块中，
    # 该路径不再点击顶部“账号”，避免点击后跳到另一套旧版布局。
    if direct_target:
        selection: dict[str, Any] = {
            "selected": True,
            "mode": "all-account-section",
            "reason": "新版全部页已直接确认公众号卡片",
        }
        target = direct_target
        (output_dir / "account-card-direct.json").write_text(
            json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log_event(
            "account_card_directly_selected",
            account=account_name,
            matched_name=target.get("matched_name") or target.get("name"),
            method=target.get("method"),
        )
    else:
        # 旧版路径：选择一级“账号”，并用下划线和二级筛选项验证点击确实生效。
        selection = {"selected": False}
        for selection_attempt in range(1, 4):
            click(
                search_window.rect.left
                + round(search_window.rect.width * int(account_tab["center_x_1000"]) / 1000),
                search_window.rect.top
                + round(search_window.rect.height * int(account_tab["center_y_1000"]) / 1000),
            )
            time.sleep(1.2)
            screenshot = capture_window(search_window.rect)
            screenshot.save(output_dir / f"account-tab-after-{selection_attempt}.png")
            selection = PROFILE_OCR.validate_account_tab_selected(screenshot)
            log_event(
                "account_tab_validation",
                account=account_name,
                attempt=selection_attempt,
                selected=bool(selection.get("selected")),
                reason=selection.get("reason"),
                filters=selection.get("visible_account_filters"),
            )
            (output_dir / f"account-tab-validation-{selection_attempt}.json").write_text(
                json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if selection.get("selected"):
                break
        if not selection.get("selected"):
            # OCR 偶发把顶部合并成“全部账号”/整行筛选项，导致下划线与二级筛选
            # 读取失败；同一帧里只要已经出现可确认的公众号/服务号结果卡片，
            # 旧版账号点击即视为生效，不需要继续依赖账号标签文字。
            fallback_target = PROFILE_OCR.locate_search_result(screenshot, search_name)
            if fallback_target.get("found") and fallback_target.get("is_official_account"):
                selection = {
                    "selected": True,
                    "mode": "legacy-account-result-evidence",
                    "reason": "账号下划线未读但结果卡片已确认公众号/服务号",
                }
                log_event(
                    "account_tab_validation_by_result_evidence",
                    account=account_name,
                    matched_name=fallback_target.get("matched_name"),
                    account_type=fallback_target.get("account_type"),
                    reason=selection["reason"],
                )
        if (
            not selection.get("selected")
            and allow_vl
            and client is not None
        ):
            # 本地 OCR 仍然负责首选验证；只有连续三次无法确认下划线/账号结果区时，
            # 才让模型只判断同一个顶部“账号”分类是否已选中。
            local_reason = str(selection.get("reason") or "本地 OCR 未确认账号分类选中状态")
            log_event(
                "vl_fallback_requested",
                stage="search_account_tab_selected",
                account=account_name,
                local_attempts=3,
                reason=local_reason,
            )
            try:
                raw_selection = client.detect_search_account_tab(screenshot)
                qwen_selection = normalize_qwen_account_tab_action(
                    raw_selection, require_selected=True
                )
                (output_dir / "account-tab-selected-qwen-raw.json").write_text(
                    json.dumps(raw_selection, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if qwen_selection.get("found"):
                    selection = {
                        **qwen_selection,
                        "selected": True,
                        "reason": "",
                    }
                    log_event(
                        "vl_fallback_succeeded",
                        stage="search_account_tab_selected",
                        account=account_name,
                        confidence=qwen_selection.get("confidence"),
                    )
                else:
                    log_event(
                        "vl_fallback_failed",
                        stage="search_account_tab_selected",
                        account=account_name,
                        local_reason=local_reason,
                        error=qwen_selection.get("reason"),
                    )
            except Exception as exc:
                log_event(
                    "vl_fallback_failed",
                    stage="search_account_tab_selected",
                    account=account_name,
                    local_reason=local_reason,
                    error=str(exc),
                )
        if not selection.get("selected"):
            raise RuntimeError(
                f"连续3次点击后仍无法确认账号分类已选中：{selection.get('reason', '')}"
            )

    # 旧版搜一搜在一级“账号”下还有“公众号”二级筛选；新版界面可能直接展示
    # 公众号结果卡片，不再显示这组筛选项。两种布局都必须先完成同名卡片和
    # “公众号/篇原创内容”证据校验，再允许点击。
    official_filter = {"found": False, "reason": "新版全部页已直接确认公众号卡片"}
    (output_dir / "official-account-filter-detection.json").write_text(
        json.dumps(official_filter, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    target: dict[str, Any] = direct_target or {"found": False}
    official_filter_confirmed = bool(direct_target and direct_target.get("found"))
    filter_selected_once = False
    filter_failure_reason = ""
    if direct_target:
        log_event(
            "official_account_filter_not_present",
            account=account_name,
            search_name=search_name,
            result_found=True,
            official_evidence=target.get("official_evidence"),
            reason=official_filter["reason"],
            action="use_new_ui_all_page_account_card",
        )
    elif official_filter.get("found"):
        for filter_attempt in range(1, 4):
            click(
                search_window.rect.left
                + round(search_window.rect.width * int(official_filter["center_x_1000"]) / 1000),
                search_window.rect.top
                + round(search_window.rect.height * int(official_filter["center_y_1000"]) / 1000),
            )
            time.sleep(1.2)
            screenshot = capture_window(search_window.rect)
            screenshot.save(output_dir / f"official-account-filter-after-{filter_attempt}.png")
            filter_selection = PROFILE_OCR.validate_official_account_filter_selected(screenshot)
            target = PROFILE_OCR.locate_search_result(screenshot, search_name)
            log_event(
                "official_account_filter_validation",
                account=account_name,
                attempt=filter_attempt,
                selected=bool(filter_selection.get("selected")),
                foreground_median=filter_selection.get("foreground_median"),
                official_evidence=target.get("official_evidence"),
                personal_evidence=target.get("personal_evidence"),
                reason=filter_selection.get("reason") or target.get("reason"),
            )
            (output_dir / f"official-account-filter-validation-{filter_attempt}.json").write_text(
                json.dumps(filter_selection, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if filter_selection.get("selected"):
                filter_selected_once = True
            else:
                filter_failure_reason = str(filter_selection.get("reason") or "未检测到选中状态")
            if filter_selected_once and target.get("found") and target.get("is_official_account"):
                official_filter_confirmed = True
                break
    else:
        # 新版页面的“账号”结果已经限定在公众号账号区域；此时不能因为缺少
        # 旧版二级筛选栏而停在搜索页。仍要求本地 OCR 同时确认精确名称和
        # “公众号/篇原创内容”等官方账号证据，避免直接盲点。
        target = PROFILE_OCR.locate_search_result(screenshot, search_name)
        (output_dir / "search-result-detection-before-click.json").write_text(
            json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        official_filter_confirmed = bool(
            target.get("found") and target.get("is_official_account")
        )
        log_event(
            "official_account_filter_not_present",
            account=account_name,
            search_name=search_name,
            result_found=bool(target.get("found")),
            official_evidence=target.get("official_evidence"),
            reason=target.get("reason"),
            action="use_new_ui_account_card",
        )
        if not official_filter_confirmed:
            filter_failure_reason = str(
                target.get("reason")
                or official_filter.get("reason")
                or "新版账号页未找到可确认的公众号卡片"
            )

    if not official_filter_confirmed and filter_selected_once and allow_vl and client is not None:
        # “账号 → 公众号”已经本地确认后，才允许 Qwen-VL 复核名称卡片。这里没有任何盲点分类标签的逻辑，
        # 模型只用来解决本地 OCR 已经连续三次未能读出精确名称的情形。
        local_reason = str(target.get("reason") or "本地 OCR 未能确认公众号卡片")
        log_event(
            "vl_fallback_requested",
            stage="profile_search_result",
            account=account_name,
            search_name=search_name,
            local_attempts=3,
            reason=local_reason,
        )
        try:
            target = _qwen_search_target(client, screenshot, search_name)
            official_filter_confirmed = True
            log_event(
                "vl_fallback_succeeded",
                stage="profile_search_result",
                account=account_name,
                matched_name=target.get("matched_name"),
                confidence=target.get("confidence"),
            )
        except Exception as exc:
            log_event(
                "vl_fallback_failed",
                stage="profile_search_result",
                account=account_name,
                local_reason=local_reason,
                error=str(exc),
            )

    if not official_filter_confirmed:
        # 筛选状态和账号命中是两个独立条件。过去将它们合并后，OCR 名称差异也会
        # 被误报为“筛选没有点上”，使人工排查走错方向。
        if not official_filter.get("found"):
            raise RuntimeError(
                "新版账号结果页未找到可确认的同名公众号卡片："
                f"{search_name}。{filter_failure_reason}"
            )
        if not filter_selected_once:
            raise RuntimeError(
                f"二级公众号筛选未确认选中：{filter_failure_reason}"
            )
        raise RuntimeError(
            "公众号筛选已选中，但未找到可确认的同名公众号："
            f"{search_name}。{target.get('reason') or '名称或账号类型校验未通过'}"
        )

    screenshot.save(output_dir / "search-result.png")
    log_event(
        "account_search_result",
        account=account_name,
        found=bool(target.get("found")),
        reason=target.get("reason"),
        matched_name=target.get("matched_name") or target.get("name"),
        name_match_method=target.get("name_match_method"),
        account_type=target.get("account_type"),
        candidate_types=target.get("candidate_types"),
        selection_policy=target.get("selection_policy"),
    )
    (output_dir / "search-detection.json").write_text(
        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not target.get("found"):
        raise RuntimeError(str(target.get("reason") or "搜一搜没有精确匹配公众号"))
    empty_service_profile = bool(
        str(target.get("account_type") or "").strip() == "服务号"
        and not (target.get("original_content_evidence") or [])
    )

    # 不在点击前关闭任何公众号资料页。已有资料页可能就是目标账号，也可能是
    # 上一轮残留页面；统一交给点击后的资料页名称校验决定“复用、关闭或重试”。
    activate_window(search_window.hwnd)
    name_click_x = search_window.rect.left + round(
        search_window.rect.width * int(target["center_x_1000"]) / 1000
    )
    name_click_y = search_window.rect.top + round(
        search_window.rect.height * int(target["center_y_1000"]) / 1000
    )
    click(name_click_x, name_click_y)
    log_event(
        "profile_name_clicked",
        account=account_name,
        screen_x=name_click_x,
        screen_y=name_click_y,
    )
    time.sleep(0.08)
    # 记录点击后的窗口类名和标题，方便定位不同微信版本的窗口差异。
    log_event(
        "profile_click_window_inventory",
        account=account_name,
        windows=[
            {
                "hwnd": item.hwnd,
                "title": item.title,
                "class_name": item.class_name,
                "process_name": item.process_name,
                "width": item.rect.width,
                "height": item.rect.height,
            }
            for item in enumerate_wechat_windows()
        ],
    )
    deadline = time.time() + 10
    avatar_retry_at = time.time() + 2
    avatar_retry_done = False
    vl_header_checked = False
    last_reason = ""
    arranged_profile_hwnds: set[int] = set()

    def try_qwen_profile_confirmation(
        image: Image.Image,
        candidate: WindowInfo,
        local_reason: str,
    ) -> tuple[WindowInfo, str] | None:
        """本地无法识别时，用 Qwen 只读确认资料页名称，不返回点击坐标。"""
        nonlocal vl_header_checked, search_window
        if not allow_vl or client is None or vl_header_checked:
            return None
        vl_header_checked = True
        log_event(
            "vl_fallback_requested",
            stage="profile_header",
            account=account_name,
            search_name=search_name,
            local_reason=local_reason,
        )
        try:
            vl_validation = client.verify_profile_header(image, search_name)
            observed_name = str(vl_validation.get("name") or "").strip()
            if not _qwen_profile_header_confirmed(vl_validation, search_name):
                raise ValueError(
                    "Qwen-VL 未确认资料窗口名称："
                    f"预期={search_name!r}，识别={observed_name!r}，"
                    f"置信度={vl_validation.get('confidence')!r}"
                )
            (output_dir / "profile-validation-qwen.json").write_text(
                json.dumps(vl_validation, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if candidate.hwnd == search_window.hwnd:
                candidate = WindowInfo(
                    candidate.hwnd,
                    candidate.title,
                    candidate.class_name,
                    candidate.rect,
                    candidate.process_name,
                    "embedded_profile_tab",
                )
                candidate = arrange_automation_window(candidate, "profile")
                search_window = candidate
            log_event(
                "vl_fallback_succeeded",
                stage="profile_header",
                account=account_name,
                matched_name=observed_name,
                confidence=vl_validation.get("confidence"),
                model_matched=vl_validation.get("matched"),
                page_kind=candidate.page_kind,
            )
            mark_watch_profile_active(account_name)
            return candidate, observed_name
        except Exception as exc:
            log_event(
                "vl_fallback_failed",
                stage="profile_header",
                account=account_name,
                local_reason=local_reason,
                error=str(exc),
            )
            return None

    while time.time() < deadline:
        try:
            profile = find_official_profile_window(
                search_name,
                excluded_hwnds={search_window.hwnd},
                search_window=search_window,
            )
            if profile.hwnd not in arranged_profile_hwnds:
                profile = arrange_automation_window(profile, "profile")
                arranged_profile_hwnds.add(profile.hwnd)
                if profile.page_kind == "embedded_profile_tab":
                    # 资料页与搜一搜共用 HWND；窗口调整后，后续头像重试和文章坐标
                    # 必须使用调整后的同一矩形。
                    search_window = profile
            activate_window(profile.hwnd)
            header_image = capture_window(profile.rect)
            validation = PROFILE_OCR.validate_profile_header(header_image, search_name)
            (output_dir / "profile-validation.json").write_text(
                json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if validation.get("matched"):
                # 资料页确认成功的这一帧是后续采集的事实基线；不能只保存搜索页
                # 或稍后重新激活窗口时的截图。
                header_image.save(output_dir / "profile-opened.png")
                log_event(
                    "profile_screenshot_saved",
                    account=account_name,
                    path=str(output_dir / "profile-opened.png"),
                    page_kind=profile.page_kind or "separate_profile_window",
                )
                log_event("profile_opened_and_verified", account=account_name, validation=validation)
                # 后续正文页严格校验微信实际展示的名称，避免“库内别名”导致误报。
                mark_watch_profile_active(account_name)
                return profile, str(target.get("matched_name") or target.get("name") or account_name)
            last_reason = str(validation.get("reason") or "资料窗口名称不匹配")
            layout_evidence = PROFILE_OCR.inspect_profile_layout(header_image)
            log_event(
                "profile_detection_attempt",
                account=account_name,
                page_kind=profile.page_kind or "separate_profile_window",
                matched=bool(validation.get("matched")),
                observed_header_candidates=validation.get("observed_header_candidates"),
                structural_found=bool(layout_evidence.get("found")),
                structural_terms=layout_evidence.get("terms"),
                reason=last_reason,
            )
            header_image.save(output_dir / "profile-header-mismatch.png")
            # 当前标签校验失败时先完成本地标签扫描，之后才允许视觉模型复核。
            # 对内嵌资料页尤其重要：不能在确认失败前调用 close_window，
            # 因为那会关闭整个 Chrome_WidgetWin_0，而不是当前资料标签。
            if time.time() >= avatar_retry_at:
                if profile.page_kind == "embedded_profile_tab" and activate_embedded_profile_tab(
                    profile,
                    search_name,
                    max_tabs=WIN11_MAX_TAB_SCAN,
                    allow_name_only_profile=empty_service_profile,
                ):
                    recovered_profile = WindowInfo(
                        profile.hwnd,
                        profile.title,
                        profile.class_name,
                        profile.rect,
                        profile.process_name,
                        "embedded_profile_tab",
                        bool(
                            _WATCH_PROFILE_SCAN_STATE.get(
                                search_name, {}
                            ).get("empty_profile_name_only")
                        ),
                    )
                    recovered_profile = arrange_automation_window(
                        recovered_profile, "profile"
                    )
                    log_event(
                        "profile_tab_recovered",
                        account=account_name,
                        method="content-scan-before-vl",
                    )
                    mark_watch_profile_active(account_name)
                    return recovered_profile, search_name
                header_image = capture_window(profile.rect)
                confirmed = try_qwen_profile_confirmation(
                    header_image, profile, last_reason
                )
                if confirmed is not None:
                    mark_watch_profile_active(account_name)
                    return confirmed
            if not avatar_retry_done and time.time() >= avatar_retry_at:
                # 当前页不匹配不能证明它可以安全关闭；它可能是其他九个账号或
                # 未知页面。保留现场，找到已确认的搜一搜工作面后再重试头像。
                activate_window(search_window.hwnd)
                # 再次确认搜一搜工作面后才允许使用结果卡片坐标，避免把头像
                # 坐标点到其他账号资料页或未知页面上。
                search_page = _inspect_sogou_search_results(
                    capture_window(search_window.rect)
                )
                if not search_page.get("found"):
                    activate_window(search_window.hwnd)
                    if find_and_pin_search_tab(
                        search_window,
                        account_name,
                        max_tabs=WIN11_MAX_TAB_SCAN,
                    ):
                        search_page = _inspect_sogou_search_results(
                            capture_window(search_window.rect)
                        )
                    else:
                        recovered_profile = _SEARCH_SCAN_RECOVERED_PROFILE.pop(
                            account_name, None
                        )
                        if recovered_profile is not None:
                            recovered_profile = arrange_automation_window(
                                recovered_profile, "profile"
                            )
                            return recovered_profile, account_name
                if not search_page.get("found"):
                    raise RuntimeError("资料页重试前无法安全回到搜一搜结果页")
                avatar_x = search_window.rect.left + round(
                    search_window.rect.width * int(target["avatar_x_1000"]) / 1000
                )
                avatar_y = search_window.rect.top + round(
                    search_window.rect.height * int(target["avatar_y_1000"]) / 1000
                )
                click(avatar_x, avatar_y)
                avatar_retry_done = True
                log_event(
                    "profile_avatar_fallback_clicked",
                    account=account_name,
                    reason="profile_header_mismatch",
                    screen_x=avatar_x,
                    screen_y=avatar_y,
                    observed_headers=validation.get("observed_header_candidates"),
                )
        except RuntimeError as exc:
            last_reason = str(exc)
            if not avatar_retry_done and time.time() >= avatar_retry_at:
                # 资料页可能是搜一搜浏览器中的新标签，顶层窗口标题和窗口枚举
                # 都无法区分它；先扫描全部本地标签，只有本地路径穷尽后才请求
                # Qwen-VL，避免资料页已经打开时仍等待不可用的模型接口。
                embedded_profile = WindowInfo(
                    search_window.hwnd,
                    search_window.title,
                    search_window.class_name,
                    search_window.rect,
                    search_window.process_name,
                    "embedded_profile_tab",
                )
                # 点击结果后，资料页可能在同一个 Chromium 窗口的新标签中打开，
                # 但并未成为当前活动标签。先扫描所有标签找资料页，再决定是否
                # 回到搜一搜结果页重试，避免把真实资料页误当成未知旧标签。
                try:
                    if activate_embedded_profile_tab(
                        search_window,
                        search_name,
                        max_tabs=WIN11_MAX_TAB_SCAN,
                        allow_name_only_profile=empty_service_profile,
                    ):
                        recovered_profile = WindowInfo(
                            search_window.hwnd,
                            search_window.title,
                            search_window.class_name,
                            search_window.rect,
                            search_window.process_name,
                            "embedded_profile_tab",
                            bool(
                                _WATCH_PROFILE_SCAN_STATE.get(
                                    search_name, {}
                                ).get("empty_profile_name_only")
                            ),
                        )
                        recovered_profile = arrange_automation_window(
                            recovered_profile, "profile"
                        )
                        log_event(
                            "profile_tab_recovered",
                            account=account_name,
                            method="content-scan-after-click",
                        )
                        mark_watch_profile_active(account_name)
                        return recovered_profile, search_name
                except Exception as scan_exc:
                    log_event(
                        "profile_tab_scan_failed",
                        account=account_name,
                        error=str(scan_exc),
                    )
                current_image = capture_window(search_window.rect)
                confirmed = try_qwen_profile_confirmation(
                    current_image, embedded_profile, last_reason
                )
                if confirmed is not None:
                    mark_watch_profile_active(account_name)
                    return confirmed
                # 某些版本只有头像或整张卡片响应点击，名称链接本身可能不触发资料窗口。
                activate_window(search_window.hwnd)
                search_page = _inspect_sogou_search_results(
                    capture_window(search_window.rect)
                )
                if not search_page.get("found"):
                    activate_window(search_window.hwnd)
                    if find_and_pin_search_tab(
                        search_window,
                        account_name,
                        max_tabs=WIN11_MAX_TAB_SCAN,
                    ):
                        search_page = _inspect_sogou_search_results(
                            capture_window(search_window.rect)
                        )
                    else:
                        recovered_profile = _SEARCH_SCAN_RECOVERED_PROFILE.pop(
                            account_name, None
                        )
                        if recovered_profile is not None:
                            recovered_profile = arrange_automation_window(
                                recovered_profile, "profile"
                            )
                            return recovered_profile, account_name
                if not search_page.get("found"):
                    raise RuntimeError("资料页重试前无法安全回到搜一搜结果页")
                avatar_x = search_window.rect.left + round(
                    search_window.rect.width * int(target["avatar_x_1000"]) / 1000
                )
                avatar_y = search_window.rect.top + round(
                    search_window.rect.height * int(target["avatar_y_1000"]) / 1000
                )
                click(avatar_x, avatar_y)
                avatar_retry_done = True
                log_event(
                    "profile_avatar_fallback_clicked",
                    account=account_name,
                    screen_x=avatar_x,
                    screen_y=avatar_y,
                )
        time.sleep(0.12)
    raise RuntimeError(f"点击搜一搜结果后未打开正确公众号资料窗口：{last_reason}")


def analyze_profile_window(
    profile_window: WindowInfo,
    output_dir: Path,
    move_to_latest: bool = False,
    *,
    expected_name: str | None = None,
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
    scan_range: str = "today_yesterday",
    preserve_scrolled: bool = False,
) -> dict[str, Any]:
    """分析公众号资料页中的时间分组和文章卡片。

    本地 OCR 先连续重新截图识别两次；只要得到可靠的“日期标签 + 文章卡片”，
    即可先打开文章。阅读/赞仅作为列表指标补充，不作为点击门槛；只有日期或文章卡片
    本身缺失时，才会交给 Qwen-VL 复核一次。
    """
    if profile_window.empty_article_profile:
        activate_window(profile_window.hwnd)
        output_dir.mkdir(parents=True, exist_ok=True)
        log_event(
            "profile_feed_local_empty_no_more_messages",
            hwnd=profile_window.hwnd,
            reason="服务号无原创内容且资料页无文章导航，按空账号结束",
        )
        return {
            "time_labels": [],
            "articles": [],
            "empty_feed_reason": "no_more_messages",
            "empty_feed_marker": "服务号无原创内容",
            "recognition_method": "search-result-empty-service",
        }
    if profile_window.page_kind == "embedded_profile_tab":
        if not expected_name:
            raise RuntimeError("内嵌资料页缺少目标公众号名称，无法安全激活标签")
        activated = confirm_embedded_profile_preserved(
            profile_window, expected_name
        ) if preserve_scrolled else False
        if not activated and not activate_embedded_profile_tab(profile_window, expected_name):
            raise RuntimeError("无法定位公众号资料页标签，为避免误采集已停止")
    activate_window(profile_window.hwnd)
    if move_to_latest:
        press_ctrl_home()
        # Ctrl+Home 只负责把页面移到最新位置；窗口激活和后续 OCR 已经是状态确认，
        # 不再固定等待完整一秒。
        time.sleep(0.25)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot: Image.Image | None = None
    feed: dict[str, Any] | None = None
    local_failure_reason = ""

    def safe_article_candidates(candidate: dict[str, Any]) -> dict[str, Any]:
        """去掉资料头部、纯数字和越界坐标，避免噪声成为点击目标。"""
        articles = candidate.get("articles")
        if not isinstance(articles, list):
            return candidate
        safe_articles = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            title = normalize_title(str(article.get("title") or ""))
            if not title or re.fullmatch(r"[0-9０-９.,，。:：/／\\\\+＋%％()（）\-—_＿]+", title):
                continue
            try:
                center_x = int(article.get("center_x_1000"))
                center_y = int(article.get("center_y_1000"))
            except (TypeError, ValueError):
                continue
            if not (0 <= center_x <= 1000 and 0 <= center_y <= 1000):
                continue
            safe_articles.append(article)
        filtered = dict(candidate)
        filtered["articles"] = safe_articles
        return filtered

    for local_attempt in range(1, 3):
        screenshot = capture_window(profile_window.rect)
        screenshot.save(output_dir / f"profile-window-local-{local_attempt}.png")
        try:
            candidate = PROFILE_OCR.inspect_profile_feed(screenshot)
            candidate = safe_article_candidates(candidate)
            time_labels = candidate.get("time_labels") or []
            articles = candidate.get("articles") or []
            terminal_empty_labels = [
                str(item.get("text") or "")
                for item in time_labels
                if is_older_time_boundary(str(item.get("text") or ""))
                or (
                    scan_range == "today"
                    and "昨天" in str(item.get("text") or "")
                )
            ]
            if time_labels and not articles and terminal_empty_labels:
                candidate["empty_feed_reason"] = "outside_scan_range_boundary"
                candidate["empty_feed_labels"] = terminal_empty_labels
                feed = candidate
                log_event(
                    "profile_feed_local_empty_range",
                    hwnd=profile_window.hwnd,
                    attempt=local_attempt,
                    scan_range=scan_range,
                    time_labels=[str(item.get("text") or "") for item in time_labels],
                    reason="页面只有扫描范围外的日期分组",
                )
                break
            if candidate.get("empty_feed_reason") == "no_more_messages":
                feed = candidate
                log_event(
                    "profile_feed_local_empty_no_more_messages",
                    hwnd=profile_window.hwnd,
                    attempt=local_attempt,
                    marker=candidate.get("empty_feed_marker"),
                    reason="页面显示无更多消息，视为公众号没有发布文章",
                )
                break
            if not time_labels and not articles:
                raise ValueError(
                    "本地资料页识别结果不完整："
                    f"time_labels={len(time_labels)}，"
                    f"articles={len(articles)}"
                )
            if not time_labels and articles:
                # 滚动重叠区可能让本屏时间分组刚好在视口上方；文章仍应作为
                # 未分组卡片返回，由外层继续翻页寻找日期，而不是调用视觉模型。
                feed = candidate
                log_event(
                    "profile_feed_local_ungrouped",
                    hwnd=profile_window.hwnd,
                    attempt=local_attempt,
                    time_label_count=0,
                    article_count=len(articles),
                    recognition_method=candidate.get("recognition_method"),
                    reason="本屏没有时间分组，文章按未分组处理",
                )
                break
            # “阅读/赞”只用于补充列表互动数，不再作为文章点击的硬门槛。
            # Win11 资料页有时能读到标题和日期，但互动栏被图片、懒加载或布局遮挡；
            # 此时先打开文章，再用文章页的真实标题、公众号和 URL 做最终校验。
            feed = candidate
            log_event(
                "profile_feed_local_succeeded",
                hwnd=profile_window.hwnd,
                attempt=local_attempt,
                time_label_count=len(candidate.get("time_labels", [])),
                article_count=len(candidate.get("articles", [])),
                recognition_method=candidate.get("recognition_method"),
                metric_anchor_count=sum(
                    1
                    for article in candidate.get("articles", [])
                    if isinstance(article.get("list_read_count"), int)
                    and isinstance(article.get("list_like_count"), int)
                ),
            )
            break
        except Exception as exc:
            local_failure_reason = str(exc)
            log_event(
                "profile_feed_local_attempt_failed",
                hwnd=profile_window.hwnd,
                attempt=local_attempt,
                error=local_failure_reason,
            )
            if local_attempt == 1:
                # 等待动画和懒加载结束后再截一次，不改变滚动位置。
                time.sleep(0.25)

    if feed is None:
        if not allow_vl or client is None:
            raise RuntimeError(f"资料页本地识别失败且已禁用VL：{local_failure_reason}")
        assert screenshot is not None
        log_event(
            "vl_fallback_requested",
            stage="profile_feed",
            hwnd=profile_window.hwnd,
            local_attempts=2,
            reason=local_failure_reason,
        )
        try:
            feed = client.inspect_profile_feed(screenshot)
            feed = safe_article_candidates(feed)
            if (
                feed.get("empty_feed_reason") != "no_more_messages"
                and (not feed.get("time_labels") or not feed.get("articles"))
            ):
                raise ValueError(
                    "Qwen-VL 资料页识别结果不完整："
                    f"time_labels={len(feed.get('time_labels', []))}，"
                    f"articles={len(feed.get('articles', []))}"
                )
            feed["recognition_method"] = "qwen-vl-profile-feed-fallback"
            feed["fallback_reason"] = local_failure_reason
            log_event(
                "vl_fallback_succeeded",
                stage="profile_feed",
                hwnd=profile_window.hwnd,
                time_label_count=len(feed.get("time_labels", [])),
                article_count=len(feed.get("articles", [])),
            )
        except Exception as exc:
            log_event(
                "vl_fallback_failed",
                stage="profile_feed",
                hwnd=profile_window.hwnd,
                local_reason=local_failure_reason,
                error=str(exc),
            )
            raise RuntimeError(f"Qwen-VL 资料页识别失败：{exc}") from exc

    assert screenshot is not None
    screenshot.save(output_dir / "profile-window.png")
    for article in feed["articles"]:
        article["screen_point"] = (
            profile_window.rect.left
            + round(profile_window.rect.width * int(article["center_x_1000"]) / 1000),
            profile_window.rect.top
            + round(profile_window.rect.height * int(article["center_y_1000"]) / 1000),
        )
    (output_dir / "feed.json").write_text(
        json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return feed


def analyze_account_window(
    client: QwenVisionClient,
    account_window: WindowInfo,
    output_dir: Path,
    move_to_latest: bool = False,
    allow_vl: bool = True,
) -> dict[str, Any]:
    activate_window(account_window.hwnd)
    if move_to_latest:
        # 微信会记住公众号窗口上次的滚动位置，首屏先到底部确保读取最新推送组。
        press_ctrl_end()
        time.sleep(1.2)
    screenshot = capture_window(account_window.rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot.save(output_dir / "account-window.png")
    fallback_reason = ""
    try:
        feed = FEED_OCR.inspect_account_feed(screenshot)
        if not feed.get("time_labels") or not feed.get("articles"):
            raise ValueError(
                f"本地消息识别结果不完整：time_labels={len(feed.get('time_labels', []))}, "
                f"articles={len(feed.get('articles', []))}"
            )
        first_label_y = min(
            int(item.get("center_y_1000", 0)) for item in feed["time_labels"]
        )
        if first_label_y > 500 and any(
            int(article.get("center_y_1000", 0)) < first_label_y
            for article in feed["articles"]
        ):
            # 时间标签落在下半屏且上方已有卡片时，顶部可能还有被标题栏遮住的分组标签。
            # 本地 OCR 不应猜测卡片归属，此类边界页交给 VL 确认一次。
            if allow_vl:
                raise ValueError("屏幕顶部可能存在被遮挡的时间标签")
            feed["local_only_warning"] = "屏幕顶部可能存在被遮挡的时间标签"
    except Exception as exc:
        if not allow_vl:
            # 对比实验要求严格禁止模型调用，边界页面保留错误和截图供人工核验。
            raise RuntimeError(f"本地消息列表识别失败且已禁用VL：{exc}") from exc
        # 窗口被遮挡、版式变化或 OCR 无结果时，保留 Qwen-VL 兜底以避免漏采。
        fallback_reason = str(exc)
        feed = client.inspect_account_feed(screenshot)
        feed["recognition_method"] = "qwen-vl-fallback"
        feed["fallback_reason"] = fallback_reason
    articles = feed["articles"]
    for article in articles:
        article["screen_point"] = (
            account_window.rect.left
            + round(account_window.rect.width * int(article["center_x_1000"]) / 1000),
            account_window.rect.top
            + round(account_window.rect.height * int(article["center_y_1000"]) / 1000),
        )
    (output_dir / "feed.json").write_text(
        json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return feed


def select_latest_article_group(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """优先选择今天最新的消息组；只有没有今天内容时才考虑昨天。"""
    ordered = sorted(articles, key=lambda item: item["screen_point"][1])
    today = [item for item in ordered if "昨天" not in str(item.get("group_time") or "")]
    candidates = today or [item for item in ordered if "昨天" in str(item.get("group_time") or "")]
    if not candidates:
        return []
    # 同一个推送包的所有卡片应共享相同的时间分组。
    latest_group = str(candidates[-1].get("group_time") or "")
    if latest_group:
        grouped = [item for item in candidates if str(item.get("group_time") or "") == latest_group]
        if grouped:
            return grouped
    return candidates


PROMOTION_TITLE_KEYWORDS = (
    "招聘",
    "招募",
    "诚聘",
    "投稿合作",
    "商务合作",
    "广告合作",
    # 远程桌面浮层可能覆盖公众号窗口并被 OCR 当成卡片，必须在点击前过滤。
    "ToDesk",
    "设备代码",
)


def promotion_reason(title: str) -> str | None:
    """识别不需要采集的招聘、招募及合作推广卡片。"""
    normalized = normalize_title(title)
    for keyword in PROMOTION_TITLE_KEYWORDS:
        if normalize_title(keyword) in normalized:
            return f"标题包含推广关键词：{keyword}"
    return None


def is_older_time_boundary(value: str) -> bool:
    """星期标签或明确年月日均表示已经早于昨天，应停止继续采集。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    return bool(
        re.search(r"(?:星期|周)[一二三四五六日天1-7]", text)
        or re.search(r"(?:\d{4}年)?\d{1,2}月\d{1,2}日", text)
        or re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", text)
    )


def is_recent_time_group(value: str, scan_range: str = "today_yesterday") -> bool:
    text = unicodedata.normalize("NFKC", value or "").strip()
    if not text or is_older_time_boundary(text):
        return False
    if "昨天" in text:
        return scan_range in {"yesterday", "today_yesterday"}
    if "今天" in text:
        return scan_range in {"today", "today_yesterday"}
    # 当天推送在微信中通常只显示 HH:MM。
    return scan_range in {"today", "today_yesterday"} and bool(
        re.fullmatch(r"\d{1,2}:\d{2}", text)
    )


def publish_time_matches_scan_range(
    value: Any,
    scan_range: str,
    *,
    reference_date: date | None = None,
) -> bool:
    """按北京时间复核文章真实发布时间是否属于本次任务范围。"""
    if scan_range not in {"today", "yesterday", "today_yesterday"}:
        raise ValueError(f"未知扫描范围：{scan_range}")
    publish_value = value if isinstance(value, datetime) else parse_publish_time(str(value or ""))
    publish_date = publish_value.date()
    today = reference_date or datetime.now(shanghai_timezone()).date()
    if scan_range == "today":
        return publish_date == today
    if scan_range == "yesterday":
        return publish_date == today - timedelta(days=1)
    return publish_date in {today, today - timedelta(days=1)}


def build_card_signature(
    time_group: str, article: dict[str, Any]
) -> tuple[str, str, int, int] | None:
    """生成保守的卡片指纹；任一互动数字缺失时不做点击前去重。"""
    title = canonical_title_for_match(str(article.get("title") or ""))
    group = normalize_title(time_group)
    read_count = article.get("list_read_count")
    like_count = article.get("list_like_count")
    if not title or not group or not isinstance(read_count, int) or not isinstance(like_count, int):
        return None
    return group, title, read_count, like_count


def build_card_title_signature(time_group: str, article: dict[str, Any]) -> tuple[str, str] | None:
    """生成本轮终态去重指纹；互动数缺失时仍可阻止同一卡片重复打开。"""
    # 去除省略号、引号和 OCR 常见 AI/Al 差异，避免同一卡片在相邻屏幕中
    # 仅因展示截断不同而被重复打开。
    title = canonical_title_for_match(str(article.get("title") or ""))
    group = normalize_title(time_group)
    return (group, title) if group and title else None


CARD_PROGRESS_COLLECTION = os.getenv("CARD_PROGRESS_COLLECTION", "card_progress")


def load_known_card_title_signatures(
    mongo_uri: str,
    database_name: str,
    account_name: str,
    *,
    collection_name: str = CARD_PROGRESS_COLLECTION,
) -> set[tuple[str, str]]:
    """读取该账号已成功采集过的卡片标题指纹，供增量批量点击前快速停止。"""
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        collection = client[database_name][collection_name]
        cursor = collection.find(
            {"account.name": account_name},
            {"timeGroup": 1, "title": 1},
        )
        signatures: set[tuple[str, str]] = set()
        for document in cursor:
            group = normalize_title(str(document.get("timeGroup") or ""))
            title = canonical_title_for_match(str(document.get("title") or ""))
            if group and title:
                signatures.add((group, title))
        return signatures
    finally:
        client.close()


def remember_account_card_title_signature(
    mongo_uri: str,
    database_name: str,
    account_name: str,
    url: str,
    time_group: str,
    title: str,
    *,
    collection_name: str = CARD_PROGRESS_COLLECTION,
) -> None:
    """把成功采集文章的卡片标题指纹写入 Mongo，供后续批量轮次做点击前预停止。"""
    normalized_url = normalize_article_url(url)
    normalized_group = normalize_title(time_group)
    normalized_title = canonical_title_for_match(title)
    if not normalized_url or not normalized_group or not normalized_title:
        return
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        collection = client[database_name][collection_name]
        collection.update_one(
            {
                "article.urlNormalized": normalized_url,
                "account.name": account_name,
            },
            {
                "$set": {
                    "account.name": account_name,
                    "article.urlNormalized": normalized_url,
                    "timeGroup": normalized_group,
                    "title": normalized_title,
                    "lastCollectedAt": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
    finally:
        client.close()


def collect_searched_account(
    client: QwenVisionClient,
    account_name: str,
    output_dir: Path,
    max_articles: int,
    export_jsonl: str | None,
    export_csv: str | None,
    allow_vl: bool = True,
    write_mongo: bool = False,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    metric_mode: str = "all",
    task_timeout_minutes: float | None = None,
    scan_range: str = "today_yesterday",
) -> dict[str, Any]:
    account_window: WindowInfo | None = None
    collected: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    detected_count = 0
    deadline = (
        time.monotonic() + task_timeout_minutes * 60
        if task_timeout_minutes and task_timeout_minutes > 0
        else None
    )
    try:
        search_error = ""
        for search_attempt in range(1, 4):
            try:
                account_window = search_and_open_account(
                    client,
                    account_name,
                    output_dir / "search" / f"attempt-{search_attempt}",
                    allow_vl=allow_vl,
                )
                break
            except Exception as exc:
                search_error = str(exc)
                time.sleep(1.0)
        if account_window is None:
            raise RuntimeError(f"公众号搜索连续3次失败：{search_error}")
        seen_cards: set[str] = set()
        processed_count = 0
        stop_reason = "达到最大翻页数"
        for page_index in range(1, 13):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"账号 {account_name} 达到任务超时限制")
            feed = analyze_account_window(
                client,
                account_window,
                output_dir / "messages" / f"page-{page_index:02d}",
                move_to_latest=page_index == 1,
                allow_vl=allow_vl,
            )
            detected = feed["articles"]
            time_labels = sorted(
                feed["time_labels"], key=lambda item: int(item.get("center_y_1000", 0))
            )
            older_boundary = next(
                (
                    str(item.get("text") or "")
                    for item in time_labels
                    if is_older_time_boundary(str(item.get("text") or ""))
                ),
                "",
            )
            for article in detected:
                article_y = int(article.get("center_y_1000", 0))
                labels_above = [
                    item for item in time_labels
                    if int(item.get("center_y_1000", 0)) < article_y
                ]
                # 时间标签不在当前截图中时不推断归属，只继续向上翻页等待标签出现。
                if not labels_above:
                    continue
                group_time = str(labels_above[-1].get("text") or "").strip()
                if not is_recent_time_group(group_time, scan_range):
                    continue
                title = str(article.get("title") or "").strip()
                card_key = f"{normalize_title(group_time)}|{normalize_title(title)}"
                if card_key in seen_cards:
                    continue
                seen_cards.add(card_key)
                detected_count += 1

                reason = promotion_reason(title)
                if reason:
                    skipped.append({"title": title, "reason": reason})
                    continue
                if processed_count >= max_articles:
                    stop_reason = f"达到文章上限 {max_articles}"
                    break

                processed_count += 1
                article_dir = output_dir / f"article-{processed_count:02d}-{safe_path_name(title)}"
                last_error = ""
                for attempt in range(1, 4):
                    try:
                        activate_window(account_window.hwnd)
                        click(*article["screen_point"])
                        time.sleep(2.5)
                        record = collect_open_article(
                            client,
                            article_dir,
                            write_mongo=write_mongo,
                            export_jsonl=export_jsonl,
                            export_csv=export_csv,
                            expected_title=title,
                            expected_account=account_name,
                            allow_vl=allow_vl,
                            mongo_uri=mongo_uri,
                            mongo_database=mongo_database,
                            mongo_collection=mongo_collection,
                            mongo_target_collection=mongo_target_collection,
                            metric_mode=metric_mode,
                            scan_range=scan_range,
                        )
                        if record.get("status") == "skipped_outside_scan_range":
                            skipped.append(
                                {
                                    "title": title,
                                    "reason": str(record.get("skip_reason") or "真实发布时间不在扫描范围"),
                                    "url": str(record.get("url") or ""),
                                }
                            )
                            break
                        collected.append(
                            {key: value for key, value in record.items() if key != "content"}
                        )
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        (article_dir / f"attempt-{attempt}-error.txt").parent.mkdir(
                            parents=True, exist_ok=True
                        )
                        (article_dir / f"attempt-{attempt}-error.txt").write_text(
                            last_error, encoding="utf-8"
                        )
                    finally:
                        # 每次尝试都只关闭右侧当前文章标签，保留公众号消息窗口。
                        try:
                            article_hwnd, _ = find_article_window()
                            activate_window(article_hwnd)
                            press_ctrl_w()
                            time.sleep(0.8)
                        except Exception as cleanup_exc:
                            log_event(
                                "article_tab_cleanup_failed",
                                account=account_name,
                                title=title,
                                attempt=attempt,
                                error=str(cleanup_exc),
                                action="abort_account_to_prevent_tab_accumulation",
                            )
                            raise RuntimeError(
                                "文章标签关闭失败，为避免重复打开和数据错配，已停止当前公众号采集："
                                f"{cleanup_exc}"
                            ) from cleanup_exc
                else:
                    failure = {
                        "account": account_name,
                        "title": title,
                        "error": last_error,
                        "category": classify_collection_error(RuntimeError(last_error)),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    failures.append(failure)
                    append_failure_queue(output_dir, failure)
                    # 三次尝试都失败后才发出终态事件；控制台据此计入警告。
                    log_event("article_collect_failed", **failure)

            if processed_count >= max_articles:
                stop_reason = f"达到文章上限 {max_articles}"
                break
            if older_boundary:
                stop_reason = f"遇到更早时间边界：{older_boundary}"
                break
            activate_window(account_window.hwnd)
            scroll_window_up(account_window.rect)
            time.sleep(1.0)
    finally:
        # 一个公众号结束后关闭中间窗口，左侧微信搜索窗口始终保留。
        if account_window and user32.IsWindow(account_window.hwnd):
            close_window(account_window.hwnd)

    summary = {
        "account": account_name,
        "detected_articles": detected_count,
        "stop_reason": stop_reason,
        "collected": collected,
        "skipped": skipped,
        "failures": failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return summary


def collect_profile_account(
    client: QwenVisionClient,
    account_name: str,
    output_dir: Path,
    max_articles: int,
    export_jsonl: str | None,
    export_csv: str | None,
    allow_vl: bool = True,
    write_mongo: bool = False,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    metric_mode: str = "all",
    task_timeout_minutes: float | None = None,
    scan_range: str = "today_yesterday",
    recent_card_limit: int | None = None,
    known_urls: set[str] | None = None,
    stop_after_known_url: bool = False,
    known_card_title_signatures: set[tuple[str, str]] | None = None,
    stop_after_known_card_signature: bool = False,
    manual_search_fallback_seconds: float = 0.0,
    reuse_profile_window: WindowInfo | None = None,
    preserve_profile: bool = False,
    refresh_profile: bool = False,
) -> dict[str, Any]:
    global _SEARCH_WINDOW_HOT
    log_event(
        "account_collection_started",
        account=account_name,
        max_articles=max_articles,
        allow_vl=allow_vl,
        write_mongo=write_mongo,
        metric_mode=metric_mode,
        scan_range=scan_range,
    )
    """从搜一搜进入公众号资料窗口，采集今天和昨天的文章。"""
    profile_window: WindowInfo | None = reuse_profile_window
    # 数据库中的账号名是文章归属的唯一标准。搜一搜 OCR 读到的名称可能会
    # 带上“媒体”“官方”等身份后缀，只能用于搜索结果校验，不能污染文章页校验。
    observed_account_name = account_name
    collected: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    detected_count = 0
    current_group = ""
    # 卡片指纹仅在成功后登记；URL 仍作为打开文章后的最终精确去重依据。
    successful_card_signatures: set[tuple[str, str, int, int]] = set()
    # 同一标题卡片无论成功、跳过还是三次失败，达到终态后本轮都不再重复打开。
    terminal_card_title_signatures: set[tuple[str, str]] = set()
    successful_urls: set[str] = set()
    repeated_card_signature_skipped_count = 0
    repeated_title_signature_count = 0
    skipped_url_duplicate_count = 0
    dedupe_urls = {
        normalized
        for item in (known_urls or set())
        if (normalized := normalize_article_url(str(item)))
    }
    seeded_known_urls = set(dedupe_urls)
    known_urls_seeded_count = len(seeded_known_urls)
    known_url_stop = False
    known_card_stop = False
    recent_cards_checked = 0
    recent_limit_reached = False
    observed_card_count = 0
    out_of_range_card_count = 0
    ungrouped_card_count = 0
    promotion_card_count = 0
    processed_count = 0
    opened_count = 0
    deadline = (
        time.monotonic() + task_timeout_minutes * 60
        if task_timeout_minutes and task_timeout_minutes > 0
        else None
    )
    stop_reason = "达到最大翻页数"
    partial_summary_path = output_dir / "partial-summary.json"
    rebuild_lock_acquired = False

    def write_partial_checkpoint() -> None:
        """逐篇保存账号进度，避免后续窗口清理失败掩盖已成功结果。"""
        checkpoint = {
            "account": account_name,
            "discovery_mode": "sogou-profile",
            "partial": True,
            "detected_articles": detected_count,
            "stop_reason": "账号仍在采集，已保存成功文章检查点",
            "scan": {
                "range": scan_range,
                "observed_cards": observed_card_count,
                "eligible_cards": detected_count,
                "outside_range_cards": out_of_range_card_count,
                "ungrouped_cards": ungrouped_card_count,
                "promotion_cards": promotion_card_count,
                "recent_card_limit": recent_card_limit,
                "recent_cards_checked": recent_cards_checked,
            },
            "collected": collected,
            "skipped": skipped,
            "failures": failures,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = partial_summary_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary_path.replace(partial_summary_path)

    try:
        if profile_window is not None:
            try:
                if not user32.IsWindow(profile_window.hwnd):
                    raise RuntimeError("缓存资料页窗口句柄已失效")
                if profile_window.page_kind == "embedded_profile_tab":
                    if not activate_embedded_profile_tab(profile_window, account_name):
                        raise RuntimeError("缓存资料页标签未找到")
                else:
                    activate_window(profile_window.hwnd)
                profile_window = arrange_automation_window(profile_window, "profile")
                if refresh_profile and not refresh_verified_profile_window(
                    profile_window,
                    account_name,
                    output_dir=output_dir / "profile" / "refresh",
                ):
                    # 刷新超时不等于标签已消失。微信内嵌页偶尔在 12 秒后才
                    # 恢复，或刷新后把焦点切到相邻标签；先穷尽本地标签恢复，
                    # 只有确实找不到目标资料页时才回退搜一搜。
                    recovered_locally = (
                        profile_window.page_kind == "embedded_profile_tab"
                        and activate_embedded_profile_tab(
                            profile_window,
                            account_name,
                            max_tabs=WIN11_MAX_TAB_SCAN,
                        )
                    )
                    if not recovered_locally:
                        raise RuntimeError("缓存资料页刷新后本地扫描仍未恢复目标账号")
                    recovery_validation = PROFILE_OCR.validate_profile_header(
                        capture_window(profile_window.rect), account_name
                    )
                    if not recovery_validation.get("matched"):
                        raise RuntimeError("缓存资料页刷新后本地恢复身份校验失败")
                    log_event(
                        "profile_refresh_recovered_by_local_scan",
                        account=account_name,
                        validation=recovery_validation,
                    )
                validation = PROFILE_OCR.validate_profile_header(
                    capture_window(profile_window.rect), account_name
                )
                if not validation.get("matched"):
                    raise RuntimeError("缓存资料页账号名称校验失败")
                log_event(
                    "profile_tab_reused",
                    account=account_name,
                    page_kind=profile_window.page_kind,
                    refreshed=refresh_profile,
                    validation=validation,
                )
            except Exception as exc:
                scan_state = _WATCH_PROFILE_SCAN_STATE.get(account_name, {})
                log_event(
                    "profile_tab_reuse_failed",
                    account=account_name,
                    error=str(exc),
                    scan_state=scan_state,
                    action=(
                        "fallback_to_search_after_complete_cycle"
                        if scan_state.get("completed_cycle")
                        else "defer_without_rebuild"
                    ),
                )
                if preserve_profile and not scan_state.get("completed_cycle"):
                    raise ProfileTemporarilyUnverifiedError(
                        "资料页暂时无法确认，尚未可靠完成标签整圈扫描，已禁止重新搜索"
                    ) from exc
                profile_window = None
                _WATCH_PROFILE_CACHE.pop(account_name, None)
        if profile_window is None:
            if preserve_profile and reuse_profile_window is not None:
                if account_name in _WATCH_PROFILE_REBUILD_LOCKS:
                    raise ProfileTemporarilyUnverifiedError(
                        f"账号 {account_name} 已有重建任务运行，本轮稍后重试"
                    )
                _WATCH_PROFILE_REBUILD_LOCKS.add(account_name)
                rebuild_lock_acquired = True
                # 获得重建锁后必须再次扫描现有标签；此处找到目标就取消新建。
                if activate_embedded_profile_tab(
                    reuse_profile_window,
                    account_name,
                    max_tabs=WIN11_MAX_TAB_SCAN,
                ):
                    profile_window = reuse_profile_window
                    _WATCH_PROFILE_CACHE[account_name] = profile_window
                    log_event(
                        "profile_rebuild_cancelled_existing_tab_found",
                        account=account_name,
                    )
                else:
                    final_scan_state = _WATCH_PROFILE_SCAN_STATE.get(account_name, {})
                    if not final_scan_state.get("completed_cycle"):
                        raise ProfileTemporarilyUnverifiedError(
                            "重建前复扫未能证明已完整绕行一圈，已取消重新搜索"
                        )
            if profile_window is not None:
                log_event(
                    "profile_tab_reused",
                    account=account_name,
                    page_kind=profile_window.page_kind,
                    refreshed=False,
                    validation={"matched": True, "method": "pre-rebuild-rescan"},
                )
            else:
                search_error = ""
                for attempt in range(1, 4):
                    try:
                        log_event("account_search_attempt", account=account_name, attempt=attempt)
                        profile_window, observed_account_name = search_and_open_profile(
                            account_name,
                            output_dir / "search" / f"attempt-{attempt}",
                            client=client,
                            allow_vl=allow_vl,
                            manual_fallback_seconds=manual_search_fallback_seconds,
                        )
                        break
                    except Exception as exc:
                        search_error = str(exc)
                        log_event("account_search_attempt_failed", account=account_name, attempt=attempt, error=search_error)
                        time.sleep(0.8)
                if profile_window is None:
                    raise RuntimeError(f"搜一搜连续3次打开公众号失败：{search_error}")
        if preserve_profile and profile_window is not None:
            _WATCH_PROFILE_CACHE[account_name] = profile_window
        # 记录 OCR 看到的结果名，但后续文章归属仍使用 account_name。
        log_event(
            "account_identity_observed",
            account=account_name,
            observed_name=observed_account_name,
        )

        for page_index in range(1, 13):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"账号 {account_name} 达到任务超时限制")
            # 每次滚屏后的顶部可能是上一分区残留的贴图/视频卡片。不能沿用
            # 上一屏最后一个日期标签，否则这些无标签卡片会被误归到“昨天”。
            # 宁可将缺少本屏日期证据的卡片记为 ungrouped，下一屏重叠区域仍可补抓。
            current_group = ""
            feed = analyze_profile_window(
                profile_window,
                output_dir / "profile" / f"page-{page_index:02d}",
                move_to_latest=page_index == 1,
                expected_name=account_name,
                client=client,
                allow_vl=allow_vl,
                scan_range=scan_range,
                preserve_scrolled=True,
            )
            log_event(
                "profile_page_analyzed",
                account=account_name,
                page=page_index,
                time_labels=[item.get("text") for item in feed.get("time_labels", [])],
                article_count=len(feed.get("articles", [])),
            )
            if feed.get("empty_feed_reason") == "no_more_messages":
                stop_reason = "公众号或服务号没有发布文章（页面显示无更多消息）"
                log_event(
                    "account_empty_feed_no_more_messages",
                    account=account_name,
                    marker=feed.get("empty_feed_marker"),
                    reason=stop_reason,
                )
                break
            labels = [
                {"kind": "label", **item}
                for item in feed.get("time_labels", [])
            ]
            articles = [
                {"kind": "article", **item}
                for item in feed.get("articles", [])
            ]
            events = sorted(
                labels + articles,
                key=lambda item: int(item.get("center_y_1000", 0)),
            )
            older_boundary = ""
            for event in events:
                if known_url_stop or recent_limit_reached:
                    break
                if event["kind"] == "label":
                    label = str(event.get("text") or "").strip()
                    current_group = label
                    if is_older_time_boundary(label):
                        older_boundary = label
                    continue
                observed_card_count += 1
                if not current_group:
                    # 不猜测没有日期分组的卡片属于哪一天，等待下一屏的时间标签。
                    ungrouped_card_count += 1
                    continue
                if older_boundary or not is_recent_time_group(current_group, scan_range):
                    out_of_range_card_count += 1
                    continue
                title = str(event.get("title") or "").strip()
                detected_count += 1
                reason = promotion_reason(title)
                if reason:
                    promotion_card_count += 1
                    log_event("article_card_skipped_promotion", account=account_name, title=title, reason=reason)
                    skipped.append({"title": title, "reason": reason})
                    continue
                if recent_card_limit and recent_cards_checked >= recent_card_limit:
                    recent_limit_reached = True
                    break
                recent_cards_checked += 1
                card_signature = build_card_signature(current_group, event)
                title_signature = build_card_title_signature(current_group, event)
                # 卡片标题和互动数都来自 OCR，不能在点击前作为最终去重键：
                # 同一篇文章可能因截断差异产生多个签名，不同文章也可能有相同标题。
                # 保留标题签名用于审计，但统一点击后以规范化文章 URL 精确去重。
                full_signature_repeated = (
                    card_signature is not None
                    and card_signature in successful_card_signatures
                )
                if (
                    title_signature is not None
                    and not full_signature_repeated
                    and title_signature in terminal_card_title_signatures
                ):
                    repeated_title_signature_count += 1
                    log_event(
                        "article_card_signature_repeated_not_skipped",
                        account=account_name,
                        title=title,
                        time_group=current_group,
                        reason="卡片 OCR 指纹不能替代文章 URL，继续点击确认",
                    )
                if (
                    stop_after_known_card_signature
                    and title_signature is not None
                    and known_card_title_signatures
                    and title_signature in known_card_title_signatures
                ):
                    known_card_stop = True
                    stop_reason = "遇到已采集文章卡片指纹，增量批量本轮结束"
                    log_event(
                        "profile_card_known_signature_stop",
                        account=account_name,
                        title=title,
                        time_group=current_group,
                        reason=stop_reason,
                    )
                    skipped.append(
                        {
                            "title": title,
                            "reason": stop_reason,
                            "known_card_signature": True,
                        }
                    )
                    break
                if full_signature_repeated:
                    repeated_card_signature_skipped_count += 1
                    log_event(
                        "article_card_full_signature_repeated_skipped_without_click",
                        account=account_name,
                        title=title,
                        time_group=current_group,
                        reason="完整卡片指纹已在本轮成功采集，跳过滚动重叠区域的同一卡片",
                    )
                    skipped.append(
                        {
                            "title": title,
                            "reason": "完整卡片指纹已在本轮成功采集，跳过重复卡片",
                            "repeated_signature": True,
                        }
                    )
                    continue
                if processed_count >= max_articles:
                    stop_reason = f"达到文章上限 {max_articles}"
                    break

                opened_count += 1
                article_dir = output_dir / f"article-{opened_count:02d}-{safe_path_name(title)}"
                last_error = ""
                for article_attempt in range(1, 4):
                    try:
                        log_event(
                            "article_open_attempt",
                            account=account_name,
                            title=title,
                            attempt=article_attempt,
                            time_group=current_group,
                            list_read_count=event.get("list_read_count"),
                            list_like_count=event.get("list_like_count"),
                        )
                        activate_window(profile_window.hwnd)
                        navigation_baseline = capture_window(profile_window.rect)
                        click(*event["screen_point"])
                        navigation_difference = wait_for_visual_change(
                            profile_window.rect,
                            navigation_baseline,
                            timeout_seconds=2.5,
                            threshold=3.0,
                        )
                        log_event(
                            "article_navigation_wait_finished",
                            account=account_name,
                            title=title,
                            visual_difference=round(navigation_difference, 3),
                        )
                        record = collect_open_article(
                            client,
                            article_dir,
                            write_mongo=write_mongo,
                            export_jsonl=export_jsonl,
                            export_csv=export_csv,
                            expected_title=title,
                            # 文章页按微信实际名称校验，MongoDB 仍使用数据库标准名，
                            # 因此别名账号不会在文章页被误报“公众号不匹配”。
                            expected_account=resolve_search_account_name(account_name),
                            storage_account=account_name,
                            allow_vl=allow_vl,
                            mongo_uri=mongo_uri,
                            mongo_database=mongo_database,
                            mongo_collection=mongo_collection,
                            mongo_target_collection=mongo_target_collection,
                            list_read_count=event.get("list_read_count"),
                            list_like_count=event.get("list_like_count"),
                            successful_urls_in_run=dedupe_urls,
                            metric_mode=metric_mode,
                            scan_range=scan_range,
                        )
                        if record.get("status") == "skipped_outside_scan_range":
                            skipped.append(
                                {
                                    "title": title,
                                    "reason": str(record.get("skip_reason") or "真实发布时间不在扫描范围"),
                                    "url": str(record.get("url") or ""),
                                }
                            )
                            if title_signature is not None:
                                terminal_card_title_signatures.add(title_signature)
                            successful_url = normalize_article_url(str(record.get("url") or ""))
                            if successful_url:
                                successful_urls.add(successful_url)
                                dedupe_urls.add(successful_url)
                            log_event(
                                "article_attempt_skipped_outside_scan_range",
                                account=account_name,
                                title=title,
                                publish_time=record.get("publish_time"),
                                scan_range=scan_range,
                            )
                            break
                        if record.get("status") == "skipped_duplicate_in_run":
                            skipped_url_duplicate_count += 1
                            skipped.append(
                                {
                                    "title": title,
                                    "reason": "本次公众号任务已成功采集相同 URL，打开后跳过",
                                    "url": str(record.get("url") or ""),
                                }
                            )
                            log_event("article_attempt_duplicate_url", account=account_name, title=title, url=record.get("url"))
                            duplicate_url = normalize_article_url(str(record.get("url") or ""))
                            if stop_after_known_url and duplicate_url in seeded_known_urls:
                                known_url_stop = True
                                log_event(
                                    "incremental_known_url_stop",
                                    account=account_name,
                                    title=title,
                                    url=duplicate_url,
                                    reason="资料页按最新到最旧排序，后续卡片视为已知历史",
                                )
                            if (
                                known_url_stop
                                and title_signature is not None
                                and write_mongo
                                and stop_after_known_card_signature
                            ):
                                try:
                                    remember_account_card_title_signature(
                                        mongo_uri
                                        or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
                                        mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
                                        account_name,
                                        duplicate_url,
                                        title_signature[0],
                                        title_signature[1],
                                    )
                                except Exception as exc:
                                    log_event(
                                        "card_signature_persist_failed",
                                        account=account_name,
                                        title=title,
                                        error=str(exc),
                                        action="continue_without_card_fingerprint",
                                    )
                            if title_signature is not None:
                                terminal_card_title_signatures.add(title_signature)
                            break
                        collected.append(
                            {key: value for key, value in record.items() if key != "content"}
                        )
                        successful_url = normalize_article_url(str(record.get("url") or ""))
                        if successful_url:
                            successful_urls.add(successful_url)
                            dedupe_urls.add(successful_url)
                        if (
                            title_signature is not None
                            and successful_url
                            and write_mongo
                            and stop_after_known_card_signature
                        ):
                            try:
                                remember_account_card_title_signature(
                                    mongo_uri
                                    or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
                                    mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
                                    account_name,
                                    successful_url,
                                    title_signature[0],
                                    title_signature[1],
                                )
                            except Exception as exc:
                                log_event(
                                    "card_signature_persist_failed",
                                    account=account_name,
                                    title=title,
                                    error=str(exc),
                                    action="continue_without_card_fingerprint",
                                )
                        if card_signature is not None:
                            successful_card_signatures.add(card_signature)
                        if title_signature is not None:
                            terminal_card_title_signatures.add(title_signature)
                        processed_count += 1
                        log_event(
                            "article_collect_succeeded",
                            account=account_name,
                            title=record.get("title") or title,
                            url=successful_url,
                            processed_count=processed_count,
                        )
                        write_partial_checkpoint()
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        log_event(
                            "article_collect_attempt_failed",
                            account=account_name,
                            title=title,
                            attempt=article_attempt,
                            error=last_error,
                        )
                        article_dir.mkdir(parents=True, exist_ok=True)
                        (article_dir / f"attempt-{article_attempt}-error.txt").write_text(
                            last_error, encoding="utf-8"
                        )
                    finally:
                        try:
                            close_article_after_attempt(
                                account_name,
                                title,
                                return_window=profile_window,
                            )
                        except Exception as cleanup_exc:
                            log_event(
                                "article_tab_cleanup_failed",
                                account=account_name,
                                title=title,
                                error=str(cleanup_exc),
                                action="abort_account_to_prevent_tab_accumulation",
                            )
                            # 标签页数量是采集正确性的硬约束。清理失败后继续点击会让旧文章成为活动页，
                            # 造成标题、链接和互动数错配，因此宁可停止当前公众号也不能继续累积标签。
                            raise RuntimeError(
                                "文章标签清理失败，为避免旧标签累积和文章数据错配，"
                                f"已停止当前公众号采集：{cleanup_exc}"
                            ) from cleanup_exc
                else:
                    failure = {
                        "account": account_name,
                        "title": title,
                        "error": last_error,
                        "category": classify_collection_error(RuntimeError(last_error)),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    failures.append(failure)
                    append_failure_queue(output_dir, failure)
                    if title_signature is not None:
                        terminal_card_title_signatures.add(title_signature)

                if known_url_stop:
                    break
                if recent_card_limit and recent_cards_checked >= recent_card_limit:
                    recent_limit_reached = True
                    break

            if processed_count >= max_articles:
                stop_reason = f"达到文章上限 {max_articles}"
                break
            if known_card_stop:
                break
            if known_url_stop:
                stop_reason = "遇到已知文章 URL，增量监听本轮结束"
                break
            if recent_limit_reached:
                stop_reason = f"达到本轮最新卡片上限 {recent_card_limit}"
                break
            if older_boundary:
                stop_reason = f"遇到更早时间边界：{older_boundary}"
                break
            if profile_window.page_kind == "embedded_profile_tab":
                if not confirm_embedded_profile_preserved(profile_window, account_name):
                    if not activate_embedded_profile_tab(profile_window, account_name):
                        raise RuntimeError("滚屏前无法定位公众号资料页标签，为避免错采已停止")
                else:
                    log_event(
                        "profile_scroll_position_preserved",
                        account=account_name,
                        reason="profile_structure_confirmed_without_ctrl_home",
                    )
            else:
                activate_window(profile_window.hwnd)
            scroll_window_down(profile_window.rect)
            # 下一屏由 OCR 结果决定是否继续；只留短暂的懒加载缓冲。
            time.sleep(0.4)
    finally:
        if rebuild_lock_acquired:
            _WATCH_PROFILE_REBUILD_LOCKS.discard(account_name)
        cleanup_succeeded = False
        preserve_failure: str | None = None
        if preserve_profile and profile_window and user32.IsWindow(profile_window.hwnd):
            try:
                exact_profile_activated = True
                if profile_window.page_kind == "embedded_profile_tab" and not activate_embedded_profile_tab(
                    profile_window, account_name, max_tabs=WIN11_MAX_TAB_SCAN
                ):
                    exact_profile_activated = False
                    if opened_count > 0:
                        raise ProfileTemporarilyUnverifiedError(
                            "轮询结束时无法重新定位本账号资料页"
                        )
                    log_event(
                        "profile_tab_preserve_exact_scan_failed_no_article",
                        account=account_name,
                        action="fallback_to_current_structure_evidence",
                    )
                activate_window(profile_window.hwnd)
                if not profile_window.empty_article_profile:
                    press_ctrl_home()
                    _home_image = wait_for_stable_frames(profile_window.rect)
                    home_validation = PROFILE_OCR.validate_profile_header(
                        _home_image, account_name
                    )
                    structure_without_name = bool(
                        not exact_profile_activated
                        and opened_count == 0
                        and home_validation.get("profile_structure_found")
                        and not home_validation.get("search_page_evidence")
                    )
                    if structure_without_name:
                        log_event(
                            "profile_tab_preserved_structure_only_without_new_article",
                            account=account_name,
                            validation=home_validation,
                            action="preserve_for_next_watch_round",
                        )
                    elif not home_validation.get("matched"):
                        raise ProfileTemporarilyUnverifiedError(
                            "轮询结束归位后账号资料页身份未通过精确校验"
                        )
                cleanup_succeeded = True
                _WATCH_PROFILE_CACHE[account_name] = profile_window
                log_event(
                    "profile_tab_preserved",
                    account=account_name,
                    page_kind=profile_window.page_kind,
                    reason=(
                        "multi_account_watch_empty_service_preserved"
                        if profile_window.empty_article_profile
                        else "multi_account_watch_cache_after_ctrl_home"
                    ),
                    validation=(
                        {"matched": True, "empty_article_profile": True}
                        if profile_window.empty_article_profile
                        else home_validation
                    ),
                )
            except Exception as preserve_exc:
                preserve_failure = str(preserve_exc)
                log_event(
                    "profile_tab_preserve_temporarily_unverified",
                    account=account_name,
                    error=preserve_failure,
                    action="preserve_tab_and_retry_later_without_rebuild",
                )
        elif profile_window and user32.IsWindow(profile_window.hwnd):
            if profile_window.page_kind == "embedded_profile_tab":
                # 资料页是搜一搜浏览器中的活动标签，只能关闭当前标签，不能关闭
                # 整个 Chrome_WidgetWin_0，否则会连搜索页一起退出。
                adapter = Win11WeChatAdapter(
                    activate_window=activate_window,
                    capture_window=capture_window,
                    validate_profile_header=PROFILE_OCR.validate_profile_header,
                    press_ctrl_tab=press_ctrl_tab,
                    press_ctrl_w=press_ctrl_w,
                    press_ctrl_home=press_ctrl_home,
                    wait_for_stable_frames=wait_for_stable_frames,
                    inspect_search_page=lambda image: bool(
                        _inspect_sogou_search_results(image).get("found")
                    ),
                    same_search_page=same_sogou_search_workspace,
                    log_event=log_event,
                    allow_name_only_profile=profile_window.empty_article_profile,
                )
                cleanup_succeeded = adapter.close_profile_tab_if_confirmed(
                    profile_window, resolve_search_account_name(account_name)
                )
            else:
                close_window(profile_window.hwnd)
                cleanup_succeeded = True
        # 单账号旧流程以搜一搜热状态作为下一轮入口；多账号流程保留资料页缓存，
        # 由调度器在切换时重新探测搜一搜标签，因此此时不标记搜索页热状态。
        _SEARCH_WINDOW_HOT = cleanup_succeeded and not preserve_profile
        log_event(
            "search_window_hot_state_updated",
            account=account_name,
            enabled=_SEARCH_WINDOW_HOT,
            reason=(
                "profile_preserved_for_watch_cache"
                if preserve_profile and cleanup_succeeded
                else "profile_cleanup_confirmed"
                if cleanup_succeeded
                else "profile_cleanup_not_confirmed"
            ),
        )
        if preserve_failure is not None:
            raise ProfileTemporarilyUnverifiedError(preserve_failure)

    summary = {
        "account": account_name,
        "discovery_mode": "sogou-profile",
        "detected_articles": detected_count,
        "stop_reason": stop_reason,
        "scan": {
            "range": scan_range,
            "observed_cards": observed_card_count,
            "eligible_cards": detected_count,
            "outside_range_cards": out_of_range_card_count,
            "ungrouped_cards": ungrouped_card_count,
            "promotion_cards": promotion_card_count,
            "recent_card_limit": recent_card_limit,
            "recent_cards_checked": recent_cards_checked,
        },
        "dedupe": {
            "successful_card_signatures_in_run": len(successful_card_signatures),
            "terminal_card_title_signatures_in_run": len(terminal_card_title_signatures),
            "successful_urls_in_run": len(successful_urls),
            "known_urls_seeded": known_urls_seeded_count,
            "known_url_stop": known_url_stop,
            "known_card_stop": known_card_stop,
            "full_card_signature_repeats_skipped": repeated_card_signature_skipped_count,
            "full_card_signature_repeats_not_skipped": 0,
            "title_signature_repeats_not_skipped": repeated_title_signature_count,
            "skipped_url_duplicate_after_open": skipped_url_duplicate_count,
        },
        "collected": collected,
        "skipped": skipped,
        "failures": failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    partial_summary_path.unlink(missing_ok=True)
    log_event("account_collection_finished", **summary)
    return summary


def recover_partial_account_summary(
    output_dir: Path,
    account_name: str,
    error: str,
    category: str,
) -> dict[str, Any]:
    """将账号异常前的逐篇检查点合并到最终失败摘要。"""
    fatal_summary: dict[str, Any] = {
        "account": account_name,
        "fatal_error": error,
        "fatal_category": category,
    }
    partial_summary_path = output_dir / "partial-summary.json"
    if not partial_summary_path.exists():
        return fatal_summary
    try:
        recovered = json.loads(partial_summary_path.read_text(encoding="utf-8"))
        if not isinstance(recovered, dict):
            return fatal_summary
        fatal_summary = recovered
        fatal_summary.update(
            {
                "partial": True,
                "fatal_error": error,
                "fatal_category": category,
                "stop_reason": "账号中途失败；已保留中断前成功采集的文章",
            }
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "summary.json").write_text(
            json.dumps(fatal_summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log_event(
            "account_partial_result_recovered",
            account=account_name,
            collected=len(fatal_summary.get("collected") or []),
            fatal_error=error,
            fatal_category=category,
        )
    except (OSError, ValueError, TypeError) as checkpoint_exc:
        log_event(
            "account_partial_result_recovery_failed",
            account=account_name,
            error=str(checkpoint_exc),
        )
    return fatal_summary


def load_account_names(
    names: list[str],
    accounts_file: str | None,
    accounts_from_mongo: bool = False,
    mongo_uri: str = "",
    mongo_database: str = "weixin",
    mongo_collection: str = "collection_target",
) -> list[str]:
    values = [name.strip() for name in names if name.strip()]
    if accounts_file:
        path = Path(accounts_file)
        values.extend(
            line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if accounts_from_mongo:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
        try:
            client.admin.command("ping")
            cursor = client[mongo_database][mongo_collection].find(
                {"name": {"$type": "string", "$ne": ""}},
                {"_id": 1, "name": 1},
            ).sort("_id", 1)
            # collection_target 的 name 是采集入口；id 缺失不影响按名称搜索。
            values.extend(
                str(document.get("name") or "").strip()
                for document in cursor
                if str(document.get("name") or "").strip()
            )
        finally:
            client.close()
    # 保留配置顺序并去重。
    return list(dict.fromkeys(values))


def slice_account_names(
    names: list[str],
    start_account: str = "",
    max_accounts: int = 0,
) -> list[str]:
    """按起始公众号和数量限制截取本次实际采集的账号列表。

    ``start_account`` 按名称匹配且包含该账号；``max_accounts`` 为 0 时表示
    不限制。保持 MongoDB 账号顺序，避免控制台手动测试每次都跑完全部账号。
    """
    if not names:
        return []
    offset = 0
    start = str(start_account or "").strip()
    if start:
        normalized_start = normalize_title(start)
        for index, name in enumerate(names):
            if normalize_title(name) == normalized_start:
                offset = index
                break
        else:
            raise ValueError(f"起始公众号不存在于账号列表：{start_account}")
    limit = int(max_accounts or 0)
    if limit < 0:
        raise ValueError("--max-accounts 不能为负数")
    if limit == 0:
        limit = len(names)
    return names[offset : offset + limit]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--live", action="store_true", help="允许点击微信窗口")
    parser.add_argument("--click-account", type=int, help="点击识别结果中从0开始的公众号序号")
    parser.add_argument("--click-article", type=int, help="点击识别结果中从0开始的文章序号")
    parser.add_argument("--collect-open-article", action="store_true", help="采集当前已打开文章")
    parser.add_argument("--run-one-account", action="store_true", help="采集当前屏指定公众号的最近文章")
    parser.add_argument("--run-search-accounts", action="store_true", help="按名称搜索公众号并采集文章")
    parser.add_argument(
        "--watch-account",
        action="append",
        default=[],
        help="常驻增量监听公众号，可重复传入；单个账号是多账号调度的兼容用法",
    )
    parser.add_argument(
        "--watch-accounts-file",
        help="常驻增量监听账号文件，每行一个公众号名称，支持 # 注释",
    )
    parser.add_argument(
        "--watch-accounts-from-mongo",
        action="store_true",
        help="常驻增量监听账号从 MongoDB 的 collection_target.name 读取",
    )
    profile_pool_group = parser.add_mutually_exclusive_group()
    profile_pool_group.add_argument(
        "--bootstrap-profile-pool",
        action="store_true",
        help="只建立并验证资料页池，不采集文章；完成后保留微信标签并退出",
    )
    profile_pool_group.add_argument(
        "--watch-existing-profile-pool",
        action="store_true",
        help="热更新采集：只附着现有资料页池，不在启动阶段重新搜索账号",
    )
    parser.add_argument(
        "--accounts-per-vm",
        type=int,
        default=10,
        help="单个虚拟机最多监听账号数，默认 10",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1800.0,
        help="增量监听轮询间隔（秒），默认 1800（30 分钟）",
    )
    parser.add_argument(
        "--recent-card-limit",
        type=int,
        default=5,
        help="增量监听每轮检查的当天最新文章卡片数，默认 5",
    )
    parser.add_argument(
        "--watch-cycles",
        type=int,
        default=0,
        help="增量监听轮数，0 表示持续运行；用于冒烟测试时可设为 1 或 2",
    )
    parser.add_argument(
        "--watch-start-time",
        type=parse_watch_clock,
        help="监听每日开始时间，例如 07:30；需与 --watch-end-time 一起使用",
    )
    parser.add_argument(
        "--watch-end-time",
        type=parse_watch_clock,
        help="监听每日结束时间，例如 24:00；当前轮次完成后停止采集",
    )
    parser.add_argument(
        "--discovery-mode",
        choices=("sogou-profile", "wechat-followed"),
        default="sogou-profile",
        help="公众号发现方式，默认通过搜一搜资料窗口且无需关注",
    )
    parser.add_argument("--account-name", action="append", default=[], help="要搜索的公众号名称，可重复传入")
    parser.add_argument("--accounts-file", help="每行一个公众号名称的 UTF-8 文本文件")
    parser.add_argument(
        "--accounts-from-mongo",
        action="store_true",
        help="从MongoDB的collection_target.name读取公众号名称",
    )
    parser.add_argument(
        "--accounts-mongo-uri",
        default=os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
    )
    parser.add_argument(
        "--accounts-mongo-database",
        default=os.getenv("MONGO_DATABASE", "weixin"),
    )
    parser.add_argument(
        "--accounts-mongo-collection",
        default=os.getenv("MONGO_TARGET_COLLECTION", "collection_target"),
    )
    parser.add_argument(
        "--start-account",
        default="",
        help="从公众号列表中的指定公众号开始采集，按名称匹配且包含该账号；默认从第一个开始",
    )
    parser.add_argument(
        "--max-accounts",
        type=int,
        default=0,
        help="本次最多采集的公众号数量，0 表示全部",
    )
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument("--max-articles", type=int, default=20)
    parser.add_argument(
        "--task-timeout-minutes",
        type=float,
        default=45.0,
        help="单个公众号采集的最长时间，0 表示不限制",
    )
    parser.add_argument(
        "--window-layout",
        choices=("auto", "off"),
        default="auto",
        help="自动固定搜一搜浏览器和公众号资料窗口的位置；off 保留人工布局",
    )
    parser.add_argument(
        "--metrics",
        choices=("share", "all"),
        default="share",
        help="互动指标模式：默认只识别转发数；all 识别全部互动数",
    )
    parser.add_argument(
        "--scan-range",
        choices=("today", "yesterday", "today_yesterday"),
        default="today_yesterday",
        help="文章日期范围：today 今天、yesterday 昨天、today_yesterday 今天和昨天",
    )
    parser.add_argument(
        "--stop-after-known-url",
        action="store_true",
        help="按最新到最旧采集时，遇到 Mongo 中已采集文章 URL 就结束当前公众号",
    )
    parser.add_argument("--write-mongo", action="store_true", help="允许将采集结果写入MongoDB")
    parser.add_argument(
        "--article-mongo-uri",
        default=os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
    )
    parser.add_argument(
        "--article-mongo-database",
        default=os.getenv("MONGO_DATABASE", "weixin"),
    )
    parser.add_argument(
        "--article-mongo-collection",
        default=os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
    )
    parser.add_argument("--export-jsonl", default=str(DEFAULT_OUTPUT_DIR / "articles.jsonl"))
    parser.add_argument("--export-csv", default=str(DEFAULT_OUTPUT_DIR / "articles.csv"))
    parser.add_argument("--wait-seconds", type=float, default=2.0)
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="严格禁用所有VL调用；本地识别失败时直接记录失败",
    )
    parser.add_argument(
        "--manual-search-fallback",
        action="store_true",
        help="自动搜索连续失败后等待人工完成当前账号搜索，再由程序接管（最多120秒）",
    )
    return parser.parse_args()


def main() -> None:
    global WINDOW_LAYOUT_MODE
    args = parse_args()
    WINDOW_LAYOUT_MODE = args.window_layout
    log_path = configure_run_logging(Path(args.output_dir))
    log_event(
        "run_started",
        argv=os.sys.argv,
        output_dir=args.output_dir,
        log_path=str(log_path),
        source_fingerprint=source_fingerprint(),
    )
    if args.local_only:
        # 严格本地模式不要求配置 API Key，且所有可能调用 VL 的分支都会被禁止。
        client = QwenVisionClient(QwenVisionConfig(base_url="", api_key=""))
        vl_available = False
    else:
        try:
            client = QwenVisionClient(QwenVisionConfig.from_env())
            vl_available = True
        except RuntimeError as exc:
            # 未配置视觉模型时继续运行本地采集；真正进入兜底分支时会在日志中明确显示已跳过。
            client = QwenVisionClient(QwenVisionConfig(base_url="", api_key=""))
            vl_available = False
            log_event("qwen_vl_unavailable", reason=str(exc))
    if (
        args.watch_account
        or args.watch_accounts_file
        or args.watch_accounts_from_mongo
        or args.bootstrap_profile_pool
        or args.watch_existing_profile_pool
    ):
        if not args.live:
            raise RuntimeError("增量监听模式必须显式传入 --live")
        if args.run_search_accounts or args.run_one_account or args.collect_open_article:
            raise RuntimeError("监听模式不能与其他采集入口同时使用")
        if args.poll_interval < 30:
            raise RuntimeError("--poll-interval 最小为 30 秒，避免高频重复操作微信")
        if args.recent_card_limit < 1:
            raise RuntimeError("--recent-card-limit 必须大于等于 1")
        if args.watch_cycles < 0:
            raise RuntimeError("--watch-cycles 不能为负数")
        if args.accounts_per_vm < 1:
            raise RuntimeError("--accounts-per-vm 必须大于等于 1")
        if (args.watch_start_time is None) != (args.watch_end_time is None):
            raise RuntimeError("--watch-start-time 和 --watch-end-time 必须同时传入")
        watch_account_names = load_account_names(
            args.watch_account,
            None if args.watch_accounts_from_mongo else args.watch_accounts_file,
            accounts_from_mongo=args.watch_accounts_from_mongo,
            mongo_uri=args.accounts_mongo_uri,
            mongo_database=args.accounts_mongo_database,
            mongo_collection=args.accounts_mongo_collection,
        )
        watch_account_names = slice_account_names(
            watch_account_names,
            args.start_account,
            args.max_accounts,
        )
        if not watch_account_names:
            raise RuntimeError(
                "请通过 --watch-account、--watch-accounts-file 或 "
                "--watch-accounts-from-mongo 提供公众号名称"
            )
        if len(watch_account_names) > args.accounts_per_vm:
            raise RuntimeError(
                f"当前虚拟机最多监听 {args.accounts_per_vm} 个账号，"
                f"实际收到 {len(watch_account_names)} 个；"
                f"若确认单机运行全部账号，请显式传入 --accounts-per-vm {len(watch_account_names)}"
            )
        watch_output_dir = Path(args.output_dir)
        watch_output_dir.mkdir(parents=True, exist_ok=True)
        if not args.write_mongo:
            log_event(
                "watch_persistence_local_only",
                accounts=watch_account_names,
                reason=(
                    "资料页预热仅写 profile-pool-state.json"
                    if args.bootstrap_profile_pool
                    else "未传入 --write-mongo，跨重启文章去重依赖 watch-state.json"
                ),
            )
        result = watch_multiple_accounts(
            client,
            watch_account_names,
            watch_output_dir,
            args.poll_interval,
            args.recent_card_limit,
            (
                str(watch_output_dir / "articles.jsonl")
                if args.export_jsonl == str(DEFAULT_OUTPUT_DIR / "articles.jsonl")
                else args.export_jsonl
            ),
            (
                str(watch_output_dir / "articles.csv")
                if args.export_csv == str(DEFAULT_OUTPUT_DIR / "articles.csv")
                else args.export_csv
            ),
            allow_vl=vl_available,
            write_mongo=args.write_mongo,
            mongo_uri=args.article_mongo_uri,
            mongo_database=args.article_mongo_database,
            mongo_collection=args.article_mongo_collection,
            mongo_target_collection=args.accounts_mongo_collection,
            metric_mode=args.metrics,
            task_timeout_minutes=args.task_timeout_minutes,
            watch_cycles=args.watch_cycles,
            manual_search_fallback_seconds=120.0 if args.manual_search_fallback else 0.0,
            schedule_start_minutes=args.watch_start_time,
            schedule_end_minutes=args.watch_end_time,
            max_accounts_per_vm=args.accounts_per_vm,
            profile_pool_mode=(
                "bootstrap_only"
                if args.bootstrap_profile_pool
                else (
                    "existing_only"
                    if args.watch_existing_profile_pool
                    else "bootstrap_and_watch"
                )
            ),
        )
        log_event(
            "run_finished",
            mode=result.get("mode", "watch"),
            accounts_total=result.get("accounts_total"),
            accounts_failed=(
                len(result.get("profiles_unavailable", []))
                if result.get("mode") == "bootstrap_profile_pool"
                else sum(
                    1
                    for item in result.get("accounts", [])
                    if item.get("last_error")
                )
            ),
            rounds=result.get("rounds"),
            known_urls=result.get("known_urls"),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    if args.run_search_accounts:
        if not args.live:
            raise RuntimeError("搜索采集模式必须显式传入 --live")
        account_names = load_account_names(
            args.account_name,
            args.accounts_file,
            accounts_from_mongo=args.accounts_from_mongo,
            mongo_uri=args.accounts_mongo_uri,
            mongo_database=args.accounts_mongo_database,
            mongo_collection=args.accounts_mongo_collection,
        )
        account_names = slice_account_names(
            account_names,
            args.start_account,
            args.max_accounts,
        )
        if not account_names:
            raise RuntimeError(
                "请通过 --account-name、--accounts-file 或 --accounts-from-mongo 提供公众号名称"
            )
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.output_dir) / "accounts-source.json").write_text(
            json.dumps(
                {
                    "count": len(account_names),
                    "source": "mongo" if args.accounts_from_mongo else "arguments_or_file",
                    "mongo_database": args.accounts_mongo_database if args.accounts_from_mongo else None,
                    "mongo_collection": args.accounts_mongo_collection if args.accounts_from_mongo else None,
                    "accounts": account_names,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        # 让控制台能够在不额外查询数据库的情况下，显示公众号总数和当前位置。
        log_event(
            "accounts_loaded",
            count=len(account_names),
            source="mongo" if args.accounts_from_mongo else "arguments_or_file",
        )
        summaries = []
        for account_name in account_names:
            account_output_dir = Path(args.output_dir) / safe_path_name(account_name)
            try:
                collector = (
                    collect_profile_account
                    if args.discovery_mode == "sogou-profile"
                    else collect_searched_account
                )
                collector_kwargs: dict[str, Any] = {}
                if args.stop_after_known_url and args.discovery_mode == "sogou-profile":
                    try:
                        known_urls = load_account_article_urls(
                            args.article_mongo_uri,
                            args.article_mongo_database,
                            args.article_mongo_collection,
                            account_name,
                        )
                        log_event(
                            "batch_known_urls_loaded_from_mongo",
                            account=account_name,
                            count=len(known_urls),
                        )
                    except Exception as exc:
                        log_event(
                            "batch_known_urls_mongo_load_failed",
                            account=account_name,
                            error=str(exc),
                            action="continue_without_known_url_stop",
                        )
                        known_urls = set()
                    known_card_signatures: set[tuple[str, str]] = set()
                    try:
                        known_card_signatures = load_known_card_title_signatures(
                            args.article_mongo_uri,
                            args.article_mongo_database,
                            account_name,
                        )
                        log_event(
                            "batch_known_card_signatures_loaded",
                            account=account_name,
                            count=len(known_card_signatures),
                        )
                    except Exception as exc:
                        log_event(
                            "batch_known_card_signatures_load_failed",
                            account=account_name,
                            error=str(exc),
                            action="continue_without_card_fingerprint_stop",
                        )
                    collector_kwargs = {
                        "known_urls": known_urls,
                        "stop_after_known_url": True,
                        "known_card_title_signatures": known_card_signatures,
                        "stop_after_known_card_signature": True,
                    }
                summaries.append(collector(
                    client,
                    account_name,
                    account_output_dir,
                    args.max_articles,
                    args.export_jsonl or None,
                    args.export_csv or None,
                    allow_vl=vl_available,
                    write_mongo=args.write_mongo,
                    mongo_uri=args.article_mongo_uri,
                    mongo_database=args.article_mongo_database,
                    mongo_collection=args.article_mongo_collection,
                    mongo_target_collection=args.accounts_mongo_collection,
                    metric_mode=args.metrics,
                    task_timeout_minutes=args.task_timeout_minutes,
                    scan_range=args.scan_range,
                    manual_search_fallback_seconds=120.0 if args.manual_search_fallback else 0.0,
                    **collector_kwargs,
                ))
            except Exception as exc:
                error = str(exc)
                category = classify_collection_error(exc)
                # 账号级异常没有恢复机会，显式记录终态事件，供控制台准确统计。
                log_event(
                    "account_collection_failed",
                    account=account_name,
                    error=error,
                    category=category,
                )
                fatal_summary = recover_partial_account_summary(
                    account_output_dir,
                    account_name,
                    error,
                    category,
                )
                summaries.append(fatal_summary)
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.output_dir) / "batch-summary.json").write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        collected_records = [
            record
            for summary in summaries
            for record in (summary.get("collected") or [])
            if isinstance(record, dict)
        ]
        log_event(
            "run_finished",
            accounts_total=len(account_names),
            accounts_failed=sum(1 for summary in summaries if summary.get("fatal_error")),
            articles_collected=len(collected_records),
            articles_inserted=sum(
                1 for record in collected_records if record.get("status") == "inserted"
            ),
            articles_updated=sum(
                1 for record in collected_records if record.get("status") == "updated"
            ),
            article_failures=sum(
                len(summary.get("failures") or []) for summary in summaries
            ),
        )
        print(json.dumps(summaries, ensure_ascii=False, indent=2, default=str))
        return
    if args.run_one_account:
        if not vl_available:
            raise RuntimeError("--run-one-account 依赖视觉模型，请先配置 QWEN_VL_API_KEY")
        if not args.live:
            raise RuntimeError("公众号循环必须显式传入 --live")
        result = run_one_account(
            client,
            Path(args.output_dir),
            args.account_index,
            args.max_articles,
            args.export_jsonl or None,
            args.export_csv or None,
            args.metrics,
            args.scan_range,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    if args.collect_open_article:
        result = collect_open_article(
            client,
            Path(args.output_dir),
            args.write_mongo,
            args.export_jsonl or None,
            args.export_csv or None,
            allow_vl=vl_available,
            metric_mode=args.metrics,
        )
        summary = {key: value for key, value in result.items() if key != "content"}
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return
    if not vl_available:
        raise RuntimeError("当前操作依赖视觉模型，请先配置 QWEN_VL_API_KEY")
    result = analyze_current_window(client, Path(args.output_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.click_account is not None and args.click_article is not None:
        raise RuntimeError("一次测试只能点击公众号或文章之一")
    if args.click_account is None and args.click_article is None:
        return
    if not args.live:
        raise RuntimeError("点击操作必须同时传入 --live")
    items = result["accounts"] if args.click_account is not None else result["articles"]
    index = args.click_account if args.click_account is not None else args.click_article
    assert index is not None
    if not 0 <= index < len(items):
        raise IndexError("点击序号超出识别结果范围")
    target = items[index]
    screen_x, screen_y = target["screen_point"]
    label = target.get("name") or target.get("title") or "未知项目"
    kind = "公众号" if args.click_account is not None else "文章"
    print(f"即将点击{kind}：{label}，坐标=({screen_x}, {screen_y})")
    time.sleep(args.wait_seconds)
    click(screen_x, screen_y)


if __name__ == "__main__":
    main()
