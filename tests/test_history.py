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


# NOW 固定在 10-15，所有 add() 都落在这一天
_DAY = '2026-10-15'


class TestSteps(HistoryCase):
    """执行步骤链（trace）：落盘、脱敏、截断、回读。
    这一层最要紧的不是"能不能存"，而是**存进去的东西干不干净**：
    trace 里既有 URL 也可能有提取到的令牌，写进一个要留半年的文件里，
    遮不住就等于多了一个凭据泄露面。
    """

    def test_steps_round_trip(self):
        self.add(ok=False, error_kind='http_403',
                 trace=['GET https://a.com/x -> 403', '命中失败关键词'])
        d = [x for x in self.cal()['days'] if x['date'] == _DAY][0]
        self.assertEqual(len(d['items'][0]['steps']), 2)
        self.assertIn('403', d['items'][0]['steps'][0])

    def test_token_in_query_is_masked(self):
        e = self.h.make_entry('v2ex', 'V2EX', False,
                              trace=['GET https://x.com/redeem?once=84123 -> 302'])
        self.assertEqual(e['steps'][0],
                         'GET https://x.com/redeem?once=*** -> 302')

    def test_harmless_query_kept_for_debugging(self):
        """`random=false`、`page=2` 这类是排查时最想看的东西，不能一起遮。"""
        e = self.h.make_entry('nodeseek', 'NodeSeek', True,
                              trace=['POST https://x.com/a?random=false -> 200'])
        self.assertIn('random=false', e['steps'][0])

    def test_long_value_masked_even_with_dull_name(self):
        e = self.h.make_entry('x', 'X', False,
                              trace=['GET https://x.com/a?s=0123456789abcdefghij -> 200'])
        self.assertIn('s=***', e['steps'][0])

    def test_extracted_value_masked(self):
        e = self.h.make_entry('v2ex', 'V2EX', False,
                              trace=['提取 once=84123'])
        self.assertEqual(e['steps'][0], '提取 once=***')

    def test_steps_are_truncated(self):
        e = self.h.make_entry('x', 'X', False,
                              trace=['step %d' % i for i in range(50)])
        self.assertEqual(len(e['steps']), H.STEPS_LIMIT)
        e2 = self.h.make_entry('x', 'X', False, trace=['a' * 500])
        self.assertLessEqual(len(e2['steps'][0]), H.STEP_LIMIT + 1)

    def test_garbage_trace_is_safe(self):
        for bad in (None, 'abc', 123, ['', '  ', None, 'ok']):
            e = self.h.make_entry('x', 'X', False, trace=bad)
            self.assertIsInstance(e['steps'], list)
            for s in e['steps']:
                self.assertIsInstance(s, str)
                self.assertTrue(s.strip())

    def test_old_entry_without_steps_still_works(self):
        """升级前写的记录没有 steps 字段，读的时候不能炸。"""
        self.h.append([{'ts': NOW, 'site_id': 'v2ex', 'site_name': 'V2EX',
                        'ok': True, 'msg': '旧记录'}])
        d = [x for x in self.cal()['days'] if x['date'] == _DAY][0]
        self.assertEqual(d['items'][0]['steps'], [])

    def test_steps_never_contain_newlines(self):
        e = self.h.make_entry('x', 'X', False, trace=['a\nb\r\nc'])
        self.assertNotIn('\n', e['steps'][0])
        self.assertNotIn('\r', e['steps'][0])


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


