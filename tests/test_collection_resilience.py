"""采集结果保留与标题匹配的离线回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_rpa.runtime import collector_runtime as rpa


class CollectionResilienceTests(unittest.TestCase):
    def test_alias_storage_page_uses_standard_account_name_without_mutating_source(self) -> None:
        """别名账号的文章页仍按标准库内名称入库，不能污染解析出的实际名称。"""
        page = {
            "account_name": "厦门市市政园林局",
            "title": "测试文章",
        }

        stored = rpa._prepare_storage_page(page, "厦门市政园林")

        self.assertEqual(stored["account_name"], "厦门市政园林")
        self.assertEqual(page["account_name"], "厦门市市政园林局")
        self.assertIsNot(stored, page)
        self.assertIs(rpa._prepare_storage_page(page, "厦门市市政园林局"), page)

    def test_article_validation_uses_canonical_account_name_after_ocr_suffix(self) -> None:
        """搜索结果的 OCR 后缀不能污染文章页和 MongoDB 的账号归属。"""
        profile_window = rpa.WindowInfo(
            hwnd=0,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 100, 100),
        )
        first_page = {
            "time_labels": [{"text": "今天", "center_y_1000": 100}],
            "articles": [
                {
                    "title": "一篇测试文章",
                    "center_y_1000": 200,
                    "center_x_1000": 500,
                    "screen_point": (10, 10),
                    "list_read_count": 10,
                    "list_like_count": 2,
                }
            ],
        }
        older_page = {
            "time_labels": [{"text": "昨天", "center_y_1000": 100}],
            "articles": [],
        }
        captured_expected_accounts: list[str] = []

        def fake_collect_open_article(*args, **kwargs):
            captured_expected_accounts.append(kwargs["expected_account"])
            return {
                "url": "https://mp.weixin.qq.com/s/test",
                "title": kwargs["expected_title"],
                "account_name": kwargs["expected_account"],
                "content": "正文",
                "status": "inserted",
            }

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    rpa,
                    "search_and_open_profile",
                    return_value=(profile_window, "腾讯技术工程 媒体"),
                ),
                patch.object(
                    rpa,
                    "analyze_profile_window",
                    # 资料页会继续翻页到扫描范围结束；后续页面统一模拟为空页。
                    side_effect=[first_page, *([older_page] * 12)],
                ),
                patch.object(rpa, "collect_open_article", side_effect=fake_collect_open_article),
                patch.object(rpa, "activate_window"),
                patch.object(rpa, "click"),
                patch.object(rpa, "close_article_after_attempt"),
                patch.object(rpa, "log_event"),
                patch.object(rpa.time, "sleep"),
            ):
                summary = rpa.collect_profile_account(
                    None,
                    "腾讯技术工程",
                    Path(directory),
                    max_articles=20,
                    export_jsonl=None,
                    export_csv=None,
                    write_mongo=False,
                    scan_range="today",
                )

        self.assertEqual(captured_expected_accounts, ["腾讯技术工程"])
        self.assertEqual(summary["collected"][0]["account_name"], "腾讯技术工程")

    def test_repeated_full_card_signature_is_skipped_without_clicking(self) -> None:
        """滚动重叠区重复出现已成功采集的完整卡片时，不再二次打开。"""
        profile_window = rpa.WindowInfo(
            hwnd=0,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 100, 100),
        )
        article = {
            "title": "一篇会重复出现在下一屏的长文章",
            "center_y_1000": 200,
            "center_x_1000": 500,
            "screen_point": (10, 10),
            "list_read_count": 10,
            "list_like_count": 2,
        }
        first_page = {
            "time_labels": [{"text": "今天", "center_y_1000": 100}],
            "articles": [dict(article)],
        }
        repeated_page = {
            "time_labels": [{"text": "今天", "center_y_1000": 100}],
            "articles": [dict(article)],
        }
        blank_page = {"time_labels": [], "articles": []}
        captured_titles: list[str] = []

        def fake_collect_open_article(*args, **kwargs):
            captured_titles.append(kwargs["expected_title"])
            return {
                "url": "https://mp.weixin.qq.com/s/test",
                "title": kwargs["expected_title"],
                "account_name": kwargs["expected_account"],
                "content": "正文",
                "status": "inserted",
            }

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    rpa,
                    "search_and_open_profile",
                    return_value=(profile_window, "腾讯技术工程"),
                ),
                patch.object(
                    rpa,
                    "analyze_profile_window",
                    side_effect=[first_page, repeated_page] + [blank_page] * 10,
                ),
                patch.object(rpa, "collect_open_article", side_effect=fake_collect_open_article),
                patch.object(rpa, "activate_window"),
                patch.object(rpa, "click"),
                patch.object(rpa, "capture_window", return_value=None),
                patch.object(rpa, "wait_for_visual_change", return_value=1.0),
                patch.object(rpa, "close_article_after_attempt"),
                patch.object(rpa, "log_event") as log_event,
                patch.object(rpa.time, "sleep"),
            ):
                summary = rpa.collect_profile_account(
                    None,
                    "腾讯技术工程",
                    Path(directory),
                    max_articles=2,
                    export_jsonl=None,
                    export_csv=None,
                    write_mongo=False,
                    scan_range="today",
                )

        self.assertEqual(captured_titles, ["一篇会重复出现在下一屏的长文章"])
        self.assertEqual(len(summary["collected"]), 1)
        self.assertTrue(
            any(
                call.args
                and call.args[0] == "article_card_full_signature_repeated_skipped_without_click"
                for call in log_event.call_args_list
            )
        )

    def test_known_card_title_signature_stops_before_opening_article(self) -> None:
        """跨轮已知卡片指纹命中时，不开文章直接结束当前公众号增量检查。"""
        profile_window = rpa.WindowInfo(
            hwnd=0,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 100, 100),
        )
        article = {
            "title": "一篇已采集过的文章",
            "center_y_1000": 200,
            "center_x_1000": 500,
            "screen_point": (10, 10),
        }
        page = {
            "time_labels": [{"text": "今天", "center_y_1000": 100}],
            "articles": [article],
        }
        older_page = {"time_labels": [{"text": "昨天", "center_y_1000": 100}], "articles": []}
        title_signature = rpa.build_card_title_signature("今天", article)
        assert title_signature is not None

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    rpa,
                    "search_and_open_profile",
                    return_value=(profile_window, "测试公众号"),
                ),
                patch.object(
                    rpa,
                    "analyze_profile_window",
                    side_effect=[page, *([older_page] * 12)],
                ),
                patch.object(rpa, "collect_open_article") as collect_article,
                patch.object(rpa, "activate_window"),
                patch.object(rpa, "click"),
                patch.object(rpa, "close_article_after_attempt"),
                patch.object(rpa, "log_event"),
                patch.object(rpa.time, "sleep"),
            ):
                summary = rpa.collect_profile_account(
                    None,
                    "测试公众号",
                    Path(directory),
                    max_articles=20,
                    export_jsonl=None,
                    export_csv=None,
                    write_mongo=False,
                    scan_range="today",
                    known_card_title_signatures={title_signature},
                    stop_after_known_card_signature=True,
                )

        collect_article.assert_not_called()
        self.assertTrue(summary["dedupe"]["known_card_stop"])
        self.assertEqual(
            summary["stop_reason"],
            "遇到已采集文章卡片指纹，增量批量本轮结束",
        )
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertTrue(summary["skipped"][0]["known_card_signature"])


    def test_known_url_stop_persists_card_title_signature(self) -> None:
        """打开文章后命中已知 URL 时，也要保存卡片指纹供下一轮点击前停止。"""
        profile_window = rpa.WindowInfo(
            hwnd=0,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 100, 100),
        )
        article = {
            "title": "一篇已知历史文章",
            "center_y_1000": 200,
            "center_x_1000": 500,
            "screen_point": (10, 10),
        }
        page = {
            "time_labels": [{"text": "今天", "center_y_1000": 100}],
            "articles": [article],
        }
        older_page = {"time_labels": [{"text": "昨天", "center_y_1000": 100}], "articles": []}
        title_signature = rpa.build_card_title_signature("今天", article)
        assert title_signature is not None
        known_url = "https://mp.weixin.qq.com/s/known_duplicate"

        def fake_collect_open_article(*args, **kwargs):
            return {
                "status": "skipped_duplicate_in_run",
                "url": known_url,
                "title": article["title"],
            }

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    rpa,
                    "search_and_open_profile",
                    return_value=(profile_window, "测试公众号"),
                ),
                patch.object(
                    rpa,
                    "analyze_profile_window",
                    side_effect=[page, *([older_page] * 12)],
                ),
                patch.object(
                    rpa,
                    "collect_open_article",
                    side_effect=fake_collect_open_article,
                ),
                patch.object(rpa, "activate_window"),
                patch.object(rpa, "click"),
                patch.object(rpa, "capture_window", return_value=None),
                patch.object(rpa, "wait_for_visual_change", return_value=1.0),
                patch.object(rpa, "close_article_after_attempt"),
                patch.object(rpa, "log_event"),
                patch.object(rpa, "remember_account_card_title_signature") as remember,
                patch.object(rpa.time, "sleep"),
            ):
                summary = rpa.collect_profile_account(
                    None,
                    "测试公众号",
                    Path(directory),
                    max_articles=20,
                    export_jsonl=None,
                    export_csv=None,
                    write_mongo=True,
                    scan_range="today",
                    known_urls={rpa.normalize_article_url(known_url)},
                    stop_after_known_url=True,
                    stop_after_known_card_signature=True,
                )

        self.assertTrue(summary["dedupe"]["known_url_stop"])
        remember.assert_called_once()
        self.assertEqual(remember.call_args.args[2], "测试公众号")
        self.assertEqual(
            rpa.normalize_article_url(remember.call_args.args[3]),
            rpa.normalize_article_url(known_url),
        )
        self.assertEqual(remember.call_args.args[4], title_signature[0])
        self.assertEqual(remember.call_args.args[5], title_signature[1])


    def test_arrange_preserves_empty_article_profile_marker(self) -> None:
        """空服务号标记不能因窗口重新排列而丢失。"""
        window = rpa.WindowInfo(
            hwnd=1,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 100, 100),
            process_name="wechatappex.exe",
            page_kind="embedded_profile_tab",
            empty_article_profile=True,
        )

        with (
            patch.object(rpa.user32, "ShowWindow"),
            patch.object(rpa.user32, "MoveWindow", return_value=False),
            patch.object(rpa, "log_event"),
        ):
            arranged = rpa.arrange_automation_window(window, "profile")

        self.assertTrue(arranged.empty_article_profile)

    def test_truncated_card_title_allows_small_ocr_errors(self) -> None:
        """卡片省略标题允许 AI/Al、千/干等少量 OCR 误差。"""
        card_title = (
            "Physical Al正进入经验工程时代，Ropedia聚焦全链路数据基建，完成数干万美..."
        )
        article_title = (
            "Physical AI正进入经验工程时代，Ropedia聚焦全链路数据基建，完成数千万美元融资"
        )
        self.assertTrue(rpa.titles_match(card_title, article_title))

    def test_truncated_card_title_rejects_unrelated_article(self) -> None:
        self.assertFalse(
            rpa.titles_match(
                "Physical AI正进入经验工程时代，聚焦全链路数据基建...",
                "腾讯发布全新游戏模型，内容生产效率显著提升",
            )
        )

    def test_last_line_of_multiline_card_title_matches_article_suffix(self) -> None:
        self.assertTrue(
            rpa.titles_match(
                "热门插件被锤了",
                "一个Skill让DeepSeek V4 Pro超越Fable 5？热门插件被锤了",
            )
        )

    def test_partial_account_results_survive_later_fatal_error(self) -> None:
        """账号后续失败时，已成功写出的文章必须保留在批次摘要中。"""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            checkpoint = {
                "account": "量子位",
                "partial": True,
                "collected": [
                    {"title": "已成功文章一", "status": "inserted"},
                    {"title": "已成功文章二", "status": "updated"},
                ],
                "failures": [],
            }
            (output_dir / "partial-summary.json").write_text(
                json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
            )

            with patch.object(rpa, "log_event"):
                summary = rpa.recover_partial_account_summary(
                    output_dir,
                    "量子位",
                    "文章标签清理失败",
                    "window",
                )

            self.assertEqual(len(summary["collected"]), 2)
            self.assertEqual(summary["fatal_category"], "window")
            self.assertTrue(summary["partial"])
            persisted = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(persisted["collected"]), 2)


if __name__ == "__main__":
    unittest.main()
