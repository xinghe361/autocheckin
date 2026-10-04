"""engine 单测：用注入的假 fetcher 验证签到流程判定逻辑。

不依赖网络——这是把流程与网络分层的目的。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine as E  # noqa: E402
from app import templates as T  # noqa: E402
from app.httpclient import HttpResponse  # noqa: E402
from app.models import SiteConfig  # noqa: E402


def fake_fetcher(pages, calls=None):
    """pages: [(status, text), ...] 按调用顺序返回。"""
    it = iter(pages)

    def fetch(method, url, data=None):
        if calls is not None:
            calls.append((method, url, data))
        try:
            status, text = next(it)
        except StopIteration:
            status, text = 200, ''
        return HttpResponse(status=status, text=text, url=url)

    return fetch


class TestKeywordMatching(unittest.TestCase):
    def test_match_keywords(self):
        self.assertEqual(E.match_keywords('已领取 8 铜币', ['领取', '失败']), '领取')
        self.assertIsNone(E.match_keywords('什么都没有', ['领取']))
        self.assertIsNone(E.match_keywords('', ['领取']))
        self.assertIsNone(E.match_keywords('abc', []))

    def test_classify_prefers_already_over_success(self):
        """"今天已经领取过了"同时含成功词与已领词，必须判为 already。"""
        kind, kw = E.classify_page('今天已经领取过了', ['领取'], [], ['已经领取'])
        self.assertEqual(kind, 'already')

    def test_classify_prefers_fail_over_success(self):
        kind, _ = E.classify_page('请先登录后领取', ['领取'], ['请先登录'])
        self.assertEqual(kind, 'fail')

    def test_classify_success(self):
        kind, kw = E.classify_page('已领取 8 铜币', ['已领取'], [])
        self.assertEqual(kind, 'success')
        self.assertEqual(kw, '已领取')

    def test_classify_unknown(self):
        kind, kw = E.classify_page('随便什么内容', ['已领取'], ['错误'])
        self.assertEqual(kind, 'unknown')
        self.assertEqual(kw, '')


class TestRender(unittest.TestCase):
    def test_render_replaces_vars(self):
        self.assertEqual(E._render('a/{x}/b/{y}', {'x': '1', 'y': '2'}), 'a/1/b/2')

    def test_render_leaves_unknown(self):
        self.assertEqual(E._render('a/{z}', {'x': '1'}), 'a/{z}')

    def test_render_empty(self):
        self.assertEqual(E._render('', {'x': '1'}), '')


class TestV2exFlow(unittest.TestCase):
    """V2EX 流程：取页面 -> 抽 once 令牌 -> **GET** redeem -> 再取页面确认。

    详细的行为验证（含 405 回归）在 tests/test_v2ex_flow.py，
    这里只保留一个集成到流程引擎的冒烟，避免两处重复维护。
    """

    FLOW = T.V2EX['flow']
    VARS = {'mission_url': 'https://www.v2ex.com/mission/daily'}

    def _run(self, script, calls):
        f = fake_fetcher(script, calls)
        return E.run_http_flow(f, self.FLOW, T.V2EX['success_keywords'],
                               T.V2EX['fail_keywords'],
                               T.V2EX['already_keywords'],
                               initial_vars=dict(self.VARS))

    def test_success_with_token_extraction(self):
        page = '<a href="/mission/daily/redeem?once=12345">领取 X 铜币</a>'
        done = '<div>每日登录奖励已领取</div>'
        calls = []
        out = self._run([(200, page), (302, ''), (200, done)], calls)
        self.assertTrue(out.success, out.message)
        self.assertEqual(out.vars.get('once'), '12345')
        # redeem 必须是 GET（POST 会 405），且在 redeem 之后还要再取一次页面
        self.assertEqual([c[0] for c in calls], ['GET', 'GET', 'GET'])
        self.assertIn('once=12345', calls[1][1])

    def test_not_logged_in_is_failure(self):
        """未登录的页面没有令牌，会在取令牌这步就明确失败。"""
        calls = []
        out = self._run([(200, '请先登录'), (200, '请先登录')], calls)
        self.assertFalse(out.success)
        self.assertIn('令牌', out.message)
        self.assertIn('未登录', out.message, '应提示通常是未登录')
        self.assertEqual(out.error_kind, 'no_token')

    def test_fail_keyword_reported_when_token_present(self):
        """有令牌但最后页面提示需要登录时，走关键词判定。"""
        page = '<a href="/mission/daily/redeem?once=7">x</a>'
        calls = []
        out = self._run([(200, page), (302, ''), (200, '请先登录后再领取')], calls)
        self.assertFalse(out.success)
        self.assertEqual(out.hit, 'fail')
        self.assertIn('请先登录', out.message)

    def test_missing_token_reports_clearly(self):
        """页面到了但没有令牌：明确说是令牌问题。"""
        calls = []
        out = self._run([(200, '<html>没有令牌的页面</html>')], calls)
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'no_token')



class TestFlowEngineGeneral(unittest.TestCase):
    def test_trace_records_steps(self):
        f = fake_fetcher([(200, '已领取')])
        out = E.run_http_flow(f, [{'action': 'get', 'url': 'https://a.test/'}],
                              ['已领取'], [])
        self.assertTrue(any('GET https://a.test/' in t for t in out.trace))

    def test_unknown_action_is_skipped_not_fatal(self):
        """未知动作只记录并跳过；判定用传入的页面内容。"""
        f = fake_fetcher([])
        out = E.run_http_flow(f, [{'action': 'teleport'}], ['已领取'], [],
                              initial_text='已领取')
        self.assertTrue(out.success)
        self.assertTrue(any('未知动作' in t for t in out.trace))

    def test_post_with_dict_data_is_form_encoded(self):
        calls = []
        f = fake_fetcher([(200, '已领取')], calls)
        out = E.run_http_flow(f, [{'action': 'post', 'url': 'https://a.test/',
                                   'data': {'k': 'v', 'u': '{user}'}}],
                              ['已领取'], [], initial_vars={'user': '张三'})
        self.assertTrue(out.success)
        body = calls[0][2].decode('utf-8')
        self.assertIn('k=v', body)
        self.assertIn('%E5%BC%A0%E4%B8%89', body)

    def test_optional_extract_does_not_fail(self):
        f = fake_fetcher([(200, 'nothing')])
        out = E.run_http_flow(f, [
            {'action': 'get', 'url': 'https://a.test/'},
            {'action': 'extract_regex', 'pattern': r'once=(\d+)',
             'save_as': 'once', 'optional': True},
        ], [], [])
        self.assertNotIn('令牌', out.message)

    def test_empty_flow_with_success_page(self):
        """空流程（纯判定场景）应能靠 initial_text 正常判定。"""
        f = fake_fetcher([])
        out = E.run_http_flow(f, [], ['签到成功'], [], initial_text='签到成功')
        self.assertTrue(out.success)

    def test_empty_flow_without_text_is_unclear_not_crash(self):
        f = fake_fetcher([])
        out = E.run_http_flow(f, [], ['签到成功'], [])
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'unclear')

    def test_http_error_status_reported_when_keywords_miss(self):
        """HTTP 层失败要单独报出来，而不是笼统的"无法判断"。"""
        f = fake_fetcher([])
        out = E.run_http_flow(f, [], ['签到成功'], [],
                              initial_text='Cloudflare challenge',
                              initial_status=403)
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'http_403')
        self.assertIn('403', out.message)


class TestNeedsBrowser(unittest.TestCase):
    def test_template_flags(self):
        """内置站点当前【全部走纯 HTTP】—— 这是实测修正后的结果。

        曾经以为 nodeseek / chiphell 必须用浏览器（见 v1.0.1 的模板注释），
        后来实际请求证明：
          * nodeseek 的签到端点是 /api/attendance，不在 Cloudflare 防线后面
          * chiphell 带 Cookie 访问 forum.php 即可，证书也正常
        所以三者的 need_browser 都应为 False。
        """
        by_id = {s.id: s for s in T.builtin_sites()}
        self.assertFalse(E.needs_browser(by_id['v2ex']))
        self.assertFalse(E.needs_browser(by_id['nodeseek']),
                         'NodeSeek 实测走 API 纯请求即可')
        self.assertFalse(E.needs_browser(by_id['chiphell']),
                         'Chiphell 实测带 Cookie 访问 forum.php 即可')

    def test_custom_site_with_steps_needs_browser(self):
        from app.models import Step
        s = SiteConfig(id='c', name='C', kind='custom',
                       steps=[Step(action='goto', target='https://a/')])
        self.assertTrue(E.needs_browser(s))

    def test_explicit_flag_wins(self):
        s = SiteConfig(id='c', name='C', template='v2ex', need_browser=True)
        self.assertTrue(E.needs_browser(s))


class TestCheckinSiteDispatch(unittest.TestCase):
    """签到路径选择：浏览器站点 vs 纯请求站点。

    注意：内置站点里现在【没有】需要浏览器的了 ——
    NodeSeek 与 Chiphell 经实测确认都能走纯 HTTP
    （NodeSeek 的签到端点是 /api/attendance，不在 Cloudflare 防线后面；
      Chiphell 带 Cookie 访问 forum.php 即可）。
    所以这些用例改用显式的浏览器站点（kind=custom + need_browser=True），
    不再依赖某个内置站点的默认值。
    """

    @staticmethod
    def _browser_site():
        return SiteConfig(id='bs', name='浏览器站点', kind='custom',
                          need_browser=True, homepage='https://example.com/')

    def test_browser_site_without_browser_reports_clearly(self):
        r = E.checkin_site(self._browser_site(), proxy='', browser=None)
        self.assertFalse(r.success)
        self.assertEqual(r.error_kind, 'browser_unavailable')
        self.assertIn('浏览器', r.message)

    def test_browser_site_uses_injected_browser(self):
        class FakeBrowser:
            def run(self, site, proxy='', password='', cookie_header=None):
                return E.FlowOutcome(True, '浏览器签到成功')

        r = E.checkin_site(self._browser_site(), browser=FakeBrowser())
        self.assertTrue(r.success)
        self.assertEqual(r.message, '浏览器签到成功')
        self.assertEqual(r.site_id, 'bs')

    def test_browser_receives_cookie_header(self):
        """Cookie 必须传进浏览器：不传就永远是未登录状态（旧实现的真实缺陷）。"""
        seen = {}

        class SpyBrowser:
            def run(self, site, proxy='', password='', cookie_header=None):
                seen['cookie'] = cookie_header
                seen['proxy'] = proxy
                return E.FlowOutcome(True, 'ok')

        E.checkin_site(self._browser_site(), proxy='http://p:1',
                       browser=SpyBrowser(), cookie_header='a=1; b=2')
        self.assertEqual(seen['cookie'], 'a=1; b=2')
        self.assertEqual(seen['proxy'], 'http://p:1')

    def test_browser_exception_becomes_failure_result(self):
        class BoomBrowser:
            def run(self, site, proxy='', password='', cookie_header=None):
                raise RuntimeError('浏览器崩了')

        r = E.checkin_site(self._browser_site(), browser=BoomBrowser())
        self.assertFalse(r.success)
        self.assertEqual(r.error_kind, 'exception')
        self.assertIn('浏览器崩了', r.message)

    def test_attempt_number_taken_from_state(self):
        s = SiteConfig(id='x', name='X', kind='custom', need_browser=True,
                       state={'_attempt': 3})
        r = E.checkin_site(s, browser=None)
        self.assertEqual(r.attempt, 3)

    def test_duration_is_recorded(self):
        r = E.checkin_site(self._browser_site(), browser=None)
        self.assertGreaterEqual(r.duration_ms, 0)

    def test_http_site_receives_cookie_header(self):
        """纯请求站点也要拿到 Cookie（由 _http_fetcher 注入到请求头）。

        这是之前最要命的缺口：HTTP 路径压根没用站点 Cookie，
        所以需要登录的站点永远只能拿到未登录页面 → 必然签到失败。
        """
        captured = {}

        def fake_fetch(method, url, data):
            return HttpResponse(status=200, text='已领取 铜币', url=url)

        s = [x for x in T.builtin_sites() if x.id == 'v2ex'][0]
        import app.engine as _e
        orig = _e._http_fetcher

        def spy_fetcher(site, proxy, cookie='', headers_override=None):
            captured['cookie'] = cookie
            # 渲染后的头由调用方以 headers_override 传入（不再就地改 site.headers）
            captured['headers'] = dict(headers_override
                                       if headers_override is not None
                                       else (site.headers or {}))
            return fake_fetch

        try:
            _e._http_fetcher = spy_fetcher
            _e.checkin_via_http(s, cookie_header='session=abc')
        finally:
            _e._http_fetcher = orig

        self.assertEqual(captured.get('cookie'), 'session=abc',
                         '站点 Cookie 必须被传给 fetcher')
        # 模板预置的请求头也应该在（NodeSeek 缺 Origin/Referer 会 403）
        self.assertIn('User-Agent', captured.get('headers') or {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
