"""代理策略的单测：全局「直连/代理」+ 站点级「跟随/直连/独立」。

为什么值得单独测：
    "这个站点到底走不走代理"是排查连不上时的第一个问题。
    以前只有一个全局 proxy 字段，靠"地址是否为空"表示直连 ——
    结果用户从模板抄来的占位符地址被当成真实代理，
    报错还是"域名解析失败"，看起来像 DNS 故障（实测踩过）。

    现在拆成显式模式：
        全局 direct / custom
        站点 inherit / direct / custom
    resolve_site_proxy 是唯一的判定入口，必须钉死。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import (PROXY_CUSTOM, PROXY_DIRECT, PROXY_INHERIT, AppConfig,
                        SiteConfig, effective_global_proxy,
                        resolve_site_proxy)  # noqa: E402

G = 'http://192.168.1.10:7890'


class TestResolveSiteProxy(unittest.TestCase):
    def _site(self, mode, url=''):
        return SiteConfig(id='x', name='X', proxy_mode=mode, proxy_url=url)

    def test_inherit_uses_global_when_global_is_custom(self):
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_INHERIT), G, PROXY_CUSTOM), G)

    def test_inherit_is_direct_when_global_is_direct(self):
        """全局选直连时，跟随全局的站点必须直连（哪怕全局地址栏还有旧值）。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_INHERIT), G, PROXY_DIRECT), '')

    def test_site_direct_overrides_global_proxy(self):
        """站点强制直连：全局配了代理也不走。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_DIRECT), G, PROXY_CUSTOM), '')

    def test_site_custom_overrides_global_direct(self):
        """站点独立代理：全局是直连也能单独走。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_CUSTOM, 'http://own:1'),
                               '', PROXY_DIRECT),
            'http://own:1')

    def test_site_custom_with_empty_url_is_direct(self):
        """选了"用这个代理"却没填地址 -> 直连，而不是崩掉或乱连。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_CUSTOM, ''), G, PROXY_CUSTOM), '')

    def test_blank_and_unknown_modes_fall_back_to_inherit(self):
        for mode in ('', '  ', 'wat', None):
            with self.subTest(mode=mode):
                s = SiteConfig(id='x', name='X', proxy_mode=mode or '')
                self.assertEqual(resolve_site_proxy(s, G, PROXY_CUSTOM), G)

    def test_mode_is_case_insensitive(self):
        s = SiteConfig(id='x', name='X', proxy_mode='DIRECT')
        self.assertEqual(resolve_site_proxy(s, G, PROXY_CUSTOM), '')

    def test_whitespace_in_url_is_trimmed(self):
        s = SiteConfig(id='x', name='X', proxy_mode=PROXY_CUSTOM,
                       proxy_url='  http://own:1  ')
        self.assertEqual(resolve_site_proxy(s, '', PROXY_DIRECT), 'http://own:1')


class TestEffectiveGlobalProxy(unittest.TestCase):
    def test_direct_mode_ignores_address(self):
        """这是"选了直连就该真直连"的关键：地址栏留着旧值也不生效。"""
        self.assertEqual(effective_global_proxy(G, PROXY_DIRECT), '')

    def test_custom_mode_returns_address(self):
        self.assertEqual(effective_global_proxy(G, PROXY_CUSTOM), G)

    def test_empty_address(self):
        self.assertEqual(effective_global_proxy('', PROXY_CUSTOM), '')
        self.assertEqual(effective_global_proxy('   ', PROXY_CUSTOM), '')

    def test_unknown_mode_treated_as_not_direct(self):
        """认不出的模式不应静默变成直连（否则代理会莫名失效）。"""
        self.assertEqual(effective_global_proxy(G, ''), G)
        self.assertEqual(effective_global_proxy(G, 'whatever'), G)


class TestAppConfigProxyRoundTrip(unittest.TestCase):
    def test_round_trip(self):
        cfg = AppConfig(proxy_mode=PROXY_CUSTOM, proxy=G)
        back = AppConfig.from_dict(cfg.to_dict())
        self.assertEqual(back.proxy_mode, PROXY_CUSTOM)
        self.assertEqual(back.proxy, G)

    def test_old_config_with_proxy_becomes_custom(self):
        """升级前的配置只有 proxy 字段：有地址就该继续走代理，不能静默变直连。"""
        back = AppConfig.from_dict({'version': 1, 'proxy': G})
        self.assertEqual(back.proxy_mode, PROXY_CUSTOM)
        self.assertEqual(effective_global_proxy(back.proxy, back.proxy_mode), G)

    def test_old_config_without_proxy_becomes_direct(self):
        back = AppConfig.from_dict({'version': 1, 'proxy': ''})
        self.assertEqual(back.proxy_mode, PROXY_DIRECT)

    def test_default_is_direct(self):
        self.assertEqual(AppConfig().proxy_mode, PROXY_DIRECT)


class TestSiteConfigProxyRoundTrip(unittest.TestCase):
    def test_round_trip(self):
        s = SiteConfig(id='x', name='X', proxy_mode=PROXY_CUSTOM,
                       proxy_url='http://own:1')
        back = SiteConfig.from_dict(s.to_dict())
        self.assertEqual(back.proxy_mode, PROXY_CUSTOM)
        self.assertEqual(back.proxy_url, 'http://own:1')

    def test_default_is_inherit(self):
        self.assertEqual(SiteConfig(id='x', name='X').proxy_mode, PROXY_INHERIT)

    def test_old_site_without_field_defaults_to_inherit(self):
        back = SiteConfig.from_dict({'id': 'x', 'name': 'X', 'kind': 'template'})
        self.assertEqual(back.proxy_mode, PROXY_INHERIT)


if __name__ == '__main__':
    unittest.main(verbosity=2)
