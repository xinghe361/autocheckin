"""调度引擎单测（只用标准库 unittest，不引入额外依赖）。

覆盖：
  * ±1 小时随机偏移的边界
  * 每天固定时刻模式（含"今天已过顺延明天"）
  * 以上次成功为基准模式（今天 03:21 → 明天 03:21±随机）
  * 失败重试的次数与间隔
  * 重试用尽后回到正常节奏
  * 连续失败触发 AI 的阈值，且同一档只触发一次
"""

import os
import random
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schedule import (  # noqa: E402
    JITTER_MAX_SECONDS, MODE_DAILY, MODE_SUCCESS_BASED, SiteSchedule,
    clamp_jitter, mark_ai_analyzed, next_daily_time, next_run_at,
    next_retry_at, next_success_based_time, plan_next, should_ask_ai,
)


def ts(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s).timestamp())


class TestJitter(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp_jitter(7200), JITTER_MAX_SECONDS)
        self.assertEqual(clamp_jitter(-5), 0)
        self.assertEqual(clamp_jitter('abc'), JITTER_MAX_SECONDS)
        self.assertEqual(clamp_jitter(600), 600)

    def test_jitter_within_one_hour(self):
        """偏移必须严格落在 ±1 小时内。"""
        site = SiteSchedule(site_id='s', mode=MODE_DAILY, daily_hour=3,
                            jitter_enabled=True, jitter_seconds=JITTER_MAX_SECONDS)
        base = ts(2026, 3, 10, 3, 0)
        rng = random.Random(1234)
        for _ in range(500):
            got = next_daily_time(base - 60, site, rng)
            cand = got if got <= base + JITTER_MAX_SECONDS else got - 86400
            self.assertLessEqual(abs(cand - base), JITTER_MAX_SECONDS,
                                 '偏移超出 ±1 小时: %d' % (cand - base))

    def test_jitter_disabled_is_exact(self):
        site = SiteSchedule(site_id='s', daily_hour=3, daily_minute=0,
                            jitter_enabled=False)
        now = ts(2026, 3, 10, 1, 0)
        self.assertEqual(next_daily_time(now, site, random.Random(1)),
                         ts(2026, 3, 10, 3, 0))


class TestDailyMode(unittest.TestCase):
    def test_same_day_when_not_passed(self):
        site = SiteSchedule(site_id='s', mode=MODE_DAILY, daily_hour=3, daily_minute=0)
        now = ts(2026, 3, 10, 1, 0)          # 凌晨 1 点，今天 3 点还没到
        self.assertEqual(next_daily_time(now, site, random.Random(7)),
                         ts(2026, 3, 10, 3, 0))

    def test_rolls_to_tomorrow_when_passed(self):
        site = SiteSchedule(site_id='s', mode=MODE_DAILY, daily_hour=3, daily_minute=0)
        now = ts(2026, 3, 10, 5, 0)          # 今天 3 点已过
        self.assertEqual(next_daily_time(now, site, random.Random(7)),
                         ts(2026, 3, 11, 3, 0))

    def test_exact_boundary_rolls_forward(self):
        """正好等于目标时刻时，应排到明天（否则会立刻重复触发）。"""
        site = SiteSchedule(site_id='s', mode=MODE_DAILY, daily_hour=3, daily_minute=0)
        now = ts(2026, 3, 10, 3, 0)
        self.assertEqual(next_daily_time(now, site, random.Random(7)),
                         ts(2026, 3, 11, 3, 0))

    def test_always_in_future_with_jitter(self):
        """带偏移时也必须严格大于 now（否则会死循环触发）。"""
        site = SiteSchedule(site_id='s', daily_hour=3, daily_minute=0,
                            jitter_enabled=True)
        rng = random.Random(99)
        for hour in range(0, 24, 3):
            for minute in (0, 30, 59):
                now = ts(2026, 6, 15, hour, minute)
                self.assertGreater(next_daily_time(now, site, rng), now)

    def test_custom_minute(self):
        site = SiteSchedule(site_id='s', daily_hour=3, daily_minute=50)
        now = ts(2026, 3, 10, 1, 0)
        self.assertEqual(next_daily_time(now, site, random.Random(3)),
                         ts(2026, 3, 10, 3, 50))


