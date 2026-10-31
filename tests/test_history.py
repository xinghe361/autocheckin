"""执行历史（日历）的单测。

重点不是"能存能取"，而是这几件容易悄悄坏掉的事：
  * 一行写坏了，不能连累整页（跳过坏行，别整文件当损坏）；
  * 日历的"哪天是哪天"必须和容器时区一致（否则今天高亮会飘到昨天）；
  * 重试的失败也要记 —— 用户要看的正是"哪天不对劲"，不是"哪天最终成功"；
  * 记历史失败绝不能把签到本身搞失败。
"""

import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import history as H                                   # noqa: E402
from app import secrets as S                                   # noqa: E402
from app import store as ST                                    # noqa: E402
from app.models import AppConfig, SiteConfig                   # noqa: E402
from app.notify import CheckinResult                           # noqa: E402
from app.runner import Runner                                  # noqa: E402
from app.schedule import MODE_DAILY                            # noqa: E402
from app.service import Service                                # noqa: E402

# 固定在月中，避免"这个月只有 28 天 / 跨月"之类的偶然差异
NOW = int(datetime(2026, 10, 15, 12, 0).timestamp())


def ts(y, mo, d, hh=3, mi=0):
    return int(datetime(y, mo, d, hh, mi).timestamp())


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_hist_')
        self.h = H.History(self.dir, now_fn=lambda: NOW)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, site='v2ex', ok=True, at=None, msg='', **kw):
        e = self.h.make_entry(site_id=site, site_name=site.upper(), ok=ok,
                              msg=msg, ts=at if at is not None else NOW, **kw)
        self.h.append([e])
        return e

    def cal(self, site='', month='2026-10'):
        return self.h.calendar(site, month)

    def day(self, data, key):
        for d in data['days']:
            if d['date'] == key:
                return d
        self.fail('日历里没有这一天：%s' % key)


class TestStorage(HistoryCase):
    def test_append_and_load(self):
        self.add(at=ts(2026, 10, 1, 3, 1), msg='签到成功，获得 8 铜币')
        self.add(site='nodeseek', at=ts(2026, 10, 1, 4, 0))
        got = self.h.load()
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]['site_id'], 'v2ex')
        self.assertEqual(got[0]['msg'], '签到成功，获得 8 铜币')
        self.assertTrue(got[0]['ok'])

    def test_bad_line_does_not_break_whole_file(self):
        """手改坏一行（或被截断），其余记录必须照常读出来。"""
        self.add(at=ts(2026, 10, 2))
        with open(self.h.path, 'a', encoding='utf-8') as f:
            f.write('{这不是 json\n')
            f.write('\n')
        self.add(at=ts(2026, 10, 3))
        self.assertEqual(len(self.h.load()), 2)
        data = self.cal()
        self.assertEqual(self.day(data, '2026-10-03')['status'], H.STATUS_OK)

    def test_missing_file_is_empty_not_error(self):
        empty = H.History(os.path.join(self.dir, 'nope'), now_fn=lambda: NOW)
        self.assertEqual(empty.load(), [])
        data = empty.calendar()
        self.assertEqual(len(data['days']), 31)
        self.assertEqual(data['summary']['ok'], 0)

    def test_msg_is_flattened_and_capped(self):
        e = self.h.make_entry('x', 'X', False, msg='第一行\n第二行\t带空格\r\n')
        self.assertNotIn('\n', e['msg'])
        self.assertEqual(e['msg'], '第一行 第二行 带空格')
        long_msg = 'a' * 500
        self.assertLessEqual(len(self.h.make_entry('x', 'X', True,
                                                   msg=long_msg)['msg']),
                             H.MSG_LIMIT + 1)

    def test_append_swallows_os_errors(self):
        """数据目录不可用时只返回 0，不抛异常。"""
        bad = H.History(os.path.join(self.dir, 'a_file'), now_fn=lambda: NOW)
        os.makedirs(self.dir, exist_ok=True)
        open(os.path.join(self.dir, 'a_file'), 'w').close()   # 同路径是文件
        self.assertEqual(bad.append([{'ts': NOW}]), 0)

    def test_compaction_drops_entries_older_than_retention(self):
        old = NOW - (H.KEEP_DAYS + 10) * 86400
        self.add(at=old, msg='很久以前')
        self.add(at=NOW, msg='今天')
        dropped = self.h._compact()
        self.assertEqual(dropped, 1)
        left = self.h.load()
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]['msg'], '今天')

    def test_get_history_returns_shared_instance(self):
        a = H.get_history(self.dir)
        b = H.get_history(self.dir)
        self.assertIs(a, b, '同一数据目录必须共用一个实例（否则压缩与追加会打架）')
        self.assertIsNot(a, H.get_history(os.path.join(self.dir, 'other')))


