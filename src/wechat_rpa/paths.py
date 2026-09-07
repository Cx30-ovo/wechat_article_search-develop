"""统一管理源码重组后的项目路径，避免模块依赖自身文件位置。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "output"
ASSETS_DIR = PROJECT_ROOT / "assets"
STATIC_DIR = PROJECT_ROOT / "services" / "control_api" / "static"
WINDOWS_SCRIPTS_DIR = PROJECT_ROOT / "scripts" / "windows"
DOCKER_DIR = PROJECT_ROOT / "deploy" / "docker"
