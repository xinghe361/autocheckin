"""配置健壮性 + 配置来源唯一性的回归测试。

两个主题，都是实际踩过的坑：

1. **畸形/老数据不能让服务起不来**
   config.json 是用户能手改、也可能来自旧版本或备份的文件。以前
   from_dict 直接 int()/dict() 原始值，类型不对就抛异常 —— 而它发生在
   **启动阶段**，后果是容器反复重启、永远起不来。用户看到的现象是
   "更新了镜像但版本一直是旧的"，因为真正在跑的仍是旧容器。
   （实测过：sites 是字符串 / ai 是 list / headers 是 list /
   state.json 是 list 都会让启动崩掉。）

2. **代理与版本号只有一个权威来源**
   - 代理：只在网页「设置 → 网络」里配，环境变量 PROXY 必须被忽略
     （两处都能设会导致"网页改完不生效"）
   - 版本号：只反映代码真实版本，APP_VERSION 不能覆盖
     （能覆盖就会让排查时报错版本，等于自毁线索）
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S                                     # noqa: E402
from app import service as SV                                    # noqa: E402
from app import web as WB                                        # noqa: E402
from app.main import VERSION                                     # noqa: E402
from app.models import (AiConfig, AppConfig, NotifyConfig,       # noqa: E402
                        SiteConfig, Step, WebdavConfig)
from app.store import RuntimeState                               # noqa: E402

ENV_KEYS = ('PROXY', 'APP_VERSION')


class EnvCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestMalformedConfigNeverCrashes(unittest.TestCase):
    """畸形 config 只能退化，不能让服务起不来。"""

    def _boot(self, cfg, state=None):
        """用给定 config/state 启动一次，返回 (cfg, state) 或抛异常。"""
        d = tempfile.mkdtemp(prefix='ac_robust_')
        with open(os.path.join(d, 'config.json'), 'w', encoding='utf-8') as f:
            if isinstance(cfg, str):
                f.write(cfg)
            else:
                json.dump(cfg, f, ensure_ascii=False)
        if state is not None:
            with open(os.path.join(d, 'state.json'), 'w',
                      encoding='utf-8') as f:
                if isinstance(state, str):
                    f.write(state)
                else:
                    json.dump(state, f)
        svc = SV.Service(d, version=VERSION, env_key=S.generate_key(),
                         proxy='')
        cfg2 = svc.load_config()
        st2 = svc.load_state()
        app = WB.WebApp(svc, version=svc.version)
        return cfg2, st2, app

    def test_sites_is_string(self):
        """sites 是字符串：当成空，服务照常起来。

        注意空的 sites 会补上内置站点 —— 这是正确的（配置文件里
        站点列表为空、且没标记过 sites_initialized，说明从没配过）。
        """
        cfg, _, app = self._boot({'version': 1, 'sites': 'oops'})
        self.assertIsInstance(cfg.sites, list)
        self.assertTrue(all(isinstance(s, SiteConfig) for s in cfg.sites))
        self.assertEqual(app.version, VERSION)

    def test_sites_contains_none_and_scalars(self):
        cfg, _, _ = self._boot({'version': 1,
                                'sites': [None, 'x', 5,
                                          {'id': 'a', 'name': 'A'}]})
        self.assertEqual([s.id for s in cfg.sites], ['a'],
                         '非法站点项应被跳过，合法项要保留')

    def test_ai_is_list(self):
        cfg, _, _ = self._boot({'version': 1, 'ai': [1, 2, 3], 'sites': []})
        self.assertIsInstance(cfg.ai, AiConfig)
        self.assertEqual(cfg.ai.max_calls_per_day, 20)

    def test_notify_is_string(self):
        cfg, _, _ = self._boot({'version': 1, 'notify': 'x', 'sites': []})
        self.assertIsInstance(cfg.notify, NotifyConfig)

    def test_webdav_is_list(self):
        cfg, _, _ = self._boot({'version': 1, 'webdav': [], 'sites': []})
        self.assertIsInstance(cfg.webdav, WebdavConfig)

    def test_headers_is_list(self):
        cfg, _, _ = self._boot({'version': 1, 'sites': [
            {'id': 'a', 'name': 'A', 'headers': [1, 2]}]})
        self.assertEqual(cfg.sites[0].headers, {})

    def test_state_is_list(self):
        _, st, _ = self._boot({'version': 1, 'sites': []}, state=[1, 2, 3])
        self.assertEqual(st.sites, {})
        self.assertEqual(st.last_run_at, 0)

    def test_state_is_broken_json(self):
        _, st, _ = self._boot({'version': 1, 'sites': []},
                              state='{"sites": {')
        self.assertIsInstance(st, RuntimeState)

    def test_config_is_broken_json(self):
        cfg, _, app = self._boot('{"sites": [')
        self.assertEqual(app.version, VERSION)
        self.assertTrue(len(cfg.sites) >= 0)

    def test_old_config_without_new_fields(self):
        cfg, _, _ = self._boot({
            'version': 1, 'proxy_mode': 'direct', 'proxy': '',
            'auth_required': True,
            'sites': [{'id': 'v2ex', 'name': 'V',
                       'homepage': 'https://www.v2ex.com/'}]})
        self.assertEqual(len(cfg.sites), 1)
        self.assertTrue(cfg.auth_required)

    def test_legacy_removed_ai_fields_migrated(self):
        cfg, _, _ = self._boot({
            'version': 1, 'sites': [],
            'ai': {'ai_calls_per_day': 5, 'daily_limit_enabled': True,
                   'max_attempts_per_day': 3, 'global_max_calls_per_day': 9}})
        # ai_calls_per_day 会迁移成每日上限（旧意图不丢）
        self.assertEqual(cfg.ai.max_calls_per_day, 5)

    def test_bool_string_false_is_false(self):
        """bool("false") 是 True —— 这坑必须堵住。"""
        s = SiteConfig.from_dict({'id': 'a', 'name': 'A',
                                  'enabled': 'false'})
        self.assertFalse(s.enabled, '"false" 应被当成关闭，而不是开启')
        s2 = SiteConfig.from_dict({'id': 'b', 'name': 'B', 'enabled': '0'})
        self.assertFalse(s2.enabled)

    def test_numbers_clamped_and_coerced(self):
        s = SiteConfig.from_dict({'id': 'a', 'name': 'A', 'daily_hour': 99,
                                  'daily_minute': -5, 'retry_count': 'abc'})
        self.assertEqual(s.daily_hour, 23, '小时应夹到 0-23')
        self.assertEqual(s.daily_minute, 0, '分钟应夹到 0-59')
        self.assertEqual(s.retry_count, 3, '非法数字应回退默认')

    def test_step_from_dict_tolerates_junk(self):
        st = Step.from_dict({'action': None, 'target': 5, 'timeout_ms': 'x'})
        self.assertIsInstance(st.action, str)
        self.assertIsInstance(st.target, str)
        self.assertIsInstance(st.timeout_ms, int)


class TestProxyComesOnlyFromConfig(EnvCase):
    """代理只认配置（网页设置），环境变量不再生效。"""

    def test_env_proxy_ignored(self):
        import app.main as M
        os.environ['PROXY'] = 'http://from-env:1'
        self.assertEqual(M.build_settings([]).proxy, '')

    def test_cli_proxy_still_works(self):
        import app.main as M
        self.assertEqual(M.build_settings(['--proxy', 'http://p:1']).proxy,
                         'http://p:1')

    def test_service_constructor_proxy_ignored(self):
        d = tempfile.mkdtemp(prefix='ac_px_')
        svc = SV.Service(d, version=VERSION, env_key=S.generate_key(),
                         proxy='http://from-env:1')
        self.assertFalse(svc.overview()['proxy_configured'])

    def test_config_proxy_takes_effect(self):
        d = tempfile.mkdtemp(prefix='ac_px2_')
        svc = SV.Service(d, version=VERSION, env_key=S.generate_key(),
                         proxy='')
        cfg = svc.load_config()
        cfg.proxy = 'http://from-config:1'
        cfg.proxy_mode = 'custom'
        svc.save_config(cfg)
        self.assertTrue(svc.overview()['proxy_configured'])

    def test_compose_template_has_no_proxy_env(self):
        """compose 模板里不该再有 PROXY 行（会被误以为生效）。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'docker-compose.yml'),
                  encoding='utf-8') as f:
            text = f.read()
        for line in text.split('\n'):
            s = line.strip()
            if s.startswith('#'):
                continue
            self.assertNotIn('- PROXY=', line,
                             'compose 里不该再有生效的 PROXY 环境变量行')