class TestCalendar(HistoryCase):
    def test_status_ok_fail_none(self):
        self.add(at=ts(2026, 10, 1, 3, 0))                 # 全绿
        self.add(at=ts(2026, 10, 2, 3, 0), ok=False)        # 失败
        self.add(at=ts(2026, 10, 2, 3, 10), ok=True)        # 重试成功，仍是"有问题"
        data = self.cal()
        self.assertEqual(self.day(data, '2026-10-01')['status'], H.STATUS_OK)
        d2 = self.day(data, '2026-10-02')
        self.assertEqual(d2['status'], H.STATUS_FAIL,
                         '当天有过失败就算"有问题"，哪怕后来重试成功')
        self.assertEqual((d2['ok'], d2['fail'], d2['total']), (1, 1, 2))
        self.assertEqual(self.day(data, '2026-10-03')['status'], H.STATUS_NONE)
        self.assertEqual(data['summary']['green_days'], 1)
        self.assertEqual(data['summary']['problem_days'], 1)

    def test_site_filter(self):
        self.add(site='v2ex', at=ts(2026, 10, 5))
        self.add(site='nodeseek', ok=False, at=ts(2026, 10, 5))
        all_data = self.cal()
        self.assertEqual(self.day(all_data, '2026-10-05')['status'],
                         H.STATUS_FAIL)
        only = self.cal(site='v2ex')
        self.assertEqual(self.day(only, '2026-10-05')['status'], H.STATUS_OK)
        self.assertEqual(len(self.day(only, '2026-10-05')['items']), 1)

    def test_month_bounds_exclude_other_months(self):
        self.add(at=ts(2026, 9, 30, 23, 59))
        self.add(at=ts(2026, 11, 1, 0, 1))
        data = self.cal(month='2026-10')
        self.assertEqual(len(data['days']), 31)
        self.assertEqual(data['summary']['ok'], 0)
        sep = self.cal(month='2026-09')
        self.assertEqual(self.day(sep, '2026-09-30')['status'], H.STATUS_OK)

    def test_first_weekday_matches_real_calendar(self):
        """0=周一。算错一格，整个月都会错位一天。"""
        data = self.cal(month='2026-10')
        first = datetime(2026, 10, 1)
        self.assertEqual(data['first_weekday'], first.weekday())
        self.assertEqual(data['days'][0]['date'], '2026-10-01')

    def test_today_and_future_flags(self):
        data = self.cal()
        self.assertTrue(self.day(data, '2026-10-15')['is_today'])
        self.assertTrue(self.day(data, '2026-10-16')['future'])
        self.assertFalse(self.day(data, '2026-10-14')['future'])

    def test_invalid_month_falls_back_to_current(self):
        now_month = datetime.fromtimestamp(NOW).strftime('%Y-%m')
        for bad in ('', 'abc', '2026-13', '2026/10', '1999-01'):
            with self.subTest(bad=bad):
                self.assertEqual(H.parse_month(bad, NOW), now_month)
        self.assertEqual(H.parse_month('2026-03', NOW), '2026-03')

    def test_recent_rate(self):
        self.add(at=NOW - 3600, ok=True)
        self.add(at=NOW - 7200, ok=False)
        r = self.h.recent('', 30)
        self.assertEqual((r['ok'], r['fail'], r['total']), (1, 1, 2))
        self.assertAlmostEqual(r['rate'], 50.0)
        self.assertIsNone(self.h.recent('nosuch', 30)['rate'])

    def test_items_sorted_by_time(self):
        self.add(at=ts(2026, 10, 7, 9, 0), msg='后')
        self.add(at=ts(2026, 10, 7, 3, 0), msg='先')
        items = self.day(self.cal(), '2026-10-07')['items']
        self.assertEqual([i['msg'] for i in items], ['先', '后'])


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_hist_run_')
        self.now = NOW
        self.box = S.SecretBox(S.generate_key())
        self.store = ST.Store(self.dir)
        self.cfg = AppConfig(proxy='')
        self.cfg.sites = [SiteConfig(id='s1', name='S1', daily_hour=3,
                                     daily_minute=0, mode=MODE_DAILY)]
        self.store.save_config(self.cfg)
        self.results = {}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_runner(self):
        def fake_checkin(site, proxy, password, browser, cookie_header=''):
            r = self.results.get(site.id)
            if callable(r):
                return r(site)
            return r or CheckinResult(site_id=site.id, site_name=site.name,
                                      success=True, message='签到成功',
                                      duration_ms=120)

        return Runner(self.store, box=self.box, proxy='',
                      now_fn=lambda: self.now, rng=random.Random(1),
                      checkin_fn=fake_checkin,
                      analyze_fn=lambda *a, **kw: None,
                      notify_fn=lambda *a, **kw: {})

    def history(self):
        return H.get_history(self.dir)


