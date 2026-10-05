"""「以【上次签到成功时间】为基准」的初始基准时刻（anchor）。

用户需求原文：
    这个设置需要一个初始时间，目前默认 03:00 不好。改成以「时刻（24 小时制）」
    （例如 9:35）填入的值为初始时间，并强制锁定无法修改；等签到成功之后就
    自动把这个改成签到成功的时间，这样下次就可以直接以这个时间为基准了，
    然后再加上随机值（如果有的话）。设置成这个模式之后人工就不能修改签到
    时刻了；没设置之前修改的时间，在设置的那一刻直接锁定作为初始时间。

对应实现：
    * SiteConfig.success_anchor_minutes：当天 00:00 起的分钟数，-1 = 未锁定
    * 基准 = max(初始锚点, 上次成功时间) —— 首次成功前用锚点，之后自动
      被真实成功时间取代
    * 界面上：锁定后输入框 disabled；保存时只有 data-locked=1 才提交锚点
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S                                     # noqa: E402
from app import service as SV                                    # noqa: E402
from app.models import SiteConfig                                # noqa: E402
from app.schedule import (MODE_DAILY, MODE_SUCCESS_BASED,        # noqa: E402
                          SiteSchedule, anchor_base_ts,
                          next_success_based_time)
from app.service import Service                                  # noqa: E402

import random                                                     # noqa: E402

RNG = random.Random(7)
NOW = int(datetime(2026, 6, 15, 14, 0, 0).timestamp())   # 本地 14:00


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')


def sched(anchor=-1, interval=1440, last_success=None, jitter=0):
    return SiteSchedule(
        site_id='x', mode=MODE_SUCCESS_BASED,
        success_anchor_minutes=anchor, success_interval_minutes=interval,
        last_success_at=last_success, jitter_enabled=bool(jitter),
        jitter_seconds=jitter)


class TestAnchorBaseTs(unittest.TestCase):
    def test_today_when_still_ahead(self):
        """今天该时刻还没到 -> 就排今天。"""
        got = anchor_base_ts(NOW, 23 * 60)
        self.assertEqual(fmt(got), '06-15 23:00')

    def test_tomorrow_when_already_passed(self):
        """今天该时刻已过 -> 顺延明天（用户要求的第一次语义）。"""
        got = anchor_base_ts(NOW, 9 * 60 + 20)
        self.assertEqual(fmt(got), '06-16 09:20')

    def test_equal_to_now_goes_tomorrow(self):
        """正好等于现在 -> 也算"已过"，顺延明天（避免立即触发）。"""
        self.assertEqual(fmt(anchor_base_ts(NOW, 14 * 60)), '06-16 14:00')

    def test_midnight_is_valid(self):
        """00:00 是合法基准，不能被当成"未设置"。"""
        self.assertEqual(fmt(anchor_base_ts(NOW, 0)), '06-16 00:00')

    def test_boundaries(self):
        self.assertEqual(fmt(anchor_base_ts(NOW, 0)), '06-16 00:00')
        self.assertEqual(fmt(anchor_base_ts(NOW, 23 * 60 + 59)), '06-15 23:59')

    def test_out_of_range_clamped(self):
        self.assertEqual(fmt(anchor_base_ts(NOW, -5)), '06-16 00:00')
        self.assertEqual(fmt(anchor_base_ts(NOW, 99999)), '06-15 23:59')


class TestNextSuccessBasedWithAnchor(unittest.TestCase):
    def test_first_run_uses_anchor_day(self):
        """第一次排在基准当天（已过则明天），不是"锚点+间隔"。"""
        got = next_success_based_time(NOW, sched(anchor=9 * 60 + 20), RNG)
        self.assertEqual(fmt(got), '06-16 09:20',
                         '第一次应在基准当天（已过则明天），而不是再加一个间隔')

    def test_first_run_today_when_anchor_ahead(self):
        got = next_success_based_time(NOW, sched(anchor=23 * 60), RNG)
        self.assertEqual(fmt(got), '06-15 23:00')

    def test_midnight_anchor_does_not_fall_back(self):
        """基准 00:00 必须走锚点分支，不能因 `0 or -1` 退回旧行为。

        改回去会怎样：`x or -1` 把 0 变成 -1，"午夜基准"被当成未设置，
        第一次会排到"现在+间隔"（实测过这个静默退化）。
        """
        got = next_success_based_time(NOW, sched(anchor=0), RNG)
        self.assertEqual(fmt(got), '06-16 00:00',
                         '午夜基准被当成未设置了')

    def test_jitter_can_push_to_next_day_but_not_skip_windows(self):
        """抖动把它推到过去时，只顺延到下一个基准点，不能跳过大段。"""
        s = sched(anchor=0, jitter=3600)          # 基准 00:00，±60 分钟
        got = next_success_based_time(NOW, s, RNG)
        # 只可能是今天 00:xx 或明天 00:xx —— 不该出现 14:xx 这种跳段结果
        hh = datetime.fromtimestamp(got).hour
        self.assertIn(hh, (0, 23),
                      '顺延跳过了整段（实测过的 b ug）：%s' % fmt(got))

    def test_success_time_replaces_anchor(self):
        """签到成功后：基准换成真实成功时间（用户例子：09:34 成功）。"""
        succ = int(datetime(2026, 6, 15, 9, 34, 0).timestamp())
        got = next_success_based_time(NOW, sched(anchor=9 * 60 + 20,
                                                 last_success=succ), RNG)
        self.assertEqual(fmt(got), '06-16 09:34',
                         '成功之后应以真实成功时间为基准')

    def test_second_day_after_success_keeps_rolling(self):
        """第三天继续以第二天成功时间为基准。"""
        succ1 = int(datetime(2026, 6, 15, 9, 34, 0).timestamp())
        succ2 = int(datetime(2026, 6, 16, 9, 34, 0).timestamp())
        got = next_success_based_time(succ2, sched(anchor=9 * 60 + 20,
                                                   last_success=succ2), RNG)
        self.assertEqual(fmt(got), '06-17 09:34')

    def test_no_anchor_keeps_old_behaviour(self):
        """老配置（没设过锚点）保持旧行为：现在 + 间隔。"""
        got = next_success_based_time(NOW, sched(anchor=-1), RNG)
        self.assertEqual(fmt(got), '06-16 14:00')

    def test_jitter_applied_on_top_of_anchor(self):
        """基准之上再加随机偏移（用户："然后再加上随机值"）。"""
        s = sched(anchor=23 * 60, jitter=1800)   # 今天 23:00 ±30 分钟
        base = datetime(2026, 6, 15, 23, 0).timestamp()
        for _ in range(50):
            got = next_success_based_time(NOW, s, RNG)
            self.assertLessEqual(abs(got - base), 1800,
                                 '偏移应不超过 jitter_seconds')
            self.assertGreater(got, NOW)

    def test_never_returns_past(self):
        """任何情况下都不能返回过去的时间（否则会立刻重复触发）。"""
        cases = [sched(anchor=a, interval=i, last_success=ls, jitter=j)
                 for a in (-1, 0, 575, 1439)
                 for i in (60, 1440, 10080)
                 for j in (0, 3600)
                 for ls in (None, NOW - 86400 * 3, NOW - 3600)]
        for s in cases:
            with self.subTest(anchor=s.success_anchor_minutes,
                              interval=s.success_interval_minutes,
                              jitter=s.jitter_seconds):
                self.assertGreater(next_success_based_time(NOW, s, RNG), NOW)

    def test_short_interval_with_anchor(self):
        """间隔 1 小时时，第一次仍按基准当天（已过则明天）排。

        基准 09:20 在今天 14:00 已过 -> 第一次 = 明天 09:20。
        之后的节奏由"实际成功时间 + 60 分钟"决定。
        """
        got = next_success_based_time(NOW, sched(anchor=9 * 60 + 20,
                                                 interval=60), RNG)
        self.assertEqual(fmt(got), '06-16 09:20')


class TestSiteConfigAnchorField(unittest.TestCase):
    def test_default_is_unset(self):
        self.assertEqual(SiteConfig(id='a', name='A').success_anchor_minutes,
                         -1)

    def test_round_trip(self):
        s = SiteConfig(id='a', name='A', success_anchor_minutes=575)
        back = SiteConfig.from_dict(s.to_dict())
        self.assertEqual(back.success_anchor_minutes, 575)

    def test_helpers(self):
        s = SiteConfig(id='a', name='A')
        self.assertIsNone(s.anchor_time_of_day())
        s.set_anchor_from_time_of_day(9, 35)
        self.assertEqual(s.success_anchor_minutes, 575)
        self.assertEqual(s.anchor_time_of_day(), (9, 35))

    def test_helpers_clamp(self):
        s = SiteConfig(id='a', name='A')
        s.set_anchor_from_time_of_day(99, 99)
        self.assertEqual(s.anchor_time_of_day(), (23, 59))

    def test_schedule_carries_anchor(self):
        s = SiteConfig(id='a', name='A', success_anchor_minutes=575)
        self.assertEqual(s.to_schedule().success_anchor_minutes, 575)

    def test_bad_value_coerced(self):
        for bad, want in (('x', -1), (None, -1), (-5, -1), (99999, 1439)):
            with self.subTest(bad=bad):
                got = SiteConfig.from_dict(
                    {'id': 'a', 'name': 'A',
                     'success_anchor_minutes': bad}).success_anchor_minutes
                self.assertEqual(got, want)

    def test_daily_mode_untouched(self):
        """锚点不影响 daily 模式。"""
        s = SiteConfig(id='a', name='A', mode=MODE_DAILY, daily_hour=20,
                       daily_minute=11, success_anchor_minutes=575)
        sch = s.to_schedule()
        self.assertEqual(sch.mode, MODE_DAILY)
        self.assertEqual((sch.daily_hour, sch.daily_minute), (20, 11))


class TestServiceAnchor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_anchor_svc_')
        self.svc = Service(self.dir, version='t',
                           env_key=S.generate_key(), proxy='')
        self.svc.upsert_site({'id': 'a', 'name': 'A'})

    def _site(self):
        return [s for s in self.svc.load_config().sites if s.id == 'a'][0]

    def test_upsert_stores_anchor(self):
        self.svc.upsert_site({'id': 'a', 'name': 'A',
                              'success_anchor_minutes': 575})
        self.assertEqual(self._site().success_anchor_minutes, 575)

    def test_upsert_accepts_zero(self):
        """0 点（00:00）是合法值，不能被当成"空"跳过。"""
        self.svc.upsert_site({'id': 'a', 'name': 'A',
                              'success_anchor_minutes': 0})
        self.assertEqual(self._site().success_anchor_minutes, 0)

    def test_upsert_keeps_existing_when_absent(self):
        """不传该字段时不能被清掉（否则切模式就丢基准）。"""
        self.svc.upsert_site({'id': 'a', 'name': 'A',
                              'success_anchor_minutes': 575})
        self.svc.upsert_site({'id': 'a', 'name': 'A', 'daily_hour': 5})
        self.assertEqual(self._site().success_anchor_minutes, 575)

    def test_upsert_clamps(self):
        self.svc.upsert_site({'id': 'a', 'name': 'A',
                              'success_anchor_minutes': 99999})
        self.assertEqual(self._site().success_anchor_minutes, 1439)

    def test_list_sites_exposes_anchor(self):
        self.svc.upsert_site({'id': 'a', 'name': 'A',
                              'success_anchor_minutes': 575})
        row = [x for x in self.svc.list_sites() if x['id'] == 'a'][0]
        self.assertEqual(row['success_anchor_minutes'], 575)

    def test_anchor_survives_schedule_state_writeback(self):
        """运行结束后回写调度状态，不能把锚点冲掉。

        这是本功能的关键：调度状态（last_success_at 等）每轮都写，
        而锚点必须留到首次成功为止。
        """
        self.svc.upsert_site({'id': 'a', 'name': 'A',
                              'success_anchor_minutes': 575})
        site = self._site()
        sch = site.to_schedule()
        sch.last_attempt_at = 12345
        sch.consecutive_failures = 2
        site.apply_schedule_state(sch)
        self.svc.save_config(self.svc.load_config())
        self.assertEqual(self._site().success_anchor_minutes, 575,
                         '回写调度状态后锚点丢了')


if __name__ == '__main__':
    unittest.main(verbosity=2)
