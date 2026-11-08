"""点「检查登录状态」之后，状态必须同时出现在两处：
  1. 站点卡片上的标签
  2. 展开表单里的「登录状态（Cookie）」那一行

踩过的坑：只调 loadSites() 的话，结构没变时它只更新卡片标签，
表单里那块还是「未检查」—— 用户明明看到下面写着"登录状态有效"，
上面却仍叫未检查，只能来问。
"""

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine as E                                   # noqa: E402
from app import secrets as S                                  # noqa: E402
from app import service as SV                                 # noqa: E402
from app.models import SiteConfig                             # noqa: E402

WEB = pathlib.Path(__file__).parent.parent / 'app' / 'web.py'


class UiWiring(unittest.TestCase):
    """前端接线：没有浏览器也能钉住的关键点。"""

    def setUp(self):
        self.html = WEB.read_text(encoding='utf-8')

    def test_form_status_is_rerenderable(self):
        """状态区要有独立容器，否则检查完只能整表重建（会丢用户输入）。"""
        self.assertIn("'<div id=\"ckstat-'", self.html)
        self.assertIn('function ckStatBody', self.html)

    def test_check_triggers_status_refresh(self):
        """checkCookie 里必须调用 refreshCookieStat。"""
        start = self.html.index('function checkCookie(')
        end = self.html.index('function refreshCookieStat', start)
        body = self.html[start:end]
        self.assertIn('refreshCookieStat(id)', body)
        self.assertIn('function refreshCookieStat', self.html)


class ServiceSide(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_ck_')
        self.svc = SV.Service(self.dir, version='t', env_key=S.generate_key())
        cfg = self.svc.load_config()
        cfg.auth_required = False
        cfg.sites = [SiteConfig(id='demo', name='Demo', kind='custom',
                                homepage='https://a.test/')]
        self.svc.save_config(cfg)
        self.svc.set_site_cookie('demo', 'a=1; b=2')

    def test_check_result_is_persisted(self):
        with mock.patch.object(E, 'check_cookie_valid',
                               return_value=(1, '登录状态有效')):
            res = self.svc.check_site_cookie('demo')
        self.assertEqual(res['status'], 1)
        info = self.svc.cookie_info('demo')
        self.assertEqual(info['status'], 1, '状态要能立刻读回')
        self.assertTrue(info['checked_at'])
        # 换一个 Service 实例（等于重新读盘）
        again = SV.Service(self.dir, version='t', env_key=S.generate_key())
        self.assertEqual(again.cookie_info('demo')['status'], 1,
                         '状态必须落盘，不能只活在内存里')

    def test_list_exposes_checked_at(self):
        """列表里要带 checked_at —— 界面靠它区分"没检查"和"检查了但判不出"。"""
        row = self.svc.list_sites()[0]
        self.assertIn('cookie_checked_at', row)
        self.assertIn('cookie_status', row)

    def test_unclear_result_keeps_zero_but_marks_checked(self):
        with mock.patch.object(E, 'check_cookie_valid',
                               return_value=(0, '无法判断')):
            self.svc.check_site_cookie('demo')
        row = self.svc.list_sites()[0]
        self.assertEqual(row['cookie_status'], 0)
        self.assertTrue(row['cookie_checked_at'], '检查过就要记下来，否则界面永远显示"未检查"')


if __name__ == '__main__':
    unittest.main(verbosity=2)
