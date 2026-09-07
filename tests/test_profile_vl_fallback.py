"""“搜一搜 → 资料页”默认采集链路的 Qwen-VL 兼容回归测试。

测试不连接真实微信、Qwen-VL 或 MongoDB，只验证模型具备明确的触发条件和不可通过的安全边界。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from wechat_rpa.runtime import collector_runtime as rpa


class ProfileVLFallbackTests(unittest.TestCase):
    def test_profile_window_is_found_by_header_when_title_is_generic_wechat(self) -> None:
        """新版微信资料窗口标题也是“微信”，必须使用窗口内容确认账号。"""
        search = rpa.WindowInfo(
            1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1600, 1440)
        )
        profile = rpa.WindowInfo(
            2, "微信", "Qt51514QWindowIcon", rpa.Rect(100, 100, 1246, 993)
        )
        main = rpa.WindowInfo(
            3, "微信", "Qt51514QWindowIcon", rpa.Rect(0, 0, 586, 773)
        )
        profile_image = Image.new("RGB", (1146, 893), "white")
        main_image = Image.new("RGB", (586, 773), "gray")

        def capture(window_rect: rpa.Rect) -> Image.Image:
            return profile_image if window_rect == profile.rect else main_image

        def validate(image: Image.Image, expected: str) -> dict[str, object]:
            return {"matched": image is profile_image and expected == "厦门日报"}

        with (
            patch.object(rpa, "enumerate_wechat_windows", return_value=[search, profile, main]),
            patch.object(rpa, "capture_window", side_effect=capture),
            patch.object(rpa.PROFILE_OCR, "validate_profile_header", side_effect=validate),
        ):
            result = rpa.find_official_profile_window(
                "厦门日报", excluded_hwnds={search.hwnd}
            )

        self.assertEqual(result.hwnd, profile.hwnd)

    def test_profile_page_in_search_browser_is_marked_as_embedded_tab(self) -> None:
        """公众号资料页可能是搜一搜浏览器新标签，不能按窗口关闭。"""
        search = rpa.WindowInfo(
            1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1600, 1440)
        )
        profile_image = Image.new("RGB", (1600, 1440), "white")

        with (
            patch.object(rpa, "enumerate_wechat_windows", return_value=[search]),
            patch.object(rpa, "capture_window", return_value=profile_image),
            patch.object(
                rpa.PROFILE_OCR,
                "validate_profile_header",
                return_value={"matched": True},
            ),
        ):
            result = rpa.find_official_profile_window(
                "厦门日报",
                excluded_hwnds={search.hwnd},
                search_window=search,
            )

        self.assertEqual(result.hwnd, search.hwnd)
        self.assertEqual(result.page_kind, "embedded_profile_tab")

    def test_search_initialization_preserves_existing_non_search_tabs(self) -> None:
        """启动搜索流程时不能把已打开的公众号资料页当成旧标签关闭。"""
        screenshot = Image.new("RGB", (1600, 1440), "white")
        window = rpa.WindowInfo(1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1600, 1440))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.time, "sleep"),
            patch.object(rpa, "_inspect_sogou_search_results", return_value={"found": True}),
            patch.object(rpa, "press_ctrl_9") as ctrl_9,
            patch.object(rpa, "press_ctrl_w") as ctrl_w,
        ):
            removed = rpa.keep_only_search_tab(
                window,
                "厦门日报",
                Path(directory),
                close_non_search_tabs=False,
            )

        self.assertEqual(removed, 0)
        ctrl_9.assert_not_called()
        ctrl_w.assert_not_called()

    def test_search_account_tab_vl_has_enough_output_budget(self) -> None:
        """内网 Qwen3 推理不能因过小 token 限额返回空 content。"""
        client = rpa.QwenVisionClient(
            rpa.QwenVisionConfig(
                base_url="http://127.0.0.1:8000/v1",
                api_key="",
                model="Qwen3.8-27B-FP8",
                allow_no_auth=True,
            )
        )
        expected = {
            "found": True,
            "label": "账号",
            "center_x_1000": 285,
            "center_y_1000": 185,
            "selected": False,
            "confidence": 0.97,
        }
        screenshot = Image.new("RGB", (1600, 1440), "white")
        with patch.object(client, "analyze", return_value=expected) as analyze:
            self.assertEqual(client.detect_search_account_tab(screenshot), expected)
        self.assertEqual(analyze.call_args.kwargs["max_tokens"], 2048)

    def test_qwen_account_tab_requires_exact_label_safe_region_and_confidence(self) -> None:
        """模型只能返回顶部一级“账号”，不能返回结果标题或低置信度坐标。"""
        accepted = rpa.normalize_qwen_account_tab_action(
            {
                "found": True,
                "label": "账号",
                "center_x_1000": 285,
                "center_y_1000": 185,
                "selected": False,
                "confidence": 0.97,
            }
        )
        self.assertTrue(accepted["found"], accepted)
        self.assertEqual(accepted["method"], "qwen-vl-sogou-account-tab")

        for rejected in (
            {
                "found": True,
                "label": "厦门日报 - 账号",
                "center_x_1000": 180,
                "center_y_1000": 270,
                "confidence": 0.99,
            },
            {
                "found": True,
                "label": "账号",
                "center_x_1000": 285,
                "center_y_1000": 520,
                "confidence": 0.99,
            },
            {
                "found": True,
                "label": "账号",
                "center_x_1000": 285,
                "center_y_1000": 185,
                "confidence": 0.52,
            },
        ):
            self.assertFalse(
                rpa.normalize_qwen_account_tab_action(rejected)["found"], rejected
            )

    def test_qwen_account_tab_selected_requires_explicit_selected_flag(self) -> None:
        result = {
            "found": True,
            "label": "账号",
            "center_x_1000": 285,
            "center_y_1000": 185,
            "selected": False,
            "confidence": 0.97,
        }
        self.assertFalse(
            rpa.normalize_qwen_account_tab_action(
                result, require_selected=True
            )["found"]
        )
        self.assertTrue(
            rpa.normalize_qwen_account_tab_action(
                {**result, "selected": True}, require_selected=True
            )["found"]
        )

    def test_search_account_tab_retries_local_ocr_before_qwen(self) -> None:
        """页面加载较慢时应持续截图，本地识别成功后不得调用视觉模型。"""
        client = Mock()
        screenshot = Image.new("RGB", (1600, 1440), "white")
        window = rpa.WindowInfo(1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1600, 1440))
        local_success = {
            "found": True,
            "center_x_1000": 285,
            "center_y_1000": 185,
            "confidence": 0.91,
            "method": "rapidocr-sogou-account-tab-subregion",
        }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.time, "sleep"),
            patch.object(
                rpa.PROFILE_OCR,
                "locate_all_page_account_result",
                return_value={"found": False, "reason": "结果仍在加载"},
            ),
            patch.object(
                rpa.PROFILE_OCR,
                "locate_account_tab",
                side_effect=[
                    {"found": False, "reason": "结果仍在加载"},
                    {"found": False, "reason": "OCR 暂未识别"},
                    local_success,
                ],
            ) as local_ocr,
        ):
            result, observed = rpa.wait_for_search_account_tab(
                window,
                Path(directory),
                "厦门日报",
                client=client,
                allow_vl=True,
                wait_intervals=(0, 0, 0),
            )

        self.assertEqual(result, local_success)
        self.assertIs(observed, screenshot)
        self.assertEqual(local_ocr.call_count, 3)
        client.detect_search_account_tab.assert_not_called()

    def test_search_account_wait_prefers_win11_all_page_account_card(self) -> None:
        """新版“全部”页命中公众号卡片时，不应再点击顶部账号分类。"""
        client = Mock()
        screenshot = Image.new("RGB", (1568, 1439), "white")
        window = rpa.WindowInfo(1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1568, 1439))
        direct_target = {
            "found": True,
            "layout": "all-account-section",
            "center_x_1000": 230,
            "center_y_1000": 350,
            "matched_name": "厦门日报",
            "method": "rapidocr-sogou-all-page-account-card",
        }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.time, "sleep"),
            patch.object(
                rpa.PROFILE_OCR,
                "locate_all_page_account_result",
                return_value=direct_target,
            ),
            patch.object(rpa.PROFILE_OCR, "locate_account_tab") as old_account_tab,
        ):
            result, observed = rpa.wait_for_search_account_tab(
                window,
                Path(directory),
                "厦门日报",
                client=client,
                allow_vl=True,
                wait_intervals=(0,),
            )

        self.assertIs(observed, screenshot)
        self.assertEqual(result["mode"], "all-account-section")
        self.assertEqual(result["target"], direct_target)
        old_account_tab.assert_not_called()
        client.detect_search_account_tab.assert_not_called()

    def test_search_account_tab_uses_qwen_after_three_local_failures(self) -> None:
        client = Mock()
        client.detect_search_account_tab.return_value = {
            "found": True,
            "label": "账号",
            "center_x_1000": 285,
            "center_y_1000": 185,
            "selected": False,
            "confidence": 0.98,
        }
        screenshot = Image.new("RGB", (1600, 1440), "white")
        window = rpa.WindowInfo(1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1600, 1440))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.time, "sleep"),
            patch.object(
                rpa.PROFILE_OCR,
                "locate_all_page_account_result",
                return_value={"found": False, "reason": "本地未识别"},
            ),
            patch.object(
                rpa.PROFILE_OCR,
                "locate_account_tab",
                return_value={"found": False, "reason": "本地未识别"},
            ) as local_ocr,
        ):
            result, _ = rpa.wait_for_search_account_tab(
                window,
                Path(directory),
                "厦门日报",
                client=client,
                allow_vl=True,
                wait_intervals=(0, 0, 0),
            )

        self.assertTrue(result["found"], result)
        self.assertEqual(result["method"], "qwen-vl-sogou-account-tab")
        self.assertEqual(local_ocr.call_count, 3)
        client.detect_search_account_tab.assert_called_once_with(screenshot)

    def test_search_account_tab_local_only_never_calls_qwen(self) -> None:
        client = Mock()
        screenshot = Image.new("RGB", (1600, 1440), "white")
        window = rpa.WindowInfo(1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1600, 1440))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.time, "sleep"),
            patch.object(
                rpa.PROFILE_OCR,
                "locate_all_page_account_result",
                return_value={"found": False, "reason": "本地未识别"},
            ),
            patch.object(
                rpa.PROFILE_OCR,
                "locate_account_tab",
                return_value={"found": False, "reason": "本地未识别"},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "连续多次未找到一级账号分类"):
                rpa.wait_for_search_account_tab(
                    window,
                    Path(directory),
                    "厦门日报",
                    client=client,
                    allow_vl=False,
                    wait_intervals=(0, 0, 0),
                )

        client.detect_search_account_tab.assert_not_called()

    def test_profile_header_accepts_exact_qwen_name_when_matched_flag_is_false(self) -> None:
        """兼容网关误写 matched 时，精确名称和高置信度仍可安全确认。"""
        self.assertTrue(
            rpa._qwen_profile_header_confirmed(
                {"matched": False, "name": "书生Intern", "confidence": 0.98},
                "书生Intern",
            )
        )

    def test_profile_header_rejects_wrong_name_or_low_confidence(self) -> None:
        """模型不得凭 matched=true 接受其他公众号，也不得接受低置信结果。"""
        self.assertFalse(
            rpa._qwen_profile_header_confirmed(
                {"matched": True, "name": "雅书生Intern", "confidence": 0.99},
                "书生Intern",
            )
        )
        self.assertFalse(
            rpa._qwen_profile_header_confirmed(
                {"matched": True, "name": "书生Intern", "confidence": 0.52},
                "书生Intern",
            )
        )

    def test_qwen_search_target_requires_exact_account_name_and_valid_coordinates(self) -> None:
        """Qwen 只能在精确名称和合法坐标同时存在时返回可点击卡片。"""
        client = Mock()
        client.detect_search_account.return_value = {
            "found": True,
            "name": "ComfyUi中文",
            "center_x_1000": 428,
            "center_y_1000": 304,
            "avatar_x_1000": 118,
            "avatar_y_1000": 304,
            "confidence": 0.98,
        }

        target = rpa._qwen_search_target(
            client, Image.new("RGB", (800, 600), "white"), "ComfyUI中文"
        )

        self.assertTrue(target["found"])
        self.assertTrue(target["is_official_account"])
        self.assertEqual(target["matched_name"], "ComfyUi中文")
        self.assertEqual(target["avatar_x_1000"], 118)

    def test_qwen_search_target_rejects_similar_but_different_account(self) -> None:
        """不能因模型返回相似名称就点击，避免误采同类账号。"""
        client = Mock()
        client.detect_search_account.return_value = {
            "found": True,
            "name": "游戏圈内那些事",
            "center_x_1000": 428,
            "center_y_1000": 304,
        }

        with self.assertRaisesRegex(ValueError, "名称不匹配"):
            rpa._qwen_search_target(
                client, Image.new("RGB", (800, 600), "white"), "游戏那些事Gamez"
            )

    def test_profile_feed_uses_local_ocr_without_calling_qwen(self) -> None:
        """正常页面必须直接使用本地 OCR，不产生额外模型调用。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        local_feed = {
            "time_labels": [{"text": "今天 11:35", "center_y_1000": 210}],
            "articles": [{
                "title": "测试文章",
                "center_x_1000": 500,
                "center_y_1000": 420,
                "list_read_count": 100,
                "list_like_count": 3,
            }],
            "recognition_method": "rapidocr-profile-feed-metric-anchored",
        }
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value=local_feed),
        ):
            feed = rpa.analyze_profile_window(window, Path(directory), client=client)

        self.assertEqual(feed["recognition_method"], "rapidocr-profile-feed-metric-anchored")
        self.assertEqual(feed["articles"][0]["screen_point"], (500, 332))
        client.inspect_profile_feed.assert_not_called()

    def test_profile_feed_without_metric_anchor_opens_article_first(self) -> None:
        """本地已有日期和文章候选时，不应因缺少阅读/赞而提前失败。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(
                rpa.PROFILE_OCR,
                "inspect_profile_feed",
                return_value={
                    "time_labels": [{"text": "今天", "center_y_1000": 300}],
                    "articles": [{"title": "封面误识别", "center_x_1000": 500, "center_y_1000": 500}],
                    "recognition_method": "rapidocr-profile-feed",
                },
            ),
        ):
            feed = rpa.analyze_profile_window(window, Path(directory), client=client)

        client.inspect_profile_feed.assert_not_called()
        self.assertEqual(feed["recognition_method"], "rapidocr-profile-feed")
        self.assertIsNone(feed["articles"][0].get("list_read_count"))
        self.assertIsNone(feed["articles"][0].get("list_like_count"))

    def test_profile_feed_with_only_older_labels_is_normal_empty_result(self) -> None:
        """当天监听遇到历史日期且没有卡片时应正常结束，不能调用 Qwen。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))
        local_feed = {
            "time_labels": [
                {"text": "星期四", "center_y_1000": 300},
                {"text": "8月17日", "center_y_1000": 700},
            ],
            "articles": [],
            "recognition_method": "rapidocr-profile-feed",
        }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value=local_feed),
        ):
            feed = rpa.analyze_profile_window(
                window,
                Path(directory),
                client=client,
                scan_range="today",
            )

        self.assertEqual(feed["articles"], [])
        self.assertEqual(feed["empty_feed_reason"], "outside_scan_range_boundary")
        client.inspect_profile_feed.assert_not_called()

    def test_profile_feed_no_more_messages_is_normal_empty_result(self) -> None:
        """没有发布文章的服务号显示无更多消息时，不应触发 Qwen 或记为失败。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))
        local_feed = {
            "time_labels": [],
            "articles": [],
            "empty_feed_reason": "no_more_messages",
            "empty_feed_marker": "已无更多消息",
            "recognition_method": "rapidocr-profile-feed-empty-message",
        }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value=local_feed),
        ):
            feed = rpa.analyze_profile_window(
                window,
                Path(directory),
                client=client,
                allow_vl=False,
            )

        self.assertEqual(feed["empty_feed_reason"], "no_more_messages")
        self.assertEqual(feed["articles"], [])
        client.inspect_profile_feed.assert_not_called()

    def test_profile_feed_with_articles_but_no_time_labels_stays_local(self) -> None:
        """滚动重叠区没有本屏日期标签时，文章按未分组处理并继续翻页，不调用 Qwen。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))
        local_feed = {
            "time_labels": [],
            "articles": [
                {
                    "title": "未分组测试文章",
                    "center_x_1000": 500,
                    "center_y_1000": 420,
                }
            ],
            "recognition_method": "rapidocr-profile-feed",
        }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value=local_feed),
            patch.object(rpa, "log_event") as log_event,
        ):
            feed = rpa.analyze_profile_window(
                window,
                Path(directory),
                client=client,
                allow_vl=True,
            )

        self.assertEqual(feed["articles"][0]["title"], "未分组测试文章")
        self.assertEqual(feed["recognition_method"], "rapidocr-profile-feed")
        client.inspect_profile_feed.assert_not_called()
        self.assertTrue(
            any(
                call.args and call.args[0] == "profile_feed_local_ungrouped"
                for call in log_event.call_args_list
            )
        )

    def test_empty_service_profile_skips_feed_ocr_entirely(self) -> None:
        """点击后已按无原创服务号确认的资料页，直接按零文章结束。"""
        client = Mock()
        window = rpa.WindowInfo(
            1,
            "公众号",
            "test",
            rpa.Rect(100, 80, 900, 680),
            process_name="wechatappex.exe",
            page_kind="embedded_profile_tab",
            empty_article_profile=True,
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window"),
        ):
            feed = rpa.analyze_profile_window(
                window,
                Path(directory),
                client=client,
                allow_vl=False,
            )

        self.assertEqual(feed["empty_feed_reason"], "no_more_messages")
        self.assertEqual(feed["articles"], [])
        client.inspect_profile_feed.assert_not_called()

    def test_profile_feed_calls_qwen_once_after_two_local_failures(self) -> None:
        """本地连续两次缺少分组或卡片后，才调用一次 Qwen-VL 复核。"""
        client = Mock()
        client.inspect_profile_feed.return_value = {
            "time_labels": [{"text": "昨天 18:20", "center_y_1000": 180}],
            "articles": [{"title": "Qwen 复核文章", "center_x_1000": 510, "center_y_1000": 410}],
        }
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa, "time") as mocked_time,
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value={"time_labels": [], "articles": []}) as local_ocr,
        ):
            feed = rpa.analyze_profile_window(window, Path(directory), client=client)

        self.assertEqual(local_ocr.call_count, 2)
        client.inspect_profile_feed.assert_called_once_with(screenshot)
        self.assertEqual(feed["recognition_method"], "qwen-vl-profile-feed-fallback")
        self.assertIn("本地资料页识别结果不完整", feed["fallback_reason"])
        mocked_time.sleep.assert_called_once_with(0.5)

    def test_profile_feed_local_only_does_not_call_qwen(self) -> None:
        """--local-only 依然严格禁止 Qwen-VL，不能因新增兼容逻辑而穿透。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa, "time") as mocked_time,
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value={"time_labels": [], "articles": []}),
        ):
            with self.assertRaisesRegex(RuntimeError, "已禁用VL"):
                rpa.analyze_profile_window(
                    window, Path(directory), client=client, allow_vl=False
                )

        client.inspect_profile_feed.assert_not_called()
        mocked_time.sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
