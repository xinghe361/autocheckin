"""DeepSeek 模型选择的测试。

为什么值得测：
    设置里的"模型"原来是个自由文本框、默认 `deepseek-chat`。
    而官方文档现在的模型表里只有 `deepseek-flash` 与 `deepseek-v4-pro`；
    旧名字可能仍能调用，但会被服务端**静默映射**成别的模型 ——
    用户填 chat、实际跑的可能是 flash，价格与能力都不同。
    所以需要：
      1. 能列出账号真实可用的模型（验证 key + 看清选项）
      2. 记录并显示"服务端实际返回的模型名"，而不是只信自己填的
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import deepseek as D                                     # noqa: E402
from app.httpclient import HttpResponse                            # noqa: E402
from app.models import AiConfig, SiteConfig                        # noqa: E402
from app.notify import CheckinResult                               # noqa: E402

SITE = SiteConfig(id='s1', name='S1')
RESULT = CheckinResult('s1', 'S1', False, '失败了')


def transport_returning(models=None, chat=None, status=200):
    def t(url, body, headers, timeout, proxy):
        if url.endswith('/models'):
            return HttpResponse(status, json.dumps(models or {}), url)
        return HttpResponse(status, json.dumps(chat or {}), url)
    return t


class TestDefaultModel(unittest.TestCase):
    def test_default_is_a_current_name(self):
        """默认值应是官方当前文档里的名字，而不是已下线的旧名。

        改回去会怎样：用户在一个"不知道实际会调到什么"的名字上做默认选择。
        """
        self.assertIn(D.DEEPSEEK_DEFAULT_MODEL,
                      ('deepseek-flash', 'deepseek-v4-pro'))

    def test_request_uses_configured_model(self):
        seen = {}

        def t(url, body, headers, timeout, proxy):
            seen['body'] = json.loads(body.decode())
            return HttpResponse(200, json.dumps({
                'model': 'DeepSeek-V4.1-Flash',
                'choices': [{'message': {'content': json.dumps(
                    {'analysis': 'ok', 'confidence': 50})}}]}), url)

        ai = AiConfig(model='deepseek-v4-pro')
        out = D.analyze(SITE, RESULT, ai, 'k', transport=t)
        self.assertEqual(seen['body']['model'], 'deepseek-v4-pro')
        self.assertEqual(out.requested_model, 'deepseek-v4-pro')

    def test_empty_model_falls_back_to_default(self):
        seen = {}

        def t(url, body, headers, timeout, proxy):
            seen['body'] = json.loads(body.decode())
            return HttpResponse(200, json.dumps({
                'choices': [{'message': {'content': json.dumps(
                    {'analysis': 'x', 'confidence': 1})}}]}), url)

        D.analyze(SITE, RESULT, AiConfig(model=''), 'k', transport=t)
        self.assertEqual(seen['body']['model'], D.DEEPSEEK_DEFAULT_MODEL)


class TestServedModelReported(unittest.TestCase):
    def test_served_model_recorded(self):
        """服务端返回的 model 要记录下来 —— 它才是真正回答的模型。"""
        t = transport_returning(chat={
            'model': 'DeepSeek-V4.1-Flash',
            'choices': [{'message': {'content': json.dumps(
                {'analysis': 'ok', 'confidence': 70})}}]})
        out = D.analyze(SITE, RESULT, AiConfig(model='deepseek-chat'), 'k',
                        transport=t)
        self.assertTrue(out.ok)
        self.assertEqual(out.requested_model, 'deepseek-chat')
        self.assertEqual(out.served_model, 'DeepSeek-V4.1-Flash')

    def test_mapping_is_surfaced_in_note(self):
        """映射发生时，通知文本里要明确写出来。

        改回去会怎样：用户付了 A 模型的钱、实际跑的是 B，却完全看不出来。
        """
        from app.models import AppConfig
        from app.runner import Runner
        from app.secrets import SecretBox, generate_key
        from app.store import Store, RuntimeState
        import tempfile

        store = Store(tempfile.mkdtemp(prefix='ac_model_'))
        box = SecretBox(generate_key())
        cfg = AppConfig()
        cfg.ai = AiConfig(enabled=True, model='deepseek-chat',
                          max_calls_per_day=0,
                          api_key_enc=box.encrypt('fake-key'))
        site = SiteConfig(id='s1', name='S1', ai_enabled=True,
                          state={'consecutive_failures': 2})

        def fake_analyze(site_, result_, ai_cfg, key, proxy):
            return D.AiSuggestion(True, analysis='建议重试',
                                  requested_model='deepseek-chat',
                                  served_model='DeepSeek-V4.1-Flash')

        r = Runner(store, box=box, global_proxy_mode='direct')
        r.analyze_fn = fake_analyze
        r.now_fn = lambda: 1000000
        st = RuntimeState()
        note = r._maybe_analyze(site, cfg, st, RESULT, 1000000)
        self.assertIn('DeepSeek-V4.1-Flash', note)
        self.assertIn('deepseek-chat', note)
        self.assertIn('映射', note)


class TestListModels(unittest.TestCase):
    def test_lists_ids(self):
        t = transport_returning(models={'data': [
            {'id': 'deepseek-v4-pro'}, {'id': 'deepseek-flash'}]})
        out = D.list_models(AiConfig(), 'k', transport=t)
        self.assertTrue(out['ok'])
        self.assertEqual(out['models'], ['deepseek-flash', 'deepseek-v4-pro'])

    def test_http_error_reported(self):
        t = transport_returning(models={'error': {'message': 'bad key'}},
                                status=401)
        out = D.list_models(AiConfig(), 'k', transport=t)
        self.assertFalse(out['ok'])
        self.assertEqual(out['status'], 401)
        self.assertIn('401', out['error'])

    def test_bad_json_reported(self):
        def t(url, body, headers, timeout, proxy):
            return HttpResponse(200, 'not json', url)
        out = D.list_models(AiConfig(), 'k', transport=t)
        self.assertFalse(out['ok'])
        self.assertIn('JSON', out['error'])

    def test_empty_list_is_ok(self):
        out = D.list_models(AiConfig(), 'k',
                            transport=transport_returning(models={}))
        self.assertTrue(out['ok'])
        self.assertEqual(out['models'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