class TestCalendarReasons(HistoryCase):
    """失败原因：明细上要能看出"是哪一类"，月度上要能看出"哪类最多"。"""

    def test_failures_are_grouped_by_reason(self):
        self.add(ok=False, at=ts(2026, 10, 2, 3, 0), error_kind='http_403')
        self.add(ok=False, at=ts(2026, 10, 3, 3, 0), error_kind='http_403')
        self.add(ok=False, at=ts(2026, 10, 4, 3, 0), error_kind='timeout')
        data = self.cal()
        counts = {r['code']: r['count'] for r in data['reasons']}
        self.assertEqual(counts, {'http_403': 2, 'timeout': 1})
        self.assertEqual(data['reasons'][0]['code'], 'http_403',
                         '次数多的排前面')

    def test_reason_meta_carries_label_and_color(self):
        self.add(ok=False, at=ts(2026, 10, 2), error_kind='http_403')
        m = self.cal()['reason_meta']['http_403']
        self.assertEqual(m['label'], '被拒绝（403）')
        self.assertEqual(m['category'], 'block')
        self.assertTrue(m['color'].startswith('#'))

    def test_item_carries_reason_code(self):
        self.add(ok=False, at=ts(2026, 10, 2), error_kind='unclear',
                 msg='请先登录后再签到')
        it = self.day(self.cal(), '2026-10-02')['items'][0]
        self.assertEqual(it['reason'], 'not_logged_in',
                         'unclear 要看消息才能归对类')

    def test_success_items_have_no_reason(self):
        self.add(ok=True, at=ts(2026, 10, 2))
        it = self.day(self.cal(), '2026-10-02')['items'][0]
        self.assertEqual(it['reason'], '')
        self.assertEqual(self.cal()['reasons'], [])

    def test_day_top_reason(self):
        self.add(ok=False, at=ts(2026, 10, 2, 3, 0), error_kind='timeout')
        self.add(ok=False, at=ts(2026, 10, 2, 3, 10), error_kind='timeout')
        self.add(ok=False, at=ts(2026, 10, 2, 3, 20), error_kind='http_403')
        self.assertEqual(self.day(self.cal(), '2026-10-02')['top_reason'],
                         'timeout')

    def test_all_green_month_has_no_reason_block(self):
        self.add(at=ts(2026, 10, 2))
        data = self.cal()
        self.assertEqual(data['reasons'], [])
        self.assertEqual(data['reason_meta'], {})
        self.assertEqual(self.day(data, '2026-10-02')['top_reason'], '')

    def test_reason_sites_and_last_ts(self):
        self.add(site='v2ex', ok=False, at=ts(2026, 10, 5), error_kind='timeout',
                 msg='旧的失败')
        self.add(site='nodeseek', ok=False, at=ts(2026, 10, 6),
                 error_kind='timeout', msg='新的失败')
        r = self.cal()['reasons'][0]
        self.assertEqual(sorted(r['sites']), ['NODESEEK', 'V2EX'])
        self.assertEqual(r['last_ts'], ts(2026, 10, 6))
        # 单站点视图里拿它顶替站点名（站点名在那时是冗余的）
        self.assertEqual(r['last_msg'], '新的失败')

    def test_aggregation_respects_site_filter(self):
        self.add(site='v2ex', ok=False, at=ts(2026, 10, 5), error_kind='timeout')
        self.add(site='nodeseek', ok=False, at=ts(2026, 10, 5),
                 error_kind='http_403')
        only = self.cal(site='v2ex')
        self.assertEqual([r['code'] for r in only['reasons']], ['timeout'])
        self.assertEqual(only['reasons'][0]['sites'], ['V2EX'])

    def test_recent_top_reason(self):
        self.add(ok=False, at=NOW - 100, error_kind='http_403')
        self.add(ok=False, at=NOW - 200, error_kind='http_403')
        self.add(ok=False, at=NOW - 300, error_kind='timeout')
        top = self.h.recent('', 30)['top_reason']
        self.assertEqual(top['code'], 'http_403')
        self.assertEqual(top['count'], 2)
        self.assertEqual(top['label'], '被拒绝（403）')

    def test_recent_top_reason_none_without_failures(self):
        self.add(at=NOW - 100)
        self.assertIsNone(self.h.recent('', 30)['top_reason'])


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
    def test_trace_is_passed_into_history(self):
        """引擎产出的步骤链要一路带到 history —— 以前出了函数就丢了。"""
        self.results['s1'] = CheckinResult(
            site_id='s1', site_name='S1', success=False, message='403',
            error_kind='http_403',
            trace=['GET https://a.com/x -> 403', '命中失败关键词'])
        self.make_runner().run_once(self.cfg, only_site='s1')
        e = self.history().load()[0]
        self.assertEqual(len(e['steps']), 2)
        self.assertIn('403', e['steps'][0])

    def test_trace_is_scrubbed_before_writing(self):
        """落盘前必须脱敏：这条是防"日志变凭据仓库"的底线。"""
        self.results['s1'] = CheckinResult(
            site_id='s1', site_name='S1', success=True, message='ok',
            trace=['GET https://a.com/redeem?once=84123 -> 302'])
        self.make_runner().run_once(self.cfg, only_site='s1')
        with open(self.history().path, encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('84123', raw, '令牌被原样写进了 history.jsonl')

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
        # 冻结 History 的时钟：不传月份时它按"当前月"取，而断言写的是 NOW。
        # 不冻结的话，这条用例只有在与 NOW 同一个月里跑才绿 ——
        # 一到月末最后几秒（或隔月）就报 '2026-11' != '2026-10'，
        # 看着像日历算错月份，其实是测试自己过期了。
        h = H.get_history(self.dir)
        h.now_fn = lambda: NOW
        h.append([H.History(self.dir, now_fn=lambda: NOW).make_entry(
            'v2ex', 'V2EX', True, msg='ok', ts=NOW)])
        d = svc.history_calendar(None, '')
        self.assertEqual(d['month'], datetime.fromtimestamp(NOW).strftime('%Y-%m'))
        self.assertIn('v2ex', [s['id'] for s in d['sites']])
        self.assertEqual(d['summary']['ok'], 1)
        self.assertEqual(d['recent']['ok'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
