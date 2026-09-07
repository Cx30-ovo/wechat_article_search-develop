"""Qwen-VL 最小闭环自检：截图 -> 视觉模型 -> JSON。

默认抓取当前 Windows 桌面并复用项目实际使用的 ``detect_manager_layout``
接口。脚本只读屏幕，不点击微信、不写入 MongoDB，适合首次配置模型后验证
网络、API Key、模型名和图片输入链路。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageGrab

from wechat_rpa.paths import PROJECT_ROOT
from wechat_rpa.settings import load_project_env
from wechat_rpa.vision.qwen_client import QwenVisionClient, QwenVisionConfig


PROJECT_DIR = PROJECT_ROOT
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output" / "vision-smoke"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        help="已有截图路径；不传时抓取当前 Windows 桌面",
    )
    parser.add_argument(
        "--task",
        choices=("manager-layout", "generic"),
        default="manager-layout",
        help="测试任务；默认调用项目真实的公众号管理器布局识别",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="截图和 JSON 输出目录",
    )
    return parser.parse_args()


def capture_image(image_path: str | None) -> tuple[Image.Image, str]:
    if image_path:
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"截图不存在：{path}")
        with Image.open(path) as source:
            return source.convert("RGB"), str(path)

    # ImageGrab 在 Windows 交互式桌面中抓取当前屏幕；不传 bbox 可覆盖多屏。
    return ImageGrab.grab(all_screens=True).convert("RGB"), "current-screen"


def run_smoke_test(args: argparse.Namespace) -> dict[str, object]:
    load_project_env()
    image, source = capture_image(args.image)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = output_dir / "screenshot.png"
    result_path = output_dir / "result.json"
    image.save(screenshot_path)

    config = QwenVisionConfig.from_env()
    client = QwenVisionClient(config)
    if args.task == "manager-layout":
        result = client.detect_manager_layout(image)
    else:
        result = client.analyze(
            image,
            "请检查这张桌面截图，只返回合法 JSON："
            '{"is_wechat_visible":boolean,"main_text":string|null,"confidence":number}。'
            "只有明确看到微信窗口时 is_wechat_visible 才能为 true；看不清的字段使用 null，绝不猜测。",
            max_tokens=300,
        )

    payload: dict[str, object] = {
        "status": "ok",
        "task": args.task,
        "source": source,
        "screenshot": str(screenshot_path),
        "base_url": config.base_url,
        "model": config.model,
        "result": result,
    }
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def main() -> int:
    args = parse_args()
    try:
        payload = run_smoke_test(args)
    except Exception as exc:
        print(f"视觉模型自检失败：{exc}", file=sys.stderr)
        print(
            "请检查 QWEN_VL_BASE_URL、QWEN_VL_API_KEY、QWEN_VL_MODEL，"
            "以及 Windows 虚拟机是否能访问模型服务。",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("视觉模型最小闭环已完成：截图 -> Qwen-VL -> JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
