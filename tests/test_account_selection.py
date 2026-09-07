"""手动采集账号起始位置与数量限制的离线回归测试。"""

from __future__ import annotations

import unittest

from wechat_rpa.runtime import collector_runtime as rpa


class AccountSelectionTests(unittest.TestCase):
    ACCOUNTS = ["厦门日报", "厦门晚报", "海西晨报", "海峡导报", "厦门网"]

    def test_empty_list_returns_empty(self) -> None:
        self.assertEqual(rpa.slice_account_names([], "厦门日报", 3), [])

    def test_default_returns_all_accounts(self) -> None:
        self.assertEqual(rpa.slice_account_names(self.ACCOUNTS), self.ACCOUNTS)

    def test_start_account_includes_itself_and_keeps_order(self) -> None:
        self.assertEqual(
            rpa.slice_account_names(self.ACCOUNTS, "海西晨报"),
            ["海西晨报", "海峡导报", "厦门网"],
        )

    def test_max_accounts_limits_from_start(self) -> None:
        self.assertEqual(
            rpa.slice_account_names(self.ACCOUNTS, "厦门晚报", 2),
            ["厦门晚报", "海西晨报"],
        )

    def test_max_accounts_zero_means_unlimited(self) -> None:
        self.assertEqual(rpa.slice_account_names(self.ACCOUNTS, "", 0), self.ACCOUNTS)

    def test_unknown_start_account_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rpa.slice_account_names(self.ACCOUNTS, "不存在的公众号")

    def test_negative_max_accounts_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rpa.slice_account_names(self.ACCOUNTS, "", -1)

if __name__ == "__main__":
    unittest.main()
