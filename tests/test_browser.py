"""browser 单测：用假 PageDriver 验证步骤执行与结果判定。

不需要真的启动浏览器。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import browser as B  # noqa: E402
from app.models import (ACTION_ASSERT_TEXT, ACTION_CLICK, ACTION_EXTRACT,  # noqa: E402
                        ACTION_FILL, ACTION_GOTO, ACTION_WAIT, ACTION_WAIT_FOR,
                        SiteConfig, Step)


class FakeDriver(B.PageDriver):
    """记录调用并返回预设内容。"""

    def __init__(self, text='', fail_on=None, fail_message='boom'):
        self.calls = []
        self._text = text
        self._fail_on = fail_on or []
        self._fail_message = fail_message

    def _maybe_fail(self, action, target):
        for f in self._fail_on:
            if f == action or f == target:
                raise RuntimeError(self._fail_message)

    def goto(self, url, timeout_ms):
        self.calls.append(('goto', url))
        self._maybe_fail('goto', url)

    def click(self, selector, timeout_ms):
        self.calls.append(('click', selector))
        self._maybe_fail('click', selector)

    def fill(self, selector, value, timeout_ms):
        self.calls.append(('fill', selector, value))
        self._maybe_fail('fill', selector)

    def wait(self, ms):
        self.calls.append(('wait', ms))

    def wait_for(self, selector, timeout_ms):
        self.calls.append(('wait_for', selector))
        self._maybe_fail('wait_for', selector)

    def text(self):
        return self._text

    def content(self):
        return self._text


class TestRunSteps(unittest.TestCase):
    def test_goto_and_click_success_by_keyword(self):
        d = FakeDriver(text='签到成功，获得 5 邪恶值')
        out = B.run_steps(d, [
            Step(action=ACTION_GOTO, target='https://a.b/'),
            Step(action=ACTION_CLICK, target='#sign'),
        ], ['签到成功'], ['请先登录'])
        self.assertTrue(out.success)
        self.assertEqual(out.hit, 'success')
        self.assertEqual(d.calls[0], ('goto', 'https://a.b/'))
        self.assertEqual(d.calls[1], ('click', '#sign'))

    def test_already_done_beats_success_keyword(self):
        d = FakeDriver(text='今日已签到，明天再来')
        out = B.run_steps(d, [Step(action=ACTION_GOTO, target='https://a.b/')],
                          ['签到'], [], ['今日已签到'])
        self.assertTrue(out.success)
        self.assertEqual(out.hit, 'already')

    def test_fail_keyword_beats_success_keyword(self):
        d = FakeDriver(text='请先登录后签到')
        out = B.run_steps(d, [Step(action=ACTION_GOTO, target='https://a.b/')],
                          ['签到'], ['请先登录'])
        self.assertFalse(out.success)
        self.assertEqual(out.hit, 'fail')

    def test_fill_uses_placeholders(self):
        d = FakeDriver(text='签到成功')
        out = B.run_steps(d, [
            Step(action=ACTION_FILL, target='#user', value='{username}'),
            Step(action=ACTION_FILL, target='#pw', value='{password}'),
        ], ['签到成功'], [], variables={'username': 'alice', 'password': 'secret'})
        self.assertTrue(out.success)
        self.assertEqual(d.calls[0], ('fill', '#user', 'alice'))
        self.assertEqual(d.calls[1], ('fill', '#pw', 'secret'))

    def test_placeholder_in_target_rendered(self):
        d = FakeDriver(text='OK')
        B.run_steps(d, [Step(action=ACTION_GOTO, target='{homepage}checkin')],
                    [], [], variables={'homepage': 'https://a.b/'})
        self.assertEqual(d.calls[0], ('goto', 'https://a.b/checkin'))

    def test_wait_and_wait_for(self):
        d = FakeDriver(text='签到成功')
        B.run_steps(d, [
            Step(action=ACTION_WAIT, timeout_ms=1500),
            Step(action=ACTION_WAIT_FOR, target='#done'),
        ], ['签到成功'], [])
        self.assertIn(('wait', 1500), d.calls)
        self.assertIn(('wait_for', '#done'), d.calls)

    def test_required_step_failure_fails_whole_flow(self):
        d = FakeDriver(text='随便什么', fail_on=['click'])
        out = B.run_steps(d, [
            Step(action=ACTION_GOTO, target='https://a.b/'),
            Step(action=ACTION_CLICK, target='#sign', optional=False),
        ], ['签到成功'], [])
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'step_failed')
        self.assertIn('click', out.message)

    def test_optional_step_failure_continues(self):
        d = FakeDriver(text='签到成功', fail_on=['#popup'])
        out = B.run_steps(d, [
            Step(action=ACTION_CLICK, target='#popup', optional=True),
            Step(action=ACTION_CLICK, target='#sign'),
        ], ['签到成功'], [])
        self.assertTrue(out.success, out.message)

    def test_step_failure_but_page_says_already(self):
        """点击失败但页面显示已领过 —— 不能误报失败（否则会无意义重试）。"""
        d = FakeDriver(text='今日已签到', fail_on=['click'])
        out = B.run_steps(d, [Step(action=ACTION_CLICK, target='#sign')],
                          ['签到'], [], ['今日已签到'])
        self.assertTrue(out.success)
        self.assertEqual(out.hit, 'already')

    def test_no_keywords_falls_back_to_step_success(self):
        d = FakeDriver(text='页面没有任何关键词')
        out = B.run_steps(d, [Step(action=ACTION_GOTO, target='https://a.b/')],
                          ['不会命中'], [])
        self.assertTrue(out.success)
        self.assertEqual(out.hit, 'step_ok')

    def test_no_keywords_and_failed_step_is_unclear(self):
        d = FakeDriver(text='无关键词', fail_on=['#x'])
        out = B.run_steps(d, [Step(action=ACTION_WAIT_FOR, target='#x', optional=True)],
                          ['不会命中'], [])
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'unclear')

    def test_empty_steps_is_failure(self):
        d = FakeDriver(text='')
        out = B.run_steps(d, [], ['x'], [])
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'no_steps')

    def test_unknown_action_skipped(self):
        d = FakeDriver(text='签到成功')
        out = B.run_steps(d, [Step(action='teleport', target='x')],
                          ['签到成功'], [])
        self.assertTrue(out.success)
        self.assertTrue(any('未知动作' in t for t in out.trace))

    def test_assert_text_pass_and_fail(self):
        d = FakeDriver(text='包含关键内容 ABC')
        out = B.run_steps(d, [Step(action=ACTION_ASSERT_TEXT, target='ABC',
                                   optional=True)], [], [])
        self.assertTrue(out.success)

        d2 = FakeDriver(text='没有那个词')
        out2 = B.run_steps(d2, [Step(action=ACTION_ASSERT_TEXT, target='XYZ',
                                     optional=True)], [], [])
        self.assertEqual(out2.error_kind, 'unclear')

    def test_extract_stores_value(self):
        d = FakeDriver(text='令牌是 12345')
        out = B.run_steps(d, [Step(action=ACTION_EXTRACT, value='tok')],
                          [], [], variables={})
        self.assertIn('tok', out.vars)
        self.assertEqual(out.vars['tok'], '令牌是 12345')

    def test_text_exception_does_not_crash(self):
        """读不到页面内容时不能宣称成功 —— 只能报"无法确认"。"""
        class BoomText(FakeDriver):
            def text(self):
                raise RuntimeError('页面已关闭')
        d = BoomText()
        out = B.run_steps(d, [Step(action=ACTION_GOTO, target='https://a.b/')],
                          ['x'], [])
        self.assertFalse(out.success, '拿不到页面内容时不该报成功')
        self.assertEqual(out.error_kind, 'page_unreadable')
        self.assertIn('无法读取页面内容', out.message)

    def test_empty_but_readable_page_can_fall_back_to_steps(self):
        """页面能读到但内容为空 → 仍可用步骤结果兜底。"""
        d = FakeDriver(text='')
        out = B.run_steps(d, [Step(action=ACTION_GOTO, target='https://a.b/')],
                          ['不会命中'], [])
        self.assertTrue(out.success)
        self.assertEqual(out.hit, 'step_ok')

    def test_trace_records_actions(self):
        d = FakeDriver(text='签到成功')
        out = B.run_steps(d, [
            Step(action=ACTION_GOTO, target='https://a.b/'),
            Step(action=ACTION_CLICK, target='#s'),
        ], ['签到成功'], [])
        self.assertTrue(any('goto' in t for t in out.trace))
        self.assertTrue(any('click' in t for t in out.trace))


class TestBrowserRunner(unittest.TestCase):
    def test_builtin_template_without_steps_uses_homepage(self):
        """内置浏览器模板没有录制步骤时，至少打开主页让挑战通过。"""
        site = SiteConfig(id='nodeseek', name='NodeSeek', kind='template',
                          template='nodeseek', homepage='https://www.nodeseek.com/board',
                          success_keywords=['手气'])
        runner = B.BrowserRunner()
        captured = {}

        class FakeDriverCls:
            def __init__(self, page, timeout_default=20000):
                self.page = page

        # 用一个最小假浏览器替换真实 Playwright
        class FakePage:
            def goto(self, url, timeout=None, wait_until=None):
                captured['goto'] = url
            def wait_for_timeout(self, ms):
                captured['wait'] = ms
            def inner_text(self, sel):
                return '手气不错'
            def content(self):
                return '手气不错'

        class FakeContext:
            def new_page(self):
                return FakePage()
            def close(self):
                pass

        class FakeBrowser:
            contexts = []
            def new_context(self, **kw):
                captured['ignore_https_errors'] = kw.get('ignore_https_errors')
                return FakeContext()

        import app.browser as mod
        orig_pw_driver = mod.PlaywrightDriver
        mod.PlaywrightDriver = mod.PlaywrightDriver  # 保持不变
        # 注入假浏览器时，必须一并声明"它是当前线程创建的"。
        # 否则线程亲和检查会认为线程变了，把假浏览器丢弃去加载真 Playwright。
        import threading as _th
        runner._browser = FakeBrowser()
        runner._owner_thread = _th.get_ident()
        try:
            out = runner.run(site)
        finally:
            runner._browser = None
            runner._owner_thread = None
        self.assertEqual(captured.get('goto'), 'https://www.nodeseek.com/board')
        self.assertTrue(out.success, out.message)

    def test_no_steps_and_no_homepage_fails_clearly(self):
        site = SiteConfig(id='x', name='X', kind='template', template='',
                          homepage='')
        runner = B.BrowserRunner()
        out = runner.run(site)
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'no_steps')

    def test_ignore_https_errors_follows_verify_ssl(self):
        site = SiteConfig(id='chiphell', name='C', kind='template',
                          template='chiphell', homepage='https://www.chiphell.com/',
                          verify_ssl=False)
        captured = {}

        class FakePage:
            def goto(self, *a, **k):
                pass
            def wait_for_timeout(self, ms):
                pass
            def inner_text(self, sel):
                return '已签到'
            def content(self):
                return '已签到'

        class FakeContext:
            def new_page(self):
                return FakePage()
            def close(self):
                pass

        class FakeBrowser:
            contexts = []
            def new_context(self, **kw):
                captured.update(kw)
                return FakeContext()

        runner = B.BrowserRunner()
        # 同 test_builtin_template...：注入假浏览器需一并声明线程归属
        import threading as _th
        runner._browser = FakeBrowser()
        runner._owner_thread = _th.get_ident()
        runner.run(site)
        self.assertTrue(captured.get('ignore_https_errors'),
                        'verify_ssl=False 时浏览器也要忽略证书错误')


class TestBrowserAvailable(unittest.TestCase):
    def test_returns_bool(self):
        self.assertIsInstance(B.browser_available(), bool)


if __name__ == '__main__':
    unittest.main(verbosity=2)
