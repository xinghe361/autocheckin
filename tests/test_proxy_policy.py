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
                        SiteConfig, resolve_proxy,
                        resolve_site_proxy)  # noqa: E402

G = 'http://192.168.1.10:7890'


class TestResolveSiteProxy(unittest.TestCase):
    def _site(self, mode, url=''):
        return SiteConfig(id='x', name='X', proxy_mode=mode, proxy_url=url)

    def test_inherit_uses_global_when_global_is_custom(self):
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_INHERIT), G), G)

    def test_inherit_uses_main_proxy_regardless_of_legacy_global_mode(self):
        """选「主代理」就用主代理地址 —— 全局已经没有开关了。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_INHERIT), G), G)

    def test_site_direct_overrides_global_proxy(self):
        """站点强制直连：全局配了代理也不走。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_DIRECT), G), '')

    def test_site_custom_overrides_global_direct(self):
        """站点独立代理：全局是直连也能单独走。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_CUSTOM, 'http://own:1'), ''),
            'http://own:1')

    def test_site_custom_with_empty_url_is_direct(self):
        """选了"用这个代理"却没填地址 -> 直连，而不是崩掉或乱连。"""
        self.assertEqual(
            resolve_site_proxy(self._site(PROXY_CUSTOM, ''), G), '')

    def test_blank_and_unknown_modes_fall_back_to_direct(self):
        """空值/写错的值一律当直连：默认就该是"不折腾"。"""
        for mode in ('', '  ', 'wat', None):
            with self.subTest(mode=mode):
                s = SiteConfig(id='x', name='X', proxy_mode=mode or '')
                self.assertEqual(resolve_site_proxy(s, G), '')

    def test_mode_is_case_insensitive(self):
        s = SiteConfig(id='x', name='X', proxy_mode='DIRECT')
        self.assertEqual(resolve_site_proxy(s, G), '')

    def test_whitespace_in_url_is_trimmed(self):
        s = SiteConfig(id='x', name='X', proxy_mode=PROXY_CUSTOM,
                       proxy_url='  http://own:1  ')
        self.assertEqual(resolve_site_proxy(s, ''), 'http://own:1')


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



class TestNoLegacyProxyMigration(unittest.TestCase):
    """不兼容老配置：旧的全局开关读到就丢，各功能一律从直连开始。

    以前有个全局 proxy_mode（direct/custom）决定所有出站请求走不走代理。
    现在它被拆成五处各自三选一，那个键已经没有任何含义 —— 不做迁移，
    免得"以前随手设过走代理"的机器升级后所有流量还默默走代理。
    「proxy」本身保留，它只是主代理地址的存放处，不填就不用。
    """

    def test_legacy_global_mode_is_ignored(self):
        cfg = AppConfig.from_dict({'proxy': 'http://g:1',
                                   'proxy_mode': 'custom'})
        self.assertFalse(hasattr(cfg, 'proxy_mode'), '旧的全局开关字段应已删除')
        self.assertEqual(cfg.plugin_sync_proxy_mode, PROXY_DIRECT)
        self.assertEqual(cfg.webdav.proxy_mode, PROXY_DIRECT)
        self.assertEqual(cfg.ai.proxy_mode, PROXY_DIRECT)

    def test_main_address_is_still_kept(self):
        """地址是"仓库"，留着不影响行为；谁选「主代理」才用得上。"""
        cfg = AppConfig.from_dict({'proxy': 'http://g:1'})
        self.assertEqual(cfg.proxy, 'http://g:1')

    def test_explicit_choice_is_respected(self):
        cfg = AppConfig.from_dict({
            'proxy': 'http://g:1',
            'plugin_sync_proxy_mode': 'inherit',
            'webdav': {'proxy_mode': 'custom', 'proxy_url': ''},
            'ai': {'proxy_mode': 'direct'},
        })
        self.assertEqual(cfg.plugin_sync_proxy_mode, PROXY_INHERIT)
        self.assertEqual(cfg.webdav.proxy_mode, PROXY_CUSTOM)
        self.assertEqual(cfg.ai.proxy_mode, PROXY_DIRECT)

    def test_garbage_mode_falls_back_to_direct(self):
        cfg = AppConfig.from_dict({'ai': {'proxy_mode': 'main'}})
        self.assertEqual(cfg.ai.proxy_mode, 'main')
        # 落盘时的值不在三选一里 -> 解析成直连，绝不偷偷走代理
        from app.models import resolve_proxy
        self.assertEqual(resolve_proxy(cfg.ai.proxy_mode, '', 'http://g:1'), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
