"""Cookie 管理（Service 层）与登录态巡检的单测。

这些逻辑直接决定"签到能不能用"，所以重点覆盖：
  * 保存 Cookie：各种粘贴格式都要能存进去并归一化
  * 解析失败要明确报错，不能静默存垃圾
  * cookie_info 绝不能泄露 Cookie 内容
  * 清除要干净
  * 巡检只在"有效→失效"那一刻通知一次（不重复刷屏）
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import notify as N  # noqa: E402
from app import secrets as S  # noqa: E402
from app.models import SiteConfig, default_config  # noqa: E402
from app.service import Service  # noqa: E402


def make_service(tmp):
    """构造一个不启浏览器、不联网的 Service。

    必须用合法的 Fernet key（32 字节 urlsafe base64），随手编一个字符串
    会在 SecretBox 构造时就抛"密钥格式不正确"，把测试掩盖掉。
    """
    return Service(tmp, version='test', env_key=S.generate_key())


class TestSetSiteCookie(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        cfg = self.svc.load_config()
        cfg.sites.append(SiteConfig(id='s1', name='站点一', kind='custom'))
        self.svc.save_config(cfg)

    def test_saves_cookie_string(self):
        r = self.svc.set_site_cookie('s1', 'session=abc; sid=xyz')
        self.assertTrue(r['ok'])
        self.assertEqual(r['count'], 2)
        self.assertEqual(r['format'], 'Cookie 串')
        self.assertIn('session', r['names'])

    def test_saves_curl_and_normalizes(self):
        curl = ("curl 'https://a.com/' -H 'cookie: session=abc; sid=xyz'")
        r = self.svc.set_site_cookie('s1', curl)
        self.assertEqual(r['count'], 2)
        self.assertIn('cURL', r['format'])

        # 存进去的应该是归一化后的 "a=1; b=2" 形式
        site = self.svc.get_site('s1')
        raw = self.svc.box.try_decrypt(site.cookie_enc, '')
        self.assertIn('session=abc', raw)
        self.assertIn('sid=xyz', raw)
        self.assertNotIn('curl', raw)

    def test_bad_input_raises_and_does_not_save(self):
        with self.assertRaises(ValueError):
            self.svc.set_site_cookie('s1', '这里没有任何 Cookie')
        site = self.svc.get_site('s1')
        self.assertEqual(site.cookie_enc, '')

    def test_curl_without_cookie_raises(self):
        """不含 cookie 的 cURL 必须报错，不能把别的请求头当成 Cookie。"""
        with self.assertRaises(ValueError):
            self.svc.set_site_cookie('s1', "curl 'https://a.com/' -H 'accept: text/html'")

    def test_unknown_site_raises(self):
        with self.assertRaises(ValueError):
            self.svc.set_site_cookie('nope', 'a=1; b=2')

    def test_records_metadata(self):
        before = int(time.time())
        self.svc.set_site_cookie('s1', 'a=1; b=2')
        site = self.svc.get_site('s1')
        self.assertGreaterEqual(site.cookie_updated_at, before)
        self.assertEqual(site.cookie_source, 'Cookie 串')
        self.assertEqual(site.cookie_status, 0)   # 换 Cookie 后旧结论作废

    def test_replacing_resets_check_status(self):
        self.svc.set_site_cookie('s1', 'a=1; b=2')
        cfg = self.svc.load_config()
        cfg.site('s1').cookie_status = 2            # 假装之前判定为失效
        self.svc.save_config(cfg)

        self.svc.set_site_cookie('s1', 'a=1; b=2; c=3')
        self.assertEqual(self.svc.get_site('s1').cookie_status, 0)


class TestCookieInfo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        cfg = self.svc.load_config()
        cfg.sites.append(SiteConfig(id='s1', name='站点一', kind='custom'))
        self.svc.save_config(cfg)

    def test_no_cookie(self):
        info = self.svc.cookie_info('s1')
        self.assertFalse(info['has_cookie'])
        self.assertEqual(info['count'], 0)
        self.assertEqual(info['names'], [])

    def test_with_cookie_reports_names_only(self):
        self.svc.set_site_cookie('s1', 'session=SECRETVALUE; sid=ANOTHER')
        info = self.svc.cookie_info('s1')
        self.assertTrue(info['has_cookie'])
        self.assertEqual(info['count'], 2)
        self.assertIn('session', info['names'])
        # 关键：绝不能把值带出来
        blob = repr(info)
        self.assertNotIn('SECRETVALUE', blob)
        self.assertNotIn('ANOTHER', blob)

    def test_unknown_site_raises(self):
        with self.assertRaises(ValueError):
            self.svc.cookie_info('nope')


class TestClearCookie(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        cfg = self.svc.load_config()
        cfg.sites.append(SiteConfig(id='s1', name='站点一', kind='custom'))
        self.svc.save_config(cfg)

    def test_clear_removes_everything(self):
        self.svc.set_site_cookie('s1', 'a=1; b=2')
        self.assertTrue(self.svc.clear_site_cookie('s1'))
        info = self.svc.cookie_info('s1')
        self.assertFalse(info['has_cookie'])
        self.assertEqual(info['count'], 0)
        self.assertEqual(info['updated_at'], 0)
        self.assertEqual(info['source'], '')

    def test_clear_unknown_returns_false(self):
        self.assertFalse(self.svc.clear_site_cookie('nope'))


class TestMaybeCheckCookies(unittest.TestCase):
    """登录态巡检：重点是"只报一次"，避免每天重复推同一条。

    注意：default_config() 自带 v2ex / nodeseek / chiphell 三个内置站点，
    所以这里直接用内置站点，不要再 append 一个自己的 —— 否则
    sites[0] 会是内置站点，断言就会对着错的站点做。
    """

    SID = 'nodeseek'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        cfg = self.svc.load_config()
        site = cfg.site(self.SID)
        site.cookie_enc = self.svc.box.encrypt('a=1; b=2')
        site.enabled = True
        cfg.notify.channels = ['pushplus']
        cfg.notify.pushplus_token = 'token'
        self.svc.save_config(cfg)

        # 打桩：假装检查结果由测试控制
        self.result = (1, '有效')
        self.sent = []

        import app.engine as E
        self._orig_check = E.check_cookie_valid
        E.check_cookie_valid = lambda site, raw, proxy='': self.result

        import app.notify as NN
        self._orig_send = NN.send_alert
        NN.send_alert = lambda ncfg, title, body, proxy='': (
            self.sent.append((title, body)) or {'ok': True})

    def tearDown(self):
        import app.engine as E
        import app.notify as NN
        E.check_cookie_valid = self._orig_check
        NN.send_alert = self._orig_send

    def test_all_valid_no_alert(self):
        self.result = (1, '有效')
        out = self.svc.maybe_check_cookies(force=True)
        self.assertEqual(out['checked'], 1)
        self.assertEqual(out['newly_failed'], 0)
        self.assertEqual(self.sent, [])

    def test_new_failure_sends_one_alert(self):
        self.result = (2, '登录状态已失效')
        out = self.svc.maybe_check_cookies(force=True)
        self.assertEqual(out['newly_failed'], 1)
        self.assertEqual(len(self.sent), 1)
        title, body = self.sent[0]
        self.assertIn('失效', title)
        # 通知里要能看出是哪个站点（用站点名，不是 id）
        site_name = self.svc.get_site(self.SID).name
        self.assertIn(site_name, body)
        self.assertIn(self.SID, body)

    def test_repeated_failure_does_not_resend(self):
        """已经是失效状态，再巡检不该重复推送。"""
        self.result = (2, '登录状态已失效')
        self.svc.maybe_check_cookies(force=True)
        self.assertEqual(len(self.sent), 1)

        # 第二次：状态没变，不该再推
        self.svc.maybe_check_cookies(force=True)
        self.assertEqual(len(self.sent), 1, '重复失效不该重复通知')

    def test_recovers_then_fails_again_notifies_again(self):
        """失效 → 修好 → 再失效，应该再报一次（这是有价值的信息）。"""
        self.result = (2, '失效')
        self.svc.maybe_check_cookies(force=True)
        self.assertEqual(len(self.sent), 1)

        self.result = (1, '有效')
        self.svc.maybe_check_cookies(force=True)

        self.result = (2, '失效')
        self.svc.maybe_check_cookies(force=True)
        self.assertEqual(len(self.sent), 2)

    def test_respects_interval_without_force(self):
        self.result = (1, '有效')
        self.svc.maybe_check_cookies(force=True)
        out = self.svc.maybe_check_cookies()          # 未 force
        self.assertTrue(out.get('skipped'))

    def test_no_channels_is_safe_and_records_time(self):
        """没有任何通知渠道时：不能报错，也不能每分钟都重跑巡检。

        注意 configured_channels 的判定是"填了 token 就算配了这个渠道"，
        所以只清空 channels 列表是不够的 —— token 也要清掉才真的算"没渠道"。
        """
        cfg = self.svc.load_config()
        cfg.notify.channels = []
        cfg.notify.pushplus_token = ''
        self.svc.save_config(cfg)
        self.assertFalse(N.has_any_channel(cfg.notify),
                         '前置条件不成立的话本用例就没意义了')

        out = self.svc.maybe_check_cookies(force=True)
        self.assertTrue(out.get('skipped'))
        # 仍要记录时间，否则调度每分钟都会重跑。
        # 注意 _cookie_check_at 存在 state.meta 里，不是顶层键
        # （以前这里写 st.get(...)，RuntimeState 没有 get 方法，所以一直是假通过）。
        st = self.svc.load_state()
        self.assertGreater(st.meta.get('_cookie_check_at', 0), 0)

    def test_alert_when_token_present_even_if_channels_empty(self):
        """填了 token 但没勾选渠道 —— 仍视为"有渠道"，失效时要能推出去。

        这是 configured_channels 的设计：用户填了凭据就说明想用。
        """
        cfg = self.svc.load_config()
        cfg.notify.channels = []
        cfg.notify.pushplus_token = 'token'
        self.svc.save_config(cfg)
        self.assertTrue(N.has_any_channel(cfg.notify))

        self.result = (2, '失效')
        out = self.svc.maybe_check_cookies(force=True)
        self.assertEqual(out['newly_failed'], 1)
        self.assertEqual(len(self.sent), 1)

    def test_undecryptable_cookie_counts_as_failure(self):
        """密钥变了导致解不开 —— 这也必须报，否则用户完全看不出原因。"""
        cfg = self.svc.load_config()
        cfg.site(self.SID).cookie_enc = 'not-a-valid-fernet-token'
        self.svc.save_config(cfg)
        out = self.svc.maybe_check_cookies(force=True)
        self.assertEqual(out['newly_failed'], 1)
        self.assertEqual(len(self.sent), 1)

    def test_disabled_site_is_skipped(self):
        cfg = self.svc.load_config()
        cfg.site(self.SID).enabled = False
        self.svc.save_config(cfg)
        out = self.svc.maybe_check_cookies(force=True)
        self.assertEqual(out['checked'], 0)

    def test_site_without_cookie_is_skipped(self):
        cfg = self.svc.load_config()
        cfg.site(self.SID).cookie_enc = ''
        self.svc.save_config(cfg)
        out = self.svc.maybe_check_cookies(force=True)
        self.assertEqual(out['checked'], 0)


class TestCookieStatusInListSites(unittest.TestCase):
    def test_list_sites_exposes_status_but_not_content(self):
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        cfg = svc.load_config()
        # 用内置站点（default_config 自带 3 个）
        cfg.site('nodeseek').cookie_enc = svc.box.encrypt('a=SECRET; b=2')
        svc.save_config(cfg)

        sites = svc.list_sites()
        by_id = {s['id']: s for s in sites}
        s = by_id['nodeseek']
        self.assertTrue(s['has_cookie'])
        self.assertIn('cookie_status', s)
        self.assertIn('headers', s)
        # 列表接口不能带出任何 Cookie 内容
        self.assertNotIn('SECRET', repr(sites))
        self.assertNotIn('cookie_enc', s)


if __name__ == '__main__':
    unittest.main(verbosity=2)
