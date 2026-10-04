"""runner / store / secrets / webdav 的单测。

全部用注入的假实现，不依赖网络、浏览器、真实时钟。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import runner as R  # noqa: E402
from app import secrets as S  # noqa: E402
from app import store as ST  # noqa: E402
from app import webdav as W  # noqa: E402
from app.models import (  # noqa: E402
    NOTIFY_ALL, NOTIFY_FAIL_ONLY, NOTIFY_NONE, NOTIFY_SUCCESS_ONLY, AiConfig,
    AppConfig, NotifyConfig, SiteConfig, WebdavConfig)
from app.notify import CheckinResult  # noqa: E402
from app.schedule import MODE_DAILY  # noqa: E402


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='autocheckin_test_')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------- secrets

class TestSecrets(TempDirCase):
    def test_checked_decrypt_distinguishes_cases(self):
        box = S.SecretBox(S.generate_key())
        # 未配置 → 空明文且无错误
        self.assertEqual(box.decrypt_checked(''), ('', ''))
        # 正常 → 有明文无错误
        plain, err = box.decrypt_checked(box.encrypt('pw'))
        self.assertEqual(plain, 'pw')
        self.assertEqual(err, '')
        # 密钥不对 → 空明文但有明确错误
        other = S.SecretBox(S.generate_key())
        plain2, err2 = other.decrypt_checked(box.encrypt('pw'))
        self.assertEqual(plain2, '')
        self.assertIn('密钥', err2)

    def test_generate_and_round_trip(self):
        box = S.SecretBox(S.generate_key())
        self.assertTrue(box.enabled)
        token = box.encrypt('my-password')
        self.assertNotIn('my-password', token, '密文里不能出现明文')
        self.assertEqual(box.decrypt(token), 'my-password')

    def test_empty_stays_empty(self):
        box = S.SecretBox(S.generate_key())
        self.assertEqual(box.encrypt(''), '')
        self.assertEqual(box.decrypt(''), '')

    def test_wrong_key_raises_not_silent_empty(self):
        a = S.SecretBox(S.generate_key())
        b = S.SecretBox(S.generate_key())
        token = a.encrypt('secret')
        with self.assertRaises(S.SecretError):
            b.decrypt(token)
        self.assertEqual(b.try_decrypt(token, 'fallback'), 'fallback')

    def test_key_file_created_with_600(self):
        key = S.load_or_create_key(self.dir)
        path = os.path.join(self.dir, S.KEY_FILENAME)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(S.load_or_create_key(self.dir), key)
        if os.name == 'posix':
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600, '密钥文件权限应为 600，实际 %o' % mode)
        else:
            self.skipTest('Windows 的 os.chmod 只影响只读位，无法验证 600')

    def test_env_key_preferred_and_no_file_written(self):
        key = S.generate_key()
        got = S.load_or_create_key(self.dir, env_key=key)
        self.assertEqual(got, key)
        self.assertFalse(os.path.exists(os.path.join(self.dir, S.KEY_FILENAME)),
                         '给了环境变量就不该再落盘密钥文件')

    def test_invalid_key_rejected(self):
        with self.assertRaises(S.SecretError):
            S.SecretBox('not-a-valid-key')

    def test_plain_fallback_without_key(self):
        box = S.SecretBox(None)
        self.assertFalse(box.enabled)
        token = box.encrypt('pw')
        self.assertTrue(token.startswith(S.PLAIN_PREFIX))
        self.assertEqual(box.decrypt(token), 'pw')

    def test_decrypt_encrypted_without_key_raises(self):
        encrypted = S.SecretBox(S.generate_key()).encrypt('pw')
        box = S.SecretBox(None)
        with self.assertRaises(S.SecretError):
            box.decrypt(encrypted)

    def test_mask(self):
        box = S.SecretBox(S.generate_key())
        self.assertEqual(box.mask('abcdefg'), 'abc****')
        self.assertEqual(box.mask(''), '')
        self.assertEqual(box.mask('ab'), '**')


# ----------------------------------------------------------------------- store

class TestStore(TempDirCase):
    def test_round_trip_config(self):
        st = ST.Store(self.dir)
        cfg = AppConfig(proxy='http://p:1')
        cfg.sites = [SiteConfig(id='a', name='A', password_enc='enc')]
        st.save_config(cfg)
        back = st.load_config()
        self.assertEqual(back.proxy, 'http://p:1')
        self.assertEqual(back.sites[0].password_enc, 'enc')

    def test_config_file_permission_600(self):
        st = ST.Store(self.dir)
        st.save_config(AppConfig())
        if os.name != 'posix':
            self.skipTest('Windows 的 os.chmod 只影响只读位，无法验证 600')
        mode = os.stat(st.config_path).st_mode & 0o777
        self.assertEqual(mode, 0o600, '配置文件含凭据，权限应为 600，实际 %o' % mode)

    def test_private_write_requests_600(self):
        """跨平台地验证"确实请求了 600"（用假的 os.open 捕获 mode）。"""
        import app.store as mod
        captured = {}
        real_open = os.open

        def spy_open(path, flags, mode=0o777):
            captured['mode'] = mode
            return real_open(path, flags, mode)

        mod.os.open = spy_open
        try:
            mod.atomic_write_json(os.path.join(self.dir, 'p.json'), {'a': 1},
                                  private=True)
        finally:
            mod.os.open = real_open
        self.assertEqual(captured.get('mode'), 0o600)

    def test_missing_config_returns_defaults(self):
        st = ST.Store(self.dir)
        cfg = st.load_config()
        self.assertEqual(sorted(s.id for s in cfg.sites),
                         ['chiphell', 'nodeseek', 'v2ex'])

    def test_corrupt_config_is_quarantined_not_fatal(self):
        st = ST.Store(self.dir)
        with open(st.config_path, 'w', encoding='utf-8') as f:
            f.write('{ this is not json')
        cfg = st.load_config()                 # 不应抛异常
        self.assertIsInstance(cfg, AppConfig)
        broken = [p for p in os.listdir(self.dir) if '.broken.' in p]
        self.assertTrue(broken, '损坏的配置应被另存备查')

    def test_atomic_write_leaves_no_tmp(self):
        st = ST.Store(self.dir)
        st.save_config(AppConfig())
        self.assertFalse(os.path.exists(st.config_path + '.tmp'))

    def test_state_round_trip(self):
        st = ST.Store(self.dir)
        s = ST.RuntimeState()
        s.set_next_run_at('v2ex', 12345)
        s.bump_ai_calls('v2ex', '2026-01-01')
        st.save_state(s)
        back = st.load_state()
        self.assertEqual(back.next_run_at('v2ex'), 12345)
        self.assertEqual(back.ai_calls_today('v2ex', '2026-01-01'), 1)

    def test_ai_calls_reset_on_new_day(self):
        s = ST.RuntimeState()
        s.bump_ai_calls('x', '2026-01-01')
        self.assertEqual(s.ai_calls_today('x', '2026-01-02'), 0)
        s.bump_ai_calls('x', '2026-01-02')
        self.assertEqual(s.ai_calls_today('x', '2026-01-02'), 1)

    def test_due_sites(self):
        s = ST.RuntimeState()
        s.set_next_run_at('a', 100)
        s.set_next_run_at('b', 300)
        self.assertEqual(sorted(s.due_sites(200)), ['a'])


# --------------------------------------------------------------------- webdav

class TestWebdavHelpers(unittest.TestCase):
    def test_normalize_adds_scheme_and_slash(self):
        self.assertEqual(W.normalize_base_url('dav.example.com/dav'),
                         'https://dav.example.com/dav/')

    def test_normalize_encodes_chinese(self):
        got = W.normalize_base_url('http://10.0.0.9:5005/我的 备份/')
        self.assertIn('%E6%88%91%E7%9A%84', got)
        self.assertIn('%20', got)
        self.assertTrue(got.endswith('/'))

    def test_normalize_does_not_double_encode(self):
        """已编码的地址不能被二次编码成 %25。"""
        once = W.normalize_base_url('http://a.b/我的/')
        twice = W.normalize_base_url(once)
        self.assertEqual(once, twice)
        self.assertNotIn('%25', twice)

    def test_normalize_rejects_empty(self):
        with self.assertRaises(ValueError):
            W.normalize_base_url('')
        with self.assertRaises(ValueError):
            W.normalize_base_url('   ')

    def test_client_builds_expected_urls(self):
        c = W.WebdavClient('https://dav.example.com/dav')
        self.assertEqual(c.root, 'https://dav.example.com/dav/autocheckin/')
        self.assertEqual(c.url_of(W.META_FILE),
                         'https://dav.example.com/dav/autocheckin/backup.json')

    def test_backup_payload_shape(self):
        p = W.backup_payload({'a': 1}, {'b': 2}, '1.0.0')
        self.assertEqual(p['version'], W.BACKUP_VERSION)
        self.assertEqual(p['app'], 'autocheckin')
        self.assertEqual(p['config'], {'a': 1})
        self.assertEqual(p['state'], {'b': 2})
        self.assertIn('saved_at', p)


class TestWebdavFlow(unittest.TestCase):
    """用假的 request 替换真实网络，验证建目录/上传/下载顺序。"""

    def setUp(self):
        self.calls = []
        self.responses = []
        self._orig_request = W.request

        def fake_request(method, url, cfg=None, global_proxy='', data=None,
                         cookie_jar=None):
            self.calls.append((method, url, data))
            if self.responses:
                st, text = self.responses.pop(0)
            else:
                st, text = 200, ''
            from app.httpclient import HttpResponse
            return HttpResponse(status=st, text=text, url=url, headers={})

        W.request = fake_request

    def tearDown(self):
        W.request = self._orig_request

    def client(self):
        return W.WebdavClient('https://dav.example.com/dav', 'u', 'p')

    def test_backup_does_mkcol_then_put(self):
        r = W.do_backup(self.client(), {'x': 1}, {'y': 2})
        methods = [c[0] for c in self.calls]
        self.assertEqual(methods[0], 'MKCOL')
        self.assertIn('PUT', methods)
        self.assertTrue(r['ok'])
        # PUT 的地址应在 autocheckin/ 目录下
        put_url = [c[1] for c in self.calls if c[0] == 'PUT'][0]
        self.assertIn('/autocheckin/backup.json', put_url)

    def test_mkcol_405_is_ok(self):
        self.responses = [(405, '')]           # 目录已存在
        c = self.client()
        self.assertTrue(c.mkcol(c.root))

    def test_put_409_gives_actionable_message(self):
        self.responses = [(201, ''), (409, '')]   # MKCOL 成功，PUT 409
        with self.assertRaises(W.WebdavError) as ctx:
            W.do_backup(self.client(), {}, {})
        self.assertIn('目录不存在或账号没有写权限', str(ctx.exception))

    def test_put_401_mentions_credentials(self):
        self.responses = [(201, ''), (401, '')]
        with self.assertRaises(W.WebdavError) as ctx:
            W.do_backup(self.client(), {}, {})
        self.assertIn('用户名或密码', str(ctx.exception))

    def test_restore_missing_file(self):
        self.responses = [(404, '')]
        with self.assertRaises(W.WebdavError) as ctx:
            W.do_restore(self.client())
        self.assertIn('没有找到备份文件', str(ctx.exception))

    def test_restore_round_trip(self):
        payload = W.backup_payload({'proxy': 'p'}, {'sites': {}}, '1.0.0')
        self.responses = [(200, json.dumps(payload))]
        got = W.do_restore(self.client())
        self.assertEqual(got['config']['proxy'], 'p')
        self.assertEqual(got['version'], W.BACKUP_VERSION)

    def test_restore_bad_json_reports_clearly(self):
        self.responses = [(200, '<html>proxy login page</html>')]
        with self.assertRaises(W.WebdavError) as ctx:
            W.do_restore(self.client())
        self.assertIn('不是合法 JSON', str(ctx.exception))

    def test_test_connection_writes_and_reads_probe(self):
        self.responses = [(201, ''), (201, ''), (200, '{"ok":true}')]
        r = W.test_connection(self.client())
        self.assertTrue(r['ok'])

    def test_authorization_header_present(self):
        seen = {}

        def cap(method, url, cfg=None, global_proxy='', data=None, cookie_jar=None):
            seen['headers'] = cfg.headers
            from app.httpclient import HttpResponse
            return HttpResponse(status=201, text='', url=url, headers={})

        W.request = cap
        self.client().mkcol('https://dav.example.com/dav/autocheckin/')
        self.assertIn('Authorization', seen['headers'])
        self.assertTrue(seen['headers']['Authorization'].startswith('Basic '))


# --------------------------------------------------------------------- runner

class RunnerCase(TempDirCase):
    def make_runner(self, cfg=None, state=None, results=None, ai=None,
                    notify_records=None, now=1_700_000_000):
        self.box = S.SecretBox(S.generate_key())
        self.store = ST.Store(self.dir)
        self.cfg = cfg or AppConfig(proxy='')
        if not self.cfg.sites:
            self.cfg.sites = [SiteConfig(id='s1', name='S1', daily_hour=3,
                                         daily_minute=0, mode=MODE_DAILY)]
        self.store.save_config(self.cfg)
        self.state = state or ST.RuntimeState()

        self.checkin_calls = []
        self.ai_calls = []
        self.notify_calls = []

        results = results or {}

        def fake_checkin(site, proxy, password, browser, cookie_header=None):
            self.checkin_calls.append(site.id)
            r = results.get(site.id)
            if callable(r):
                return r(site)
            if r is not None:
                return r
            return CheckinResult(site_id=site.id, site_name=site.name, success=True,
                                 message='ok')

        def fake_analyze(site, result, ai_cfg, api_key, proxy):
            self.ai_calls.append(site.id)
            if ai:
                return ai
            from app.deepseek import AiSuggestion
            return AiSuggestion(ok=True, analysis='改成浏览器', confidence=90,
                                patch={'need_browser': True})

        def fake_notify(result, notify_cfg, proxy):
            self.notify_calls.append((result.site_id, result.success))
            return {'pushplus': '已推送'}

        self.now_value = now

        return R.Runner(self.store, box=self.box, proxy='',
                        now_fn=lambda: self.now_value,
                        rng=random.Random(42),
                        checkin_fn=fake_checkin, analyze_fn=fake_analyze,
                        notify_fn=fake_notify)


class TestRunnerScheduling(RunnerCase):
    def test_ensure_scheduled_sets_future_time(self):
        r = self.make_runner()
        r.ensure_scheduled(self.cfg, self.state, self.now_value)
        nxt = self.state.next_run_at('s1')
        self.assertGreater(nxt, self.now_value, '首次排程必须排到未来，不能立刻执行')

    def test_due_sites_empty_when_not_due(self):
        r = self.make_runner()
        self.state.set_next_run_at('s1', self.now_value + 3600)
        self.assertEqual(r.due_sites(self.cfg, self.state, self.now_value), [])

    def test_due_sites_includes_when_past(self):
        r = self.make_runner()
        self.state.set_next_run_at('s1', self.now_value - 10)
        due = r.due_sites(self.cfg, self.state, self.now_value)
        self.assertEqual([s.id for s in due], ['s1'])

    def test_disabled_site_skipped(self):
        cfg = AppConfig()
        cfg.sites = [SiteConfig(id='s1', name='S1', enabled=False)]
        r = self.make_runner(cfg=cfg)
        self.state.set_next_run_at('s1', 0)
        self.assertEqual(r.due_sites(cfg, self.state, self.now_value), [])

    def test_run_once_only_processes_due(self):
        r = self.make_runner()
        self.state.set_next_run_at('s1', self.now_value + 9999)
        report = r.run_once(self.cfg, self.state)
        self.assertEqual(report.runs, [])
        self.assertEqual(self.checkin_calls, [])

    def test_run_once_success_advances_schedule(self):
        r = self.make_runner()
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(self.cfg, self.state)
        self.assertEqual(len(report.runs), 1)
        self.assertTrue(report.runs[0].result.success)
        self.assertGreater(self.state.next_run_at('s1'), self.now_value)

    def test_only_site_forces_run(self):
        r = self.make_runner()
        self.state.set_next_run_at('s1', self.now_value + 9999)
        report = r.run_once(self.cfg, self.state, only_site='s1')
        self.assertEqual(len(report.runs), 1)


class TestRunnerRetry(RunnerCase):
    def test_failure_uses_retry_interval(self):
        site = SiteConfig(id='s1', name='S1', retry_enabled=True, retry_count=2,
                          retry_interval_minutes=30, daily_hour=3)
        cfg = AppConfig()
        cfg.sites = [site]
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, '失败', error_kind='timeout')})
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(self.state.next_run_at('s1'), self.now_value + 1800,
                         '失败后应按重试间隔排下一次')

    def test_success_clears_failure_state(self):
        site = SiteConfig(id='s1', name='S1', state={'consecutive_failures': 5})
        cfg = AppConfig()
        cfg.sites = [site]
        r = self.make_runner(cfg=cfg)
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(self.cfg.sites[0].state.get('consecutive_failures'), 0)

    def test_exception_in_one_site_does_not_stop_others(self):
        cfg = AppConfig()
        cfg.sites = [SiteConfig(id='bad', name='Bad', daily_hour=3),
                     SiteConfig(id='good', name='Good', daily_hour=3)]
        r = self.make_runner(cfg=cfg)

        def flaky(site, proxy, password, browser, cookie_header=None):
            if site.id == 'bad':
                raise RuntimeError('崩了')
            return CheckinResult(site.id, site.name, True, 'ok')

        r.checkin_fn = flaky
        self.state.set_next_run_at('bad', self.now_value - 1)
        self.state.set_next_run_at('good', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertEqual(len(report.runs), 2)
        by = {x.site_id: x for x in report.runs}
        self.assertFalse(by['bad'].result.success)
        self.assertIn('崩了', by['bad'].result.message)
        self.assertTrue(by['good'].result.success)


class TestRunnerAi(RunnerCase):
    def test_ai_not_called_below_threshold(self):
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=3,
                          state={'consecutive_failures': 0})
        cfg = AppConfig()
        cfg.sites = [site]
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x', error_kind='timeout')})
        # 阈值未到，连 key 都不需要配
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(self.ai_calls, [], '连败未达阈值不该调用 AI')

    def test_ai_called_at_threshold_and_patch_applied(self):
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=3,
                          state={'consecutive_failures': 2})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True, auto_apply=True)
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x', error_kind='timeout')})
        cfg.ai.api_key_enc = self.box.encrypt('fake-api-key')
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertEqual(self.ai_calls, ['s1'], '达到阈值应调用 AI')
        self.assertTrue(report.runs[0].ai_used)
        self.assertIn('浏览器', report.runs[0].ai_note)
        # 补丁应已写回配置
        self.assertTrue(cfg.sites[0].need_browser)

    def test_ai_not_repeated_for_same_streak(self):
        """同一档连败只分析一次：4、5 次时不该再分析（否则持续故障天天烧 token）。"""
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=3,
                          state={'consecutive_failures': 2})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True)
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x', error_kind='timeout')})
        cfg.ai.api_key_enc = self.box.encrypt('fake-api-key')
        # 第 1 轮：连败 2→3，到达阈值 → 分析
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(len(self.ai_calls), 1)
        # 第 2、3 轮：连败 3→4→5，处于同一档 → 不再分析
        for _ in range(2):
            self.state.set_next_run_at('s1', self.now_value - 1)
            r.run_once(cfg, self.state)
        self.assertEqual(len(self.ai_calls), 1,
                         '未跨越下一个阈值倍数时不该重复调用 AI')
        # 第 4 轮：连败 5→6，到达下一档 → 再分析一次
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(len(self.ai_calls), 2,
                         '到达下一个阈值倍数应再次分析')

    def test_ai_skipped_without_api_key(self):
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=1,
                          state={'consecutive_failures': 3})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True, api_key_enc='')
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x')})
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertEqual(self.ai_calls, [])
        self.assertIn('API Key', report.runs[0].ai_note)

    def test_daily_limit_enforced(self):
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=1,
                          state={'consecutive_failures': 3})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True, max_calls_per_day=1)
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x')})
        cfg.ai.api_key_enc = self.box.encrypt('fake-api-key')
        today = time.strftime('%Y-%m-%d', time.localtime(self.now_value))
        self.state.bump_ai_calls('s1', today)
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertEqual(self.ai_calls, [])
        self.assertIn('上限', report.runs[0].ai_note)

    def test_ai_failure_is_reported_not_swallowed(self):
        from app.deepseek import AiSuggestion
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=1,
                          state={'consecutive_failures': 5})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True)
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x')},
            ai=AiSuggestion(False, error='额度用尽'))
        cfg.ai.api_key_enc = self.box.encrypt('fake-api-key')
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertIn('额度用尽', report.runs[0].ai_note)


class TestRunnerNotify(RunnerCase):
    def test_success_triggers_notify(self):
        r = self.make_runner()
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(self.cfg, self.state)
        self.assertEqual(self.notify_calls, [('s1', True)])
        self.assertEqual(report.runs[0].notified.get('pushplus'), '已推送')

    def test_notify_none_yields_nothing(self):
        """notify 被调用但策略为 none 时应返回空（由 notify 模块负责策略）。"""
        cfg = AppConfig()
        cfg.notify = NotifyConfig(mode=NOTIFY_NONE)
        cfg.sites = [SiteConfig(id='s1', name='S1', daily_hour=3)]
        r = self.make_runner(cfg=cfg)
        # 用真实 notify 逻辑（不注入）来验证策略生效
        from app import notify as real_notify
        r.notify_fn = real_notify.notify
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertEqual(report.runs[0].notified, {})


class TestRunnerPersistence(RunnerCase):
    def test_state_is_saved_to_disk(self):
        r = self.make_runner()
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(self.cfg, self.state)
        reloaded = self.store.load_state()
        self.assertGreater(reloaded.next_run_at('s1'), self.now_value)
        self.assertEqual(reloaded.last_run_at, self.now_value)

    def test_config_changes_persist(self):
        """AI 补丁必须落盘，否则重启后又按旧配置签到。"""
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=1,
                          need_browser=False, state={'consecutive_failures': 5})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True,
                          api_key_enc=S.SecretBox(S.generate_key()).encrypt('k'))
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x')})
        # 用真实 box 解 api key
        r.box = S.SecretBox(S.generate_key())
        cfg.ai.api_key_enc = r.box.encrypt('k')
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        reloaded = self.store.load_config()
        self.assertTrue(reloaded.sites[0].need_browser,
                        'AI 改动应已写回磁盘')


class TestAiFailureAutoDisable(RunnerCase):
    """用户要求：AI 调用也失败则自动停止该站点签到，并推送"分析失败"告警。"""

    def make(self, ai_result, fail_threshold=2, auto_disable=True,
             consecutive_failures=5):
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=1,
                          enabled=True,
                          state={'consecutive_failures': consecutive_failures})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True, fail_threshold=fail_threshold,
                          auto_disable=auto_disable, max_calls_per_day=99)
        cfg.notify = NotifyConfig(channels=['pushplus'], mode=NOTIFY_FAIL_ONLY,
                                  pushplus_token='tok')
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x', error_kind='timeout')},
            ai=ai_result)
        cfg.ai.api_key_enc = self.box.encrypt('fake-key')
        return r, cfg, site

    def test_ai_failure_counted(self):
        from app.deepseek import AiSuggestion
        r, cfg, site = self.make(AiSuggestion(False, error='额度用尽'),
                                 fail_threshold=5)
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertEqual(site.state['ai_consecutive_failures'], 1)
        self.assertIn('额度用尽', report.runs[0].ai_note)
        self.assertTrue(site.enabled, '未到阈值不该停用')

    def test_disabled_after_threshold(self):
        from app.deepseek import AiSuggestion
        r, cfg, site = self.make(AiSuggestion(False, error='401'),
                                 fail_threshold=2)
        # 第 1 次 AI 失败
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertTrue(site.enabled)
        self.assertEqual(site.state['ai_consecutive_failures'], 1)
        # 第 2 次 AI 失败 → 达到阈值
        site.state['ai_analyzed_for_streak'] = 0     # 模拟跨到下一档
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(site.state['ai_consecutive_failures'], 2)
        self.assertFalse(site.enabled, 'AI 连续失败达阈值应自动停用站点')
        self.assertIn('自动停止', site.state.get('disabled_reason', ''))

    def test_alert_sent_regardless_of_notify_policy(self):
        """通知策略是"仅成功"时，告警仍然要发出去（系统级事件）。"""
        from app.deepseek import AiSuggestion
        alerts = []
        r, cfg, site = self.make(AiSuggestion(False, error='401'), fail_threshold=1)
        cfg.notify.mode = NOTIFY_SUCCESS_ONLY
        r.alert_fn = lambda c, title, body, proxy, _ck=None: alerts.append((title, body))
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(len(alerts), 1, '告警不该被站点通知策略拦住')
        self.assertIn('分析失败', alerts[0][0])
        self.assertIn('S1', alerts[0][1])
        self.assertIn('401', alerts[0][1])
        self.assertIn('自动停止', alerts[0][1])

    def test_no_alert_when_no_channel_configured(self):
        from app.deepseek import AiSuggestion
        alerts = []
        r, cfg, site = self.make(AiSuggestion(False, error='x'), fail_threshold=1)
        cfg.notify = NotifyConfig()          # 没有任何渠道
        r.alert_fn = lambda *a: alerts.append(a)
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(alerts, [], '没有通知渠道就不该尝试发送')

    def test_auto_disable_can_be_turned_off(self):
        from app.deepseek import AiSuggestion
        r, cfg, site = self.make(AiSuggestion(False, error='x'),
                                 fail_threshold=1, auto_disable=False)
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertTrue(site.enabled, '关闭自动停用后不应改状态')
        self.assertEqual(site.state['ai_consecutive_failures'], 1)

    def test_ai_exception_also_counted(self):
        """AI 调用直接抛异常也要计入失败，不能只在返回 not-ok 时才计。"""
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=1,
                          state={'consecutive_failures': 9})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True, fail_threshold=1, auto_disable=True)

        def boom(site_, result_, ai_cfg, key, proxy):
            raise RuntimeError('连接被重置')
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x')})
        r.analyze_fn = boom
        cfg.ai.api_key_enc = self.box.encrypt('k')
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertEqual(site.state['ai_consecutive_failures'], 1)
        self.assertIn('连接被重置', report.runs[0].ai_note)
        self.assertFalse(site.enabled)

    def test_success_resets_ai_failure_counter(self):
        """AI 分析恢复成功 → 计数清零，避免"历史上失败过就一直算。"。"""
        from app.deepseek import AiSuggestion
        site = SiteConfig(id='s1', name='S1', ai_enabled=True, ai_after_failures=1,
                          state={'consecutive_failures': 5,
                                 'ai_consecutive_failures': 3})
        cfg = AppConfig()
        cfg.sites = [site]
        cfg.ai = AiConfig(enabled=True, fail_threshold=5)
        r = self.make_runner(cfg=cfg, results={
            's1': CheckinResult('s1', 'S1', False, 'x')},
            ai=AiSuggestion(True, analysis='改成浏览器', patch={}))
        cfg.ai.api_key_enc = self.box.encrypt('k')
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(site.state['ai_consecutive_failures'], 0)

    def test_disabled_site_is_not_run_again(self):
        """停用后再跑一轮，不该再执行该站点。"""
        from app.deepseek import AiSuggestion
        r, cfg, site = self.make(AiSuggestion(False, error='x'), fail_threshold=1)
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertFalse(site.enabled)
        before = len(self.checkin_calls)
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(len(self.checkin_calls), before,
                         '停用后不该再触发签到')


class TestSecretMismatchHandling(RunnerCase):
    """密钥不匹配时必须给出明确原因，且不能拿空密码去登录。"""

    def test_uses_checked_decrypt_with_clear_message(self):
        # 站点密码用 A 密钥加密，但运行器拿的是 B 密钥
        box_a = S.SecretBox(S.generate_key())
        box_b = S.SecretBox(S.generate_key())
        site = SiteConfig(id='s1', name='S1', username='u',
                          password_enc=box_a.encrypt('RealPassword'))
        cfg = AppConfig()
        cfg.sites = [site]

        r = self.make_runner(cfg=cfg)
        r.box = box_b                       # 模拟换机后密钥不对
        tried = []

        def spy(site_, proxy, password, browser):
            tried.append(password)
            return CheckinResult(site_.id, site_.name, True)

        r.checkin_fn = spy
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)

        msg = report.runs[0].result.message
        self.assertIn('解密', msg, '消息里要说明是解密问题：%s' % msg)
        self.assertEqual(tried, [], '不该拿空密码去登录')
        self.assertFalse(report.runs[0].result.success)
        self.assertEqual(report.runs[0].result.error_kind, 'secret_error')

    def test_correct_key_still_works(self):
        box = S.SecretBox(S.generate_key())
        site = SiteConfig(id='s1', name='S1', username='u',
                          password_enc=box.encrypt('RealPassword'))
        cfg = AppConfig()
        cfg.sites = [site]
        r = self.make_runner(cfg=cfg)
        r.box = box
        seen = []
        r.checkin_fn = lambda s, p, pw, b, _ck=None: (
            seen.append(pw) or CheckinResult(s.id, s.name, True))
        self.state.set_next_run_at('s1', self.now_value - 1)
        r.run_once(cfg, self.state)
        self.assertEqual(seen, ['RealPassword'])

    def test_no_password_configured_is_not_an_error(self):
        """站点没配密码（比如只靠 Cookie）时不该被当成解密失败。"""
        site = SiteConfig(id='s1', name='S1')
        cfg = AppConfig()
        cfg.sites = [site]
        r = self.make_runner(cfg=cfg)
        seen = []
        r.checkin_fn = lambda s, p, pw, b, _ck=None: (
            seen.append(pw) or CheckinResult(s.id, s.name, True))
        self.state.set_next_run_at('s1', self.now_value - 1)
        report = r.run_once(cfg, self.state)
        self.assertTrue(report.runs[0].result.success)
        self.assertEqual(seen, [''])


class TestRunReport(RunnerCase):
    def test_summary_counts(self):
        rep = R.RunReport(started_at=0)
        rep.runs = [
            R.SiteRun('a', 'A', CheckinResult('a', 'A', True)),
            R.SiteRun('b', 'B', CheckinResult('b', 'B', False)),
            R.SiteRun('c', 'C', CheckinResult('c', 'C', True), skipped='disabled'),
        ]
        self.assertEqual(rep.success_count, 2)
        self.assertEqual(rep.fail_count, 1)
        s = rep.summary()
        self.assertIn('成功 2', s)
        self.assertIn('失败 1', s)
        self.assertIn('跳过 1', s)

    def test_empty_summary(self):
        self.assertEqual(R.RunReport(started_at=0).summary(), '没有到期的站点')


if __name__ == '__main__':
    unittest.main(verbosity=2)
