"""Windows 11 新版微信的 Chromium 标签页适配器。

新版微信的搜一搜、公众号资料页和文章页可能共享同一个
``Chrome_WidgetWin_0`` 窗口句柄，因此窗口激活不等于页面标签激活。
本模块只处理窗口/标签页生命周期，不包含公众号或文章业务规则。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PIL import Image


class Win11WeChatAdapter:
    """通过页面内容管理新版微信的活动标签。"""

    def __init__(
        self,
        *,
        activate_window: Callable[[int], None],
        capture_window: Callable[[Any], Image.Image],
        validate_profile_header: Callable[[Image.Image, str], dict[str, Any]],
        press_ctrl_tab: Callable[[], None],
        press_ctrl_w: Callable[[], None],
        log_event: Callable[..., None],
        sleep: Callable[[float], None] = time.sleep,
        press_ctrl_home: Callable[[], None] | None = None,
        wait_for_stable_frames: Callable[[Any], Image.Image] | None = None,
        inspect_search_page: Callable[[Image.Image], bool] | None = None,
        same_search_page: Callable[[Image.Image, Image.Image], bool] | None = None,
        identify_profile_account: Callable[[Image.Image, list[str]], dict[str, Any]] | None = None,
        allow_name_only_profile: bool = False,
    ) -> None:
        self._activate_window = activate_window
        self._capture_window = capture_window
        self._validate_profile_header = validate_profile_header
        self._press_ctrl_tab = press_ctrl_tab
        self._press_ctrl_w = press_ctrl_w
        self._log_event = log_event
        self._sleep = sleep
        self._press_ctrl_home = press_ctrl_home
        self._wait_for_stable_frames = wait_for_stable_frames
        self._inspect_search_page = inspect_search_page
        self._same_search_page = same_search_page
        self._identify_profile_account = identify_profile_account
        self._allow_name_only_profile = allow_name_only_profile
        self.last_switch_steps = 0
        self.last_scan_completed_cycle = False
        self.last_scan_saw_search = False
        self.last_match_mode = "none"

    @staticmethod
    def _is_sparse_intermediate(validation: dict[str, Any]) -> bool:
        return bool(
            not validation.get("matched")
            and "observed_header_candidates" in validation
            and not validation.get("profile_structure_found")
            and not validation.get("structural_terms")
            and not validation.get("search_page_evidence")
            and not validation.get("name_candidates")
            and len(validation.get("observed_header_candidates") or []) <= 1
        )

    @staticmethod
    def _is_partial_structure_intermediate(validation: dict[str, Any]) -> bool:
        """资料页名称已出现但导航结构尚未完整渲染时，属于半加载中间态。"""
        return bool(
            not validation.get("matched")
            and not validation.get("profile_structure_found")
            and not validation.get("search_page_evidence")
            and bool(validation.get("name_candidates"))
        )

    @staticmethod
    def _should_retry_current_tab(validation: dict[str, Any]) -> bool:
        return bool(
            Win11WeChatAdapter._is_sparse_intermediate(validation)
            or Win11WeChatAdapter._is_partial_structure_intermediate(validation)
        )

    def _stable_capture(self, window: Any) -> Image.Image:
        if self._wait_for_stable_frames is not None:
            return self._wait_for_stable_frames(window.rect)
        return self._capture_window(window.rect)

    def _validate_current_profile(
        self,
        window: Any,
        expected_name: str,
    ) -> tuple[Image.Image, dict[str, Any]]:
        screenshot = self._stable_capture(window)
        validation = self._validate_profile_header(screenshot, expected_name)
        home_used = False

        # 页面结构已经成立但名称被滚动出视口时，先回到顶部再做身份校验。
        if (
            not validation.get("matched")
            and validation.get("profile_structure_found")
            and self._press_ctrl_home is not None
        ):
            home_used = True
            self._press_ctrl_home()
            screenshot = self._stable_capture(window)
            validation = self._validate_profile_header(screenshot, expected_name)
            self._log_event(
                "profile_identity_retried_after_home",
                account=expected_name,
                retry=1,
                matched=bool(validation.get("matched")),
                reason=validation.get("reason"),
            )
            # Ctrl+Home 后的滚动动画可能尚未结束，结构成立但名称仍不可见时
            # 再有限等待一帧，避免把刚回到顶部的资料页误判为不存在。
            if (
                not validation.get("matched")
                and validation.get("profile_structure_found")
            ):
                self._sleep(0.35)
                screenshot = self._stable_capture(window)
                validation = self._validate_profile_header(screenshot, expected_name)
                self._log_event(
                    "profile_identity_retried_after_home",
                    account=expected_name,
                    retry=2,
                    matched=bool(validation.get("matched")),
                    reason=validation.get("reason"),
                )

        # 空白、加载动画、只有极少文字，或名称已出现但导航结构尚未完整
        # 渲染时，都留在当前标签有限重试，不能立即切走。
        for retry_index in range(2):
            if not self._should_retry_current_tab(validation):
                break
            intermediate_type = (
                "partial_structure"
                if self._is_partial_structure_intermediate(validation)
                else "sparse"
            )
            self._sleep(0.35)
            screenshot = self._stable_capture(window)
            validation = self._validate_profile_header(screenshot, expected_name)
            self._log_event(
                "profile_tab_intermediate_retry",
                account=expected_name,
                retry=retry_index + 1,
                intermediate_type=intermediate_type,
                matched=bool(validation.get("matched")),
                reason=validation.get("reason"),
            )
        # 名称可见但结构证据持续不足时，可能不是渲染中间态，而是页面停在
        # 置顶/列表内容区域导致导航行不在视口；此时回顶一次再确认。
        if (
            not validation.get("matched")
            and not validation.get("search_page_evidence")
            and validation.get("name_candidates")
            and self._press_ctrl_home is not None
        ):
            home_used = True
            self._press_ctrl_home()
            screenshot = self._stable_capture(window)
            validation = self._validate_profile_header(screenshot, expected_name)
            self._log_event(
                "profile_tab_home_after_partial_structure",
                account=expected_name,
                matched=bool(validation.get("matched")),
                reason=validation.get("reason"),
            )
        # 名称在重试后才与资料页结构一起出现时，仍可能是列表/置顶内容遮挡了
        # 头部；未回顶过则补一次 Ctrl+Home，避免清理阶段误判标签不存在。
        if (
            not home_used
            and not validation.get("matched")
            and validation.get("profile_structure_found")
            and not validation.get("search_page_evidence")
            and self._press_ctrl_home is not None
        ):
            home_used = True
            self._press_ctrl_home()
            screenshot = self._stable_capture(window)
            validation = self._validate_profile_header(screenshot, expected_name)
            self._log_event(
                "profile_identity_retried_after_home",
                account=expected_name,
                retry=1,
                matched=bool(validation.get("matched")),
                reason=validation.get("reason"),
            )
        return screenshot, validation

    def activate_profile_tab(
        self,
        window: Any,
        expected_name: str,
        *,
        max_tabs: int = 96,
    ) -> bool:
        """从当前活动标签开始有界扫描，并按整页资料身份确认目标标签。

        不再用压缩标签栏的相似图标判断“回到起点”。多个公众号标签在 Win11
        微信中常显示成完全相同的蓝色图标，该判断会在第二个标签就提前终止。
        """
        self._activate_window(window.hwnd)
        self.last_switch_steps = 0
        self.last_scan_completed_cycle = False
        self.last_scan_saw_search = False
        search_anchor: Image.Image | None = None
        tabs_since_search = 0
        for probe_index in range(max_tabs):
            screenshot, validation = self._validate_current_profile(window, expected_name)
            # 目标账号名称和资料页结构已经同时通过时立即成功返回。此时再对
            # 同一截图识别搜索框/账号分类/搜一搜首页既不增加安全证据，又会
            # 触发多轮本地 OCR，显著拖慢启动预热和后续轮询。
            if validation.get("matched"):
                self.last_match_mode = "profile"
                self._log_event(
                    "profile_tab_probe",
                    account=expected_name,
                    probe_index=probe_index + 1,
                    tab_index=probe_index + 1,
                    scan_origin="current_active_tab",
                    matched=True,
                    observed_name=validation.get("name"),
                    reason=validation.get("reason"),
                    search_page=False,
                    profile_structure_found=True,
                    search_detection_skipped=True,
                )
                self.last_switch_steps = probe_index
                return True

            # 只有资料页身份不匹配时，才需要识别搜一搜工作面并维护绕圈锚点。
            # 专用搜一搜检查偶尔会因 OCR 差异漏检；整页校验已经看到
            # “搜索/-账号”证据时同样视为搜一搜，否则绕圈锚点无法建立，
            # 扫描会一直 Ctrl+Tab 到安全上限。
            is_search = bool(
                validation.get("search_page_evidence")
                or (
                    self._inspect_search_page is not None
                    and self._inspect_search_page(screenshot)
                )
            )
            if is_search:
                self.last_scan_saw_search = True
                if search_anchor is None:
                    search_anchor = screenshot
                    tabs_since_search = 0
                elif tabs_since_search > 0 and (
                    self._same_search_page is None
                    or self._same_search_page(search_anchor, screenshot)
                ):
                    self.last_scan_completed_cycle = True
                    self._log_event(
                        "profile_tab_scan_cycle_completed",
                        account=expected_name,
                        probes=probe_index + 1,
                        matched=False,
                    )
                    return False
            if (
                self._allow_name_only_profile
                and validation.get("name_candidates")
            ):
                self.last_match_mode = "empty_profile_name_only"
                self.last_switch_steps = probe_index
                self._log_event(
                    "profile_tab_empty_service_accepted",
                    account=expected_name,
                    probe_index=probe_index + 1,
                    tab_index=probe_index + 1,
                    observed_name=validation.get("name_candidates"),
                    reason="服务号没有原创内容，名称已匹配且当前标签不是搜一搜页面",
                )
                return True
            self._log_event(
                "profile_tab_probe",
                account=expected_name,
                probe_index=probe_index + 1,
                # 兼容既有日志消费者；它是相对探测次数，不是顶部绝对标签序号。
                tab_index=probe_index + 1,
                scan_origin="current_active_tab",
                matched=bool(validation.get("matched")),
                observed_name=validation.get("name"),
                reason=validation.get("reason"),
                search_page=is_search,
                profile_structure_found=bool(validation.get("profile_structure_found")),
            )
            if probe_index + 1 < max_tabs:
                self._press_ctrl_tab()
                if search_anchor is not None:
                    tabs_since_search += 1
                # 新版微信的内嵌页切换后需要等待标签高亮和页面头部同步更新。
                self._sleep(0.35)
        self._log_event(
            "profile_tab_scan_limit_reached",
            account=expected_name,
            probes=max_tabs,
            search_seen=self.last_scan_saw_search,
            completed_cycle=self.last_scan_completed_cycle,
            reason="tab_cycle_not_observed_before_safety_limit",
        )
        return False

    def inventory_profile_tabs(
        self,
        window: Any,
        expected_names: list[str],
        *,
        max_tabs: int = 96,
        evidence_callback: Callable[[int, Image.Image, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """只绕标签池一圈，同时登记全部监听账号资料页。

        与 ``activate_profile_tab`` 的逐账号查找不同，本方法每个标签只执行一次
        资料页 OCR。明确识别成其他监听账号时直接登记并切换；只有资料页结构
        成立、头部身份确实不可见时才回到顶部重试。
        """
        if self._identify_profile_account is None:
            raise RuntimeError("未配置资料页批量身份识别器")
        accounts = list(dict.fromkeys(name for name in expected_names if name))
        self._activate_window(window.hwnd)
        profiles: dict[str, list[int]] = {}
        search_anchor: Image.Image | None = None
        first_search_probe: int | None = None
        tabs_since_search = 0
        completed_cycle = False
        cycle_length: int | None = None

        for probe_index in range(max_tabs):
            screenshot = self._stable_capture(window)
            identity = self._identify_profile_account(screenshot, accounts)
            home_used = False

            for retry_index in range(2):
                if (
                    identity.get("matched")
                    or identity.get("profile_structure_found")
                    or identity.get("search_page_evidence")
                    or len(identity.get("observed_header_candidates") or []) > 1
                ):
                    break
                self._sleep(0.35)
                screenshot = self._stable_capture(window)
                identity = self._identify_profile_account(screenshot, accounts)
                self._log_event(
                    "watch_profile_inventory_intermediate_retry",
                    probe_index=probe_index + 1,
                    retry=retry_index + 1,
                    matched_account=identity.get("account"),
                    profile_structure_found=bool(identity.get("profile_structure_found")),
                    observed_header_candidates=identity.get("observed_header_candidates") or [],
                )

            if (
                not identity.get("matched")
                and identity.get("profile_structure_found")
                and not identity.get("header_identity_visible")
                and self._press_ctrl_home is not None
            ):
                home_used = True
                self._press_ctrl_home()
                screenshot = self._stable_capture(window)
                identity = self._identify_profile_account(screenshot, accounts)
                self._log_event(
                    "watch_profile_inventory_retried_after_home",
                    probe_index=probe_index + 1,
                    matched=bool(identity.get("matched")),
                    account=identity.get("account"),
                    observed_header_candidates=identity.get("observed_header_candidates") or [],
                )

            account = identity.get("account") if identity.get("matched") else None
            if account in accounts:
                profiles.setdefault(account, []).append(probe_index)

            is_search = bool(
                not identity.get("profile_structure_found")
                and (
                    identity.get("search_page_evidence")
                    or (
                        self._inspect_search_page is not None
                        and self._inspect_search_page(screenshot)
                    )
                )
            )
            self._log_event(
                "watch_profile_inventory_probe",
                probe_index=probe_index + 1,
                matched_account=account,
                observed_name=identity.get("name"),
                observed_header_candidates=identity.get("observed_header_candidates") or [],
                profile_structure_found=bool(identity.get("profile_structure_found")),
                search_page=is_search,
                ctrl_home_used=home_used,
            )
            if evidence_callback is not None and (
                identity.get("profile_structure_found") and not account
            ):
                evidence_callback(probe_index + 1, screenshot, identity)

            if is_search:
                if search_anchor is None:
                    search_anchor = screenshot
                    first_search_probe = probe_index
                    tabs_since_search = 0
                elif tabs_since_search > 0 and (
                    self._same_search_page is None
                    or self._same_search_page(search_anchor, screenshot)
                ):
                    completed_cycle = True
                    cycle_length = probe_index - int(first_search_probe or 0)
                    break

            if probe_index + 1 < max_tabs:
                self._press_ctrl_tab()
                if search_anchor is not None:
                    tabs_since_search += 1
                self._sleep(0.35)

        primary_positions = {
            account: positions[0]
            for account, positions in profiles.items()
            if positions
        }
        duplicate_positions = {}
        for account, positions in profiles.items():
            if len(positions) <= 1:
                continue
            first_position = int(positions[0])
            real_duplicates = [
                position
                for position in positions[1:]
                if cycle_length is None
                or (int(position) - first_position) < int(cycle_length)
            ]
            if real_duplicates:
                duplicate_positions[account] = real_duplicates
        result = {
            "completed_cycle": completed_cycle,
            "cycle_length": cycle_length,
            "profile_positions": primary_positions,
            "duplicate_positions": duplicate_positions,
            "profiles_found": [name for name in accounts if name in primary_positions],
            "profiles_missing": [name for name in accounts if name not in primary_positions],
        }
        self._log_event("watch_profile_inventory_finished", **result)
        return result

    def close_profile_tab_if_confirmed(
        self,
        window: Any,
        expected_name: str,
    ) -> bool:
        """仅在当前标签再次确认是目标资料页时关闭它。"""
        if not self.activate_profile_tab(window, expected_name):
            self._log_event(
                "profile_tab_close_skipped",
                account=expected_name,
                reason="profile_tab_not_confirmed_before_cleanup",
                action="preserve_all_tabs",
            )
            return False
        self._press_ctrl_w()
        self._sleep(0.15)
        self._log_event(
            "profile_tab_closed",
            account=expected_name,
            method="ctrl-w-active-profile-tab",
        )
        return True
