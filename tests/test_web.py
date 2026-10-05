"""web / scheduler 单测：路由分发、设置写入、凭据不外泄、调度循环。"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth as AUTH  # noqa: E402
from app import recorder as RC  # noqa: E402
from app import scheduler as SCH  # noqa: E402
from app import secrets as S  # noqa: E402
from app import service as SV  # noqa: E402
from app import web as WB  # noqa: E402
from app.models import PROXY_DIRECT, SiteConfig  # noqa: E402
from app.notify import CheckinResult  # noqa: E402


class WebCase(unittest.TestCase):
    """所有用例默认以"已登录"身份操作 —— 认证本身另有专门的用例覆盖。"""

    PASSWORD = 'test-password-123'

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='autocheckin_web_')
        self.svc = SV.Service(self.dir, version='9.9.9',
                              env_key=S.generate_key(), proxy='')
        self.app = WB.WebApp(self.svc, version='9.9.9')
        self.token = self._login()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _login(self):
        """完成口令初始化并返回会话令牌。"""
        st, _, out = self._raw_dispatch('POST', '/api/auth/setup', {},
                                        {'password': self.PASSWORD})
        self.assertEqual(st, 200, '口令初始化失败：%s' % out)
        return self.app._new_token

    def _raw_dispatch(self, method, path, query=None, body=None, token=''):
        result = self.app.dispatch(method, path, query or {}, body or {},
                                   token=token, source='test')
        if len(result) == 2:
            return result[0], result[1], None
        return result[0], result[1], result[2]

    def call(self, method, path, body=None, query=None, token=None):
        """统一返回 (status, out)。默认带登录令牌。"""
        t = self.token if token is None else token
        status, ctype, out = self._raw_dispatch(method, path, query, body, t)
        return status, out

    def json(self, method, path, body=None, query=None, token=None):
        status, out = self.call(method, path, body, query, token)
        self.assertEqual(status, 200)
        self.assertIsInstance(out, dict)
        return out


class TestAuthRequired(WebCase):
    """受保护接口在未登录时必须拒绝。"""

    def setUp(self):
        # 这里不用父类的登录
        self.dir = tempfile.mkdtemp(prefix='autocheckin_auth_')
        self.svc = SV.Service(self.dir, version='9.9.9',
                              env_key=S.generate_key(), proxy='')
        self.app = WB.WebApp(self.svc, version='9.9.9')
        self.token = ''

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _call(self, method, path, body=None, query=None, token=''):
        result = self.app.dispatch(method, path, query or {}, body or {},
                                   token=token, source='test')
        return result[0], (result[2] if len(result) == 3 else result[1])

    def test_protected_endpoints_reject_anonymous(self):
        cases = [
            ('GET', '/api/sites', None, None),
            ('GET', '/api/settings', None, None),
            ('GET', '/api/overview', None, None),
            ('GET', '/api/meta', None, None),
            ('POST', '/api/settings', {'proxy': 'http://attacker:1'}, None),
            ('POST', '/api/site/delete', {'id': 'v2ex'}, None),
            ('POST', '/api/run', {}, None),
            ('POST', '/api/notify/test', {}, None),
            ('POST', '/api/key/restore', {}, None),
            ('GET', '/api/site', None, {'id': ['v2ex']}),
        ]
        for method, path, body, query in cases:
            with self.assertRaises(WB.ApiError, msg='%s %s 竟然放行了' % (method, path)) as ctx:
                self._call(method, path, body, query)
            self.assertIn(ctx.exception.status, (401, 403),
                          '%s %s 返回了 %s' % (method, path, ctx.exception.status))

    def test_healthz_and_auth_state_are_public(self):
        st, out = self._call('GET', '/api/auth/state', None, None)
        self.assertEqual(st, 200)
        self.assertTrue(out['needs_setup'])

    def test_setup_then_access(self):
        st, out = self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        self.assertEqual(st, 200)
        token = self.app._new_token
        self.assertTrue(token)
        st, out = self._call('GET', '/api/sites', None, None, token=token)
        self.assertEqual(st, 200)

    def test_setup_requires_min_length(self):
        with self.assertRaises(WB.ApiError) as ctx:
            self._call('POST', '/api/auth/setup', {'password': '123'})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn('至少', ctx.exception.message)

    def test_setup_cannot_run_twice(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        with self.assertRaises(WB.ApiError) as ctx:
            self._call('POST', '/api/auth/setup', {'password': 'other123'})
        self.assertEqual(ctx.exception.status, 403)

    def test_wrong_password_rejected(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        with self.assertRaises(WB.ApiError) as ctx:
            self._call('POST', '/api/auth/login', {'password': 'nope'})
        self.assertEqual(ctx.exception.status, 401)

    def test_response_never_contains_password_or_token(self):
        st, out = self._call('POST', '/api/auth/setup', {'password': 'topsecret1'})
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn('topsecret1', blob)
        self.assertNotIn(self.app._new_token, blob,
                         '令牌只应通过 Cookie 下发，不能出现在响应体里')

    def test_session_can_be_revoked(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        token = self.app._new_token
        self.assertEqual(self._call('GET', '/api/sites', None, None, token=token)[0], 200)
        self._call('POST', '/api/auth/logout', {}, None, token=token)
        with self.assertRaises(WB.ApiError):
            self._call('GET', '/api/sites', None, None, token=token)

    def test_changing_password_invalidates_old_sessions(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        old = self.app._new_token
        self._call('POST', '/api/auth/password',
                   {'old': 'abcdef', 'new': 'newpassword'}, None, token=old)
        with self.assertRaises(WB.ApiError):
            self._call('GET', '/api/sites', None, None, token=old)

    def test_password_change_requires_old_password(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        t = self.app._new_token
        with self.assertRaises(WB.ApiError) as ctx:
            self._call('POST', '/api/auth/password',
                       {'old': 'wrong', 'new': 'newpassword'}, None, token=t)
        self.assertEqual(ctx.exception.status, 401)

    def test_failed_attempts_are_rate_limited(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        codes = []
        for _ in range(AUTH.MAX_ATTEMPTS + 3):
            try:
                self._call('POST', '/api/auth/login', {'password': 'bad'})
                codes.append(200)
            except WB.ApiError as e:
                codes.append(e.status)
        self.assertIn(429, codes, '连续失败应触发限速，实际：%s' % codes)

    def test_auth_can_be_disabled_with_password(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        t = self.app._new_token
        st, out = self._call('POST', '/api/auth/disable',
                             {'password': 'abcdef'}, None, token=t)
        self.assertEqual(st, 200)
        self.assertIn('warning', out)
        # 关闭后匿名可访问
        self.assertEqual(self._call('GET', '/api/sites')[0], 200)

    def test_disable_requires_correct_password(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        t = self.app._new_token
        with self.assertRaises(WB.ApiError) as ctx:
            self._call('POST', '/api/auth/disable', {'password': 'nope'}, None, token=t)
        self.assertEqual(ctx.exception.status, 401)

    def test_token_stored_hashed_not_plaintext(self):
        self._call('POST', '/api/auth/setup', {'password': 'abcdef'})
        token = self.app._new_token
        raw = open(self.svc.store.config_path, encoding='utf-8').read()
        self.assertNotIn(token, raw, '配置里不应出现令牌明文')
        self.assertIn(AUTH.hash_token(token), raw, '应存令牌的哈希')
        self.assertNotIn('abcdef', raw, '口令不应明文落盘')


class TestSiteIdValidation(WebCase):
    """站点标识必须限制为安全字符。"""

    def test_rejects_path_traversal_ids(self):
        for bad in ['../../etc/passwd', '..', 'a/b', '/abs', '', ' ',
                    'a' * 200, 'a b', 'a;b', 'a../b']:
            with self.assertRaises((WB.ApiError, ValueError),
                                   msg='不该接受站点标识 %r' % bad):
                self.json('POST', '/api/site', {'id': bad, 'name': 'x'})

    def test_accepts_reasonable_ids(self):
        for good in ['v2ex', 'my-site', 'my_site', 'site123', 'A1']:
            st, out = self.call('POST', '/api/site', {'id': good, 'name': 'x'})
            self.assertEqual(st, 200, '不该拒绝 %r：%s' % (good, out))


class TestRouting(WebCase):
    def test_unknown_route_404(self):
        with self.assertRaises(WB.ApiError) as ctx:
            self.app.dispatch('GET', '/api/nope', {}, token=self.token)
        self.assertEqual(ctx.exception.status, 404)

    def test_trailing_slash_tolerated(self):
        self.json('GET', '/api/sites/')

    def test_overview(self):
        d = self.json('GET', '/api/overview')
        self.assertEqual(d['site_count'], 3)
        self.assertEqual(d['version'], '9.9.9')

    def test_meta_lists_templates_and_channels(self):
        d = self.json('GET', '/api/meta')
        ids = [t['id'] for t in d['templates']]
        self.assertEqual(sorted(ids), ['chiphell', 'nodeseek', 'v2ex'])
        self.assertIn('pushplus', d['channels'])
        self.assertIn('telegram', d['channels'])
        self.assertIn(PROXY_DIRECT, d['proxy_modes'])
        self.assertIn('goto', d['actions'])

    def test_sites_list(self):
        d = self.json('GET', '/api/sites')
        self.assertEqual(len(d['sites']), 3)

    def test_site_missing_404(self):
        with self.assertRaises(WB.ApiError) as ctx:
            self.app.dispatch('GET', '/api/site', {'id': ['nope']}, token=self.token)
        self.assertEqual(ctx.exception.status, 404)

    def test_record_script_served_as_js(self):
        status, ctype, out = self.app.dispatch('GET', '/api/record/script.js', {}, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn('javascript', ctype)
        self.assertIn(b'__autocheckinRecorder', out)

    def test_value_error_becomes_400(self):
        with self.assertRaises(WB.ApiError) as ctx:
            self.app.dispatch('POST', '/api/record/start', {}, {'id': 'x'}, token=self.token)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn('主页地址', ctx.exception.message)

    def test_internal_error_becomes_500(self):
        orig = self.svc.list_sites
        self.svc.list_sites = lambda: (_ for _ in ()).throw(RuntimeError('炸了'))
        try:
            with self.assertRaises(WB.ApiError) as ctx:
                self.app.dispatch('GET', '/api/sites', {}, token=self.token)
            self.assertEqual(ctx.exception.status, 500)
        finally:
            self.svc.list_sites = orig


class TestCredentialsNeverLeak(WebCase):
    def test_site_endpoint_omits_credentials(self):
        self.json('POST', '/api/site', {'id': 'x', 'name': 'X',
                                        'username': 'alice', 'password': 'TOPSECRET'})
        d = self.json('GET', '/api/site', query={'id': ['x']})
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn('TOPSECRET', blob)
        self.assertNotIn('password_enc', blob)
        self.assertNotIn('cookie_enc', blob)

    def test_settings_masks_secrets(self):
        self.json('POST', '/api/settings', {
            'notify': {'pushplus_token': 'PUSHTOKEN123456'},
            'ai': {'api_key': 'sk-SECRETKEY'},
            'webdav': {'url': 'https://dav.example.com/dav/', 'password': 'WDPASS'},
        })
        d = self.json('GET', '/api/settings')
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn('PUSHTOKEN123456', blob, '通知 token 不能明文回显')
        self.assertNotIn('sk-SECRETKEY', blob, 'AI Key 不能明文回显')
        self.assertNotIn('WDPASS', blob, 'WebDAV 密码不能明文回显')
        # 但要有"已配置"标记与打码形式
        self.assertTrue(d['notify']['has_credentials']['pushplus'])
        self.assertTrue(d['ai']['has_key'])
        self.assertTrue(d['webdav']['has_password'])
        self.assertTrue(d['notify']['pushplus_token_masked'].startswith('PUS'))
        self.assertIn('*', d['notify']['pushplus_token_masked'])

    def test_site_list_has_no_credentials(self):
        self.json('POST', '/api/site', {'id': 'x', 'name': 'X', 'password': 'SEC'})
        blob = json.dumps(self.json('GET', '/api/sites'), ensure_ascii=False)
        self.assertNotIn('SEC', blob)
        self.assertNotIn('password', blob)


class TestSettingsWrite(WebCase):
    def test_proxy_and_browser_saved(self):
        self.json('POST', '/api/settings', {
            'proxy': 'http://10.0.0.1:7890',
            'headless': False,
            'remote_cdp_url': 'http://browser:9222',
        })
        cfg = self.svc.load_config()
        self.assertEqual(cfg.proxy, 'http://10.0.0.1:7890')
        self.assertFalse(cfg.headless)
        self.assertEqual(cfg.remote_cdp_url, 'http://browser:9222')

    def test_notify_channel_proxy_saved(self):
        self.json('POST', '/api/settings', {'notify': {
            'mode': 'fail',
            'channels': ['telegram', 'wecom'],
            'proxy_mode': 'custom',
            'proxy_url': 'http://n:1',
            'channel_proxy': {'wecom': {'mode': 'direct', 'url': ''}},
        }})
        cfg = self.svc.load_config()
        self.assertEqual(cfg.notify.mode, 'fail')
        self.assertEqual(cfg.notify.channels, ['telegram', 'wecom'])
        self.assertEqual(cfg.notify.proxy_mode, 'custom')
        self.assertEqual(cfg.notify.channel_proxy['wecom']['mode'], 'direct')

    def test_empty_credentials_not_overwritten(self):
        """界面上凭据留空时不能把已保存的清掉。"""
        self.json('POST', '/api/settings', {
            'notify': {'pushplus_token': 'ORIGINAL'},
            'ai': {'api_key': 'sk-orig'},
            'webdav': {'url': 'https://d.example/dav/', 'password': 'wdorig'},
        })
        # 第二次提交时凭据字段留空
        self.json('POST', '/api/settings', {
            'notify': {'pushplus_token': ''},
            'ai': {'api_key': ''},
            'webdav': {'url': 'https://d.example/dav/', 'password': ''},
        })
        cfg = self.svc.load_config()
        self.assertEqual(cfg.notify.pushplus_token, 'ORIGINAL')
        self.assertEqual(self.svc.box.decrypt(cfg.ai.api_key_enc), 'sk-orig')
        self.assertEqual(self.svc.box.decrypt(cfg.webdav.password_enc), 'wdorig')

    def test_ai_settings_saved(self):
        self.json('POST', '/api/settings', {'ai': {
            'api_key': 'sk-x', 'model': 'deepseek-reasoner',
            'ai_after_failures': 4, 'max_calls_per_day': 5,
            'auto_apply': False}})
        cfg = self.svc.load_config()
        self.assertEqual(cfg.ai.model, 'deepseek-reasoner')
        self.assertEqual(cfg.ai.ai_after_failures, 4)
        self.assertEqual(cfg.ai.max_calls_per_day, 5)
        self.assertFalse(cfg.ai.auto_apply)
        self.assertEqual(self.svc.box.decrypt(cfg.ai.api_key_enc), 'sk-x')

    def test_ai_after_failures_zero_means_disabled(self):
        """每几次失败调用一次 = 0 表示不启用 AI 分析。"""
        self.json('POST', '/api/settings', {'ai': {'ai_after_failures': 0}})
        self.assertEqual(self.svc.load_config().ai.ai_after_failures, 0)

    def test_max_calls_per_day_zero_means_unlimited(self):
        """每天最多分析几次 = 0 表示不限制。"""
        self.json('POST', '/api/settings', {'ai': {'max_calls_per_day': 0}})
        self.assertEqual(self.svc.load_config().ai.max_calls_per_day, 0)

    def test_legacy_per_site_daily_fields_are_dropped(self):
        """已删除的"每日尝试/调用上限"字段不该让配置读不出来。"""
        from app.models import AiConfig
        back = AiConfig.from_dict({
            'daily_limit_enabled': True, 'max_attempts_per_day': 3,
            'ai_calls_per_day': 2, 'global_max_calls_per_day': 10})
        # ai_calls_per_day 的意图迁移到"每天最多分析几次"
        self.assertEqual(back.max_calls_per_day, 2)
        self.assertFalse(hasattr(back, 'max_attempts_per_day'))
        self.assertFalse(hasattr(back, 'daily_limit_enabled'))

    def test_legacy_global_cap_migrated_if_no_per_site_value(self):
        from app.models import AiConfig
        back = AiConfig.from_dict({'global_max_calls_per_day': 7})
        self.assertEqual(back.max_calls_per_day, 7)

    def test_ai_failure_handling_settings_saved(self):
        """AI 失败处理只由 fail_threshold 一个数字控制（0 = 不限制）。"""
        self.json('POST', '/api/settings', {'ai': {'fail_threshold': 3}})
        self.assertEqual(self.svc.load_config().ai.fail_threshold, 3)

    def test_fail_threshold_zero_means_unlimited(self):
        """0 是合法值，表示不限制连续失败次数（不再被夹到 1）。"""
        self.json('POST', '/api/settings', {'ai': {'fail_threshold': 0}})
        self.assertEqual(self.svc.load_config().ai.fail_threshold, 0)

    def test_negative_fail_threshold_clamped_to_zero(self):
        self.json('POST', '/api/settings', {'ai': {'fail_threshold': -5}})
        self.assertEqual(self.svc.load_config().ai.fail_threshold, 0)

    def test_alert_test_endpoint_requires_channel(self):
        with self.assertRaises(WB.ApiError) as ctx:
            self.app.dispatch('POST', '/api/notify/alert-test', {}, {}, token=self.token)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn('通知渠道', ctx.exception.message)

    def test_alert_test_endpoint_sends(self):
        self.json('POST', '/api/settings', {'notify': {'pushplus_token': 'tok'}})
        sent = {}

        def fake_alert(cfg, title, body, proxy=''):
            sent['title'] = title
            sent['body'] = body
            return {'pushplus': '已推送'}

        orig = SV.notify_mod.send_alert
        SV.notify_mod.send_alert = fake_alert
        try:
            out = self.json('POST', '/api/notify/alert-test', {})
        finally:
            SV.notify_mod.send_alert = orig
        self.assertIn('分析失败', sent['title'])
        self.assertIn('自动停止', sent['body'])
        self.assertIn('pushplus', out['report'])

    def test_site_list_exposes_ai_failure_state(self):
        self.json('POST', '/api/site', {'id': 'x', 'name': 'X'})
        sites = {s['id']: s for s in self.json('GET', '/api/sites')['sites']}
        self.assertIn('ai_consecutive_failures', sites['x'])
        self.assertIn('last_ai_error', sites['x'])
        self.assertIn('disabled_reason', sites['x'])


class TestRunAndNotifyEndpoints(WebCase):
    def test_run_endpoint_returns_runs(self):
        def factory(svc):
            from app.runner import Runner
            return Runner(svc.store, box=svc.box, proxy='',
                          checkin_fn=lambda s, p, pw, b, _ck=None: CheckinResult(
                              s.id, s.name, True, 'ok'),
                          notify_fn=lambda *a: {})
        svc = SV.Service(self.dir, env_key=S.generate_key(),
                         runner_factory=factory)
        # 直接设置口令（不经过 /api/auth/setup，避免依赖调用顺序），
        # 然后用同一个存储再建 app 并登录。
        cfg = svc.load_config()
        cfg.auth_password_enc = svc.box.encrypt('abc12345')
        cfg.session_hashes = []
        svc.save_config(cfg)

        app = WB.WebApp(svc)
        app.dispatch('POST', '/api/auth/login', {}, {'password': 'abc12345'})
        token = app._new_token
        self.assertTrue(token, '登录未拿到令牌')

        result = app.dispatch('POST', '/api/run', {}, {'id': 'v2ex'},
                              token=token)
        status, out = (result if len(result) == 2 else (result[0], result[2]))
        self.assertEqual(status, 200)
        self.assertEqual(len(out['runs']), 1)

    def test_notify_test_endpoint(self):
        sent = {}

        def fake_send(cfg, result, proxy=''):
            sent['proxy'] = proxy
            return {'pushplus': 'ok'}

        orig = SV.notify_mod.send_all
        SV.notify_mod.send_all = fake_send
        try:
            out = self.json('POST', '/api/notify/test', {'channel': 'pushplus'})
        finally:
            SV.notify_mod.send_all = orig
        self.assertIn('pushplus', out['report'])


class TestRecordingEndpoints(WebCase):
    def test_full_record_flow_via_api(self):
        self.json('POST', '/api/record/start',
                  {'id': 'n1', 'name': '新站', 'start_url': 'https://a.b/'})
        out = self.json('POST', '/api/record/events', {'id': 'n1', 'events': [
            {'kind': 'click', 'info': {'tag': 'button', 'id': 'go'}}]})
        self.assertEqual(out['accepted'], 1)
        st = self.json('GET', '/api/record/status', query={'id': ['n1']})
        self.assertTrue(st['running'])
        self.assertEqual(st['event_count'], 1)
        stop = self.json('POST', '/api/record/stop', {'id': 'n1', 'save': True})
        self.assertTrue(stop['saved'])
        site = self.svc.get_site('n1')
        self.assertTrue(site.steps)

    def test_cancel_endpoint(self):
        self.json('POST', '/api/record/start',
                  {'id': 'n2', 'name': 'N2', 'start_url': 'https://a.b/'})
        out = self.json('POST', '/api/record/cancel', {'id': 'n2'})
        self.assertTrue(out['ok'])


class TestHtmlPage(unittest.TestCase):
    def test_inline_scripts_are_balanced(self):
        """用 HTML 解析器验证：内联脚本必须成对，内容不能提前闭合。

        （踩过的坑：生成的脚本字符串里含结束标签字面量，浏览器会提前闭合 script 元素，
          整段脚本静默失效。这里用真正的解析器来判定，比数字符更靠谱。）
        """
        from html.parser import HTMLParser

        class P(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.depth = 0
                self.max_depth = 0
                self.script_chunks = []
                self._buf = []

            def handle_starttag(self, tag, attrs):
                if tag == 'script':
                    self.depth += 1
                    self.max_depth = max(self.max_depth, self.depth)
                    self._buf = []

            def handle_endtag(self, tag):
                if tag == 'script':
                    self.script_chunks.append(''.join(self._buf))
                    self.depth -= 1

            def handle_data(self, data):
                if self.depth:
                    self._buf.append(data)

        p = P()
        p.feed(WB.INDEX_HTML)
        self.assertEqual(p.depth, 0, 'script 标签没有成对闭合')
        self.assertGreaterEqual(p.max_depth, 1, '应该至少有一个 script 块')
        self.assertGreaterEqual(len(p.script_chunks), 1)
        # 每个脚本块内部都不能再出现结束标签（那就意味着被提前切断了）
        for chunk in p.script_chunks:
            self.assertNotIn('<' + '/script>', chunk)

    def test_recorder_script_has_no_tag_closer(self):
        """录制脚本会被拼进响应，里面同样不能有结束标签字面量。"""
        self.assertNotIn('<' + '/script>', RC.RECORDER_SCRIPT)

    def test_page_has_required_tabs(self):
        for t in ('tab-sites', 'tab-add', 'tab-settings'):
            self.assertIn(t, WB.INDEX_HTML)
        for label in ('新增站点', '开始录制', '结束并保存', '测试连接',
                      '立即备份', '发送测试通知'):
            self.assertIn(label, WB.INDEX_HTML)

    def test_page_escapes_helpfully(self):
        self.assertIn('function esc(', WB.INDEX_HTML)


class TestSchedulerLoop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='autocheckin_sch_')
        self.calls = []

        def fake_checkin(site, proxy, password, browser, cookie_header=None):
            self.calls.append(site.id)
            return CheckinResult(site.id, site.name, True, 'ok')

        def factory(svc):
            from app.runner import Runner
            return Runner(svc.store, box=svc.box, proxy='',
                          checkin_fn=fake_checkin,
                          notify_fn=lambda *a: {},
                          now_fn=lambda: 1_700_000_000,
                          rng=__import__('random').Random(1))

        self.svc = SV.Service(self.dir, env_key=S.generate_key(),
                              runner_factory=factory)
        self.now = 1_700_000_000.0
        self.sch = SCH.Scheduler(self.svc, interval=60,
                                 now_fn=lambda: self.now)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_first_tick_schedules_without_running(self):
        """启动时只排程，不立即签到（用户明确要求）。"""
        out = self.sch.tick()
        self.assertIsNone(out)
        self.assertEqual(self.calls, [], '启动时不应立即签到')
        state = self.svc.load_state()
        for s in self.svc.load_config().sites:
            self.assertGreater(state.next_run_at(s.id), int(self.now))

    def test_tick_runs_when_due(self):
        self.sch.tick()                       # 排程
        state = self.svc.load_state()
        for s in self.svc.load_config().sites:
            state.set_next_run_at(s.id, int(self.now) - 1)
        self.svc.store.save_state(state)
        out = self.sch.tick()
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(out['summary'].count('成功'), 1)

    def test_tick_reports_no_due(self):
        self.sch.tick()
        self.assertIsNone(self.sch.tick())

    def test_tick_swallows_errors_and_records(self):
        orig = self.svc.load_config
        self.svc.load_config = lambda: (_ for _ in ()).throw(RuntimeError('坏了'))
        try:
            self.assertIsNone(self.sch.tick())
            self.assertIn('坏了', self.sch.last_error)
        finally:
            self.svc.load_config = orig

    def test_status_shape(self):
        st = self.sch.status()
        for k in ('running', 'interval_seconds', 'ticks', 'last_tick_at', 'last_error'):
            self.assertIn(k, st)
        self.assertFalse(st['running'])

    def test_start_stop_thread(self):
        self.sch.interval = 3600              # 避免测试期间反复 tick
        self.assertTrue(self.sch.start())
        self.assertTrue(self.sch.running)
        self.assertFalse(self.sch.start(), '重复启动应返回 False')
        self.sch.stop()
        self.assertFalse(self.sch.running)

    def test_minimum_interval_enforced(self):
        s = SCH.Scheduler(self.svc, interval=1)
        self.assertGreaterEqual(s.interval, 5)

    def test_tick_calls_plugin_sync(self):
        """每次 tick 都要问一次"该不该同步模板"（内部自限）。

        没这条测试的话，很容易出现"功能写好了但没接进调度"，
        表现为开关打开却永远不同步。
        """
        calls = []
        orig = self.svc.maybe_sync_plugins
        self.svc.maybe_sync_plugins = lambda *a, **k: calls.append(1) or {}
        try:
            self.sch.tick()
        finally:
            self.svc.maybe_sync_plugins = orig
        self.assertEqual(len(calls), 1, 'tick 应调用一次插件同步检查')

    def test_plugin_sync_error_does_not_break_tick(self):
        """同步失败绝不能影响签到（否则网络问题会让签到也停）。"""
        def boom(*a, **k):
            raise RuntimeError('同步炸了')
        orig = self.svc.maybe_sync_plugins
        self.svc.maybe_sync_plugins = boom
        try:
            self.assertIsNone(self.sch.tick())       # 不抛出去
        finally:
            self.svc.maybe_sync_plugins = orig


class TestHttpServerEndToEnd(unittest.TestCase):
    """用真实 HTTP 走一遍，确认服务器层能通。

    注意：本类需要真实 DNS 解析（连 127.0.0.1）。而其它测试模块会临时替换
    socket.getaddrinfo（强制 IPv4 / DNS 覆盖）。如果哪个用例还原不干净，
    这里就会以 "getaddrinfo failed" 的形式失败，且单独跑本文件却是通过的。
    所以这里在 setUp 里主动还原一次，保证网络测试不受别人影响。
    """

    def setUp(self):
        import socket
        from app import netutil

        # 还原可能被其它测试模块改过的 DNS 解析。
        # （别的用例会替换 socket.getaddrinfo 做 IPv4 强制 / DNS 覆盖，
        #   若还原不干净，这里就会以 "getaddrinfo failed" 失败，
        #   而单独跑本文件却通过 —— 很难查的一类失败。）
        socket.getaddrinfo = netutil._TRUE_GETADDRINFO
        netutil._REAL_GETADDRINFO = netutil._TRUE_GETADDRINFO
        netutil.force_ipv4._depth = 0
        netutil.dns_override._depth = 0
        netutil.dns_override._saved = None

        self.dir = tempfile.mkdtemp(prefix='autocheckin_http_')
        self.svc = SV.Service(self.dir, env_key=S.generate_key(), version='1.0')
        self.app = WB.WebApp(self.svc, version='1.0')
        self.httpd = WB.serve(self.app, host='127.0.0.1', port=0, block=False)
        self.port = self.httpd.server_address[1]

        # 显式直连的 opener：不受环境变量里 http_proxy 的影响。
        # 认证改成基于 Cookie 会话后，这里也要带上 CookieJar，
        # 否则每个请求都会因为"未登录"被 403 拦住。
        import urllib.request
        import http.cookiejar
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar))
        # 完成口令初始化，拿到会话 Cookie
        self._post('/api/auth/setup', {'password': 'http-e2e-pw'})

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _get(self, path):
        with self.opener.open('http://127.0.0.1:%d%s' % (self.port, path),
                              timeout=10) as r:
            return r.status, r.read()

    def _post(self, path, payload):
        import urllib.request
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (self.port, path),
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with self.opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode('utf-8'))

    def test_index_served(self):
        status, body = self._get('/')
        self.assertEqual(status, 200)
        self.assertIn('自动签到', body.decode('utf-8'))

    def test_healthz(self):
        status, body = self._get('/healthz')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])

    def test_api_overview(self):
        status, body = self._get('/api/overview')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['site_count'], 3)

    def test_api_post_site(self):
        status, body = self._post('/api/site', {'id': 'zz', 'name': 'ZZ'})
        self.assertEqual(status, 200)
        self.assertEqual(body['site']['id'], 'zz')

    def test_api_error_status(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            'http://127.0.0.1:%d/api/nope' % self.port, method='GET')
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.opener.open(req, timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_recorder_script_served(self):
        status, body = self._get('/api/record/script.js')
        self.assertEqual(status, 200)
        self.assertIn(b'__autocheckinRecorder', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
