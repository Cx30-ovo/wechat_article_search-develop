from __future__ import annotations

import unittest
from unittest.mock import Mock

from PIL import Image

from wechat_rpa.vision.profile import WeChatProfileOCR


class Win11MainLayoutTests(unittest.TestCase):
    def make_ocr(self, rows: list[dict]) -> WeChatProfileOCR:
        ocr = object.__new__(WeChatProfileOCR)
        ocr._rows = Mock(return_value=rows)
        return ocr

    def test_new_win11_main_search_placeholder_is_preferred(self) -> None:
        ocr = self.make_ocr(
            [
                {
                    "normalized": "搜索",
                    "center_x": 210.0,
                    "center_y": 100.0,
                    "confidence": 0.94,
                }
            ]
        )
        result = ocr.locate_wechat_main_search_box(Image.new("RGB", (1750, 1280)))
        self.assertTrue(result["found"])
        self.assertEqual(result["method"], "rapidocr-win11-main-search-bar")
        self.assertEqual(result["center_x_1000"], 120)
        self.assertEqual(result["center_y_1000"], 78)

    def test_new_win11_main_layout_has_coordinate_fallback(self) -> None:
        ocr = self.make_ocr([])
        result = ocr.locate_wechat_main_search_box(Image.new("RGB", (1750, 1280)))
        self.assertTrue(result["found"])
        self.assertEqual(result["method"], "win11-main-search-bar-layout-v1")
        self.assertEqual(result["center_x_1000"], 185)
        self.assertEqual(result["center_y_1000"], 80)

    def test_profile_structure_is_diagnostic_only(self) -> None:
        ocr = self.make_ocr(
            [
                {"normalized": "关注", "center_y": 160.0},
                {"normalized": "全部", "center_y": 240.0},
                {"normalized": "文章", "center_y": 240.0},
            ]
        )
        result = ocr.inspect_profile_layout(Image.new("RGB", (1100, 1324)))
        self.assertTrue(result["found"])
        self.assertEqual(result["terms"], ["全部", "文章", "关注"])

    def test_profile_structure_accepts_merged_navigation_line(self) -> None:
        ocr = self.make_ocr(
            [
                {"normalized": "关注", "center_y": 160.0},
                {"normalized": "全部", "center_y": 240.0},
                {"normalized": "贴图文章·视频号", "center_y": 240.0},
            ]
        )
        result = ocr.inspect_profile_layout(Image.new("RGB", (1100, 1324)))

        self.assertTrue(result["found"])
        self.assertEqual(result["terms"], ["全部", "贴图", "文章", "视频号", "关注"])


if __name__ == "__main__":
    unittest.main()