class TestSuccessBasedMode(unittest.TestCase):
    def test_next_is_24h_after_last_success(self):
        """需求原例：今天 03:21 成功 → 下一次是明天 03:21。"""
        site = SiteSchedule(site_id='s', mode=MODE_SUCCESS_BASED,
                            jitter_enabled=False)
        site.last_success_at = ts(2026, 3, 10, 3, 21)
        now = ts(2026, 3, 10, 3, 22)
        self.assertEqual(next_success_based_time(now, site, random.Random(1)),
                         ts(2026, 3, 11, 3, 21))

    def test_chain_shifts_forward_each_day(self):
        """需求原例：03:21 → 明天 03:50 → 后天 03:50（含偏移时逐日顺延）。"""
        site = SiteSchedule(site_id='s', mode=MODE_SUCCESS_BASED,
                            jitter_enabled=False)
        site.last_success_at = ts(2026, 3, 10, 3, 21)
        n1 = next_success_based_time(ts(2026, 3, 10, 3, 22), site, random.Random(1))
        self.assertEqual(n1, ts(2026, 3, 11, 3, 21))
        # 假设第二天实际签到成功时刻是 03:50
        site.last_success_at = ts(2026, 3, 11, 3, 50)
        n2 = next_success_based_time(ts(2026, 3, 11, 3, 51), site, random.Random(1))
        self.assertEqual(n2, ts(2026, 3, 12, 3, 50))

    def test_never_in_past_even_with_jitter(self):
        """偏移可能把结果拉到过去，必须继续顺延。"""
        site = SiteSchedule(site_id='s', mode=MODE_SUCCESS_BASED,
                            jitter_enabled=True, jitter_seconds=JITTER_MAX_SECONDS)
        rng = random.Random(2026)
        for gap_hours in range(0, 30):
            site.last_success_at = ts(2026, 5, 1, 12, 0)
            now = site.last_success_at + gap_hours * 3600
            got = next_success_based_time(now, site, rng)
            self.assertGreater(got, now,
                               'gap=%dh 时排到了过去: %d' % (gap_hours, got))

    def test_first_time_without_history(self):
        """从未成功过时，从 now 起 24 小时。"""
        site = SiteSchedule(site_id='s', mode=MODE_SUCCESS_BASED)
        now = ts(2026, 3, 10, 8, 0)
        self.assertEqual(next_success_based_time(now, site, random.Random(1)),
                         ts(2026, 3, 11, 8, 0))

    def test_jitter_within_one_hour(self):
        site = SiteSchedule(site_id='s', mode=MODE_SUCCESS_BASED,
                            jitter_enabled=True)
        site.last_success_at = ts(2026, 3, 10, 3, 0)
        now = ts(2026, 3, 10, 4, 0)
        rng = random.Random(5)
        for _ in range(200):
            got = next_success_based_time(now, site, rng)
            expect = ts(2026, 3, 11, 3, 0)
            self.assertLessEqual(abs(got - expect), JITTER_MAX_SECONDS)


