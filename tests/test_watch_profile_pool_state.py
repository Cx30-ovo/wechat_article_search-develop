from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_rpa.runtime import collector_runtime as rpa


class WatchProfilePoolStateTests(unittest.TestCase):
    def test_legacy_profile_is_removed_from_account_watch_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch-state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "account": "厦门日报",
                        "known_urls": ["https://mp.weixin.qq.com/s/example"],
                        "cycle_count": 3,
                        "profile": {"hwnd": 123, "status": "ready"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            state = rpa.load_watch_state(path, "厦门日报")

        self.assertNotIn("profile", state)
        self.assertEqual(state["cycle_count"], 3)
        self.assertEqual(state["known_urls"], ["https://mp.weixin.qq.com/s/example"])

    def test_profile_pool_state_is_independent_and_keeps_account_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile-pool-state.json"
            state = rpa.load_profile_pool_state(
                path,
                ["厦门日报", "厦门晚报", "厦门日报"],
                10,
            )

        self.assertEqual(state["accounts"], ["厦门日报", "厦门晚报"])
        self.assertEqual(state["profiles_unavailable"], ["厦门日报", "厦门晚报"])
        self.assertEqual(state["profile_registry"], {})

    def test_profile_pool_state_with_different_accounts_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile-pool-state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "accounts": ["旧账号"],
                        "profile_registry": {"旧账号": {"status": "ready", "hwnd": 123}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            state = rpa.load_profile_pool_state(path, ["厦门日报"], 10)

        self.assertEqual(state["accounts"], ["厦门日报"])
        self.assertEqual(state["profile_registry"], {})

    def test_profile_pool_cli_modes_are_mutually_exclusive(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "wechat-rpa",
                "--watch-account",
                "厦门日报",
                "--bootstrap-profile-pool",
                "--watch-existing-profile-pool",
            ],
        ):
            with self.assertRaises(SystemExit):
                rpa.parse_args()

    def test_existing_profile_pool_cli_mode_parses(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "wechat-rpa",
                "--watch-account",
                "厦门日报",
                "--watch-existing-profile-pool",
            ],
        ):
            args = rpa.parse_args()

        self.assertTrue(args.watch_existing_profile_pool)
        self.assertFalse(args.bootstrap_profile_pool)

    def test_watch_cli_defaults_use_30_minutes_and_five_cards(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["wechat-rpa", "--watch-account", "厦门日报"],
        ):
            args = rpa.parse_args()

        self.assertEqual(args.poll_interval, 1800.0)
        self.assertEqual(args.recent_card_limit, 5)
        self.assertFalse(args.watch_accounts_from_mongo)

    def test_watch_accounts_from_mongo_flag_parses(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "wechat-rpa",
                "--watch-accounts-from-mongo",
                "--accounts-per-vm",
                "120",
            ],
        ):
            args = rpa.parse_args()

        self.assertTrue(args.watch_accounts_from_mongo)
        self.assertEqual(args.accounts_per_vm, 120)

    def test_bootstrap_only_writes_pool_state_without_collecting_articles(self) -> None:
        window = rpa.WindowInfo(
            hwnd=123,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1600, 1440),
            process_name="wechatappex.exe",
            page_kind="embedded_profile_tab",
            empty_article_profile=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with (
                patch.object(
                    rpa,
                    "search_and_open_profile",
                    side_effect=[(window, "厦门日报"), (window, "厦门晚报")],
                ) as search_profile,
                patch.object(rpa, "collect_profile_account") as collect_account,
                patch.object(rpa, "log_event"),
            ):
                result = rpa.watch_multiple_accounts(
                    None,
                    ["厦门日报", "厦门晚报"],
                    output_dir,
                    300,
                    3,
                    None,
                    None,
                    allow_vl=False,
                    watch_cycles=0,
                    profile_pool_mode="bootstrap_only",
                )

            pool = json.loads(
                (output_dir / "profile-pool-state.json").read_text(encoding="utf-8")
            )
            scheduler_exists = (output_dir / "scheduler-state.json").exists()
            account_watch_exists = (output_dir / "厦门日报" / "watch-state.json").exists()

        self.assertEqual(result["mode"], "bootstrap_profile_pool")
        self.assertEqual(search_profile.call_count, 2)
        collect_account.assert_not_called()
        self.assertEqual(pool["profiles_ready"], ["厦门日报", "厦门晚报"])
        self.assertTrue(
            pool["profile_registry"]["厦门日报"]["empty_article_profile"]
        )
        self.assertTrue(
            pool["profile_registry"]["厦门晚报"]["empty_article_profile"]
        )
        self.assertFalse(scheduler_exists)
        self.assertFalse(account_watch_exists)

    def test_existing_only_never_searches_when_profile_is_missing(self) -> None:
        browser = rpa.WindowInfo(
            hwnd=123,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1600, 1440),
            process_name="wechatappex.exe",
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(rpa, "find_sogou_search_window", return_value=browser),
                patch.object(rpa, "arrange_automation_window", return_value=browser),
                patch.object(
                    rpa,
                    "inventory_existing_profile_pool",
                    return_value=(
                        {},
                        {
                            "profiles_found": [],
                            "profiles_missing": ["厦门日报"],
                            "completed_cycle": True,
                        },
                    ),
                ),
                patch.object(rpa, "search_and_open_profile") as search_profile,
                patch.object(rpa, "collect_profile_account") as collect_account,
                patch.object(rpa, "log_event"),
                patch.object(rpa.time, "sleep"),
            ):
                result = rpa.watch_multiple_accounts(
                    None,
                    ["厦门日报"],
                    Path(directory),
                    30,
                    1,
                    None,
                    None,
                    allow_vl=False,
                    watch_cycles=1,
                    profile_pool_mode="existing_only",
                )

        search_profile.assert_not_called()
        collect_account.assert_not_called()
        self.assertIn("热更新采集模式未附着", result["accounts"][0]["last_error"])

    def test_existing_only_reattaches_empty_service_by_name_only(self) -> None:
        browser = rpa.WindowInfo(
            hwnd=123,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1600, 1440),
            process_name="wechatappex.exe",
        )
        summary = {
            "detected_articles": 0,
            "stop_reason": "没有新文章",
            "collected": [],
            "failures": [],
            "dedupe": {"known_url_stop": False},
        }
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "profile-pool-state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "accounts": ["厦门预警信息发布"],
                        "profile_registry": {
                            "厦门预警信息发布": {
                                "status": "ready",
                                "hwnd": 123,
                                "empty_article_profile": True,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with (
                patch.object(rpa, "find_sogou_search_window", return_value=browser),
                patch.object(rpa, "arrange_automation_window", return_value=browser),
                patch.object(
                    rpa,
                    "inventory_existing_profile_pool",
                    return_value=(
                        {},
                        {
                            "profiles_found": [],
                            "profiles_missing": ["厦门预警信息发布"],
                            "completed_cycle": True,
                        },
                    ),
                ),
                patch.object(rpa, "activate_embedded_profile_tab", return_value=True),
                patch.object(rpa, "search_and_open_profile") as search_profile,
                patch.object(rpa, "collect_profile_account", return_value=summary),
                patch.object(rpa, "log_event"),
                patch.object(rpa.time, "sleep"),
            ):
                result = rpa.watch_multiple_accounts(
                    None,
                    ["厦门预警信息发布"],
                    output_dir,
                    30,
                    1,
                    None,
                    None,
                    allow_vl=False,
                    watch_cycles=1,
                    profile_pool_mode="existing_only",
                )

        search_profile.assert_not_called()
        self.assertIsNone(result["accounts"][0]["last_error"])

    def test_existing_only_registers_profile_without_full_tab_dedup(self) -> None:
        browser = rpa.WindowInfo(
            hwnd=123,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1600, 1440),
            process_name="wechatappex.exe",
            page_kind="embedded_profile_tab",
        )
        summary = {
            "detected_articles": 0,
            "stop_reason": "没有新文章",
            "collected": [],
            "failures": [],
            "dedupe": {"known_url_stop": False},
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(rpa._WATCH_PROFILE_CACHE, {}, clear=True),
                patch.object(rpa, "find_sogou_search_window", return_value=browser),
                patch.object(rpa, "arrange_automation_window", return_value=browser),
                patch.object(
                    rpa,
                    "inventory_existing_profile_pool",
                    return_value=(
                        {"厦门日报": browser},
                        {
                            "profiles_found": ["厦门日报"],
                            "profiles_missing": [],
                            "completed_cycle": True,
                        },
                    ),
                ) as inventory,
                patch.object(rpa, "deduplicate_account_profile_tabs") as deduplicate,
                patch.object(rpa, "search_and_open_profile") as search_profile,
                patch.object(rpa, "collect_profile_account", return_value=summary),
                patch.object(rpa, "log_event") as log_event,
                patch.object(rpa.time, "sleep"),
            ):
                result = rpa.watch_multiple_accounts(
                    None,
                    ["厦门日报"],
                    Path(directory),
                    30,
                    1,
                    None,
                    None,
                    allow_vl=False,
                    watch_cycles=1,
                    profile_pool_mode="existing_only",
                )

        deduplicate.assert_not_called()
        inventory.assert_called_once()
        search_profile.assert_not_called()
        self.assertEqual(result["accounts"][0]["last_error"], None)
        self.assertTrue(
            any(
                call.args
                and call.args[0] == "watch_profile_attach_registered_without_dedup"
                for call in log_event.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