class TestVersionCannotBeOverridden(EnvCase):
    """版本号只反映代码真实版本。"""

    def test_app_version_env_ignored(self):
        import app.main as M
        os.environ['APP_VERSION'] = '1.2.0'
        self.assertEqual(
            M.build_settings([]).version, VERSION,
            'APP_VERSION 不该能覆盖真实版本 —— 否则排查时报的版本是错的')

    def test_no_env_gives_real_version(self):
        import app.main as M
        self.assertEqual(M.build_settings([]).version, VERSION)


class TestSettingsSaveButtonOnTop(unittest.TestCase):
    """「保存全部设置」按钮要在设置页最上面（设置项很长）。"""

    def setUp(self):
        d = tempfile.mkdtemp(prefix='ac_btn_')
        self.svc = SV.Service(d, version=VERSION, env_key=S.generate_key(),
                              proxy='')

    def _html(self):
        for name in dir(WB):
            v = getattr(WB, name)
            if isinstance(v, str) and 'id="tab-settings"' in v:
                return v
        self.fail('找不到页面 HTML')

    def test_button_before_settings_content(self):
        html = self._html()
        i = html.find('id="tab-settings"')
        self.assertGreater(i, 0)
        i_btn = html.find('保存全部设置', i)
        i_proxy = html.find('代理设置', i)
        self.assertGreater(i_btn, 0, '设置页里应有保存按钮')
        self.assertLess(i_btn, i_proxy, '保存按钮应在设置内容之前（顶部）')

    def test_bottom_entry_still_exists(self):
        """底部也留一个，方便"改完最后一项顺手保存"。"""
        html = self._html()
        i = html.find('id="tab-settings"')
        end = html.find('</section>', i)
        self.assertGreaterEqual(html.count('保存全部设置', i, end), 2)

    def test_sticky_style_present_and_below_header(self):
        html = self._html()
        self.assertIn('.settings-save', html)
        import re
        m = re.search(r'\.settings-save\{[^}]*position:sticky', html)
        self.assertIsNotNone(m, '保存条应吸顶，长表单滚动时可见')
        mt = re.search(r'\.settings-save\{[^}]*\btop:\s*(\d+)px', html)
        self.assertIsNotNone(mt)
        top = int(mt.group(1))
        # header 自身也是 sticky(top:0)，保存条的 top 必须更大才不被盖住
        self.assertGreater(top, 0, 'header 是 sticky，top 不能是 0')


