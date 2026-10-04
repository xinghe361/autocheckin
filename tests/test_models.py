"""models / notify / templates 的单测。

重点验证：
  * 配置往返序列化不丢字段
  * 凭据在"导出给备份"时会被排除
  * 通知策略（全部/仅失败/仅成功/自定义/不通知）判定正确
  * 通知正文格式包含关键信息
  * 内置模板齐全且字段合法
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import notify as N  # noqa: E402
from app import templates as T  # noqa: E402
from app.models import (  # noqa: E402
    NOTIFY_ALL, NOTIFY_CUSTOM, NOTIFY_FAIL_ONLY, NOTIFY_NONE,
    NOTIFY_SUCCESS_ONLY, PROXY_INHERIT, AiConfig, AppConfig, NotifyConfig,
    SiteConfig, Step, WebdavConfig, default_config,
)
from app.schedule import MODE_DAILY, MODE_SUCCESS_BASED  # noqa: E402


class TestSiteConfigRoundTrip(unittest.TestCase):
    def test_full_round_trip(self):
        s = SiteConfig(
            id='x', name='X 站', enabled=False, kind='custom', template='v2ex',
            homepage='https://x.test/', need_browser=True, verify_ssl=False,
            mode=MODE_SUCCESS_BASED, daily_hour=7, daily_minute=25,
            jitter_enabled=True, jitter_seconds=1200,
            retry_enabled=True, retry_count=5, retry_interval_minutes=12,
            ai_enabled=False, ai_after_failures=7,
            notify=NOTIFY_FAIL_ONLY,
            steps=[Step(action='goto', target='https://x.test/checkin'),
                   Step(action='click', target='#btn', timeout_ms=9000)],
            success_keywords=['好的'], fail_keywords=['坏了'],
            username='u1', password_enc='enc', cookie_enc='ck',
            state={'consecutive_failures': 2},
        )
        back = SiteConfig.from_dict(s.to_dict())
        self.assertEqual(back.id, 'x')
        self.assertEqual(back.name, 'X 站')
        self.assertFalse(back.enabled)
        self.assertEqual(back.mode, MODE_SUCCESS_BASED)
        self.assertEqual(back.daily_hour, 7)
        self.assertEqual(back.daily_minute, 25)
        self.assertTrue(back.jitter_enabled)
        self.assertEqual(back.jitter_seconds, 1200)
        self.assertEqual(back.retry_count, 5)
        self.assertEqual(back.retry_interval_minutes, 12)
        self.assertFalse(back.ai_enabled)
        self.assertEqual(back.ai_after_failures, 7)
        self.assertEqual(back.notify, NOTIFY_FAIL_ONLY)
        self.assertEqual(len(back.steps), 2)
        self.assertEqual(back.steps[1].action, 'click')
        self.assertEqual(back.steps[1].target, '#btn')
        self.assertEqual(back.steps[1].timeout_ms, 9000)
        self.assertEqual(back.success_keywords, ['好的'])
        self.assertEqual(back.username, 'u1')
        self.assertEqual(back.password_enc, 'enc')
        self.assertEqual(back.state['consecutive_failures'], 2)
        self.assertFalse(back.verify_ssl)

    def test_exclude_secrets(self):
        s = SiteConfig(id='x', name='X', password_enc='SECRET', cookie_enc='CK')
        d = s.to_dict(include_secrets=False)
        self.assertNotIn('password_enc', d)
        self.assertNotIn('cookie_enc', d)

    def test_missing_fields_get_defaults(self):
        """备份文件可能来自旧版本/被手改，缺字段不能崩。"""
        back = SiteConfig.from_dict({'id': 'only-id'})
        self.assertEqual(back.id, 'only-id')
        self.assertEqual(back.name, 'only-id')
        self.assertTrue(back.enabled)
        self.assertEqual(back.retry_count, 3)
        self.assertEqual(back.steps, [])


class TestScheduleBridge(unittest.TestCase):
    def test_to_schedule_restores_state(self):
        s = SiteConfig(id='a', name='A', mode=MODE_SUCCESS_BASED,
                       state={'last_success_at': 1700000000,
                              'consecutive_failures': 4,
                              'retries_done': 2,
                              'ai_analyzed_for_streak': 3})
        sch = s.to_schedule()
        self.assertEqual(sch.mode, MODE_SUCCESS_BASED)
        self.assertEqual(sch.last_success_at, 1700000000)
        self.assertEqual(sch.consecutive_failures, 4)
        self.assertEqual(sch.retries_done, 2)
        self.assertEqual(sch.ai_analyzed_for_streak, 3)

    def test_apply_schedule_state_writes_back(self):
        s = SiteConfig(id='a', name='A')
        sch = s.to_schedule()
        sch.last_success_at = 123
        sch.consecutive_failures = 2
        s.apply_schedule_state(sch)
        self.assertEqual(s.state['last_success_at'], 123)
        self.assertEqual(s.state['consecutive_failures'], 2)
        # 再转回来应保持一致
        self.assertEqual(s.to_schedule().consecutive_failures, 2)

    def test_apply_keeps_other_state_keys(self):
        s = SiteConfig(id='a', name='A', state={'custom_key': 'keep'})
        s.apply_schedule_state(s.to_schedule())
        self.assertEqual(s.state['custom_key'], 'keep')


class TestAppConfig(unittest.TestCase):
    def test_round_trip(self):
        cfg = default_config()
        cfg.proxy = 'http://10.0.0.1:7890'
        cfg.notify.mode = NOTIFY_FAIL_ONLY
        cfg.notify.pushplus_token = 'pt'
        cfg.webdav.url = 'https://dav.example.com/dav/'
        cfg.ai.api_key_enc = 'enc'
        back = AppConfig.from_dict(cfg.to_dict())
        self.assertEqual(back.proxy, cfg.proxy)
        self.assertEqual(back.notify.mode, NOTIFY_FAIL_ONLY)
        self.assertEqual(back.notify.pushplus_token, 'pt')
        self.assertEqual(back.webdav.url, cfg.webdav.url)
        self.assertEqual(back.ai.api_key_enc, 'enc')
        self.assertEqual(len(back.sites), len(cfg.sites))

    def test_exclude_secrets_removes_credentials(self):
        cfg = default_config()
        cfg.notify.pushplus_token = 'SHOULD_STAY'      # 渠道 token 属于通知配置
        cfg.webdav.password_enc = 'WD_SECRET'
        cfg.ai.api_key_enc = 'AI_SECRET'
        d = cfg.to_dict(include_secrets=False)
        self.assertNotIn('password_enc', d['webdav'])
        self.assertNotIn('api_key_enc', d['ai'])

    def test_site_lookup(self):
        cfg = default_config()
        self.assertIsNotNone(cfg.site('v2ex'))
        self.assertIsNone(cfg.site('nope'))

    def test_default_config_has_three_builtin_sites(self):
        cfg = default_config()
        ids = sorted(s.id for s in cfg.sites)
        self.assertEqual(ids, ['chiphell', 'nodeseek', 'v2ex'])


class TestNotifyPolicy(unittest.TestCase):
    def res(self, success, site='v2ex'):
        return N.CheckinResult(site_id=site, site_name=site, success=success)

    def test_all(self):
        c = NotifyConfig(mode=NOTIFY_ALL)
        self.assertTrue(N.should_notify(self.res(True), c))
        self.assertTrue(N.should_notify(self.res(False), c))

    def test_fail_only(self):
        c = NotifyConfig(mode=NOTIFY_FAIL_ONLY)
        self.assertFalse(N.should_notify(self.res(True), c))
        self.assertTrue(N.should_notify(self.res(False), c))

    def test_success_only(self):
        c = NotifyConfig(mode=NOTIFY_SUCCESS_ONLY)
        self.assertTrue(N.should_notify(self.res(True), c))
        self.assertFalse(N.should_notify(self.res(False), c))

    def test_custom_sites(self):
        c = NotifyConfig(mode=NOTIFY_CUSTOM, custom_sites=['v2ex'])
        self.assertTrue(N.should_notify(self.res(True, 'v2ex'), c))
        self.assertFalse(N.should_notify(self.res(True, 'chiphell'), c))
        self.assertFalse(N.should_notify(self.res(False, 'chiphell'), c))

    def test_none(self):
        c = NotifyConfig(mode=NOTIFY_NONE)
        self.assertFalse(N.should_notify(self.res(True), c))
        self.assertFalse(N.should_notify(self.res(False), c))

    def test_unknown_mode_defaults_to_notify(self):
        """未知策略不能静默丢通知。"""
        c = NotifyConfig(mode='something-weird')
        self.assertTrue(N.should_notify(self.res(True), c))


class TestSiteNotifyOverride(unittest.TestCase):
    def test_site_override_wins(self):
        s = SiteConfig(id='a', name='A', notify_override=NOTIFY_FAIL_ONLY)
        self.assertEqual(s.effective_notify(NOTIFY_ALL), NOTIFY_FAIL_ONLY)

    def test_site_notify_used_when_no_override(self):
        s = SiteConfig(id='a', name='A', notify=NOTIFY_SUCCESS_ONLY)
        self.assertEqual(s.effective_notify(NOTIFY_ALL), NOTIFY_SUCCESS_ONLY)

    def test_falls_back_to_global(self):
        s = SiteConfig(id='a', name='A')
        self.assertEqual(s.effective_notify(NOTIFY_ALL), NOTIFY_ALL)


class TestChannelProxy(unittest.TestCase):
    """按渠道选择走不走代理（用户明确要求的能力）。"""

    GLOBAL = 'http://10.0.0.1:7890'

    def test_default_follows_global_proxy(self):
        c = NotifyConfig()
        self.assertEqual(N.channel_proxy(c, 'telegram', self.GLOBAL), self.GLOBAL)

    def test_channel_can_force_direct(self):
        """Telegram 走代理、企业微信必须直连 —— 这种组合要能配出来。"""
        c = NotifyConfig(channel_proxy={'wecom': {'mode': 'direct'}})
        self.assertEqual(N.channel_proxy(c, 'wecom', self.GLOBAL), '')
        self.assertEqual(N.channel_proxy(c, 'telegram', self.GLOBAL), self.GLOBAL)

    def test_channel_can_use_its_own_proxy(self):
        c = NotifyConfig(channel_proxy={
            'telegram': {'mode': 'custom', 'url': 'socks5://10.0.0.1:1080'}})
        self.assertEqual(N.channel_proxy(c, 'telegram', self.GLOBAL),
                         'socks5://10.0.0.1:1080')

    def test_channel_custom_url_without_scheme_normalized(self):
        c = NotifyConfig(channel_proxy={
            'pushplus': {'mode': 'custom', 'url': '10.0.0.1:8080'}})
        self.assertEqual(N.channel_proxy(c, 'pushplus', self.GLOBAL),
                         'http://10.0.0.1:8080')

    def test_notify_level_proxy_removed(self):
        """通知级代理已移除：没给渠道单独设置时就跟随主代理。

        用户指出"通知代理"那一层是多余的 —— 已经有主代理，
        每个渠道又能独立设置，中间那层没有存在意义。
        所以即使旧配置里还留着 proxy_mode / proxy_url，也不再参与判定。
        """
        c = NotifyConfig(proxy_mode='custom', proxy_url='http://notify:8080')
        self.assertEqual(N.channel_proxy(c, 'pushplus', self.GLOBAL),
                         self.GLOBAL,
                         '通知级代理不该再生效，应跟随主代理')

    def test_main_proxy_direct_means_direct(self):
        """主代理为空（选了直连）时，未单独设置的渠道就是直连。"""
        c = NotifyConfig(proxy_mode='direct')
        self.assertEqual(N.channel_proxy(c, 'pushplus', ''), '')

    def test_channel_setting_beats_main_proxy(self):
        """渠道单独设置优先于主代理（这是保留的能力）。"""
        c = NotifyConfig(channel_proxy={'telegram': {'mode': 'custom',
                                                     'url': 'http://t:1'},
                                        'wecom': {'mode': 'direct'}})
        self.assertEqual(N.channel_proxy(c, 'telegram', self.GLOBAL), 'http://t:1')
        self.assertEqual(N.channel_proxy(c, 'wecom', self.GLOBAL), '')
        self.assertEqual(N.channel_proxy(c, 'pushplus', self.GLOBAL), self.GLOBAL)

    def test_empty_main_proxy_stays_direct(self):
        c = NotifyConfig()
        self.assertEqual(N.channel_proxy(c, 'pushplus', ''), '')

    def test_mode_is_case_insensitive(self):
        c = NotifyConfig(channel_proxy={'telegram': {'mode': 'DIRECT'}})
        self.assertEqual(N.channel_proxy(c, 'telegram', self.GLOBAL), '')

    def test_no_override_key_is_safe(self):
        c = NotifyConfig(channel_proxy={})
        self.assertEqual(N.channel_proxy(c, 'unknown-channel', self.GLOBAL),
                         self.GLOBAL)


class TestNotifyConfigRoundTrip(unittest.TestCase):
    def test_channel_proxy_survives_round_trip(self):
        c = NotifyConfig(proxy_mode='custom', proxy_url='http://p:1',
                         channel_proxy={'wecom': {'mode': 'direct'}})
        back = NotifyConfig.from_dict(c.to_dict())
        self.assertEqual(back.proxy_mode, 'custom')
        self.assertEqual(back.proxy_url, 'http://p:1')
        self.assertEqual(back.channel_proxy['wecom']['mode'], 'direct')

    def test_legacy_config_without_new_fields(self):
        """旧配置文件没有这些字段，不能崩。"""
        back = NotifyConfig.from_dict({'mode': NOTIFY_ALL, 'pushplus_token': 't'})
        self.assertEqual(back.pushplus_token, 't')
        self.assertEqual(back.proxy_mode, PROXY_INHERIT)
        self.assertEqual(back.channel_proxy, {})

    def test_corrupt_channel_proxy_coerced(self):
        back = NotifyConfig.from_dict({'channel_proxy': 'not-a-dict'})
        self.assertEqual(back.channel_proxy, {})
        self.assertEqual(NotifyConfig.from_dict({'channels': 'x'}).channels, [])


class TestMessageFormat(unittest.TestCase):
    def test_success_message_has_site_and_time(self):
        r = N.CheckinResult(site_id='v2ex', site_name='V2EX', success=True,
                            message='已领取 8 铜币', attempt=2, duration_ms=1500)
        txt = N.format_message(r)
        self.assertIn('V2EX', txt)
        self.assertIn('已领取 8 铜币', txt)
        self.assertIn('第 2 次尝试', txt)
        self.assertIn('耗时 1.5s', txt)
        self.assertIn('✅', txt)

    def test_failure_message_explains_network_error(self):
        r = N.CheckinResult(site_id='x', site_name='X', success=False,
                            error_kind='tls_error')
        txt = N.format_message(r)
        self.assertIn('❌', txt)
        self.assertIn('证书', txt, '失败通知应给出可读原因')

    def test_ai_note_included(self):
        r = N.CheckinResult(site_id='x', site_name='X', success=False,
                            ai_note='把签到按钮选择器改成 #new-btn')
        self.assertIn('把签到按钮选择器改成 #new-btn', N.format_message(r))

    def test_title(self):
        r = N.CheckinResult(site_id='x', site_name='X', success=True)
        self.assertIn('成功', N.title_of(r))
        r2 = N.CheckinResult(site_id='x', site_name='X', success=False)
        self.assertIn('失败', N.title_of(r2))


class TestChannelDetection(unittest.TestCase):
    def test_none_configured(self):
        self.assertEqual(N.configured_channels(NotifyConfig()), [])

    def test_each_channel(self):
        c = NotifyConfig(pushplus_token='a')
        self.assertEqual(N.configured_channels(c), [N.CHANNEL_PUSHPLUS])
        c = NotifyConfig(wecom_webhook='https://qyapi.weixin.qq.com/x')
        self.assertEqual(N.configured_channels(c), [N.CHANNEL_WECOM])
        c = NotifyConfig(tg_bot_token='t', tg_chat_id='1')
        self.assertEqual(N.configured_channels(c), [N.CHANNEL_TELEGRAM])

    def test_serverchan_removed_from_channels(self):
        """Server 酱已从界面移除，不再算作可用渠道。

        旧配置里如果还留着 serverchan_key，也不该让它被发送 ——
        否则用户以为已经关掉了，实际还在推送。
        """
        self.assertNotIn(N.CHANNEL_SERVERCHAN, N.ALL_CHANNELS)
        self.assertEqual(N.configured_channels(NotifyConfig(serverchan_key='b')),
                         [], 'Server 酱不该再被识别为已配置渠道')

    def test_telegram_needs_both_fields(self):
        self.assertEqual(N.configured_channels(NotifyConfig(tg_bot_token='t')), [])
        self.assertEqual(N.configured_channels(NotifyConfig(tg_chat_id='1')), [])

    def test_send_all_skips_unconfigured(self):
        c = NotifyConfig(channels=[N.CHANNEL_PUSHPLUS, N.CHANNEL_TELEGRAM],
                         pushplus_token='a')
        report = N.send_all(c, N.CheckinResult('s', 'S', True), proxy='')
        self.assertEqual(report[N.CHANNEL_TELEGRAM], '跳过：未配置')

    def test_notify_respects_policy_before_sending(self):
        c = NotifyConfig(mode=NOTIFY_NONE, pushplus_token='a')
        self.assertEqual(N.notify(N.CheckinResult('s', 'S', True), c), {})

    def test_telegram_without_proxy_gives_clear_error(self):
        with self.assertRaises(N.HttpError) as ctx:
            N.send_telegram('tok', '1', 't', 'c', proxy='')
        self.assertEqual(ctx.exception.kind, 'no_proxy')
        self.assertIn('代理', str(ctx.exception))


class TestTemplates(unittest.TestCase):
    def test_all_templates_have_required_fields(self):
        for t in T.BUILTIN:
            for key in ('id', 'name', 'homepage', 'need_browser',
                        'success_keywords', 'fail_keywords'):
                self.assertIn(key, t, '%s 缺少 %s' % (t.get('id'), key))

    def test_ids_unique(self):
        ids = [t['id'] for t in T.BUILTIN]
        self.assertEqual(len(ids), len(set(ids)))

    def test_template_by_id(self):
        self.assertEqual(T.template_by_id('v2ex')['name'], 'V2EX 每日铜币')
        self.assertEqual(T.template_by_id('nope'), {})

    def test_builtin_sites_carry_notable_flags(self):
        """内置站点的浏览器/证书标记 —— 依据真实请求实测修正。

        曾经的判断：nodeseek 需要浏览器（以为 Cloudflare 挡死了）；
        chiphell 需要浏览器且证书链不完整需放宽校验。实测推翻：
          * NodeSeek：签到走 API /api/attendance，该端点不在 Cloudflare
            防线后面；只带 Cookie 会 403，补 Origin+Referer 后 200 且签到成功
          * Chiphell：带 Cookie 访问 forum.php 即签到（页面出现「用户组」）；
            证书链正常，无需 verify_ssl=False
        """
        by_id = {s.id: s for s in T.builtin_sites()}
        self.assertFalse(by_id['v2ex'].need_browser, 'V2EX 纯请求即可')
        self.assertFalse(by_id['nodeseek'].need_browser,
                         'NodeSeek 实测走 API 纯请求即可')
        self.assertFalse(by_id['chiphell'].need_browser,
                         'Chiphell 实测纯请求即可')
        self.assertTrue(by_id['chiphell'].verify_ssl,
                        'Chiphell 证书正常，不该放宽校验')
        # 内置站点要预置请求头：NodeSeek 缺 Origin/Referer 会 403
        self.assertTrue(by_id['nodeseek'].headers.get('Origin'))
        self.assertTrue(by_id['nodeseek'].headers.get('Referer'))

    def test_custom_site_defaults(self):
        s = T.generic_custom_site('my', '我的站', 'https://a.b/')
        self.assertEqual(s.kind, 'custom')
        self.assertTrue(s.need_browser)
        self.assertEqual(s.steps, [])
        self.assertTrue(s.verify_ssl)


if __name__ == '__main__':
    unittest.main(verbosity=2)