class TestRunnerRecordsHistory(RunnerCase):
    def test_success_is_recorded(self):
        r = self.make_runner()
        r.run_once(self.cfg, only_site='s1', trigger='manual')
        entries = self.history().load()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertTrue(e['ok'])
        self.assertEqual(e['site_id'], 's1')
        self.assertEqual(e['site_name'], 'S1')
        self.assertEqual(e['trigger'], 'manual')
        self.assertEqual(e['duration_ms'], 120)

    def test_failure_is_recorded_with_reason(self):
        self.results['s1'] = CheckinResult(site_id='s1', site_name='S1',
                                           success=False,
                                           message='未登录，请更新 Cookie',
                                           error_kind='not_logged_in')
        r = self.make_runner()
        r.run_once(self.cfg, only_site='s1')
        e = self.history().load()[0]
        self.assertFalse(e['ok'])
        self.assertEqual(e['msg'], '未登录，请更新 Cookie')
        self.assertEqual(e['error_kind'], 'not_logged_in')
        self.assertEqual(e['trigger'], 'schedule')

    def test_calendar_sees_the_run(self):
        r = self.make_runner()
        r.run_once(self.cfg, only_site='s1')
        data = self.history().calendar('s1', '2026-10')
        day = [d for d in data['days'] if d['date'] == '2026-10-15'][0]
        self.assertEqual(day['status'], H.STATUS_OK)

    def test_exception_still_recorded_as_failure(self):
        def boom(site):
            raise RuntimeError('炸了')
        self.results['s1'] = boom
        r = self.make_runner()
        r.run_once(self.cfg, only_site='s1')
        e = self.history().load()[0]
        self.assertFalse(e['ok'])
        self.assertIn('炸了', e['msg'])

    def test_history_write_failure_does_not_break_run(self):
        """记历史失败绝不能影响签到结果（这里把 history 换成会炸的实现）。"""
        class Boom:
            def make_entry(self, **kw):
                raise IOError('磁盘满了')
        r = self.make_runner()
        r.history = Boom()
        report = r.run_once(self.cfg, only_site='s1')
        self.assertEqual(report.success_count, 1)


class TestServiceHistory(RunnerCase):
    def test_calendar_payload_has_sites_and_recent(self):
        svc = Service(self.dir, version='t', env_key=S.generate_key(),
                      proxy='')
        svc.upsert_site({'id': 'v2ex', 'name': 'V2EX', 'template': 'v2ex',
                         'kind': 'template'})
        H.get_history(self.dir).append([
            H.History(self.dir, now_fn=lambda: NOW).make_entry(
                'v2ex', 'V2EX', True, msg='ok', ts=NOW)])
        d = svc.history_calendar(None, '')
        self.assertEqual(d['month'], datetime.fromtimestamp(NOW).strftime('%Y-%m'))
        self.assertIn('v2ex', [s['id'] for s in d['sites']])
        self.assertEqual(d['summary']['ok'], 1)
        self.assertEqual(d['recent']['ok'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
