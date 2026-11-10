"""UI 巡检逮到的三个问题，钉死，别再回来。

1. 通知策略下拉把内部枚举值露在界面上（fail（仅失败））
2. 窄屏（375px）导航按钮文字被挤成竖排
3. 没有步骤的自定义站点：一个请求都不发，却报"HTTP 200 空页面"
"""

import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine as E                                   # noqa: E402
from app.models import SiteConfig                             # noqa: E402

WEB = pathlib.Path(__file__).parent.parent / 'app' / 'web.py'


class TestNotifyModeLabel(unittest.TestCase):
    def test_dropdown_shows_chinese_only(self):
        src = WEB.read_text(encoding='utf-8')
        # 旧写法：m + '（' + mlabels[m] + '）'
        self.assertNotIn("(m + '（' + mlabels[m] + '）')", src,
                         '下拉不该把枚举值拼进显示文本')
        self.assertIn('var txt = mlabels[m] || m;', src)


class TestNarrowScreenNav(unittest.TestCase):
    """窄屏下按钮文字不能逐字换行 —— 那是布局塌了，不是"紧凑"。"""

    def setUp(self):
        self.src = WEB.read_text(encoding='utf-8')

    def test_nav_button_does_not_wrap(self):
        block = self.src[self.src.index('nav button{'):]
        block = block[:block.index('}')]
        self.assertIn('white-space:nowrap', block)

    def test_nav_can_wrap_to_second_line(self):
        block = self.src[self.src.index('nav{'):]
        block = block[:block.index('}')]
        self.assertIn('flex-wrap:wrap', block)

    def test_header_wraps_instead_of_squeezing(self):
        block = self.src[self.src.index('header{'):]
        block = block[:block.index('}')]
        self.assertIn('flex-wrap:wrap', block)


class TestEmptyStepsSite(unittest.TestCase):
    """空步骤的站点必须有话直说，不能报成"站点返回空页面"。"""

    def _run(self, site, status=200, text=''):
        orig = E._http_fetcher
        calls = []

        def fake(site_, proxy, cookie_header='', headers_override=None):
            def fetch(method, url, data=None):
                calls.append(url)
                return E.HttpResponse(status=status, text=text, url=url)
            return fetch

        E._http_fetcher = fake
        try:
            out = E.checkin_via_http(site, cookie_header='a=1')
        finally:
            E._http_fetcher = orig
        return out, calls

    def test_no_homepage_reports_no_steps(self):
        site = SiteConfig(id='x', name='X', kind='custom', homepage='')
        out, calls = self._run(site)
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'no_steps')
        self.assertIn('还没有签到步骤', out.message)
        self.assertEqual(calls, [], '没有步骤就不该发请求')

    def test_homepage_is_visited_as_default_step(self):
        """有主页就先访问主页 —— "访问即签到"的站点能直接跑。"""
        site = SiteConfig(id='x', name='X', kind='custom',
                          homepage='https://a.test/', success_keywords=['已签到'])
        out, calls = self._run(site, text='今日已签到')
        self.assertEqual(calls, ['https://a.test/'])
        self.assertTrue(out.success, out.message)

    def test_empty_body_is_not_reported_as_http_200_page(self):
        """真发过请求、但正文为空 —— 这时才用"正文是空的"描述。"""
        site = SiteConfig(id='x', name='X', kind='custom',
                          homepage='https://a.test/')
        out, calls = self._run(site, text='')
        self.assertEqual(len(calls), 1)
        self.assertIn('正文是空的', out.message)



class TestFirstRunLogin(unittest.TestCase):
    """首次设置口令时不该出现"忘了口令怎么办"—— 还没设过，忘了什么。"""

    def test_forgot_hint_hidden_on_setup(self):
        src = WEB.read_text(encoding='utf-8')
        self.assertIn('id="loginForgot"', src)
        self.assertIn("state.needs_setup ? 'hint hide' : 'hint'", src)


class TestCookieSavedFeedback(unittest.TestCase):
    """保存 Cookie 后的提示会被列表重建冲掉，得存一份等重绘后贴回。"""

    def test_save_cookie_keeps_pending_message(self):
        src = WEB.read_text(encoding='utf-8')
        start = src.index('function saveCookie(')
        body = src[start:src.index('function checkCookie(', start)]
        self.assertIn('CK_PENDING_MSG[id]', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