class TestRetry(unittest.TestCase):
    def test_retry_schedule_and_exhaustion(self):
        site = SiteSchedule(site_id='s', retry_enabled=True, retry_count=3,
                            retry_interval_minutes=30)
        now = ts(2026, 3, 10, 3, 0)
        # retries_done 是"已经失败过的次数"，失败 1~3 次都还能排重试
        for done in (1, 2, 3):
            site.retries_done = done
            self.assertEqual(next_retry_at(now, site), now + 1800,
                             'retries_done=%d 时应该还能重试' % done)
        # 失败 4 次已超出 retry_count=3 → 不再重试
        site.retries_done = 4
        self.assertIsNone(next_retry_at(now, site))

    def test_retry_disabled(self):
        site = SiteSchedule(site_id='s', retry_enabled=False)
        self.assertIsNone(next_retry_at(ts(2026, 3, 10, 3, 0), site))

    def test_plan_next_on_failure_uses_retry(self):
        site = SiteSchedule(site_id='s', daily_hour=3, mode=MODE_DAILY,
                            retry_enabled=True, retry_count=2,
                            retry_interval_minutes=15)
        now = ts(2026, 3, 10, 3, 0)
        nxt = plan_next(site, now, succeeded=False, rng=random.Random(1))
        self.assertEqual(nxt, now + 900)
        self.assertEqual(site.consecutive_failures, 1)
        self.assertEqual(site.retries_done, 1)

    def test_plan_next_falls_back_after_retries_exhausted(self):
        """真实推进时间：重试额度用尽后，再失败就回到正常每日调度。

        retry_count=2 的含义是「总共重试 2 次」：
          第 1 次失败 → 排第 1 次重试
          第 2 次失败 → 排第 2 次重试
          第 3 次失败 → 额度用尽，回到每日调度
        """
        site = SiteSchedule(site_id='s', daily_hour=3, daily_minute=0,
                            mode=MODE_DAILY, retry_enabled=True, retry_count=2,
                            retry_interval_minutes=15)
        t0 = ts(2026, 3, 10, 3, 0)
        # 第 1 次失败 → 15 分钟后（第 1 次重试）
        n1 = plan_next(site, t0, succeeded=False, rng=random.Random(1))
        self.assertEqual(n1, t0 + 900)
        self.assertEqual(site.retries_done, 1)
        # 第 2 次失败（发生在第 1 次重试时）→ 再 15 分钟后（第 2 次重试）
        n2 = plan_next(site, n1, succeeded=False, rng=random.Random(1))
        self.assertEqual(n2, n1 + 900)
        self.assertEqual(site.retries_done, 2)
        # 第 3 次失败 → 额度用尽，回到每日调度（次日 03:00），重试计数清零
        n3 = plan_next(site, n2, succeeded=False, rng=random.Random(1))
        self.assertEqual(n3, ts(2026, 3, 11, 3, 0))
        self.assertEqual(site.retries_done, 0)
        self.assertEqual(site.consecutive_failures, 3)

    def test_retry_count_is_total_retries(self):
        """重试次数语义：retry_count=N 就恰好排 N 次重试。"""
        for n in (1, 3, 5):
            site = SiteSchedule(site_id='s', daily_hour=3, daily_minute=0,
                                mode=MODE_DAILY, retry_enabled=True,
                                retry_count=n, retry_interval_minutes=10)
            now = ts(2026, 3, 10, 3, 0)
            retries = 0
            for _ in range(n + 1):
                nxt = plan_next(site, now, succeeded=False, rng=random.Random(1))
                if nxt == now + 600:
                    retries += 1
                    now = nxt            # 推进到重试时刻
                else:
                    break
            self.assertEqual(retries, n,
                             'retry_count=%d 实际排了 %d 次重试' % (n, retries))

    def test_retry_count_zero_goes_straight_to_normal(self):
        """把重试次数设为 0，就等于失败后直接等下一次正常调度。"""
        site = SiteSchedule(site_id='s', daily_hour=3, daily_minute=0,
                            mode=MODE_DAILY, retry_enabled=True, retry_count=0)
        t0 = ts(2026, 3, 10, 3, 0)
        nxt = plan_next(site, t0, succeeded=False, rng=random.Random(1))
        self.assertEqual(nxt, ts(2026, 3, 11, 3, 0))
        self.assertEqual(site.consecutive_failures, 1)

    def test_success_resets_everything(self):
        site = SiteSchedule(site_id='s', mode=MODE_DAILY, daily_hour=3)
        now = ts(2026, 3, 10, 3, 0)
        plan_next(site, now, succeeded=False, rng=random.Random(1))
        plan_next(site, now, succeeded=False, rng=random.Random(1))
        self.assertEqual(site.consecutive_failures, 2)
        plan_next(site, now, succeeded=True, rng=random.Random(1))
        self.assertEqual(site.consecutive_failures, 0)
        self.assertEqual(site.retries_done, 0)
        self.assertEqual(site.last_success_at, now)


