"""以「上次签到成功时间」为基准的调度：间隔必须真的可配。

为什么值得专门测：
    以前这里硬编码 +86400（24 小时），界面上却显示
    "上次成功后 XX 分钟"（取的是重试间隔），显示与实际不符，
    用户也没法真的调间隔。现在间隔是 success_interval_minutes。
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import SiteConfig  # noqa: E402
from app.schedule import (MODE_DAILY, MODE_SUCCESS_BASED, SiteSchedule,
                          next_run_at, next_success_based_time)  # noqa: E402

NOW = 1_700_000_000


def no_jitter():
    """固定返回中间值的 rng：让 apply_jitter 不引入偏移。"""
    r = random.Random(1)
    return r


class TestSuccessBasedInterval(unittest.TestCase):
    def _sched(self, minutes, last_success=None):
        s = SiteSchedule(site_id='x', mode=MODE_SUCCESS_BASED,
                         success_interval_minutes=minutes,
                         jitter_enabled=False)
        s.last_success_at = last_success
        return s

    def test_never_succeeded_waits_one_interval(self):
        """从未成功过：先等一个间隔，再去尝试第一次签到。

        这回答了用户的问题："需不需要一个初始签到时间？"
        答案是不需要 —— 启用后先等一个间隔，第一次尝试成功后
        基准就自动换成真实的成功时间。
        """
        s = self._sched(1440)
        got = next_success_based_time(NOW, s, no_jitter())
        self.assertAlmostEqual(got - NOW, 1440 * 60, delta=120)

    def test_based_on_last_success_not_now(self):
        """基准是"上次成功时间"，不是"现在"。"""
        last = NOW - 3600                      # 1 小时前成功
        s = self._sched(1440, last_success=last)
        got = next_success_based_time(NOW, s, no_jitter())
        self.assertAlmostEqual(got - NOW, 1440 * 60 - 3600, delta=120)

    def test_interval_is_configurable(self):
        """间隔真的能改（以前硬编码 24 小时，改了没用）。

        注意 60 分钟那一档：上次成功在 1 小时前，base 正好等于 now，
        守卫会再顺延一个间隔到未来（见下一个测试），所以期望 1 小时。
        """
        last = NOW - 3600
        for minutes, expect in ((720, 11), (180, 2), (60, 1)):
            with self.subTest(minutes=minutes):
                s = self._sched(minutes, last_success=last)
                got = next_success_based_time(NOW, s, no_jitter())
                hours = (got - NOW) / 3600.0
                self.assertAlmostEqual(hours, expect, delta=0.1)

    def test_short_interval_does_not_fire_immediately(self):
        """间隔已过（上次成功在很久以前）也不能立即触发，要顺延到未来。"""
        s = self._sched(60, last_success=NOW - 10 * 3600)
        got = next_success_based_time(NOW, s, no_jitter())
        self.assertGreater(got, NOW, '必须排到未来，否则会立刻重复触发')

    def test_minimum_interval_enforced(self):
        """间隔下限 1 小时，避免配置成"几乎一直在签到"。"""
        s = self._sched(0, last_success=NOW - 3600)
        got = next_success_based_time(NOW, s, no_jitter())
        self.assertGreaterEqual(got - NOW, 3600 - 120)

    def test_daily_mode_unaffected(self):
        """每天固定时刻模式不受这个字段影响。"""
        s = SiteSchedule(site_id='x', mode=MODE_DAILY, daily_hour=3,
                         daily_minute=0, jitter_enabled=False,
                         success_interval_minutes=60)
        got = next_run_at(NOW, s, no_jitter())
        self.assertGreater(got, NOW)


class TestSiteConfigCarriesInterval(unittest.TestCase):
    def test_default_is_24h(self):
        self.assertEqual(SiteConfig(id='x', name='X').success_interval_minutes,
                         1440)

    def test_round_trip(self):
        s = SiteConfig(id='x', name='X', mode=MODE_SUCCESS_BASED,
                       success_interval_minutes=720)
        back = SiteConfig.from_dict(s.to_dict())
        self.assertEqual(back.success_interval_minutes, 720)

    def test_old_config_gets_default(self):
        back = SiteConfig.from_dict({'id': 'x', 'name': 'X'})
        self.assertEqual(back.success_interval_minutes, 1440)

    def test_too_small_value_clamped(self):
        back = SiteConfig.from_dict({'id': 'x', 'name': 'X',
                                     'success_interval_minutes': 5})
        self.assertGreaterEqual(back.success_interval_minutes, 60)

    def test_reaches_schedule(self):
        s = SiteConfig(id='x', name='X', mode=MODE_SUCCESS_BASED,
                       success_interval_minutes=720)
        self.assertEqual(s.to_schedule().success_interval_minutes, 720)


if __name__ == '__main__':
    unittest.main(verbosity=2)
