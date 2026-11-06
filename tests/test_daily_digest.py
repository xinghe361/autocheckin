"""每日汇总推送。

容易坏的不是"能不能发"，而是这三件：
  * 一天推多条（scheduler 每分钟都在调）；
  * 推送失败后当天再也不试（用户整天收不到，还毫无提示）；
  * 把"今天"的数据当日报发出去（签到大多在凌晨，早上推今天必然是残缺的）。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import history as H  # noqa: E402
from app import notify as N  # noqa: E402
from app import secrets as S  # noqa: E402
from app.models import NOTIFY_ALL  # noqa: E402
from app.service import Service  # noqa: E402


def ts_days_ago(days: int, hour: int = 3) -> int:
    d = datetime.now() - timedelta(days=days)
    return int(d.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp())


class DigestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_digest_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')
        cfg = self.svc.load_config()
        cfg.notify.mode = NOTIFY_ALL
        cfg.notify.channels = ['pushplus']
        cfg.notify.pushplus_token = 'tok'
        cfg.notify.digest_enabled = True
        cfg.notify.digest_hour = 9
        cfg.notify.digest_minute = 0
        self.svc.save_config(cfg)
        self.sent = []
        self._orig = N.send_alert
        outer = self

        def fake_send(cfg_, title, content, proxy=''):
            outer.sent.append({'title': title, 'content': content})
            # 用记录下来的返回值方便模拟"全部渠道失败"
            return outer.report

        self.report = {'pushplus': 'pushplus 已推送'}
        N.send_alert = fake_send

    def tearDown(self):
        N.send_alert = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def seed(self, days_ago=1, ok=True, site='v2ex', name='V2EX',
             msg='签到成功', kind=''):
        H.get_history(self.dir).append([H.History(self.dir).make_entry(
            site, name, ok, msg=msg, error_kind=kind,
            ts=ts_days_ago(days_ago))])

    def force(self):
        """绕过"到点了吗"，只看去重与内容。"""
        return self.svc.maybe_send_digest(force=True)

    def set_due_now(self):
        """把推送时刻设成"一分钟前"，让时间闸门放行（测去重要走真实路径）。"""
        cfg = self.svc.load_config()
        t = time.localtime(time.time() - 60)
        cfg.notify.digest_hour = t.tm_hour
        cfg.notify.digest_minute = t.tm_min
        self.svc.save_config(cfg)


class TestContent(DigestCase):
    def test_summarises_yesterday(self):
        self.seed(1, ok=True, site='v2ex', name='V2EX')
        self.seed(1, ok=False, site='nodeseek', name='NodeSeek',
                  msg='请先登录', kind='not_logged_in')
        got = self.force()
        self.assertTrue(got['sent'])
        self.assertEqual(len(self.sent), 1)
        content = self.sent[0]['content']
        self.assertIn('成功 1 次', content)
        self.assertIn('失败 1 次', content)
        self.assertIn('NodeSeek', content)
        self.assertIn('未登录', content, '应带失败原因的中文归类')
        self.assertIn('正常：V2EX', content)

    def test_title_uses_yesterday(self):
        self.seed(1)
        self.force()
        yday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        self.assertIn(yday, self.sent[0]['title'])

    def test_today_records_are_not_included(self):
        """今天的数据不能混进"昨天"的日报。"""
        self.seed(0, ok=True)
        got = self.force()
        self.assertFalse(got.get('sent'))
        self.assertEqual(self.sent, [])

    def test_empty_day_sends_nothing(self):
        got = self.force()
        self.assertFalse(got.get('sent'))
        self.assertEqual(self.sent, [])
        self.assertIn('没有签到记录', got.get('reason', ''))

    def test_many_sites_truncated(self):
        for i in range(15):
            self.seed(1, ok=False, site='s%d' % i, name='S%d' % i)
        self.force()
        content = self.sent[0]['content']
        self.assertIn('还有 5 个', content)


class TestDedup(DigestCase):
    def test_only_once_per_day(self):
        self.set_due_now()
        self.seed(1)
        self.assertTrue(self.svc.maybe_send_digest()['sent'])
        second = self.svc.maybe_send_digest()
        self.assertFalse(second.get('sent'))
        self.assertEqual(len(self.sent), 1, '同一天推了多条')
        self.assertIn('已经推过', second.get('reason', ''))

    def test_marks_state_so_restart_does_not_repush(self):
        self.set_due_now()
        self.seed(1)
        self.svc.maybe_send_digest()
        again = Service(self.dir, version='t', env_key=None, proxy='')
        got = again.maybe_send_digest()
        self.assertFalse(got.get('sent'))
        self.assertEqual(len(self.sent), 1)

    def test_disabled_does_nothing(self):
        cfg = self.svc.load_config()
        cfg.notify.digest_enabled = False
        self.svc.save_config(cfg)
        self.seed(1)
        got = self.svc.maybe_send_digest(force=True)
        self.assertFalse(got.get('sent'))
        self.assertEqual(self.sent, [])
        self.assertIn('未启用', got.get('reason', ''))

    def test_no_channel_configured(self):
        cfg = self.svc.load_config()
        cfg.notify.channels = []
        cfg.notify.pushplus_token = ''
        self.svc.save_config(cfg)
        self.seed(1)
        got = self.svc.maybe_send_digest(force=True)
        self.assertFalse(got.get('sent'))
        self.assertIn('通知渠道', got.get('reason', ''))


class TestFailureRetry(DigestCase):
    def test_does_not_mark_sent_when_all_channels_fail(self):
        """全渠道失败必须留着重试，否则一次网络抖动就整天收不到日报。"""
        self.report = {'pushplus': '失败：连接超时'}
        self.seed(1)
        got = self.force()
        self.assertFalse(got['sent'])
        # 第二次仍然会尝试
        self.report = {'pushplus': 'pushplus 已推送'}
        got2 = self.force()
        self.assertTrue(got2['sent'])
        self.assertEqual(len(self.sent), 2)


class TestTimeGate(DigestCase):
    def test_before_scheduled_time_is_skipped(self):
        """时钟冻在本地 10:00、推送时刻设 11:00 —— 必须跳过。

        以前写的是 (now.tm_hour + 1) % 24：23 点会回绕成 0，变成"已经到点"，
        于是每天的 23:00~23:59 跑测试才红。典型的时间炸弹，冻住时钟最稳。
        """
        from unittest import mock

        now = time.localtime()
        ten = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                           10, 0, 0, 0, 0, -1))
        with mock.patch('app.service.time.time', return_value=ten):
            cfg = self.svc.load_config()
            cfg.notify.digest_hour = 11
            cfg.notify.digest_minute = 0
            self.svc.save_config(cfg)
            self.seed(1)
            got = self.svc.maybe_send_digest()      # 不带 force

        self.assertFalse(got.get('sent'))
        self.assertIn('还没到', got.get('reason', ''))
        self.assertEqual(self.sent, [])


class TestDigestText(unittest.TestCase):
    def test_no_sites(self):
        title, content = N.digest_message({'date': '2026-10-31', 'sites': []})
        self.assertIn('2026-10-31', title)
        self.assertIn('没有任何签到记录', content)

    def test_all_success_has_no_failure_section(self):
        title, content = N.digest_message({
            'date': '2026-10-31', 'ok': 2, 'fail': 0,
            'sites': [{'site_name': 'A', 'ok': 2, 'fail': 0}]})
        self.assertNotIn('有问题的站点', content)
        self.assertIn('正常：A', content)

    def test_failure_without_reason_uses_raw_message(self):
        title, content = N.digest_message({
            'date': '2026-10-31', 'ok': 0, 'fail': 1,
            'sites': [{'site_name': 'A', 'ok': 0, 'fail': 1,
                       'reason': '', 'last_msg': '奇怪的错误'}]})
        self.assertIn('奇怪的错误', content)


class TestHistoryDaySummary(DigestCase):
    def test_counts_and_order(self):
        self.seed(1, ok=True, site='a', name='A')
        self.seed(1, ok=False, site='b', name='B', kind='timeout')
        self.seed(1, ok=True, site='b', name='B')
        s = H.get_history(self.dir).day_summary(
            (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'))
        self.assertEqual(s['ok'], 2)
        self.assertEqual(s['fail'], 1)
        self.assertEqual(s['sites'][0]['site_id'], 'b', '有失败的排前面')
        self.assertEqual(s['reasons'][0]['code'], 'timeout')

    def test_other_days_excluded(self):
        self.seed(1)
        self.seed(3)
        s = H.get_history(self.dir).day_summary(
            (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'))
        self.assertEqual(s['total'], 1)


if __name__ == '__main__':
    unittest.main()
