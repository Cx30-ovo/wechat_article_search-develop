"""浏览器标签清理的离线回归测试：宁可少清理，也不能关闭搜一搜页。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from wechat_rpa.runtime import collector_runtime as rpa


class BrowserTabCleanupTests(unittest.TestCase):
    def test_recovery_never_closes_stale_window(self) -> None:
        """页面校验失败只允许无损新建，绝不能销毁用户当前窗口。"""
        stale = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
            process_name="wechatappex.exe",
        )
        recovered = rpa.WindowInfo(
            hwnd=200,
            title="搜一搜",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
            process_name="wechatappex.exe",
        )
        with (
            patch.object(rpa, "close_window") as close_window,
            patch.object(
                rpa,
                "open_sogou_from_wechat_main",
                return_value=recovered,
            ) as open_search,
            patch.object(rpa, "arrange_automation_window", return_value=recovered),
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "press_ctrl_1"),
            patch.object(rpa.time, "sleep"),
            patch.object(rpa, "log_event"),
        ):
            result = rpa.recreate_sogou_search_window(stale, "测试公众号", "页面校验失败")

        self.assertEqual(result.hwnd, recovered.hwnd)
        close_window.assert_not_called()
        open_search.assert_called_once_with("测试公众号", excluded_hwnds={stale.hwnd})

    def test_profile_pool_browser_falls_back_to_registered_hwnd(self) -> None:
        """热更新找不到搜一搜窗口时，应复用预热阶段登记过的浏览器 HWND。"""
        stale = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        registered = rpa.WindowInfo(
            hwnd=200,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(1190, 41, 1920, 979),
            process_name="wechatappex.exe",
        )
        state = {
            "profile_registry": {
                "厦门日报": {"hwnd": 200},
            }
        }

        with (
            patch.object(
                rpa,
                "find_sogou_search_window",
                side_effect=RuntimeError("没有找到微信搜一搜窗口"),
            ),
            patch.object(rpa, "enumerate_wechat_windows", return_value=[stale, registered]),
            patch.object(rpa, "log_event"),
        ):
            window = rpa.find_profile_pool_browser_window(state)

        self.assertEqual(window.hwnd, registered.hwnd)

    def test_profile_pool_browser_raises_when_registered_hwnd_missing(self) -> None:
        """登记的 HWND 已失效时不能盲用旧窗口，仍应报搜一搜窗口缺失。"""
        state = {
            "profile_registry": {
                "厦门日报": {"hwnd": 999},
            }
        }

        with (
            patch.object(
                rpa,
                "find_sogou_search_window",
                side_effect=RuntimeError("没有找到微信搜一搜窗口"),
            ),
            patch.object(rpa, "enumerate_wechat_windows", return_value=[]),
            patch.object(rpa, "log_event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "没有找到微信搜一搜窗口"):
                rpa.find_profile_pool_browser_window(state)

    def test_cleanup_recreates_search_window_when_search_tab_is_missing(self) -> None:
        """旧窗口找不到搜一搜标签时，应重建干净窗口而不是终止整个账号。"""
        stale = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        recovered = rpa.WindowInfo(
            hwnd=200,
            title="搜一搜",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        with (
            patch.object(rpa, "find_sogou_search_window", return_value=stale),
            patch.object(rpa, "find_and_pin_search_tab", side_effect=[False, True]) as pin,
            patch.object(rpa, "recreate_sogou_search_window", return_value=recovered) as recreate,
            patch.object(rpa, "keep_only_search_tab", return_value=0) as normalize,
            patch.object(rpa, "log_event"),
        ):
            rpa.close_article_tabs_until_search("测试公众号")

        recreate.assert_called_once_with(
            stale,
            "测试公众号",
            "遍历现有标签后未找到真正的搜一搜结果页",
        )
        self.assertEqual(pin.call_count, 2)
        normalize.assert_called_once_with(
            recovered,
            "测试公众号",
            close_non_search_tabs=False,
            preserve_current_search_tab=True,
        )

    def test_cleanup_preserves_search_page_when_screen_changes(self) -> None:
        """页面动画造成截图差异时，搜索框仍存在就不应执行 Ctrl+W。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")

        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", side_effect=[image, image, image]),
            patch.object(rpa, "press_ctrl_1"),
            patch.object(rpa, "press_ctrl_9"),
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "_tab_switch_difference", side_effect=[2.5, 8.0]),
            patch.object(rpa.PROFILE_OCR, "locate_search_box", return_value={"found": True}),
            patch.object(rpa.PROFILE_OCR, "locate_account_tab", return_value={"found": True}),
            patch.object(rpa, "log_event"),
        ):
            removed = rpa.keep_only_search_tab(window, "测试公众号")

        self.assertEqual(removed, 0)
        close_tab.assert_not_called()

    def test_cleanup_reports_stable_frame_drift_without_preserving(self) -> None:
        """稳定帧已漂移成黑屏/非搜索状态时，不能继续记录“标签已保留”。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        search_image = Image.new("RGB", (1000, 800), "white")
        drifted_image = Image.new("RGB", (1000, 800), "black")
        search_evidence = {"found": True}
        missing_evidence = {"found": False}

        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", side_effect=[search_image, drifted_image]),
            patch.object(
                rpa,
                "_inspect_sogou_search_results",
                side_effect=[search_evidence, missing_evidence],
            ),
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "log_event") as log_event,
        ):
            removed = rpa.keep_only_search_tab(
                window,
                "测试公众号",
                close_non_search_tabs=False,
            )

        self.assertEqual(removed, 0)
        close_tab.assert_not_called()
        self.assertTrue(
            any(
                call.args and call.args[0] == "browser_tab_state_drifted"
                for call in log_event.call_args_list
            )
        )

    def test_cleanup_closes_false_positive_search_box_on_article_tab(self) -> None:
        """文章分享弹窗含搜索框时，只要与首标签差异显著仍应关闭该文章标签。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")

        with (
            patch.object(rpa, "activate_window") as activate,
            patch.object(rpa, "capture_window", side_effect=[image, image, image, image, image]),
            patch.object(rpa, "press_ctrl_1") as first_tab,
            patch.object(rpa, "press_ctrl_9") as last_tab,
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "_tab_switch_difference", side_effect=[0.2, 22.0, 0.0]),
            patch.object(rpa.PROFILE_OCR, "locate_search_box", return_value={"found": True}),
            patch.object(rpa.PROFILE_OCR, "locate_account_tab", return_value={"found": True}),
            patch.object(rpa.user32, "IsWindow", return_value=True),
            patch.object(rpa, "log_event"),
        ):
            removed = rpa.keep_only_search_tab(window, "测试公众号")

        self.assertEqual(removed, 1)
        close_tab.assert_called_once()
        self.assertEqual(last_tab.call_count, 2)
        # 初始定位首标签一次，关闭文章后再次回首标签校验。
        self.assertGreaterEqual(first_tab.call_count, 2)
        self.assertGreaterEqual(activate.call_count, 4)

    def test_search_tab_is_found_without_reordering_tabs(self) -> None:
        """搜一搜不在首标签时，只遍历并停留在当前已确认的标签。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")
        missing = {
            "found": False,
            "search_box": {"found": False},
            "account_tab": {"found": False},
        }
        found = {
            "found": True,
            "search_box": {"found": True},
            "account_tab": {"found": True},
        }

        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=image),
            patch.object(rpa, "press_ctrl_tab") as next_tab,
            patch.object(rpa, "press_ctrl_shift_pageup") as move_left,
            patch.object(
                rpa.PROFILE_OCR,
                "validate_profile_header",
                return_value={"matched": False},
            ),
            patch.object(rpa, "_inspect_sogou_search_results", side_effect=[missing, missing, found]),
            patch.object(rpa, "log_event"),
        ):
            recovered = rpa.find_and_pin_search_tab(window, "测试公众号", max_tabs=5)

        self.assertTrue(recovered)
        self.assertEqual(next_tab.call_count, 2)
        move_left.assert_not_called()

    def test_recovery_preserves_current_search_tab_without_ctrl1(self) -> None:
        """已扫描选中的搜一搜标签不能被清理阶段的 Ctrl+1 切走。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")

        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", side_effect=[image, image]),
            patch.object(rpa, "press_ctrl_1") as first_tab,
            patch.object(rpa, "_inspect_sogou_search_results", return_value={"found": True}),
            patch.object(rpa, "log_event"),
        ):
            removed = rpa.keep_only_search_tab(
                window,
                "测试公众号",
                close_non_search_tabs=False,
                preserve_current_search_tab=True,
            )

        self.assertEqual(removed, 0)
        first_tab.assert_not_called()

    def test_search_workbench_drift_recovers_by_polling(self) -> None:
        """清理后短暂全黑不能直接判定搜一搜失效，应等待画面恢复。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        black_image = Image.new("RGB", (1000, 800), "black")
        search_image = Image.new("RGB", (1000, 800), "white")
        missing = {
            "found": False,
            "search_box": {"found": False, "reason": "黑屏"},
            "account_tab": {"found": False},
        }
        found = {
            "found": True,
            "search_box": {"found": True},
            "account_tab": {"found": True},
        }

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(rpa, "capture_window", side_effect=[black_image, search_image]),
                patch.object(rpa, "_inspect_sogou_search_results", side_effect=[missing, found]),
                patch.object(rpa, "find_and_pin_search_tab") as pin,
                patch.object(rpa, "recreate_sogou_search_window") as recreate,
                patch.object(rpa, "activate_window"),
                patch.object(rpa.time, "sleep"),
                patch.object(rpa, "log_event"),
            ):
                recovered, image, evidence = rpa.recover_search_workbench(
                    window,
                    "测试公众号",
                    Path(directory),
                    reason="标签清理后黑屏",
                    poll_attempts=2,
                )

        self.assertEqual(recovered.hwnd, window.hwnd)
        self.assertIs(image, search_image)
        self.assertTrue(evidence["found"])
        pin.assert_not_called()
        recreate.assert_not_called()

    def test_search_workbench_recreates_when_drift_does_not_recover(self) -> None:
        """等待和标签扫描都失败时才无损重建搜一搜窗口。"""
        stale = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        recovered_window = rpa.WindowInfo(
            hwnd=200,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        black_image = Image.new("RGB", (1000, 800), "black")
        recovered_image = Image.new("RGB", (1000, 800), "white")
        missing = {
            "found": False,
            "search_box": {"found": False, "reason": "黑屏"},
            "account_tab": {"found": False},
        }
        found = {
            "found": True,
            "search_box": {"found": True},
            "account_tab": {"found": True},
        }

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    rpa,
                    "capture_window",
                    side_effect=[black_image, black_image, black_image, recovered_image],
                ),
                patch.object(
                    rpa,
                    "_inspect_sogou_search_results",
                    side_effect=[missing, missing, found],
                ),
                patch.object(rpa, "find_and_pin_search_tab", return_value=False) as pin,
                patch.object(
                    rpa,
                    "recreate_sogou_search_window",
                    return_value=recovered_window,
                ) as recreate,
                patch.object(rpa, "activate_window"),
                patch.object(rpa.time, "sleep"),
                patch.object(rpa, "log_event"),
            ):
                recovered, image, evidence = rpa.recover_search_workbench(
                    stale,
                    "测试公众号",
                    Path(directory),
                    reason="标签清理后黑屏",
                    poll_attempts=2,
                )

        self.assertEqual(recovered.hwnd, recovered_window.hwnd)
        self.assertIs(image, recovered_image)
        self.assertTrue(evidence["found"])
        self.assertEqual(pin.call_count, 1)
        recreate.assert_called_once()

    def test_direct_close_keeps_search_tab_without_global_probe(self) -> None:
        """文章正常采集后应直接关当前标签，不触发全量标签轮询。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")
        article_page = {"found": False, "search_box": {"found": False}, "account_tab": {"found": False}}
        search_page = {"found": True, "search_box": {"found": True}, "account_tab": {"found": True}}

        with (
            patch.object(rpa, "find_article_window", return_value=(window.hwnd, window.rect)),
            patch.object(rpa.user32, "GetForegroundWindow", return_value=window.hwnd),
            patch.object(rpa, "capture_window", side_effect=[image, image]),
            patch.object(rpa, "_inspect_sogou_search_results", side_effect=[article_page, search_page]),
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "find_sogou_search_window", return_value=window),
            patch.object(rpa, "log_event"),
        ):
            closed = rpa.close_current_article_tab("测试公众号", "测试文章")

        self.assertTrue(closed)
        close_tab.assert_called_once()

    def test_direct_close_accepts_scrolled_parent_profile_without_global_scan(self) -> None:
        """文章关闭后资料页名称不可见时，结构证据应保留滚动位置并跳过全标签扫描。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
            page_kind="embedded_profile_tab",
        )
        image = Image.new("RGB", (1000, 800), "white")
        article_page = {
            "found": False,
            "search_box": {"found": False},
            "account_tab": {"found": False},
        }

        with (
            patch.object(rpa, "find_article_window", return_value=(window.hwnd, window.rect)),
            patch.object(rpa.user32, "GetForegroundWindow", return_value=window.hwnd),
            patch.object(rpa, "capture_window", return_value=image),
            patch.object(rpa, "_inspect_sogou_search_results", return_value=article_page),
            patch.object(
                rpa.PROFILE_OCR,
                "validate_profile_header",
                side_effect=[
                    {"matched": False, "profile_structure_found": False},
                    {
                        "matched": False,
                        "profile_structure_found": True,
                        "aligned_navigation_count": 3,
                        "search_page_evidence": [],
                    },
                ],
            ),
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "activate_embedded_profile_tab") as global_scan,
            patch.object(rpa, "log_event") as log_event,
        ):
            closed = rpa.close_current_article_tab(
                "厦门日报",
                "测试文章",
                return_window=window,
            )

        self.assertTrue(closed)
        close_tab.assert_called_once()
        global_scan.assert_not_called()
        self.assertTrue(
            any(
                call.args[0] == "article_tab_closed_directly"
                and call.kwargs.get("global_profile_scan_skipped") is True
                for call in log_event.call_args_list
            )
        )

    def test_direct_close_uses_global_scan_only_when_immediate_return_is_not_profile(self) -> None:
        """当前返回页没有资料页结构时，才允许进入原有安全扫描。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
            page_kind="embedded_profile_tab",
        )
        image = Image.new("RGB", (1000, 800), "white")
        article_page = {
            "found": False,
            "search_box": {"found": False},
            "account_tab": {"found": False},
        }

        with (
            patch.object(rpa, "find_article_window", return_value=(window.hwnd, window.rect)),
            patch.object(rpa.user32, "GetForegroundWindow", return_value=window.hwnd),
            patch.object(rpa, "capture_window", return_value=image),
            patch.object(rpa, "_inspect_sogou_search_results", return_value=article_page),
            patch.object(
                rpa.PROFILE_OCR,
                "validate_profile_header",
                side_effect=[
                    {"matched": False, "profile_structure_found": False},
                    {
                        "matched": False,
                        "profile_structure_found": False,
                        "search_page_evidence": [],
                    },
                    {
                        "matched": False,
                        "profile_structure_found": False,
                        "search_page_evidence": [],
                    },
                    {
                        "matched": False,
                        "profile_structure_found": False,
                        "search_page_evidence": [],
                    },
                    {"matched": True, "profile_structure_found": True},
                ],
            ),
            patch.object(rpa, "press_ctrl_w"),
            patch.object(rpa, "activate_embedded_profile_tab", return_value=True) as global_scan,
            patch.object(rpa, "log_event"),
        ):
            closed = rpa.close_current_article_tab(
                "厦门日报",
                "测试文章",
                return_window=window,
            )

        self.assertTrue(closed)
        global_scan.assert_called_once_with(window, "厦门日报")

    def test_cleanup_falls_back_when_direct_close_is_uncertain(self) -> None:
        """无法确认当前标签是文章时，仍使用原有安全恢复流程。"""
        with (
            patch.object(rpa, "close_current_article_tab", return_value=False),
            patch.object(rpa, "close_article_tabs_until_search") as recover,
            patch.object(rpa, "log_event"),
        ):
            rpa.close_article_after_attempt("测试公众号", "测试文章")

        recover.assert_called_once_with("测试公众号")


if __name__ == "__main__":
    unittest.main()
