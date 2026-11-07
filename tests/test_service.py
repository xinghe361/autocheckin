"""service 单测：站点增删改、录制会话、备份接口（全部不依赖网络/浏览器）。"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S  # noqa: E402
from app import service as SV  # noqa: E402
from app.models import (ACTION_CLICK, ACTION_FILL, ACTION_GOTO,  # noqa: E402
                        NOTIFY_FAIL_ONLY, AppConfig, NotifyConfig, SiteConfig, Step)
from app.store import Store  # noqa: E402


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='autocheckin_svc_')
        self.svc = SV.Service(self.dir, version='1.0.0-test',
                              env_key=S.generate_key(), proxy='')
        # 站点模板全部来自外部 JSON。这里把三个内置模板 seed 进插件目录
        # 并加入站点，模拟"用户已同步安装"，保持与重构前一致的初始状态
        # （三个站点），让下面的测试不用逐个改。
        from _tpl_loader import seed_plugins
        seed_plugins(self.svc, 'v2ex', 'nodeseek', 'chiphell')
        for sid in ('v2ex', 'nodeseek', 'chiphell'):
            self.svc.install_template(sid)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestConfigAndOverview(ServiceCase):
    def test_fresh_install_has_no_sites(self):
        """全新安装是零站点：模板全部来自外部，不再内置。"""
        # 用一个新的 Service（不走 ServiceCase.setUp 的 seed），验证零站点
        d = tempfile.mkdtemp(prefix='ac_fresh_')
        try:
            svc = SV.Service(d, version='t', env_key=S.generate_key(), proxy='')
            self.assertEqual(svc.list_sites(), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_overview_counts(self):
        ov = self.svc.overview()
        self.assertEqual(ov['site_count'], 3)
        self.assertEqual(ov['enabled_count'], 3)
        self.assertFalse(ov['webdav_configured'])
        self.assertFalse(ov['ai_configured'])
        self.assertEqual(ov['version'], '1.0.0-test')

    def test_constructor_proxy_no_longer_overrides_config(self):
        """构造参数不再能顶替配置里的代理。

        以前 Service(proxy=...) 会在"配置里没代理"时顶上去，
        那是环境变量 PROXY 的入口。现在代理**只**来自配置
        （网页「设置 → 网络」），所以传了这个参数也不该生效。
        """
        svc = SV.Service(self.dir, env_key=S.generate_key(),
                         proxy='http://from-env:1')
        self.assertFalse(svc.overview()['proxy_configured'],
                         '代理只认配置；构造参数不该再生效')

    def test_config_proxy_is_reported(self):
        """配置里设了代理才应报告为已配置。"""
        cfg = self.svc.load_config()
        cfg.proxy = 'http://from-config:1'
        cfg.proxy_mode = 'custom'
        self.svc.save_config(cfg)
        self.assertTrue(self.svc.overview()['proxy_configured'])

    def test_list_sites_reports_runtime_fields(self):
        s = self.svc.list_sites()[0]
        for k in ('next_run_at', 'consecutive_failures', 'step_count',
                  'has_credentials', 'last_success_at', 'template_stale'):
            self.assertIn(k, s)


class TestTemplateStale(ServiceCase):
    """疑似模板过期：连败且失败原因基本是 rule 类（判定不了/步骤失败）。

    站点改版和网络抽风在界面上都表现为"连败"，但处置完全不同 ——
    前者该改模板，后者该查网络/Cookie。所以要用历史里的失败原因去区分。
    """

    def _set_fails(self, sid, n):
        cfg = self.svc.load_config()
        s = cfg.site(sid)
        s.state = dict(s.state or {})
        s.state['consecutive_failures'] = n
        self.svc.save_config(cfg)

    def _seed_failures(self, sid, kinds):
        """往历史里塞失败记录，kinds 是 error_kind 列表（旧→新）。"""
        import time as _t
        from app.history import get_history
        h = get_history(self.dir)
        now = int(_t.time())
        entries = []
        for i, k in enumerate(kinds):
            entries.append(h.make_entry(
                sid, sid.upper(), False, msg='失败', error_kind=k,
                ts=now - (len(kinds) - i) * 3600))
        h.append(entries)

    def _stale(self, sid):
        return [s for s in self.svc.list_sites() if s['id'] == sid][0]['template_stale']

    def test_rule_failures_mark_stale(self):
        self._set_fails('v2ex', 4)
        self._seed_failures('v2ex', ['unclear', 'unclear', 'step_failed', 'unclear'])
        self.assertTrue(self._stale('v2ex'))

    def test_network_failures_not_stale(self):
        self._set_fails('v2ex', 4)
        self._seed_failures('v2ex', ['timeout', 'refused', 'dns_error', 'timeout'])
        self.assertFalse(self._stale('v2ex'))

    def test_login_failures_not_stale(self):
        self._set_fails('v2ex', 3)
        self._seed_failures('v2ex', ['not_logged_in', 'not_logged_in', 'not_logged_in'])
        self.assertFalse(self._stale('v2ex'))

    def test_below_threshold_not_stale(self):
        self._set_fails('v2ex', 2)
        self._seed_failures('v2ex', ['unclear', 'unclear'])
        self.assertFalse(self._stale('v2ex'))

    def test_mixed_mostly_network_not_stale(self):
        """个别 rule 混在网络抖动里不该误报。"""
        self._set_fails('v2ex', 5)
        self._seed_failures('v2ex', ['timeout', 'unclear', 'timeout', 'refused', 'timeout'])
        self.assertFalse(self._stale('v2ex'))

    def test_success_clears_state(self):
        # 有失败历史但当前没有连败（即最近成功了），不该提示
        self._set_fails('v2ex', 0)
        self._seed_failures('v2ex', ['unclear', 'unclear', 'unclear'])
        self.assertFalse(self._stale('v2ex'))


class TestSiteCrud(ServiceCase):
    def test_upsert_creates_new_site(self):
        site = self.svc.upsert_site({'id': 'mysite', 'name': '我的站',
                                     'homepage': 'https://a.b/',
                                     'kind': 'custom', 'need_browser': True})
        self.assertEqual(site.id, 'mysite')
        self.assertIn('mysite', [s['id'] for s in self.svc.list_sites()])

    def test_upsert_requires_id(self):
        with self.assertRaises(ValueError):
            self.svc.upsert_site({'name': '没有 id'})

    def test_upsert_updates_existing(self):
        self.svc.upsert_site({'id': 'x', 'name': 'A'})
        self.svc.upsert_site({'id': 'x', 'name': 'B', 'daily_hour': 7})
        got = self.svc.get_site('x')
        self.assertEqual(got.name, 'B')
        self.assertEqual(got.daily_hour, 7)

    def test_password_encrypted_and_not_echoed(self):
        self.svc.upsert_site({'id': 'x', 'name': 'X',
                              'username': 'alice', 'password': 'secret123'})
        got = self.svc.get_site('x')
        self.assertEqual(got.username, 'alice')
        self.assertNotIn('secret123', got.password_enc, '密码不能明文落盘')
        self.assertEqual(self.svc.box.decrypt(got.password_enc), 'secret123')
        # 列表接口不能回显密码
        listed = [s for s in self.svc.list_sites() if s['id'] == 'x'][0]
        self.assertNotIn('password', listed)
        self.assertTrue(listed['has_credentials'])

    def test_empty_password_keeps_existing(self):
        """界面上密码框留空时不能把已存的密码清掉。"""
        self.svc.upsert_site({'id': 'x', 'name': 'X', 'password': 'orig'})
        self.svc.upsert_site({'id': 'x', 'name': 'X', 'password': ''})
        got = self.svc.get_site('x')
        self.assertEqual(self.svc.box.decrypt(got.password_enc), 'orig')

    def test_schedule_fields_are_saved(self):
        self.svc.upsert_site({
            'id': 'x', 'name': 'X', 'mode': 'success_based',
            'jitter_enabled': True, 'jitter_seconds': 600,
            'retry_count': 5, 'retry_interval_minutes': 10,
            'ai_after_failures': 4, 'ai_enabled': False,
        })
        got = self.svc.get_site('x')
        self.assertEqual(got.mode, 'success_based')
        self.assertTrue(got.jitter_enabled)
        self.assertEqual(got.jitter_seconds, 600)
        self.assertEqual(got.retry_count, 5)
        self.assertEqual(got.ai_after_failures, 4)
        self.assertFalse(got.ai_enabled)

    def test_steps_saved_from_dicts(self):
        self.svc.upsert_site({'id': 'x', 'name': 'X', 'steps': [
            {'action': 'goto', 'target': 'https://a.b/'},
            {'action': 'click', 'target': '#go'},
        ]})
        got = self.svc.get_site('x')
        self.assertEqual(len(got.steps), 2)
        self.assertIsInstance(got.steps[0], Step)

    def test_delete_site(self):
        self.svc.upsert_site({'id': 'x', 'name': 'X'})
        self.assertTrue(self.svc.delete_site('x'))
        self.assertIsNone(self.svc.get_site('x'))
        self.assertFalse(self.svc.delete_site('x'))

    def test_delete_clears_state(self):
        self.svc.upsert_site({'id': 'x', 'name': 'X'})
        st = self.svc.load_state()
        st.set_next_run_at('x', 12345)
        self.svc.store.save_state(st)
        self.svc.delete_site('x')
        self.assertEqual(self.svc.load_state().next_run_at('x'), 0)

    def test_toggle_enabled(self):
        self.svc.upsert_site({'id': 'x', 'name': 'X'})
        self.assertTrue(self.svc.set_site_enabled('x', False))
        self.assertFalse(self.svc.get_site('x').enabled)
        self.assertFalse(self.svc.set_site_enabled('nope', True))

    def test_empty_config_stays_empty(self):
        """配置文件存在但站点为空时，保持零站点（不再自动补内置）。"""
        store = Store(self.dir)
        store.save_config(AppConfig())          # 站点为空
        self.assertEqual(self.svc.list_sites(), [])


class TestRecording(ServiceCase):
    def test_start_requires_url(self):
        with self.assertRaises(ValueError):
            self.svc.start_recording('x', 'X', '')

    def test_record_and_save(self):
        self.svc.start_recording('new1', '新站', 'https://a.b/')
        n = self.svc.push_events('new1', [
            {'kind': 'click', 'info': {'tag': 'button', 'id': 'checkin'}},
            {'kind': 'input', 'info': {'tag': 'input', 'id': 'u'}, 'value': 'alice'},
        ])
        self.assertEqual(n, 2)
        status = self.svc.recording_status('new1')
        self.assertTrue(status['running'])
        self.assertEqual(status['event_count'], 2)
        actions = [s['action'] for s in status['steps']]
        self.assertEqual(actions[0], ACTION_GOTO)
        self.assertIn(ACTION_CLICK, actions)
        self.assertIn(ACTION_FILL, actions)

        out = self.svc.stop_recording('new1', save=True)
        self.assertTrue(out['saved'])
        site = self.svc.get_site('new1')
        self.assertEqual(site.kind, 'custom')
        self.assertEqual(site.homepage, 'https://a.b/')
        self.assertTrue(site.steps)
        # 录制的步骤含 click/fill，所以**算出来**需要浏览器 ——
        # 但不再硬编码到 site.need_browser 上（由 needs_browser() 依据
        # 步骤动作自动判断，这样纯 get/post 步骤的站点能走纯请求路径）
        from app.engine import needs_browser
        self.assertTrue(needs_browser(site), '含浏览器动作的录制站点需要浏览器')
        self.assertFalse(site.need_browser,
                         '不该把 need_browser 硬编码到配置里（会盖掉自动判断）')

    def test_events_ignored_without_session(self):
        self.assertEqual(self.svc.push_events('nope', [{'kind': 'click'}]), 0)

    def test_events_ignored_after_stop(self):
        self.svc.start_recording('s', 'S', 'https://a.b/')
        self.svc.stop_recording('s', save=False)
        self.assertEqual(self.svc.push_events('s', [{'kind': 'click'}]), 0)

    def test_append_merges_into_existing_steps(self):
        self.svc.upsert_site({'id': 's', 'name': 'S', 'kind': 'custom',
                              'homepage': 'https://a.b/', 'steps': [
                                  {'action': 'goto', 'target': 'https://a.b/'},
                                  {'action': 'click', 'target': '#first'}]})
        self.svc.start_recording('s', 'S', 'https://a.b/')
        self.svc.push_events('s', [
            {'kind': 'click', 'info': {'tag': 'button', 'id': 'second'}}])
        self.svc.stop_recording('s', save=True, append=True)
        site = self.svc.get_site('s')
        targets = [st.target for st in site.steps]
        self.assertIn('#first', targets)
        self.assertIn('#second', targets)
        gotos = [st for st in site.steps if st.action == ACTION_GOTO]
        self.assertEqual(len(gotos), 1, '重复的起始 goto 应被去掉')

    def test_replace_instead_of_append(self):
        self.svc.upsert_site({'id': 's', 'name': 'S', 'kind': 'custom',
                              'steps': [{'action': 'click', 'target': '#old'}]})
        self.svc.start_recording('s', 'S', 'https://a.b/')
        self.svc.push_events('s', [
            {'kind': 'click', 'info': {'tag': 'button', 'id': 'new'}}])
        self.svc.stop_recording('s', save=True, append=False)
        targets = [st.target for st in self.svc.get_site('s').steps]
        self.assertNotIn('#old', targets)
        self.assertIn('#new', targets)

    def test_stop_without_session_raises(self):
        with self.assertRaises(ValueError):
            self.svc.stop_recording('nope')

    def test_cancel_discards(self):
        self.svc.start_recording('s', 'S', 'https://a.b/')
        self.svc.push_events('s', [{'kind': 'click',
                                    'info': {'tag': 'button', 'id': 'x'}}])
        self.assertTrue(self.svc.cancel_recording('s'))
        self.assertIsNone(self.svc.get_site('s'), '取消不该创建站点')
        self.assertFalse(self.svc.cancel_recording('s'))

    def test_malformed_events_do_not_break_session(self):
        self.svc.start_recording('s', 'S', 'https://a.b/')
        n = self.svc.push_events('s', [
            {'kind': 'click', 'info': {'tag': 'button', 'id': 'ok'}},
            None,
            'not-a-dict',
            {},
        ])
        self.assertGreaterEqual(n, 1)
        self.assertTrue(self.svc.recording_status('s')['running'])

    def test_active_recordings_listed(self):
        self.svc.start_recording('a', 'A', 'https://a.b/')
        self.svc.start_recording('b', 'B', 'https://c.d/')
        self.assertEqual(sorted(self.svc.active_recordings()), ['a', 'b'])
        self.assertEqual(self.svc.overview()['active_recordings'],
                         sorted(['a', 'b']))


class TestRunNow(ServiceCase):
    def test_run_now_single_site(self):
        calls = []

        def fake_checkin(site, proxy, password, browser, cookie_header=None):
            calls.append(site.id)
            from app.notify import CheckinResult
            return CheckinResult(site.id, site.name, True, 'ok')

        def factory(svc):
            from app.runner import Runner
            return Runner(svc.store, box=svc.box, proxy='',
                          checkin_fn=fake_checkin,
                          notify_fn=lambda *a: {})

        svc = SV.Service(self.dir, env_key=S.generate_key(),
                         runner_factory=factory)
        out = svc.run_now('v2ex')
        self.assertEqual(calls, ['v2ex'])
        self.assertEqual(len(out['runs']), 1)
        self.assertTrue(out['runs'][0]['success'])

    def test_run_now_reports_summary(self):
        def factory(svc):
            from app.runner import Runner
            from app.notify import CheckinResult
            return Runner(svc.store, box=svc.box, proxy='',
                          checkin_fn=lambda s, p, pw, b, _ck=None: CheckinResult(
                              s.id, s.name, True, 'ok'),
                          notify_fn=lambda *a: {})
        svc = SV.Service(self.dir, env_key=S.generate_key(),
                         runner_factory=factory)
        out = svc.run_now('v2ex')
        self.assertIn('成功', out['summary'])


class TestBackupApi(ServiceCase):
    def test_backup_requires_url(self):
        with self.assertRaises(ValueError):
            self.svc.backup_now()

    def test_restore_requires_url(self):
        with self.assertRaises(ValueError):
            self.svc.restore_from_cloud()

    def test_test_webdav_requires_url(self):
        with self.assertRaises(ValueError):
            self.svc.test_webdav()

    def test_webdav_password_encrypted_and_used(self):
        cfg = self.svc.load_config()
        cfg.webdav.url = 'https://dav.example.com/dav/'
        cfg.webdav.username = 'u'
        cfg.webdav.password_enc = self.svc.box.encrypt('p')
        self.svc.save_config(cfg)
        client = self.svc.webdav_client(self.svc.load_config())
        self.assertEqual(client.password, 'p')
        self.assertTrue(client.root.endswith('/autocheckin/'))


class TestNotifyTest(ServiceCase):
    def test_test_notify_with_channel(self):
        sent = {}

        def fake_send(cfg, result, proxy=''):
            sent['channels'] = list(cfg.channels)
            sent['mode'] = cfg.mode
            return {'pushplus': '已推送'}

        orig = SV.notify_mod.send_all
        SV.notify_mod.send_all = fake_send
        try:
            out = self.svc.test_notify('pushplus')
        finally:
            SV.notify_mod.send_all = orig
        self.assertEqual(sent['channels'], ['pushplus'])
        self.assertIn('pushplus', out['report'])


class TestActionableErrors(ServiceCase):
    """"点了没反应"类问题的回归：没有渠道/没有模板时必须给出可操作的错误。"""

    def test_notify_test_without_channels_says_so(self):
        cfg = self.svc.load_config()
        cfg.notify.channels = []
        cfg.notify.pushplus_token = ''
        self.svc.save_config(cfg)
        with self.assertRaises(ValueError) as ctx:
            self.svc.test_notify()
        self.assertIn('通知渠道', str(ctx.exception))

    def test_install_missing_template_hints_sync(self):
        with self.assertRaises(ValueError) as ctx:
            self.svc.install_template('not-synced-yet')
        msg = str(ctx.exception)
        self.assertIn('同步', msg, '要告诉用户去哪儿把模板拉下来')



class TestGlobalProxyMode(ServiceCase):
    """「联网方式」选直连时，地址栏里残留的地址不该被任何功能偷偷用上。"""

    PROXY = 'http://10.0.0.1:7890'

    def _save(self, mode):
        cfg = self.svc.load_config()
        cfg.proxy = self.PROXY
        cfg.proxy_mode = mode
        self.svc.save_config(cfg)

    def test_direct_mode_ignores_leftover_address(self):
        self._save('direct')
        self.assertEqual(self.svc._sync_proxy(), '',
                         '直连时模板同步不该走代理')

        cfg = self.svc.load_config()
        cfg.webdav.url = 'https://dav.example.com/dav/'
        self.svc.save_config(cfg)
        client = self.svc.webdav_client(self.svc.load_config())
        self.assertEqual(client.proxy, '', '直连时 WebDAV 也不该走代理')

    def test_ai_models_respects_direct_mode(self):
        self._save('direct')
        cfg = self.svc.load_config()
        cfg.ai.api_key_enc = self.svc.box.encrypt('sk-test')
        self.svc.save_config(cfg)
        seen = {}

        def fake(ai, key, proxy):
            seen['proxy'] = proxy
            return {'ok': True, 'models': [{'id': 'deepseek-chat'}], 'status': 200}

        with mock.patch('app.deepseek.list_models', side_effect=fake):
            self.svc.list_ai_models()
        self.assertEqual(seen.get('proxy'), '', '直连时拉模型列表也不该走代理')

    def test_custom_mode_uses_address(self):
        self._save('custom')
        self.assertEqual(self.svc._sync_proxy(), self.PROXY)
        cfg = self.svc.load_config()
        cfg.webdav.url = 'https://dav.example.com/dav/'
        self.svc.save_config(cfg)
        client = self.svc.webdav_client(self.svc.load_config())
        self.assertEqual(client.proxy, self.PROXY)


if __name__ == '__main__':
    unittest.main(verbosity=2)
