"""AI 分析附带页面片段（默认关）与模板来源默认值。

两块都是"便利性 vs 隐私/成本"的取舍，所以要用测试把边界钉住：

  1) 页面片段默认**不发送**；只有开关打开才进提示词；
  2) 进提示词之前必须脱敏（去标签、抹 Cookie/token/邮箱/长数字、截断）；
  3) 片段只活在 CheckinResult 上，不落历史、不进通知；
  4) 「模板来源」默认填项目地址，手动同步在留空时也能用。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import deepseek as D  # noqa: E402
from app import secrets as S  # noqa: E402
from app.engine import run_http_flow, checkin_site  # noqa: E402
from app.httpclient import HttpResponse  # noqa: E402
from app.models import AiConfig, KIND_CUSTOM, SiteConfig  # noqa: E402
from app.notify import CheckinResult  # noqa: E402
from app.pluginsync import DEFAULT_TEMPLATE_SOURCE  # noqa: E402
from app.runner import _default_analyze  # noqa: E402
from app.service import Service  # noqa: E402


class TestRedact(unittest.TestCase):
    def test_strips_tags_and_scripts(self):
        raw = ('<html><head><style>body{color:red}</style>'
               '<script>var t="secret-token-123"</script></head>'
               '<body><p>今天已经签到</p></body></html>')
        out = D.redact_page_text(raw)
        self.assertIn('今天已经签到', out)
        self.assertNotIn('<p>', out)
        self.assertNotIn('color:red', out)
        self.assertNotIn('secret-token-123', out)

    def test_masks_secrets_email_and_long_numbers(self):
        raw = ('Cookie: PB3_SESSION=abcdef123456; '
               'Authorization: Bearer sk-xxxxxxxx '
               'token=deadbeef user mail me@example.com uid 123456789012')
        out = D.redact_page_text(raw)
        self.assertNotIn('abcdef123456', out)
        self.assertNotIn('sk-xxxxxxxx', out)
        self.assertNotIn('deadbeef', out)
        self.assertNotIn('me@example.com', out)
        self.assertNotIn('123456789012', out)
        self.assertIn('(已省略)', out)

    def test_truncates(self):
        out = D.redact_page_text('字' * 10000, limit=100)
        self.assertEqual(len(out), 100)

    def test_empty(self):
        self.assertEqual(D.redact_page_text(''), '')


class TestEngineCarriesExcerpt(unittest.TestCase):
    def _fetcher(self, text='<html><body>什么也没有</body></html>'):
        def fetch(method, url, data):
            return HttpResponse(status=200, text=text, url=url)
        return fetch

    def test_unclear_outcome_has_excerpt(self):
        out = run_http_flow(self._fetcher(), [{'action': 'get', 'url': 'https://a.b/'}],
                            success_kw=['成功'], fail_kw=['失败'])
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'unclear')
        self.assertIn('什么也没有', out.page_excerpt)

    def test_success_has_no_excerpt(self):
        out = run_http_flow(
            self._fetcher('签到成功'), [{'action': 'get', 'url': 'https://a.b/'}],
            success_kw=['签到成功'], fail_kw=[])
        self.assertTrue(out.success)
        self.assertEqual(out.page_excerpt, '', '成功时不该占内存')

    def test_checkin_result_carries_excerpt_on_failure(self):
        from app.engine import FlowOutcome
        fo = FlowOutcome(False, '判定不了结果', 'unknown',
                         error_kind='unclear', page_excerpt='页面线索')
        with mock.patch('app.engine.checkin_via_http', return_value=fo):
            site = SiteConfig(id='s1', name='S1', kind=KIND_CUSTOM,
                              homepage='https://a.b/')
            res = checkin_site(site)
        self.assertFalse(res.success)
        self.assertEqual(res.page_excerpt, '页面线索')

    def test_excerpt_not_written_to_history(self):
        """片段绝不能进 history.jsonl（那里会被备份、被界面展示）。"""
        from app.history import History
        d = tempfile.mkdtemp()
        h = History(d)
        res = CheckinResult('s1', 'S1', False, '判定不了结果',
                            page_excerpt='绝密页面片段XYZ')
        h.append([h.make_entry(site_id='s1', site_name='S1', ok=False,
                               msg=res.message, error_kind=res.error_kind)])
        with open(h.path, encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('绝密页面片段XYZ', raw)


class TestAnalyzeSwitch(unittest.TestCase):
    def _result(self):
        return CheckinResult('s1', 'S1', False, '判定不了结果',
                             page_excerpt='页面正文线索')

    def test_off_by_default(self):
        cfg = AiConfig()
        self.assertFalse(cfg.include_page_excerpt)
        with mock.patch.object(D, 'analyze', return_value='x') as m:
            _default_analyze(SiteConfig(id='s1', name='S1'), self._result(),
                             cfg, 'k', '')
        self.assertEqual(m.call_args.kwargs.get('page_excerpt'), '')

    def test_on_forwards_excerpt(self):
        cfg = AiConfig(include_page_excerpt=True)
        with mock.patch.object(D, 'analyze', return_value='x') as m:
            _default_analyze(SiteConfig(id='s1', name='S1'), self._result(),
                             cfg, 'k', '')
        self.assertEqual(m.call_args.kwargs.get('page_excerpt'), '页面正文线索')

    def test_config_roundtrip(self):
        cfg = AiConfig(include_page_excerpt=True)
        again = AiConfig.from_dict(cfg.to_dict())
        self.assertTrue(again.include_page_excerpt)
        # 认不出来的字符串收敛成默认（False），而不是崩掉
        self.assertFalse(AiConfig.from_dict(
            {'include_page_excerpt': 'maybe'}).include_page_excerpt)


class TestPromptHasExcerpt(unittest.TestCase):
    def test_prompt_section_only_when_given(self):
        site = SiteConfig(id='s1', name='S1')
        res = CheckinResult('s1', 'S1', False, 'x')
        self.assertNotIn('页面内容片段', D.build_prompt(site, res))
        self.assertIn('页面内容片段',
                      D.build_prompt(site, res, '这里是一段页面正文'))

    def test_analyze_redacts_before_prompt(self):
        """即使调用方直接传原始 HTML，analyze 也必须先脱敏。"""
        captured = {}

        def transport(url, body, headers, timeout, proxy):
            import json as _j
            captured['body'] = _j.loads(body.decode('utf-8'))
            return HttpResponse(status=200, text=_j.dumps({
                'choices': [{'message': {'content': '{"ok":true}'}}],
                'model': 'm'}), url=url)

        D.analyze(SiteConfig(id='s1', name='S1'),
                  CheckinResult('s1', 'S1', False, 'x'),
                  AiConfig(enabled=True), 'k',
                  page_excerpt='<b>正文</b> Cookie: a=SECRET123456',
                  transport=transport)
        prompt = captured['body']['messages'][-1]['content']
        self.assertIn('正文', prompt)
        self.assertNotIn('SECRET123456', prompt)


class TestDefaultTemplateSource(unittest.TestCase):
    def test_constant_points_at_project(self):
        self.assertIn('site-plugins', DEFAULT_TEMPLATE_SOURCE)
        self.assertIn('autocheckin', DEFAULT_TEMPLATE_SOURCE)

    def test_manual_sync_falls_back_to_default(self):
        d = tempfile.mkdtemp()
        svc = Service(d, version='t', env_key=S.generate_key(), proxy='')
        seen = {}

        class FakeSync:
            def sync(self, src, only=None):
                seen['src'] = src
                seen['only'] = only
                return {'added': [], 'updated': [], 'removed': [], 'errors': []}

        with mock.patch.object(Service, '_plugin_sync', return_value=FakeSync()):
            svc.sync_plugins(source='')

        self.assertEqual(seen['src'], DEFAULT_TEMPLATE_SOURCE)


if __name__ == '__main__':
    unittest.main()
