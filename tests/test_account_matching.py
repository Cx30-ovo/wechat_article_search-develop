"""公众号搜索结果识别的保守匹配回归测试。"""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from wechat_rpa.vision.profile import WeChatProfileOCR, _account_name_match
from wechat_rpa.runtime.collector_runtime import (
    classify_collection_error,
    resolve_search_account_name,
)


class AccountMatchingTests(unittest.TestCase):
    def test_ascii_case_difference_is_accepted(self) -> None:
        matched, method = _account_name_match("ComfyUI中文", "ComfyUi中文")
        self.assertTrue(matched)
        self.assertEqual(method, "ascii-case-ignored")

    def test_i_and_l_ocr_confusion_is_accepted_only_when_other_chars_match(self) -> None:
        matched, method = _account_name_match("海螺AI-MiniMaxHub", "海螺Al-MiniMaxHub")
        self.assertTrue(matched)
        self.assertEqual(method, "ocr-i-l-confusion")

    def test_known_bili_ocr_confusion_is_accepted(self) -> None:
        matched, method = _account_name_match("哔哩哔哩技术", "哗哩哗哩技术")
        self.assertTrue(matched)
        self.assertEqual(method, "ocr-chinese-confusion")

    def test_known_qwen_ocr_confusion_is_accepted(self) -> None:
        matched, method = _account_name_match("千问大模型", "干问大模型")
        self.assertTrue(matched)
        self.assertEqual(method, "ocr-chinese-confusion")

    def test_ai_prefix_i_omission_is_accepted(self) -> None:
        matched, method = _account_name_match("AI新工匠", "A新工匠")
        self.assertTrue(matched)
        self.assertEqual(method, "ocr-ai-prefix-i-omitted")

    def test_unrelated_similar_name_stays_rejected(self) -> None:
        matched, method = _account_name_match("ComfyUI中文", "ComfyUl中文站")
        self.assertFalse(matched)
        self.assertEqual(method, "")

    def test_alias_only_changes_the_search_name(self) -> None:
        # 开源包不携带用户私有的别名配置，测试时使用临时夹具验证解析逻辑。
        with tempfile.TemporaryDirectory() as tmp:
            alias_path = Path(tmp) / "account_aliases.json"
            alias_path.write_text(
                json.dumps({"通义千问Qwen": "千问大模型", "通义大模型": "千问大模型"}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "wechat_rpa.runtime.collector_runtime.ACCOUNT_ALIASES_PATH", alias_path
            ):
                self.assertEqual(resolve_search_account_name("通义千问Qwen"), "千问大模型")
                self.assertEqual(resolve_search_account_name("通义大模型"), "千问大模型")
                self.assertEqual(resolve_search_account_name("不存在的账号"), "不存在的账号")

    def test_verified_identity_suffix_is_accepted(self) -> None:
        matched, method = _account_name_match("书生Intern", "书生Intern事业单位")
        self.assertTrue(matched)
        self.assertEqual(method, "safe-suffix-alias")

    def test_short_name_with_verified_identity_suffix_is_accepted(self) -> None:
        matched, method = _account_name_match("政事儿", "政事儿媒体")
        self.assertTrue(matched)
        self.assertEqual(method, "safe-suffix-alias")
        matched, method = _account_name_match("鼓浪屿", "鼓浪屿政府")
        self.assertTrue(matched)
        self.assertEqual(method, "safe-suffix-alias")

    def test_empty_feed_message_confirms_no_article_service_profile(self) -> None:
        """资料页只有账号名称和“已无更多消息”时，应视为有效空账号。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)
        ocr._rows = lambda _image: [
            {
                "text": "厦门预警信息发布 事业单位",
                "normalized": "厦门预警信息发布事业单位",
                "center_x": 150.0,
                "center_y": 100.0,
                "confidence": 0.99,
            },
            {
                "text": "已无更多消息",
                "normalized": "已无更多消息",
                "center_x": 400.0,
                "center_y": 600.0,
                "confidence": 0.99,
            },
        ]

        result = ocr.validate_profile_header(
            Image.new("RGB", (800, 800), "white"),
            "厦门预警信息发布",
        )

        self.assertTrue(result["matched"])
        self.assertTrue(result["empty_feed_evidence"])
        self.assertEqual(result["empty_feed_marker"], "已无更多消息")

    def test_filter_and_account_not_found_errors_are_separated(self) -> None:
        self.assertEqual(
            classify_collection_error(
                RuntimeError("二级公众号筛选未确认选中：下划线未显示")
            ),
            "account_filter",
        )
        self.assertEqual(
            classify_collection_error(
                RuntimeError("公众号筛选已选中，但未找到可确认的同名公众号：AI新工匠")
            ),
            "account_not_found",
        )

    def test_personal_subject_with_original_count_stays_valid_after_public_filter(self) -> None:
        # 微信公众号二级筛选后，卡片的“个人”是主体类型，不代表它不是公众号。
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)
        ocr._rows = lambda _image: [
            {
                "text": "AI新工匠",
                "normalized": "AI新工匠",
                "left": 80.0,
                "right": 200.0,
                "bottom": 300.0,
                "center_x": 140.0,
                "center_y": 280.0,
                "confidence": 0.99,
            },
            {
                "text": "个人",
                "normalized": "个人",
                "left": 80.0,
                "right": 120.0,
                "bottom": 330.0,
                "center_x": 100.0,
                "center_y": 320.0,
                "confidence": 0.99,
            },
            {
                "text": "127篇原创内容",
                "normalized": "127篇原创内容",
                "left": 80.0,
                "right": 210.0,
                "bottom": 360.0,
                "center_x": 145.0,
                "center_y": 350.0,
                "confidence": 0.99,
            },
        ]
        result = ocr.locate_search_result(Image.new("RGB", (1000, 1000)), "AI新工匠")
        self.assertTrue(result["found"])
        self.assertIn("127篇原创内容", result["official_evidence"])

    def test_win11_all_page_account_section_is_detected(self) -> None:
        """新版 Win11 默认“全部”页应直接命中公众号账号区块。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, left: float, center_y: float, width: float = 120) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": left,
                "right": left + width,
                "top": center_y - 15,
                "bottom": center_y + 15,
                "center_x": left + width / 2,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("全部", 80, 150, 60),
            row("账号", 380, 150, 60),
            row("厦门日报-账号", 80, 390, 220),
            row("厦门日报", 230, 500, 140),
            row("公众号", 230, 550, 90),
            row("378篇原创内容 34分钟前更新", 230, 590, 260),
        ]

        result = ocr.locate_all_page_account_result(
            Image.new("RGB", (1568, 1439)), "厦门日报"
        )

        self.assertTrue(result["found"], result)
        self.assertEqual(result["layout"], "all-account-section")
        self.assertIn("公众号", result["official_evidence"])
        self.assertIn("378篇原创内容 34分钟前更新", result["original_content_evidence"])

    def test_search_result_without_public_or_service_type_is_rejected(self) -> None:
        """资料页可不显示类型，但搜索结果必须确认公众号或服务号。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, left: float, center_y: float, width: float = 180) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": left,
                "right": left + width,
                "top": center_y - 15,
                "bottom": center_y + 15,
                "center_x": left + width / 2,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("账号", 320, 150, 60),
            row("不限", 80, 220, 60),
            row("i厦门", 230, 360, 100),
            row("厦门市民数据服务股份有限公司", 230, 405, 300),
        ]

        result = ocr.locate_search_result(Image.new("RGB", (1000, 1000)), "i厦门")
        self.assertFalse(result["found"], result)

    def test_same_name_prefers_public_account_over_video_and_service(self) -> None:
        """同名视频号在最上方也不能被点击，公众号优先于服务号。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text, "normalized": "".join(text.split()), "left": 230.0,
                "right": 430.0, "top": center_y - 15, "bottom": center_y + 15,
                "center_x": 330.0, "center_y": center_y, "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("账号", 150),
            row("厦门晚报", 300), row("视频号", 340),
            row("厦门晚报", 500), row("服务号", 540),
            row("厦门晚报", 700), row("公众号", 740), row("127篇原创内容", 780),
        ]
        result = ocr.locate_search_result(Image.new("RGB", (1000, 1000)), "厦门晚报")
        self.assertTrue(result["found"], result)
        self.assertEqual(result["account_type"], "公众号")
        self.assertEqual(result["center_y_1000"], 700)

    def test_exact_video_does_not_hide_safe_suffix_public_account(self) -> None:
        """完全同名视频号不能让“名称+媒体”的公众号退出候选集合。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text, "normalized": "".join(text.split()), "left": 230.0,
                "right": 470.0, "top": center_y - 15, "bottom": center_y + 15,
                "center_x": 350.0, "center_y": center_y, "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("账号", 150),
            row("厦门晚报 媒体", 300), row("公众号", 340), row("1299篇原创内容", 380),
            row("厦门晚报", 600), row("视频号∞", 640),
        ]
        result = ocr.locate_search_result(Image.new("RGB", (1000, 1000)), "厦门晚报")
        self.assertTrue(result["found"], result)
        self.assertEqual(result["matched_name"], "厦门晚报 媒体")
        self.assertEqual(result["account_type"], "公众号")
        self.assertEqual(result["candidate_types"], ["公众号", "视频号"])

    def test_avatar_logo_name_does_not_truncate_service_card_type(self) -> None:
        """头像/Logo 里的同名 OCR 文字不得作为独立卡片，否则会截断真实卡片类型。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, left: float, center_y: float, width: float) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": left,
                "right": left + width,
                "top": center_y - 12,
                "bottom": center_y + 12,
                "center_x": left + width / 2,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("账号", 320, 150, 60),
            row("厦门地铁", 150, 320, 110),
            row("厦门地铁", 64, 363, 80),
            row("厦门地铁", 150, 478, 110),
            row("服务号", 150, 510, 80),
            row("厦门地铁", 64, 512, 80),
        ]

        result = ocr.locate_search_result(
            Image.new("RGB", (1114, 1020), "white"),
            "厦门地铁",
        )

        self.assertTrue(result["found"], result)
        self.assertEqual(result["account_type"], "服务号")
        self.assertEqual(result["matched_name"], "厦门地铁")
        self.assertEqual(result["center_y_1000"], 469)

    def test_same_name_mini_program_card_is_never_selected(self) -> None:
        """同名小程序卡不能借用下方公众号类型，公众号/服务号才允许被点击。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float, width: float = 120) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": 220.0,
                "right": 220.0 + width,
                "top": center_y - 14,
                "bottom": center_y + 14,
                "center_x": 280.0,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("账号", 150, 60),
            row("厦门市妇幼保健院事业单位", 320),
            row("服务号", 345, 70),
            row("厦门市妇幼保健院事业单位", 496),
            row("小程序", 523, 70),
            row("思明区妇幼保健院事业单位", 624),
            row("公众号", 651, 70),
        ]

        result = ocr.locate_search_result(
            Image.new("RGB", (1114, 1020), "white"),
            "厦门市妇幼保健院",
        )

        self.assertTrue(result["found"], result)
        self.assertEqual(result["account_type"], "服务号")
        self.assertEqual(result["candidate_types"], ["服务号", "小程序"])
        self.assertEqual(result["center_y_1000"], 314)

    def test_same_name_uses_service_only_when_public_account_absent(self) -> None:
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text, "normalized": "".join(text.split()), "left": 230.0,
                "right": 430.0, "top": center_y - 15, "bottom": center_y + 15,
                "center_x": 330.0, "center_y": center_y, "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("账号", 150),
            row("i厦门", 300), row("视频号", 340),
            row("i厦门", 500), row("服务号", 540),
        ]
        result = ocr.locate_search_result(Image.new("RGB", (1000, 1000)), "i厦门")
        self.assertTrue(result["found"], result)
        self.assertEqual(result["account_type"], "服务号")
        self.assertEqual(result["center_y_1000"], 500)

    def test_service_profile_does_not_require_service_type_or_description(self) -> None:
        """按 i厦门实际资料页，只使用名称、关注和内容导航确认身份。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": 100.0,
                "right": 320.0,
                "top": center_y - 15,
                "bottom": center_y + 15,
                "center_x": 210.0,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("i厦门", 120),
            row("厦门市民数据服务股份有限公司", 160),
            row("福建 厦门", 200),
            row("104篇原创内容", 280),
            row("关注", 360),
            row("全部", 430),
            row("贴图", 430),
            row("文章", 430),
            row("视频号", 430),
            row("昨天", 560),
        ]
        result = ocr.validate_profile_header(
            Image.new("RGB", (1000, 1000)), "i厦门"
        )
        self.assertTrue(result["matched"], result)
        self.assertNotIn("服务号", result["structural_terms"])

    def test_profile_identity_uses_viewport_name_and_structure(self) -> None:
        """名称不在顶部时，只要整页结构成立仍可确认资料页。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": 100.0,
                "right": 260.0,
                "top": center_y - 15,
                "bottom": center_y + 15,
                "center_x": 180.0,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("厦门日报", 330),
            row("全部", 450),
            row("文章", 450),
        ]
        result = ocr.validate_profile_header(
            Image.new("RGB", (1000, 1000)), "厦门日报"
        )
        self.assertTrue(result["matched"], result)
        self.assertIn("viewport", result["method"])

    def test_profile_header_accepts_follow_buttons_and_single_navigation_term(self) -> None:
        """个别公众号导航行被 OCR 误读成单个“文章”时，关注/私信仍可作结构证据。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": 200.0,
                "right": 260.0,
                "top": center_y - 12,
                "bottom": center_y + 12,
                "center_x": 230.0,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("南普陀寺", 100),
            row("关注", 270),
            row("私信", 270),
            row("文章", 315),
        ]

        result = ocr.validate_profile_header(
            Image.new("RGB", (1000, 1000), "white"),
            "南普陀寺",
        )

        self.assertTrue(result["matched"], result)
        self.assertTrue(result["profile_structure_found"])
        self.assertEqual(result["aligned_navigation_count"], 1)
        self.assertIn("关注", result["structural_terms"])
        self.assertIn("文章", result["structural_terms"])

    def test_search_result_name_alone_is_not_profile_identity(self) -> None:
        """搜索结果出现同名账号，但没有资料页结构时不得误判。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)
        ocr._rows = lambda _image: [
            {
                "text": "i厦门",
                "normalized": "i厦门",
                "left": 100.0,
                "right": 200.0,
                "top": 300.0,
                "bottom": 330.0,
                "center_x": 150.0,
                "center_y": 315.0,
                "confidence": 0.99,
            }
        ]
        result = ocr.validate_profile_header(
            Image.new("RGB", (1000, 1000)), "i厦门"
        )
        self.assertFalse(result["matched"], result)

    def test_merged_navigation_line_still_confirms_profile_structure(self) -> None:
        """RapidOCR 把导航行合并成“贴图文章·视频号”时，不能误判结构不足。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": 100.0,
                "right": 420.0,
                "top": center_y - 15,
                "bottom": center_y + 15,
                "center_x": 260.0,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("澎湃新闻", 180),
            row("关注", 320),
            row("全部", 430),
            row("贴图文章·视频号", 430),
        ]
        result = ocr.validate_profile_header(
            Image.new("RGB", (1000, 1000)), "澎湃新闻"
        )
        self.assertTrue(result["matched"], result)
        self.assertEqual(result["aligned_navigation_count"], 4)
        self.assertIn("文章", result["structural_terms"])

    def test_search_page_with_name_and_navigation_is_not_profile_identity(self) -> None:
        """输入框账号名和搜索导航同时出现时仍必须判定为搜一搜页面。"""
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": 100.0,
                "right": 260.0,
                "top": center_y - 15,
                "bottom": center_y + 15,
                "center_x": 180.0,
                "center_y": center_y,
                "confidence": 0.99,
            }

        ocr._rows = lambda _image: [
            row("i厦门", 120),
            row("搜索", 120),
            row("全部", 220),
            row("贴图", 220),
            row("文章", 220),
            row("视频号", 220),
        ]
        result = ocr.validate_profile_header(
            Image.new("RGB", (1000, 1000)), "i厦门"
        )
        self.assertFalse(result["matched"], result)
        self.assertIn("搜一搜页面", result["reason"])

    def test_profile_inventory_identifies_one_of_multiple_accounts_with_one_ocr(self) -> None:
        ocr = WeChatProfileOCR.__new__(WeChatProfileOCR)
        calls = 0

        def row(text: str, center_y: float) -> dict[str, object]:
            return {
                "text": text,
                "normalized": "".join(text.split()),
                "left": 100.0,
                "right": 300.0,
                "top": center_y - 15,
                "bottom": center_y + 15,
                "center_x": 200.0,
                "center_y": center_y,
                "confidence": 0.99,
            }

        def rows(_image: Image.Image) -> list[dict[str, object]]:
            nonlocal calls
            calls += 1
            return [
                row("厦门晚报", 120),
                row("全部", 300),
                row("贴图", 300),
                row("文章", 300),
                row("视频号", 300),
            ]

        ocr._rows = rows
        result = ocr.identify_profile_account(
            Image.new("RGB", (1000, 1000)),
            ["厦门日报", "厦门晚报", "海西晨报"],
        )

        self.assertTrue(result["matched"], result)
        self.assertEqual(result["account"], "厦门晚报")
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