class TestAiTrigger(unittest.TestCase):
    def test_threshold(self):
        site = SiteSchedule(site_id='s', ai_enabled=True, ai_after_failures=3)
        site.consecutive_failures = 2
        self.assertFalse(should_ask_ai(site))
        site.consecutive_failures = 3
        self.assertTrue(should_ask_ai(site))

    def test_only_at_threshold_multiples(self):
        """连败 3、6、9… 才分析；4、5 不再重复分析（避免持续故障时天天烧 token）。"""
        site = SiteSchedule(site_id='s', ai_enabled=True, ai_after_failures=3)
        expect = {3: True, 4: False, 5: False, 6: True, 7: False, 8: False,
                  9: True, 10: False}
        for fails, want in expect.items():
            site.consecutive_failures = fails
            site.ai_analyzed_for_streak = 0
            self.assertEqual(should_ask_ai(site), want,
                             '连败 %d 次时 should_ask_ai 应为 %s' % (fails, want))

    def test_only_once_per_streak_level(self):
        site = SiteSchedule(site_id='s', ai_enabled=True, ai_after_failures=3)
        site.consecutive_failures = 3
        self.assertTrue(should_ask_ai(site))
        mark_ai_analyzed(site)
        self.assertFalse(should_ask_ai(site), '同一档不应该重复分析')

    def test_next_multiple_triggers_again(self):
        site = SiteSchedule(site_id='s', ai_enabled=True, ai_after_failures=3)
        site.consecutive_failures = 3
        mark_ai_analyzed(site)
        site.consecutive_failures = 6
        self.assertTrue(should_ask_ai(site), '到下一档应再分析一次')

    def test_threshold_one_means_every_failure(self):
        """阈值设成 1 时，每次失败都分析（用户明确选择了这个代价）。"""
        site = SiteSchedule(site_id='s', ai_enabled=True, ai_after_failures=1)
        for fails in (1, 2, 3):
            site.consecutive_failures = fails
            site.ai_analyzed_for_streak = 0
            self.assertTrue(should_ask_ai(site), '连败 %d 次应触发' % fails)

    def test_disabled(self):
        site = SiteSchedule(site_id='s', ai_enabled=False, ai_after_failures=1)
        site.consecutive_failures = 99
        self.assertFalse(should_ask_ai(site))

    def test_reset_after_success(self):
        site = SiteSchedule(site_id='s', ai_enabled=True, ai_after_failures=2)
        site.consecutive_failures = 2
        mark_ai_analyzed(site)
        plan_next(site, ts(2026, 3, 10, 3, 0), succeeded=True, rng=random.Random(1))
        self.assertEqual(site.ai_analyzed_for_streak, 0)
        self.assertFalse(should_ask_ai(site))


class TestNextRunDispatch(unittest.TestCase):
    def test_dispatch_by_mode(self):
        now = ts(2026, 3, 10, 1, 0)
        daily = SiteSchedule(site_id='a', mode=MODE_DAILY, daily_hour=3)
        self.assertEqual(next_run_at(now, daily, random.Random(1)),
                         ts(2026, 3, 10, 3, 0))
        # 上次成功在 5 小时前 → 基准 = 上次成功 + 24h = 明天 01:00（仍在下一次）
        sb = SiteSchedule(site_id='b', mode=MODE_SUCCESS_BASED)
        sb.last_success_at = ts(2026, 3, 9, 20, 0)
        self.assertEqual(next_run_at(now, sb, random.Random(1)),
                         ts(2026, 3, 10, 20, 0))

    def test_success_based_advances_when_base_equals_now(self):
        """基准时刻正好等于 now 时必须顺延，否则会立刻重复触发。"""
        sb = SiteSchedule(site_id='b', mode=MODE_SUCCESS_BASED)
        sb.last_success_at = ts(2026, 3, 9, 1, 0)
        now = ts(2026, 3, 10, 1, 0)          # 恰好等于 last + 24h
        self.assertEqual(next_run_at(now, sb, random.Random(1)),
                         ts(2026, 3, 11, 1, 0))

    def test_many_random_runs_never_past(self):
        """大量随机时间下，两种模式都必须排到未来。"""
        rng = random.Random(4242)
        for _ in range(300):
            mode = rng.choice([MODE_DAILY, MODE_SUCCESS_BASED])
            site = SiteSchedule(
                site_id='x', mode=mode,
                daily_hour=rng.randint(0, 23), daily_minute=rng.randint(0, 59),
                jitter_enabled=rng.choice([True, False]),
                jitter_seconds=rng.randint(0, JITTER_MAX_SECONDS),
            )
            if mode == MODE_SUCCESS_BASED:
                # 上次成功在 0~25 小时前：这是真实场景（刚成功过 / 隔了一天多）
                now = ts(2026, 3, 1, 0, 0) + rng.randint(0, 60 * 86400)
                site.last_success_at = now - rng.randint(0, 25 * 3600)
            else:
                now = ts(2026, 3, 1, 0, 0) + rng.randint(0, 60 * 86400)
            got = next_run_at(now, site, rng)
            self.assertGreater(got, now, '排到了过去: mode=%s now=%d got=%d' % (mode, now, got))
            # 最多顺延一天再加 1 小时偏移（最后循环里的滚动可能再多一天）
            self.assertLess(got - now, 3 * 86400,
                            '排得太远: mode=%s 距 now %d 秒' % (mode, got - now))


if __name__ == '__main__':
    unittest.main(verbosity=2)