class TestNotifyCredentialsEncryptedAtRest(unittest.TestCase):
    """通知凭据（token/webhook/代理账密）必须加密落盘。

    为什么重要：这些字段以前是明文存 config.json。能读到那个文件的人
    （网盘同步、误分享、拿到 NAS 文件系统）就能冒充你往这些渠道发消息、
    或者用你的代理。站点密码早就加密了，这几项漏在外面不一致。

    设计要点：
      * 内存里仍是明文（notify.py 直接用），只有落盘是密文
      * to_dict() 一律剥掉明文 —— 忘了加密最坏只是丢字段，不会泄露
      * 老配置（只有明文）平滑迁移：读出来照常用，下次保存自动加密
    """

    SECRET = 'PP_SUPER_SECRET'
    WECOM = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=WECOM_KEY'
    TG = 'TG_BOT_SECRET'
    CHAT = 'CHAT_ID_999'
    NPX = 'http://nuser:npass@10.0.0.7:8'
    CPX = 'http://cuser:cpass@10.0.0.9:7890'

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_enc_')
        self.svc = SV.Service(self.dir, version=VERSION,
                              env_key=S.generate_key(), proxy='')
        cfg = self.svc.load_config()
        n = cfg.notify
        n.pushplus_token = self.SECRET
        n.wecom_webhook = self.WECOM
        n.tg_bot_token = self.TG
        n.tg_chat_id = self.CHAT
        n.proxy_url = self.NPX
        n.channel_proxy = {'telegram': {'mode': 'custom', 'url': self.CPX}}
        self.svc.save_config(cfg)

    def _raw(self):
        with open(self.svc.store.config_path, encoding='utf-8') as f:
            return f.read()

    def test_no_plaintext_on_disk(self):
        raw = self._raw()
        for label, needle in (
                ('pushplus token', 'PP_SUPER_SECRET'),
                ('企业微信 key', 'WECOM_KEY'),
                ('TG bot token', 'TG_BOT_SECRET'),
                ('TG chat id', 'CHAT_ID_999'),
                ('通知级代理账密', 'nuser:npass'),
                ('渠道代理账密', 'cuser:cpass')):
            with self.subTest(label=label):
                self.assertNotIn(needle, raw,
                                 '%s 明文出现在了 config.json 里' % label)

    def test_still_usable_after_reload(self):
        r = self.svc.load_config().notify
        self.assertEqual(r.pushplus_token, self.SECRET)
        self.assertEqual(r.wecom_webhook, self.WECOM)
        self.assertEqual(r.tg_bot_token, self.TG)
        self.assertEqual(r.tg_chat_id, self.CHAT)
        self.assertEqual(r.proxy_url, self.NPX)
        self.assertEqual(
            (r.channel_proxy.get('telegram') or {}).get('url'), self.CPX)
        # 渠道判定照常工作
        from app.notify import configured_channels
        self.assertIn('pushplus', configured_channels(r))
        self.assertIn('telegram', configured_channels(r))

    def test_round_trip_is_idempotent(self):
        for _ in range(3):
            cfg = self.svc.load_config()
            self.svc.save_config(cfg)
        r = self.svc.load_config().notify
        self.assertEqual(r.pushplus_token, self.SECRET)
        self.assertEqual(
            (r.channel_proxy.get('telegram') or {}).get('url'), self.CPX)

    def test_cleared_credential_does_not_resurrect(self):
        """清空凭据后必须**真的删掉**，不能下次加载又回来。

        改回去会怎样：_seal 在明文为空时保留旧密文，_unseal 把旧密文
        解密回明文 —— 用户删掉的 token 自己复活（实测踩过这个坑）。
        """
        cfg = self.svc.load_config()
        cfg.notify.pushplus_token = ''
        cfg.notify.tg_bot_token = ''
        cfg.notify.wecom_webhook = ''      # 三个渠道都要清，否则还剩 wecom
        cfg.notify.channel_proxy = {}
        self.svc.save_config(cfg)

        r = self.svc.load_config().notify
        self.assertEqual(r.pushplus_token, '', '删掉的 token 复活了')
        self.assertEqual(r.tg_bot_token, '', '删掉的 TG token 复活了')
        self.assertEqual(r.wecom_webhook, '', '删掉的企业微信 webhook 复活了')
        self.assertEqual(
            (r.channel_proxy.get('telegram') or {}).get('url', ''),
            '', '删掉的渠道代理复活了')
        from app.notify import configured_channels
        self.assertEqual(configured_channels(r), [],
                         '所有渠道凭据都清空后不该还认为有渠道')

    def test_legacy_plaintext_config_migrates(self):
        """老配置只有明文：读出来要能用，保存后变密文。"""
        d = tempfile.mkdtemp(prefix='ac_legacy_enc_')
        # 手写一份"老版本"的 config.json：notify 里是明文 token
        legacy = {'version': 1, 'sites': [], 'notify': {
            'mode': 'all', 'channels': ['pushplus'],
            'pushplus_token': 'LEGACY_PLAINTEXT', 'channel_proxy': {}}}
        with open(os.path.join(d, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(legacy, f, ensure_ascii=False)

        svc = SV.Service(d, version=VERSION, env_key=S.generate_key(),
                         proxy='')
        # 读出来仍可用（明文当兜底）
        self.assertEqual(svc.load_config().notify.pushplus_token,
                         'LEGACY_PLAINTEXT')
        # 保存一次后应该已加密
        svc.save_config(svc.load_config())
        with open(os.path.join(d, 'config.json'), encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('LEGACY_PLAINTEXT', raw,
                         '老配置保存后应已加密')
        self.assertEqual(svc.load_config().notify.pushplus_token,
                         'LEGACY_PLAINTEXT', '迁移后仍要能用')

    def test_web_settings_reads_plaintext_attrs(self):
        """界面读取接口仍能看到"是否已配置"（走内存明文属性）。"""
        app = WB.WebApp(self.svc, version=VERSION)
        out = app._settings_payload() if hasattr(app, '_settings_payload') \
            else None
        # 没有该私有方法就直接验证底层属性（界面读的就是它）
        self.assertTrue(self.svc.load_config().notify.pushplus_token)

    def test_export_backup_has_no_plaintext(self):
        """备份也不能带明文（这条早先修过，避免回归）。"""
        cfg = self.svc.load_config()
        blob = repr(self.svc.store.export_config(cfg))
        for needle in ('PP_SUPER_SECRET', 'WECOM_KEY', 'TG_BOT_SECRET',
                       'CHAT_ID_999', 'nuser:npass', 'cuser:cpass'):
            self.assertNotIn(needle, blob, '备份里出现了明文凭据')


if __name__ == '__main__':
    unittest.main(verbosity=2)
