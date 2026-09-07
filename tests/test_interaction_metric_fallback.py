"""互动指标局部降级的离线回归测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from wechat_rpa.runtime import collector_runtime as rpa
from wechat_rpa.vision import interaction as vision_interaction
from wechat_rpa.vision.interaction import InteractionOCR


class InteractionMetricFallbackTests(unittest.TestCase):
    def test_extract_maps_counts_below_icons(self) -> None:
        """微信底部数字在图标正下方时，仍按最近图标归属完整指标。"""
        screenshot = Image.new("RGB", (1200, 1000), "white")
        positions = {
            "like": (40, 30, 20, 20, 0.99),
            "share": (100, 30, 20, 20, 0.99),
            "favorite": (160, 30, 20, 20, 0.99),
            "comment": (220, 30, 20, 20, 0.99),
        }

        def fake_match(search, template_path):
            return positions[Path(template_path).stem]

        class FakeOCR:
            def __call__(self, image):
                boxes = [
                    ([[10, 60], [18, 60], [18, 70], [10, 70]], "108", 0.99),
                    ([[70, 60], [80, 60], [80, 70], [70, 70]], "481", 0.99),
                    ([[130, 60], [140, 60], [140, 70], [130, 70]], "22", 0.99),
                    ([[190, 60], [205, 60], [205, 70], [190, 70]], "写留言", 0.99),
                ]
                return boxes, None

        ocr = InteractionOCR()
        with (
            patch.object(vision_interaction, "_match_icon", side_effect=fake_match),
            patch.object(ocr, "ocr", FakeOCR()),
        ):
            metrics = ocr.extract(screenshot)

        self.assertEqual(metrics["like_count"], 108)
        self.assertEqual(metrics["share_count"], 481)
        self.assertEqual(metrics["favorite_count"], 22)
        self.assertEqual(metrics["comment_count"], 0)

    def test_all_metrics_keeps_verified_share_when_auxiliary_icons_fail(self) -> None:
        """收藏/评论模板失效时，不应丢弃已经独立确认的转发数。"""
        screenshot = Image.new("RGB", (1200, 800), "white")
        share_only = {
            "share_count": 106,
            "details": {"share": {"template_confidence": 0.99, "ocr_text": ["106"]}},
        }

        with (
            patch.object(rpa.INTERACTION_OCR, "extract", side_effect=ValueError("收藏图标不稳定")),
            patch.object(rpa.INTERACTION_OCR, "extract_share", return_value=share_only),
        ):
            metrics, source, reason = rpa.extract_local_interaction_metrics(screenshot, "all")

        self.assertEqual(source, "template-ocr-partial-share")
        self.assertEqual(metrics["share_count"], 106)
        self.assertIsNone(metrics["favorite_count"])
        self.assertIsNone(metrics["comment_count"])
        self.assertIn("收藏图标不稳定", reason or "")

    def test_all_metrics_still_fails_when_share_cannot_be_confirmed(self) -> None:
        """转发数本身不可确认时，仍必须失败，不能写入猜测数据。"""
        screenshot = Image.new("RGB", (1200, 800), "white")
        with (
            patch.object(rpa.INTERACTION_OCR, "extract", side_effect=ValueError("完整指标失败")),
            patch.object(rpa.INTERACTION_OCR, "extract_share", side_effect=ValueError("转发图标失败")),
        ):
            with self.assertRaisesRegex(ValueError, "完整指标失败"):
                rpa.extract_local_interaction_metrics(screenshot, "all")

    def test_all_metrics_keeps_vl_fallback_path_when_enabled(self) -> None:
        """允许 VL 时应继续抛出本地异常，让上层补齐全部互动指标。"""
        screenshot = Image.new("RGB", (1200, 800), "white")
        with patch.object(rpa.INTERACTION_OCR, "extract", side_effect=ValueError("完整指标失败")):
            with self.assertRaisesRegex(ValueError, "完整指标失败"):
                rpa.extract_local_interaction_metrics(
                    screenshot, "all", allow_partial=False
                )


if __name__ == "__main__":
    unittest.main()
