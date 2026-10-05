"""隐私泄露回归测试。

都是实际查出来的问题，不是假想：
  1) 录制的登录步骤里，用户输入的密码被原样存进 steps[].value ——
     明文落在 config.json、随 WebDAV 备份上传、还会随 AI 提示词
     发给 DeepSeek（第三方模型）。
  2) 因此录制器对密码框改记 {password} 占位符；
     发给 AI 的提示词一律不带上填写内容。
  3) 以前录好的数据仍是明文 —— 用"精确等于该站点密码"的匹配做迁移，
     不做"看起来像密码"的启发式猜测（猜错会改坏正常内容）。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S                                     # noqa: E402
from app.deepseek import build_prompt, redact_steps               # noqa: E402
from app.models import SiteConfig, Step                          # noqa: E402
from app.recorder import StepRecorder, _is_secret_input           # noqa: E402
from app.runner import CheckinResult                             # noqa: E402
from app.service import Service                                  # noqa: E402

PW = 'SUPER_SECRET_PW'


def rec(events):
    r = StepRecorder()
    for e in events:
        r.add_dict(e)
    return r.to_steps('https://x/login')


class TestSecretInputDetection(unittest.TestCase):
    def test_type_password(self):
        self.assertTrue(_is_secret_input({'type': 'password'}))
        self.assertTrue(_is_secret_input({'type': 'PASSWORD'}))

    def test_keyword_fallback(self):
        """有些站点用 type=text 配 autocomplete 装密码，光看 type 会漏。"""
        for info in ({'name': 'password'}, {'id': 'user_pwd'},
                     {'name': 'passwd'}, {'placeholder': '请输入密码'},
                     {'aria-label': '验证码'}, {'id': 'api_token'}):
            with self.subTest(info=info):
                self.assertTrue(_is_secret_input(info))

    def test_ordinary_fields_not_secret(self):
        for info in ({'id': 'u', 'type': 'text'}, {'id': 'keyword'},
                     {'name': 'email'}, {'id': 'page'}, {}, None, 'x'):
            with self.subTest(info=info):
                self.assertFalse(_is_secret_input(info))


class TestRecorderDoesNotStorePasswords(unittest.TestCase):
    def test_password_field_becomes_placeholder(self):
        steps = rec([
            {'kind': 'navigate', 'url': 'https://x/login'},
            {'kind': 'input', 'info': {'tag': 'input', 'id': 'u',
                                       'type': 'text'}, 'value': 'myuser'},
            {'kind': 'input', 'info': {'tag': 'input', 'id': 'p',
                                       'type': 'password'}, 'value': PW},
        ])
        vals = [s.value for s in steps]
        self.assertNotIn(PW, vals, '密码明文不该进步骤')
        self.assertIn('{password}', vals)

    def test_placeholder_survives_continuous_typing_merge(self):
        """连续输入会合并成一步 —— 合并后也不能变回明文。"""
        steps = rec([
            {'kind': 'input', 'info': {'tag': 'input', 'id': 'p',
                                       'type': 'password'}, 'value': 'SUP'},
            {'kind': 'input', 'info': {'tag': 'input', 'id': 'p',
                                       'type': 'password'}, 'value': PW},
        ])
        fills = [s for s in steps if s.action == 'fill']
        self.assertEqual(len(fills), 1, '同一输入框应合并成一步')
        self.assertEqual(fills[0].value, '{password}')

    def test_normal_input_kept(self):
        steps = rec([{'kind': 'input', 'info': {'tag': 'input', 'id': 'kw'},
                      'value': '每日签到'}])
        # 录制器会自动补一步 goto，所以按动作找 fill 而不是取 [0]
        fills = [s for s in steps if s.action == 'fill']
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].value, '每日签到',
                         '普通输入框的内容不该被改成占位符')


class TestRedactStepsForAi(unittest.TestCase):
    def test_fill_values_redacted(self):
        steps = [Step(action='goto', target='https://x/'),
                 Step(action='fill', target='#p', value=PW)]
        got = redact_steps(steps)
        self.assertNotIn(PW, json.dumps(got, ensure_ascii=False))
        self.assertEqual(got[1]['value'], '(已省略)')
        # 选择器要保留：AI 靠它理解流程
        self.assertEqual(got[1]['target'], '#p')

    def test_placeholder_kept(self):
        steps = [Step(action='fill', target='#p', value='{password}')]
        self.assertEqual(redact_steps(steps)[0]['value'], '{password}')

    def test_prompt_has_no_password(self):
        site = SiteConfig(id='x', name='X', kind='custom',
                          homepage='https://x/', need_browser=True,
                          steps=[Step(action='fill', target='#p', value=PW)])
        res = CheckinResult(site_id='x', site_name='X', success=False,
                            message='未登录')
        prompt = build_prompt(site, res)
        self.assertNotIn(PW, prompt, '提示词不能带登录密码（会发给第三方模型）')

    def test_redact_tolerates_dicts_and_junk(self):
        got = redact_steps([{'action': 'fill', 'value': 'x'},
                            {'action': 'fill', 'value': '{username}'},
                            None])
        self.assertEqual(len(got), 2)      # None 被跳过
        self.assertEqual(got[0]['value'], '(已省略)')


class TestLegacyPasswordMigration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_priv_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')

    def _cfg_text(self):
        with open(os.path.join(self.dir, 'config.json'), encoding='utf-8') as f:
            return f.read()

    def test_exact_match_replaced(self):
        self.svc.upsert_site({
            'id': 'x', 'name': 'X', 'homepage': 'https://x/', 'kind': 'custom',
            'need_browser': True, 'password': PW,
            'steps': [{'action': 'fill', 'target': '#p', 'value': PW}]})
        self.assertNotIn(PW, self._cfg_text(),
                         '与站点密码相同的字面值应被换成占位符')
        site = self.svc.load_config().site('x')
        self.assertEqual(site.steps[0].value, '{password}')

    def test_no_false_positives(self):
        """不等于密码的内容一律不动 —— 不做启发式猜测。"""
        self.svc.upsert_site({
            'id': 'x', 'name': 'X', 'homepage': 'https://x/', 'kind': 'custom',
            'need_browser': True, 'username': 'myuser', 'password': PW,
            'steps': [{'action': 'fill', 'target': '#u', 'value': 'myuser'},
                      {'action': 'fill', 'target': '#kw', 'value': '每日签到'}]})
        site = self.svc.load_config().site('x')
        vals = {s.target: s.value for s in site.steps}
        self.assertEqual(vals['#u'], 'myuser')
        self.assertEqual(vals['#kw'], '每日签到')

    def test_no_password_configured_leaves_steps_alone(self):
        """站点没配密码时无从比较，绝不能瞎改。"""
        self.svc.upsert_site({
            'id': 'x', 'name': 'X', 'homepage': 'https://x/', 'kind': 'custom',
            'need_browser': True,
            'steps': [{'action': 'fill', 'target': '#a', 'value': 'somevalue'}]})
        site = self.svc.load_config().site('x')
        self.assertEqual(site.steps[0].value, 'somevalue')

    def test_encrypted_password_used_for_comparison(self):
        """比较用的是**解密后**的密码，不是密文。"""
        self.svc.upsert_site({
            'id': 'x', 'name': 'X', 'homepage': 'https://x/', 'kind': 'custom',
            'password': PW,
            'steps': [{'action': 'fill', 'target': '#p', 'value': PW}]})
        site = self.svc.load_config().site('x')
        # 密码本身仍是可解密的（功能没坏）
        self.assertEqual(self.svc.box.try_decrypt(site.password_enc, ''), PW)

    def test_backup_has_no_plaintext_password(self):
        self.svc.upsert_site({
            'id': 'x', 'name': 'X', 'homepage': 'https://x/', 'kind': 'custom',
            'password': PW,
            'steps': [{'action': 'fill', 'target': '#p', 'value': PW}]})
        exp = self.svc.store.export_config(self.svc.load_config())
        self.assertNotIn(PW, json.dumps(exp, ensure_ascii=False),
                         '备份里不该出现录制的明文密码')


if __name__ == '__main__':
    unittest.main(verbosity=2)
