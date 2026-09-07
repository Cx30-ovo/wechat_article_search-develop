from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from wechat_rpa.runtime import collector_runtime as rpa
from wechat_rpa.vision.profile import WeChatProfileOCR


class SearchSubmitVisualStateTests(unittest.TestCase):
    def _search_page(self, *, result_layout: bool, focused: bool) -> Image.Image:
        image = Image.new("RGB", (1000, 800), "white")
        draw = ImageDraw.Draw(image)
        center_y = 110 if result_layout else 390
        button = (650, center_y - 30, 780, center_y + 30)
        draw.rectangle(button, fill=(0, 200, 90))
        if focused:
            draw.rectangle((40, center_y - 40, 790, center_y + 40), outline=(0, 200, 90), width=3)
        return image

    def test_result_layout_with_green_input_outline_is_focused(self) -> None:
        result = WeChatProfileOCR().inspect_search_submit_state(
            self._search_page(result_layout=True, focused=True)
        )
        self.assertTrue(result["result_layout_ready"])
        self.assertTrue(result["input_focused"])

    def test_result_layout_without_green_input_outline_is_stable(self) -> None:
        result = WeChatProfileOCR().inspect_search_submit_state(
            self._search_page(result_layout=True, focused=False)
        )
        self.assertTrue(result["result_layout_ready"])
        self.assertFalse(result["input_focused"])

    def test_search_home_is_not_mistaken_for_result_layout(self) -> None:
        result = WeChatProfileOCR().inspect_search_submit_state(
            self._search_page(result_layout=False, focused=True)
        )
        self.assertFalse(result["result_layout_ready"])

    def test_settle_blurs_again_when_escape_does_not_release_focus(self) -> None:
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        images = [Image.new("RGB", (1000, 800), "white") for _ in range(4)]
        states = [
            {"result_layout_ready": True, "input_focused": True},
            {"result_layout_ready": True, "input_focused": True},
            {"result_layout_ready": True, "input_focused": True},
            {"result_layout_ready": True, "input_focused": False},
            {"result_layout_ready": True, "input_focused": False},
        ]
        with tempfile.TemporaryDirectory() as temporary_dir:
            with (
                patch.object(rpa, "activate_window"),
                patch.object(rpa, "capture_window", side_effect=images),
                patch.object(
                    rpa.PROFILE_OCR,
                    "inspect_search_submit_state",
                    side_effect=states,
                ),
                patch.object(rpa, "press_escape") as press_escape,
                patch.object(rpa, "click") as click,
                patch.object(rpa.time, "sleep"),
                patch.object(rpa, "log_event") as log_event,
            ):
                result = rpa.settle_search_result_after_submit(
                    window,
                    Path(temporary_dir),
                    "厦门日报",
                    1,
                )

        self.assertIs(result, images[-1])
        press_escape.assert_called_once_with()
        click.assert_called_once_with(920, 160)
        self.assertTrue(
            any(
                call.args[0] == "search_suggestion_dismiss_confirmed"
                for call in log_event.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
