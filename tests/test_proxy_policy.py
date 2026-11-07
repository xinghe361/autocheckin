"""代理策略的单测：每个需要联网的地方各自三选一。

为什么值得单独测：
    "这个站点到底走不走代理"是排查连不上时的第一个问题。
    以前只有一个全局 proxy 字段，靠"地址是否为空"表示直连 ——
    结果用户从模板抄来的占位符地址被当成真实代理，
    报错还是"域名解析失败"，看起来像 DNS 故障（实测踩过）。

    现在全局**没有开关**：设置里那栏只是"主代理地址"。
    站点 / 通知 / 模板同步 / WebDAV / AI 各自选
        直连（默认） / 主代理（inherit） / 自定义（custom）
    resolve_proxy 是唯一的判定入口，必须钉死。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import (PROXY_CUSTOM, PROXY_DIRECT, PROXY_INHERIT, AppConfig,
                        SiteConfig, effective_global_proxy, resolve_proxy,
                        resolve_site_proxy)  # noqa: E402

G = 'http://192.168.1.10:7890'


class TestResolveSiteProxy(unittest.TestCase):
    def _site(self, mode, url=''):
        return SiteConfig(id='x', name='X', proxy_mode=mode, proxy_url=url)

    def test_inherit_uses_global_when_global_is_custom(self):
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_INHERIT), G, PROXY_CUSTOM), G)

    def test_inherit_uses_main_proxy_regardless_of_legacy_global_mode(self):
        """选「主代理」就用主代理地址 —— 全局已经没有开关了。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_INHERIT), G, PROXY_DIRECT), G)

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

    def test_blank_and_unknown_modes_fall_back_to_direct(self):
        """空值/写错的值一律当直连：默认就该是"不折腾"。"""
        for mode in ('', '  ', 'wat', None):
            with self.subTest(mode=mode):
                s = SiteConfig(id='x', name='X', proxy_mode=mode or '')
                self.assertEqual(resolve_site_proxy(s, G, PROXY_CUSTOM), '')

    def test_mode_is_case_insensitive(self):
        s = SiteConfig(id='x', name='X', proxy_mode='DIRECT')
        self.assertEqual(resolve_site_proxy(s, G, PROXY_CUSTOM), '')

    def test_whitespace_in_url_is_trimmed(self):
        s = SiteConfig(id='x', name='X', proxy_mode=PROXY_CUSTOM,
                       proxy_url='  http://own:1  ')
        self.assertEqual(resolve_site_proxy(s, '', PROXY_DIRECT), 'http://own:1')


class TestResolveProxy(unittest.TestCase):
    """三选一的公共实现（站点 / 通知 / 同步 / WebDAV / AI 共用）。"""

    def test_direct_is_default_for_everything(self):
        for mode in ('', '  ', None, 'direct', 'DIRECT', 'wat'):
            with self.subTest(mode=mode):
                self.assertEqual(resolve_proxy(mode, 'http://own:1', G), '')

    def test_inherit_means_main_proxy(self):
        self.assertEqual(resolve_proxy('inherit', 'http://own:1', G), G)

    def test_custom_uses_own_url(self):
        self.assertEqual(resolve_proxy('custom', 'http://own:1', G),
                         'http://own:1')

    def test_custom_without_url_is_direct(self):
        self.assertEqual(resolve_proxy('custom', '', G), '')


class TestConfigDefaults(unittest.TestCase):
    """所有地方的"初始值"都必须是直连。"""

    def test_site_default_is_direct(self):
        self.assertEqual(SiteConfig(id='x', name='X').proxy_mode, PROXY_DIRECT)

    def test_webdav_and_ai_default_to_direct(self):
        from app.models import AiConfig, WebdavConfig
        self.assertEqual(WebdavConfig().proxy_mode, PROXY_DIRECT)
        self.assertEqual(AiConfig().proxy_mode, PROXY_DIRECT)

    def test_sync_proxy_default_is_direct(self):
        self.assertEqual(AppConfig().plugin_sync_proxy_mode, PROXY_DIRECT)

    def test_roundtrip_keeps_choices(self):
        from app.models import AiConfig, WebdavConfig
        wd = WebdavConfig.from_dict({'proxy_mode': PROXY_INHERIT,
                                     'proxy_url': ''})
        self.assertEqual(wd.proxy_mode, PROXY_INHERIT)
        ai = AiConfig.from_dict({'proxy_mode': PROXY_CUSTOM,
                                 'proxy_url': 'http://ai:1'})
        self.assertEqual(ai.proxy_mode, PROXY_CUSTOM)
        self.assertEqual(ai.proxy_url, 'http://ai:1')
        cfg = AppConfig.from_dict({'plugin_sync_proxy_mode': PROXY_INHERIT,
                                   'plugin_sync_proxy_url': ''})
        self.assertEqual(cfg.plugin_sync_proxy_mode, PROXY_INHERIT)


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

    def test_default_is_direct(self):
        """新建站点默认直连（用户要求：初始一律直连）。"""
        self.assertEqual(SiteConfig(id='x', name='X').proxy_mode, PROXY_DIRECT)

    def test_old_site_without_field_defaults_to_direct(self):
        back = SiteConfig.from_dict({'id': 'x', 'name': 'X', 'kind': 'template'})
        self.assertEqual(back.proxy_mode, PROXY_DIRECT)


if __name__ == '__main__':
    unittest.main(verbosity=2)
