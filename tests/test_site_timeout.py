"""整站执行超时。

单个请求自己有超时，但一个站点可能有好几步：代理半死不活时"每步都超时"
叠加起来能把当天整轮拖死，而现象只是"今天一直没动静"，没有任何报错。
所以给每个站点一个总时长上限，超了判失败并继续下一个。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.engine as E  # noqa: E402
from app import secrets as S  # noqa: E402
from app.browser import run_steps  # noqa: E402
from app.httpclient import HttpResponse  # noqa: E402
from app.models import SiteConfig, Step  # noqa: E402
from app.service import Service  # noqa: E402

FLOW = [
    {'action': 'get', 'url': 'https://a.com/1'},
    {'action': 'get', 'url': 'https://a.com/2'},
    {'action': 'get', 'url': 'https://a.com/3'},
]


def fetch_ok(url):
    def f(method, u, data):
        return HttpResponse(status=200, text='已领取', url=u)
    return f


class TestHttpFlowDeadline(unittest.TestCase):
    def test_expired_deadline_stops_before_any_request(self):
        calls = []

        def fetcher(method, url, data):
            calls.append(url)
            return HttpResponse(status=200, text='ok', url=url)

        out = E.run_http_flow(fetcher, FLOW, ['已领取'], [],
                              deadline=time.time() - 1)
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'site_timeout')
        self.assertEqual(calls, [], '已经超时了就不该再发请求')
        self.assertIn('超时', out.message)

    def test_deadline_in_future_does_not_interfere(self):
        calls = []

        def fetcher(method, url, data):
            calls.append(url)
            return HttpResponse(status=200, text='已领取', url=url)

        out = E.run_http_flow(fetcher, FLOW, ['已领取'], [],
                              deadline=time.time() + 30)
        self.assertTrue(out.success)
        self.assertEqual(len(calls), 3)

    def test_no_deadline_means_unlimited(self):
        out = E.run_http_flow(lambda m, u, d: HttpResponse(200, '已领取', u),
                              FLOW, ['已领取'], [], deadline=None)
        self.assertTrue(out.success)

    def test_partial_trace_is_kept(self):
        """超时要能看出"跑到第几步断的"，否则和普通失败没区别。"""
        def fetcher(method, url, data):
            return HttpResponse(status=200, text='还在跑', url=url)

        # 第 1 步前时间正常，第 2 步前"时间已经过了截止点"
        ticks = iter([0, 500])

        def fake_time():
            return next(ticks, 500)

        orig = E.time.time
        E.time.time = fake_time
        try:
            out = E.run_http_flow(fetcher, FLOW, ['已领取'], [], deadline=100)
        finally:
            E.time.time = orig
        self.assertEqual(out.error_kind, 'site_timeout')
        self.assertIn('超时中止', out.trace)
        self.assertEqual(len(out.trace), 2, '应该只跑了第 1 步就中止')
        self.assertIn('超时', out.message)


class TestBrowserDeadline(unittest.TestCase):
    class Driver:
        def __init__(self):
            self.calls = []

        def goto(self, url, ms):
            self.calls.append(('goto', url))

        def click(self, sel, ms):
            self.calls.append(('click', sel))

        def _text(self):
            return '已领取'

    def steps(self):
        return [Step(action='goto', target='https://a.com/'),
                Step(action='click', target='#go')]

    def test_expired_deadline_stops_immediately(self):
        d = self.Driver()
        out = run_steps(d, self.steps(), ['已领取'], [],
                        deadline=time.time() - 1)
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'site_timeout')
        self.assertEqual(d.calls, [])

    def test_normal_run_unaffected(self):
        d = self.Driver()
        out = run_steps(d, self.steps(), ['已领取'], [],
                        deadline=time.time() + 30)
        self.assertEqual(len(d.calls), 2)


class TestWiringIntoCheckin(unittest.TestCase):
    """checkin_site 必须把 deadline 传给两条执行路径。"""

    def setUp(self):
        self.captured = {}
        self._http = E.checkin_via_http

    def tearDown(self):
        E.checkin_via_http = self._http

    def test_http_path_receives_deadline(self):
        def fake(site, proxy='', password='', cookie_header='', deadline=None):
            self.captured['deadline'] = deadline
            return E.FlowOutcome(success=True, message='ok')

        E.checkin_via_http = fake
        site = SiteConfig(id='x', name='X', kind='template', template='v2ex',
                          timeout_seconds=60)
        before = time.time()
        E.checkin_site(site)
        d = self.captured['deadline']
        self.assertIsNotNone(d)
        self.assertGreaterEqual(d, before + 55)
        self.assertLessEqual(d, before + 65)

    def test_zero_means_no_deadline(self):
        def fake(site, proxy='', password='', cookie_header='', deadline=None):
            self.captured['deadline'] = deadline
            return E.FlowOutcome(success=True, message='ok')

        E.checkin_via_http = fake
        E.checkin_site(SiteConfig(id='x', name='X', kind='template',
                                  template='v2ex', timeout_seconds=0))
        self.assertIsNone(self.captured['deadline'])


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_tmo_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')
        # seed 模板并加入站点（v2ex 用于 timeout_seconds 的存取测试）
        from _tpl_loader import seed_plugins
        seed_plugins(self.svc, 'v2ex')
        self.svc.install_template('v2ex')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _site(self, sid='v2ex'):
        return [s for s in self.svc.list_sites() if s['id'] == sid][0]

    def test_default_is_300(self):
        self.assertEqual(self._site()['timeout_seconds'], 300)

    def test_save_and_persist(self):
        self.svc.upsert_site({'id': 'v2ex', 'timeout_seconds': 90})
        self.assertEqual(self._site()['timeout_seconds'], 90)
        again = Service(self.dir, version='t', env_key=None, proxy='')
        self.assertEqual([s for s in again.list_sites()
                          if s['id'] == 'v2ex'][0]['timeout_seconds'], 90)

    def test_out_of_range_is_clamped(self):
        self.svc.upsert_site({'id': 'v2ex', 'timeout_seconds': 99999})
        self.assertEqual(self._site()['timeout_seconds'], 3600)
        self.svc.upsert_site({'id': 'v2ex', 'timeout_seconds': -5})
        self.assertEqual(self._site()['timeout_seconds'], 0)

    def test_old_config_defaults_in(self):
        s = SiteConfig.from_dict({'id': 'x', 'name': 'X'})
        self.assertEqual(s.timeout_seconds, 300)


if __name__ == '__main__':
    unittest.main()
