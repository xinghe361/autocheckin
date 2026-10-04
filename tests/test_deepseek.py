"""deepseek 单测：JSON 容错解析、补丁白名单、提示词构造、失败处理。

全部用注入的假 transport，不真调外部 API。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import deepseek as D  # noqa: E402
from app.httpclient import HttpResponse  # noqa: E402
from app.models import AiConfig, SiteConfig, Step  # noqa: E402
from app.notify import CheckinResult  # noqa: E402


def ok_resp(content):
    """构造一个 OpenAI 风格的正常返回。"""
    return HttpResponse(status=200, url='https://api.deepseek.com/chat/completions',
                        text=json.dumps({
                            'choices': [{'message': {'content': content}}]}),
                        headers={})


class TestExtractJson(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(D.extract_json('{"a":1}'), {'a': 1})

    def test_markdown_fence(self):
        self.assertEqual(D.extract_json('```json\n{"a":1}\n```'), {'a': 1})

    def test_fence_without_language(self):
        self.assertEqual(D.extract_json('```\n{"a":1}\n```'), {'a': 1})

    def test_json_with_surrounding_text(self):
        got = D.extract_json('好的，这是结果：\n{"a":1}\n希望有帮助')
        self.assertEqual(got, {'a': 1})

    def test_trailing_comma_repaired(self):
        got = D.extract_json('{"a":1,"b":[1,2,],}')
        self.assertEqual(got, {'a': 1, 'b': [1, 2]})

    def test_non_object_returns_none(self):
        self.assertIsNone(D.extract_json('[1,2,3]'))

    def test_garbage_returns_none(self):
        self.assertIsNone(D.extract_json('完全不是 JSON'))
        self.assertIsNone(D.extract_json(''))
        self.assertIsNone(D.extract_json(None))

    def test_nested_object(self):
        got = D.extract_json('{"patch":{"steps":[{"action":"click"}]}}')
        self.assertEqual(got['patch']['steps'][0]['action'], 'click')


class TestSanitizePatch(unittest.TestCase):
    def test_only_whitelisted_fields_survive(self):
        got = D.sanitize_patch({
            'need_browser': True,
            'homepage': 'https://a.b/',
            'id': 'HACKED',              # 不在白名单
            'password_enc': 'HACKED',    # 不在白名单
            'username': 'HACKED',        # 不在白名单
        })
        self.assertEqual(set(got), {'need_browser', 'homepage'})

    def test_keyword_lists_coerced(self):
        got = D.sanitize_patch({'success_keywords': ['a', '', 'b', 1]})
        self.assertEqual(got['success_keywords'], ['a', 'b', '1'])

    def test_steps_normalized(self):
        got = D.sanitize_patch({'steps': [
            {'action': 'click', 'target': '#b'},
            {'target': 'no-action'},          # 缺 action，丢弃
            'not-a-dict',                     # 丢弃
        ]})
        self.assertEqual(len(got['steps']), 1)
        self.assertEqual(got['steps'][0]['action'], 'click')
        self.assertEqual(got['steps'][0]['timeout_ms'], 15000)
        self.assertFalse(got['steps'][0]['optional'])

    def test_numeric_clamping(self):
        got = D.sanitize_patch({
            'daily_hour': 99, 'daily_minute': -5,
            'jitter_seconds': 999999, 'retry_count': -3,
            'retry_interval_minutes': 100000,
        })
        self.assertEqual(got['daily_hour'], 23)
        self.assertEqual(got['daily_minute'], 0)
        self.assertEqual(got['jitter_seconds'], 3600)
        self.assertEqual(got['retry_count'], 0)
        self.assertEqual(got['retry_interval_minutes'], 1440)

    def test_bad_numeric_skipped(self):
        got = D.sanitize_patch({'daily_hour': '不是数字'})
        self.assertNotIn('daily_hour', got)

    def test_non_dict_returns_empty(self):
        self.assertEqual(D.sanitize_patch(None), {})
        self.assertEqual(D.sanitize_patch('x'), {})
        self.assertEqual(D.sanitize_patch([1, 2]), {})

    def test_booleans_coerced(self):
        got = D.sanitize_patch({'need_browser': 1,
                                'jitter_enabled': 'yes'})
        self.assertIs(got['need_browser'], True)
        self.assertIs(got['jitter_enabled'], True)

    def test_verify_ssl_is_not_patchable(self):
        """AI 不能改 verify_ssl。

        改回去会怎样：模型可以自动把某个站点的 TLS 校验永久关掉，
        用户完全不知情就被削弱了传输安全。
        """
        got = D.sanitize_patch({'verify_ssl': False, 'need_browser': True})
        self.assertNotIn('verify_ssl', got, 'verify_ssl 不该被 AI 补丁改动')
        self.assertIn('need_browser', got)


class TestBuildPrompt(unittest.TestCase):
    def test_includes_key_context(self):
        site = SiteConfig(id='v2ex', name='V2EX', homepage='https://www.v2ex.com/',
                          success_keywords=['已领取'], fail_keywords=['请先登录'],
                          state={'consecutive_failures': 4})
        res = CheckinResult(site_id='v2ex', site_name='V2EX', success=False,
                            message='页面里没找到预期的令牌', error_kind='no_token',
                            attempt=3)
        p = D.build_prompt(site, res, page_excerpt='<html>Cloudflare</html>',
                           trace=['GET /mission/daily -> 200'])
        self.assertIn('V2EX', p)
        self.assertIn('页面里没找到预期的令牌', p)
        self.assertIn('no_token', p)
        self.assertIn('已领取', p)
        self.assertIn('<html>Cloudflare</html>', p)
        self.assertIn('GET /mission/daily -> 200', p)
        self.assertIn('只返回 JSON', p)

    def test_page_excerpt_truncated(self):
        site = SiteConfig(id='x', name='X')
        res = CheckinResult(site_id='x', site_name='X', success=False)
        p = D.build_prompt(site, res, page_excerpt='A' * 20000)
        # 截断到 4000 以内，避免提示词爆掉
        self.assertLess(p.count('A'), 5000)

    def test_custom_site_shows_steps(self):
        site = SiteConfig(id='c', name='C', kind='custom',
                          steps=[Step(action='click', target='#go')])
        p = D.build_prompt(site, CheckinResult('c', 'C', False))
        self.assertIn('#go', p)

    def test_no_steps_says_builtin(self):
        site = SiteConfig(id='c', name='C')
        p = D.build_prompt(site, CheckinResult('c', 'C', False))
        self.assertIn('内置模板', p)


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.site = SiteConfig(id='v2ex', name='V2EX',
                               success_keywords=['已领取'])
        self.res = CheckinResult(site_id='v2ex', site_name='V2EX', success=False,
                                 message='没找到令牌', error_kind='no_token')
        self.ai = AiConfig(enabled=True, api_key_enc='x')

    def test_successful_analysis(self):
        content = json.dumps({
            'analysis': '页面被 Cloudflare 拦截',
            'confidence': 85,
            'patch': {'need_browser': True, 'success_keywords': ['领取成功']},
            'reason': '纯请求拿不到页面',
        }, ensure_ascii=False)
        s = D.analyze(self.site, self.res, self.ai, api_key='k',
                      transport=lambda *a: ok_resp(content))
        self.assertTrue(s.ok, s.error)
        self.assertIn('Cloudflare', s.analysis)
        self.assertEqual(s.confidence, 85)
        self.assertTrue(s.patch['need_browser'])
        self.assertEqual(s.patch['success_keywords'], ['领取成功'])
        self.assertGreater(s.prompt_chars, 0)

    def test_no_api_key(self):
        s = D.analyze(self.site, self.res, self.ai, api_key='')
        self.assertFalse(s.ok)
        self.assertIn('API Key', s.error)

    def test_disabled(self):
        s = D.analyze(self.site, self.res, AiConfig(enabled=False), api_key='k')
        self.assertFalse(s.ok)
        self.assertIn('关闭', s.error)

    def test_http_error(self):
        s = D.analyze(self.site, self.res, self.ai, api_key='k',
                      transport=lambda *a: HttpResponse(status=401, text='bad key',
                                                        url='', headers={}))
        self.assertFalse(s.ok)
        self.assertIn('401', s.error)

    def test_empty_content(self):
        s = D.analyze(self.site, self.res, self.ai, api_key='k',
                      transport=lambda *a: ok_resp(''))
        self.assertFalse(s.ok)
        self.assertIn('为空', s.error)

    def test_non_json_content(self):
        s = D.analyze(self.site, self.res, self.ai, api_key='k',
                      transport=lambda *a: ok_resp('我觉得你应该检查一下选择器'))
        self.assertFalse(s.ok)
        self.assertIn('JSON', s.error)

    def test_transport_exception(self):
        def boom(*a):
            raise RuntimeError('网络炸了')
        s = D.analyze(self.site, self.res, self.ai, api_key='k', transport=boom)
        self.assertFalse(s.ok)
        self.assertIn('网络炸了', s.error)

    def test_confidence_clamped(self):
        for raw, want in ((150, 100), (-20, 0), ('abc', 0)):
            content = json.dumps({'analysis': 'a', 'confidence': raw, 'patch': {}})
            s = D.analyze(self.site, self.res, self.ai, api_key='k',
                          transport=lambda *a: ok_resp(content))
            self.assertEqual(s.confidence, want, 'confidence=%r' % raw)

    def test_malicious_patch_is_filtered(self):
        """模型试图改 id/凭据时必须被过滤掉。"""
        content = json.dumps({
            'analysis': 'x', 'confidence': 50,
            'patch': {'id': 'evil', 'password_enc': 'evil', 'need_browser': True},
        })
        s = D.analyze(self.site, self.res, self.ai, api_key='k',
                      transport=lambda *a: ok_resp(content))
        self.assertTrue(s.ok)
        self.assertEqual(set(s.patch), {'need_browser'})

    def test_markdown_wrapped_response_accepted(self):
        content = '```json\n{"analysis":"a","confidence":60,"patch":{}}\n```'
        s = D.analyze(self.site, self.res, self.ai, api_key='k',
                      transport=lambda *a: ok_resp(content))
        self.assertTrue(s.ok)
        self.assertEqual(s.confidence, 60)

    def test_authorization_header_sent(self):
        seen = {}

        def capture(url, body, headers, timeout, proxy):
            seen['url'] = url
            seen['headers'] = headers
            seen['body'] = body
            return ok_resp('{"analysis":"a","confidence":1,"patch":{}}')

        D.analyze(self.site, self.res, self.ai, api_key='SECRET',
                  transport=capture)
        self.assertTrue(seen['headers']['Authorization'].startswith('Bearer '))
        self.assertIn('SECRET', seen['headers']['Authorization'])
        self.assertTrue(seen['url'].endswith('/chat/completions'))
        sent = json.loads(seen['body'].decode('utf-8'))
        self.assertEqual(sent['model'], 'deepseek-chat')
        self.assertEqual(len(sent['messages']), 2)


class TestApplyPatch(unittest.TestCase):
    def test_applies_and_reports_changes(self):
        site = SiteConfig(id='x', name='X', need_browser=False,
                          success_keywords=['a'])
        changed = D.apply_patch(site, {'need_browser': True,
                                       'success_keywords': ['b', 'c']})
        self.assertIn('need_browser', changed)
        self.assertIn('success_keywords', changed)
        self.assertTrue(site.need_browser)
        self.assertEqual(site.success_keywords, ['b', 'c'])

    def test_unchanged_fields_not_reported(self):
        site = SiteConfig(id='x', name='X', need_browser=True)
        changed = D.apply_patch(site, {'need_browser': True})
        self.assertEqual(changed, [])

    def test_steps_converted_to_objects(self):
        site = SiteConfig(id='x', name='X')
        changed = D.apply_patch(site, {'steps': [
            {'action': 'goto', 'target': 'https://a.b/'},
            {'action': 'click', 'target': '#go'},
        ]})
        self.assertIn('steps', changed)
        self.assertEqual(len(site.steps), 2)
        self.assertIsInstance(site.steps[0], Step)
        self.assertEqual(site.steps[1].target, '#go')

    def test_credentials_never_touched(self):
        site = SiteConfig(id='x', name='X', username='u', password_enc='enc')
        D.apply_patch(site, {'username': 'hacked', 'password_enc': 'hacked'})
        self.assertEqual(site.username, 'u')
        self.assertEqual(site.password_enc, 'enc')


class TestShouldAnalyze(unittest.TestCase):
    def test_threshold_and_toggle(self):
        site = SiteConfig(id='x', name='X', ai_enabled=True, ai_after_failures=3,
                          state={'consecutive_failures': 3})
        ai = AiConfig(enabled=True)
        self.assertTrue(D.should_analyze(site, ai))
        site.state['consecutive_failures'] = 2
        self.assertFalse(D.should_analyze(site, ai))

    def test_disabled_globally_or_per_site(self):
        site = SiteConfig(id='x', name='X', ai_enabled=True, ai_after_failures=1,
                          state={'consecutive_failures': 5})
        self.assertFalse(D.should_analyze(site, AiConfig(enabled=False)))
        site.ai_enabled = False
        self.assertFalse(D.should_analyze(site, AiConfig(enabled=True)))

    def test_daily_call_limit(self):
        """次数限制看全局 max_calls_per_day（每天最多分析几次；0 = 不限制）。"""
        site = SiteConfig(id='x', name='X', ai_enabled=True,
                          state={'consecutive_failures': 5})
        ai = AiConfig(enabled=True, ai_after_failures=1, max_calls_per_day=3)
        self.assertTrue(D.should_analyze(site, ai, calls_today=2))
        self.assertFalse(D.should_analyze(site, ai, calls_today=3))
        self.assertFalse(D.should_analyze(site, ai, calls_today=99))

    def test_zero_max_calls_means_unlimited(self):
        """max_calls_per_day=0 表示不限制次数（由失败倍数决定调用几次）。"""
        site = SiteConfig(id='x', name='X', ai_enabled=True,
                          state={'consecutive_failures': 5})
        ai = AiConfig(enabled=True, ai_after_failures=1, max_calls_per_day=0)
        self.assertTrue(D.should_analyze(site, ai, calls_today=0))
        self.assertTrue(D.should_analyze(site, ai, calls_today=99))

    def test_after_failures_zero_disables_analysis(self):
        """ai_after_failures=0 表示不启用 AI 分析。"""
        site = SiteConfig(id='x', name='X', ai_enabled=True,
                          state={'consecutive_failures': 5})
        ai = AiConfig(enabled=True, ai_after_failures=0)
        self.assertFalse(D.should_analyze(site, ai, calls_today=0))
        self.assertFalse(D.should_analyze(site, ai, calls_today=1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
