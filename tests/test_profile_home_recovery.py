"""资料页名称或导航因滚动不可见时回顶恢复的离线测试。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from wechat_rpa.runtime import collector_runtime as rpa


class ProfileHomeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1200, 900),
        )

    def test_partial_structure_triggers_home_recovery(self) -> None:
        """名称已出现但导航结构不足时也应回顶，不能当作普通中间态一直等待。"""
        validate = Mock(
            side_effect=[
                {
                    "matched": False,
                    "reason": "找到账号名称但缺少公众号资料页结构证据",
                    "name_candidates": ["法治日报"],
                    "profile_structure_found": False,
                    "search_page_evidence": [],
                },
                {"matched": True, "name": "法治日报"},
            ]
        )
        with (
            patch.object(rpa, "wait_for_stable_frames", return_value=Mock()),
            patch.object(rpa, "press_ctrl_home") as press_home,
            patch.object(rpa, "log_event") as log_event,
            patch.object(rpa.PROFILE_OCR, "validate_profile_header", side_effect=validate),
        ):
            rpa.validate_profile_with_home_recovery(self.window, "法治日报")

        press_home.assert_called_once_with()
        self.assertEqual(validate.call_count, 2)
        event = next(
            call.kwargs
            for call in log_event.call_args_list
            if call.args[0] == "profile_identity_retried_after_home"
        )
        self.assertEqual(event["home_trigger"], "partial_structure")

    def test_structure_without_name_still_triggers_home_recovery(self) -> None:
        """结构成立但名称不可见时沿用既有回顶路径。"""
        validate = Mock(
            side_effect=[
                {
                    "matched": False,
                    "reason": "资料页结构成立但名称不可见",
                    "profile_structure_found": True,
                    "search_page_evidence": [],
                },
                {"matched": True, "name": "澎湃新闻"},
            ]
        )
        with (
            patch.object(rpa, "wait_for_stable_frames", return_value=Mock()),
            patch.object(rpa, "press_ctrl_home") as press_home,
            patch.object(rpa, "log_event") as log_event,
            patch.object(rpa.PROFILE_OCR, "validate_profile_header", side_effect=validate),
        ):
            rpa.validate_profile_with_home_recovery(self.window, "澎湃新闻")

        press_home.assert_called_once_with()
        event = next(
            call.kwargs
            for call in log_event.call_args_list
            if call.args[0] == "profile_identity_retried_after_home"
        )
        self.assertEqual(event["home_trigger"], "structure_without_name")

    def test_preserved_scroll_accepts_structure_without_name(self) -> None:
        """连续翻页中结构成立即可继续，不回顶破坏滚动位置。"""
        window = self.window
        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=Mock()),
            patch.object(
                rpa.PROFILE_OCR,
                "validate_profile_header",
                return_value={
                    "matched": False,
                    "profile_structure_found": True,
                    "search_page_evidence": [],
                },
            ),
        ):
            self.assertTrue(rpa.confirm_embedded_profile_preserved(window, "澎湃新闻"))

    def test_preserved_scroll_rejects_search_page(self) -> None:
        """搜一搜证据出现时不能把当前标签当作资料页继续滚动。"""
        window = self.window
        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=Mock()),
            patch.object(
                rpa.PROFILE_OCR,
                "validate_profile_header",
                return_value={
                    "matched": False,
                    "profile_structure_found": False,
                    "search_page_evidence": ["搜索"],
                },
            ),
        ):
            self.assertFalse(rpa.confirm_embedded_profile_preserved(window, "澎湃新闻"))


if __name__ == "__main__":
    unittest.main()
