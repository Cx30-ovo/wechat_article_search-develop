from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from wechat_rpa.desktop.win11_adapter import Win11WeChatAdapter


class Win11AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = SimpleNamespace(hwnd=100, rect=(0, 0, 1600, 1440))
        self.activate_window = Mock()
        self.capture_window = Mock(
            side_effect=[Image.new("RGB", (10, 10), "black"), Image.new("RGB", (10, 10), "white")]
        )
        self.validate_profile_header = Mock(
            side_effect=[{"matched": False}, {"matched": True, "name": "厦门日报"}]
        )
        self.press_ctrl_tab = Mock()
        self.press_ctrl_w = Mock()
        self.log_event = Mock()
        self.adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=self.capture_window,
            validate_profile_header=self.validate_profile_header,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
        )

    def test_profile_tab_is_found_by_content_not_window_handle(self) -> None:
        self.assertTrue(self.adapter.activate_profile_tab(self.window, "厦门日报"))
        self.activate_window.assert_called_once_with(100)
        self.assertEqual(self.validate_profile_header.call_count, 2)
        self.press_ctrl_tab.assert_called_once_with()
        self.adapter._sleep.assert_called_once_with(0.35)
        self.press_ctrl_w.assert_not_called()

    def test_exact_profile_match_skips_search_page_detection(self) -> None:
        inspect_search = Mock()
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=Mock(
                return_value={
                    "matched": True,
                    "name": "厦门日报",
                    "profile_structure_found": True,
                }
            ),
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            inspect_search_page=inspect_search,
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门日报"))
        inspect_search.assert_not_called()
        self.press_ctrl_tab.assert_not_called()

    def test_empty_service_name_only_profile_can_be_accepted_when_allowed(self) -> None:
        inspect_search = Mock(return_value=False)
        validate = Mock(
            return_value={
                "matched": False,
                "name_candidates": ["厦门预警信息发布 事业单位"],
                "search_page_evidence": [],
                "profile_structure_found": False,
            }
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            press_ctrl_home=Mock(),
            inspect_search_page=inspect_search,
            allow_name_only_profile=True,
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门预警信息发布"))
        self.assertEqual(adapter.last_match_mode, "empty_profile_name_only")
        self.assertTrue(
            any(
                call.args[0] == "profile_tab_empty_service_accepted"
                for call in self.log_event.call_args_list
            )
        )

    def test_empty_service_profile_can_be_closed_when_allowed(self) -> None:
        validate = Mock(
            return_value={
                "matched": False,
                "name_candidates": ["厦门预警信息发布 事业单位"],
                "search_page_evidence": [],
                "profile_structure_found": False,
            }
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            press_ctrl_home=Mock(),
            inspect_search_page=Mock(return_value=False),
            allow_name_only_profile=True,
        )

        self.assertTrue(
            adapter.close_profile_tab_if_confirmed(self.window, "厦门预警信息发布")
        )
        self.press_ctrl_w.assert_called_once_with()

    def test_similar_tab_icons_do_not_end_scan_early(self) -> None:
        self.capture_window.side_effect = None
        self.capture_window.return_value = Image.new("RGB", (10, 10), "black")
        self.validate_profile_header.side_effect = lambda image, name: {"matched": False}

        self.assertFalse(self.adapter.activate_profile_tab(self.window, "厦门晚报", max_tabs=4))
        self.assertEqual(self.press_ctrl_tab.call_count, 3)
        self.assertTrue(
            any(
                call.args[0] == "profile_tab_scan_limit_reached"
                for call in self.log_event.call_args_list
            )
        )

    def test_profile_close_is_skipped_when_no_profile_tab_is_confirmed(self) -> None:
        self.capture_window.side_effect = None
        self.capture_window.return_value = Image.new("RGB", (10, 10), "black")
        self.validate_profile_header.side_effect = lambda image, name: {"matched": False}
        self.assertFalse(
            self.adapter.close_profile_tab_if_confirmed(
                self.window, "厦门日报"
            )
        )
        self.press_ctrl_w.assert_not_called()
        self.assertTrue(
            any(call.args[0] == "profile_tab_close_skipped" for call in self.log_event.call_args_list)
        )

    def test_scrolled_profile_returns_home_before_exact_identity_check(self) -> None:
        press_home = Mock()
        stable_capture = Mock(
            side_effect=[
                Image.new("RGB", (10, 10), "black"),
                Image.new("RGB", (10, 10), "white"),
            ]
        )
        validate = Mock(
            side_effect=[
                {
                    "matched": False,
                    "profile_structure_found": True,
                    "reason": "资料页结构成立但名称不可见",
                },
                {"matched": True, "name": "厦门晚报"},
            ]
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=self.capture_window,
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            press_ctrl_home=press_home,
            wait_for_stable_frames=stable_capture,
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门晚报"))
        press_home.assert_called_once_with()
        self.press_ctrl_tab.assert_not_called()

    def test_profile_structure_without_name_retries_once_after_home(self) -> None:
        press_home = Mock()
        validate = Mock(
            side_effect=[
                {
                    "matched": False,
                    "profile_structure_found": True,
                    "reason": "资料页结构成立但名称不可见",
                },
                {
                    "matched": False,
                    "profile_structure_found": True,
                    "reason": "资料页结构成立但名称不可见",
                },
                {"matched": True, "name": "厦门晚报"},
            ]
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=self.capture_window,
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            press_ctrl_home=press_home,
            wait_for_stable_frames=Mock(return_value=Image.new("RGB", (10, 10), "black")),
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门晚报"))
        self.assertEqual(press_home.call_count, 1)
        self.assertEqual(validate.call_count, 3)
        self.press_ctrl_tab.assert_not_called()

    def test_structure_appearing_after_loading_retry_still_returns_home(self) -> None:
        """先稀疏、重试后才出现资料页结构时，也要回顶确认名称再允许关闭。"""
        press_home = Mock()
        validate = Mock(
            side_effect=[
                {
                    "matched": False,
                    "profile_structure_found": False,
                    "name_candidates": [],
                    "structural_terms": [],
                    "observed_header_candidates": ["Q"],
                    "search_page_evidence": [],
                },
                {
                    "matched": False,
                    "profile_structure_found": True,
                    "name_candidates": [],
                    "structural_terms": ["关注", "全部", "文章"],
                    "reason": "资料页结构成立但名称不可见",
                    "search_page_evidence": [],
                },
                {"matched": True, "name": "厦门晚报"},
            ]
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            press_ctrl_home=press_home,
            wait_for_stable_frames=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            inspect_search_page=Mock(return_value=False),
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门晚报"))
        self.assertEqual(press_home.call_count, 1)
        self.assertEqual(validate.call_count, 3)
        self.press_ctrl_tab.assert_not_called()

    def test_loading_intermediate_retries_current_tab_before_switching(self) -> None:
        validate = Mock(
            side_effect=[
                {
                    "matched": False,
                    "reason": "公众号资料页整屏未找到名称匹配",
                    "observed_header_candidates": ["Q"],
                    "structural_terms": [],
                    "search_page_evidence": [],
                },
                {"matched": True, "name": "厦门日报"},
            ]
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门日报"))
        self.press_ctrl_tab.assert_not_called()
        self.assertEqual(validate.call_count, 2)

    def test_partial_structure_intermediate_retries_current_tab_before_switching(self) -> None:
        validate = Mock(
            side_effect=[
                {
                    "matched": False,
                    "reason": "找到账号名称但缺少公众号资料页结构证据",
                    "name_candidates": ["厦门日报"],
                    "structural_terms": ["关注", "全部"],
                    "profile_structure_found": False,
                    "aligned_navigation_count": 1,
                    "search_page_evidence": [],
                },
                {"matched": True, "name": "厦门日报"},
            ]
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门日报"))
        self.press_ctrl_tab.assert_not_called()
        self.assertEqual(validate.call_count, 2)
        self.assertTrue(
            any(
                call.args[0] == "profile_tab_intermediate_retry"
                and call.kwargs.get("intermediate_type") == "partial_structure"
                for call in self.log_event.call_args_list
            )
        )

    def test_partial_structure_retry_is_bounded_before_tab_switch(self) -> None:
        partial_structure = {
            "matched": False,
            "reason": "找到账号名称但缺少公众号资料页结构证据",
            "name_candidates": ["厦门日报"],
            "structural_terms": ["关注", "全部"],
            "profile_structure_found": False,
            "aligned_navigation_count": 1,
            "search_page_evidence": [],
        }
        validate = Mock(return_value=partial_structure)
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
        )

        self.assertFalse(adapter.activate_profile_tab(self.window, "厦门日报", max_tabs=2))
        self.assertEqual(validate.call_count, 6)
        self.assertEqual(self.press_ctrl_tab.call_count, 1)
        self.assertEqual(
            sum(
                1
                for call in self.log_event.call_args_list
                if call.args[0] == "profile_tab_intermediate_retry"
            ),
            4,
        )

    def test_partial_structure_returns_home_after_repeated_retry(self) -> None:
        """名称可见但结构持续不足时，应回到顶部再确认，而不是直接切走。"""
        press_home = Mock()
        partial_structure = {
            "matched": False,
            "reason": "找到账号名称但缺少公众号资料页结构证据",
            "name_candidates": ["厦门日报"],
            "structural_terms": ["关注", "全部"],
            "profile_structure_found": False,
            "aligned_navigation_count": 1,
            "search_page_evidence": [],
        }
        validate = Mock(
            side_effect=[
                partial_structure,
                partial_structure,
                partial_structure,
                {"matched": True, "name": "厦门日报"},
            ]
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            press_ctrl_home=press_home,
        )

        self.assertTrue(adapter.activate_profile_tab(self.window, "厦门日报"))
        self.assertEqual(press_home.call_count, 1)
        self.assertEqual(validate.call_count, 4)
        self.press_ctrl_tab.assert_not_called()
        self.assertTrue(
            any(
                call.args[0] == "profile_tab_home_after_partial_structure"
                for call in self.log_event.call_args_list
            )
        )

    def test_search_page_with_account_name_is_not_retried(self) -> None:
        validate = Mock(
            return_value={
                "matched": False,
                "reason": "找到账号名称但当前仍是搜一搜页面",
                "name_candidates": ["厦门日报"],
                "structural_terms": [],
                "profile_structure_found": False,
                "aligned_navigation_count": 0,
                "search_page_evidence": ["搜索"],
            }
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            inspect_search_page=Mock(return_value=True),
            same_search_page=Mock(return_value=True),
        )

        self.assertFalse(adapter.activate_profile_tab(self.window, "厦门日报", max_tabs=8))
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(self.press_ctrl_tab.call_count, 1)
        self.assertFalse(
            any(
                call.args[0] == "profile_tab_intermediate_retry"
                for call in self.log_event.call_args_list
            )
        )

    def test_search_page_evidence_supports_cycle_anchor_when_inspect_misses(self) -> None:
        validate = Mock(
            return_value={
                "matched": False,
                "reason": "找到账号名称但当前仍是搜一搜页面",
                "name_candidates": ["厦门日报"],
                "profile_structure_found": False,
                "search_page_evidence": ["搜索"],
            }
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=validate,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            inspect_search_page=Mock(return_value=False),
            same_search_page=Mock(return_value=True),
        )

        self.assertFalse(adapter.activate_profile_tab(self.window, "厦门日报", max_tabs=8))
        self.assertTrue(adapter.last_scan_saw_search)
        self.assertTrue(adapter.last_scan_completed_cycle)
        self.assertEqual(self.press_ctrl_tab.call_count, 1)

    def test_absence_requires_return_to_same_search_workspace(self) -> None:
        inspect_search = Mock(side_effect=[True, False, True])
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=Mock(return_value={"matched": False}),
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            inspect_search_page=inspect_search,
            same_search_page=Mock(return_value=True),
        )

        self.assertFalse(adapter.activate_profile_tab(self.window, "不存在的账号", max_tabs=8))
        self.assertTrue(adapter.last_scan_saw_search)
        self.assertTrue(adapter.last_scan_completed_cycle)
        self.assertEqual(self.press_ctrl_tab.call_count, 2)

    def test_inventory_registers_all_accounts_in_one_tab_cycle(self) -> None:
        identify = Mock(
            side_effect=[
                {
                    "matched": False,
                    "profile_structure_found": False,
                    "search_page_evidence": ["搜索"],
                },
                {
                    "matched": True,
                    "account": "厦门日报",
                    "name": "厦门日报",
                    "profile_structure_found": True,
                    "header_identity_visible": True,
                },
                {
                    "matched": True,
                    "account": "厦门晚报",
                    "name": "厦门晚报",
                    "profile_structure_found": True,
                    "header_identity_visible": True,
                },
                {
                    "matched": False,
                    "profile_structure_found": False,
                    "search_page_evidence": ["搜索"],
                },
            ]
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=self.validate_profile_header,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            identify_profile_account=identify,
            inspect_search_page=Mock(side_effect=[True, True]),
            same_search_page=Mock(return_value=True),
        )

        result = adapter.inventory_profile_tabs(
            self.window,
            ["厦门日报", "厦门晚报"],
            max_tabs=8,
        )

        self.assertTrue(result["completed_cycle"])
        self.assertEqual(result["profiles_found"], ["厦门日报", "厦门晚报"])
        self.assertEqual(identify.call_count, 4)
        self.assertEqual(self.press_ctrl_tab.call_count, 3)

    def test_inventory_does_not_home_when_other_identity_text_is_visible(self) -> None:
        press_home = Mock()
        identify = Mock(
            return_value={
                "matched": False,
                "profile_structure_found": True,
                "header_identity_visible": True,
                "observed_header_candidates": ["未知公众号"],
            }
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=self.validate_profile_header,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            press_ctrl_home=press_home,
            identify_profile_account=identify,
        )

        adapter.inventory_profile_tabs(self.window, ["厦门日报"], max_tabs=1)

        press_home.assert_not_called()

    def test_inventory_uses_search_evidence_as_cycle_anchor_when_inspect_misses(self) -> None:
        identify = Mock(
            return_value={
                "matched": False,
                "profile_structure_found": False,
                "search_page_evidence": ["搜索"],
            }
        )
        adapter = Win11WeChatAdapter(
            activate_window=self.activate_window,
            capture_window=Mock(return_value=Image.new("RGB", (10, 10), "black")),
            validate_profile_header=self.validate_profile_header,
            press_ctrl_tab=self.press_ctrl_tab,
            press_ctrl_w=self.press_ctrl_w,
            log_event=self.log_event,
            sleep=Mock(),
            identify_profile_account=identify,
            inspect_search_page=Mock(return_value=False),
            same_search_page=Mock(return_value=True),
        )

        result = adapter.inventory_profile_tabs(self.window, ["厦门日报"], max_tabs=8)

        self.assertTrue(result["completed_cycle"])
        self.assertEqual(self.press_ctrl_tab.call_count, 1)


if __name__ == "__main__":
    unittest.main()
