"""失败原因归类的测试。

这一层最容易出的错不是"崩了"，而是**归错类**：标签看着挺像，
点开建议却让人去改一个没问题的地方。所以重点测三件事：
  1. 明确的 error_kind 不会被消息里的字眼带偏；
  2. 只有 unclear/failed 这种"没信息量"的 kind 才回头翻消息；
  3. 表里每一条都得是完整的（有中文标签、有建议、category 合法）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import reasons as R                                        # noqa: E402


class TestClassifyByKind(unittest.TestCase):
    def test_http_status_families(self):
        cases = {
            'http_401': 'not_logged_in',
            'http_403': 'http_403',
            'http_404': 'http_4xx',
            'http_429': 'http_429',
            'http_451': 'http_451',
            'http_500': 'http_5xx',
            'http_503': 'http_5xx',
            'http_302': 'http_3xx',
        }
        for kind, want in cases.items():
            self.assertEqual(R.classify(kind)['code'], want, kind)

    def test_known_kinds_keep_their_category(self):
        self.assertEqual(R.classify('timeout')['category'], 'network')
        self.assertEqual(R.classify('proxy_refused')['category'], 'network')
        self.assertEqual(R.classify('step_failed')['category'], 'rule')
        self.assertEqual(R.classify('secret_error')['category'], 'auth')
        self.assertEqual(R.classify('browser_unavailable')['category'], 'env')

    def test_network_hint_comes_from_netutil(self):
        # 文案只在 netutil 里维护一份；这里改回硬编码就会和通知里的说法不一致
        from app.netutil import describe_network_error
        r = R.classify('timeout')
        self.assertEqual(r['hint'], describe_network_error('timeout'))

    def test_http_5xx_hint_keeps_raw_status(self):
        # 5xx 都归一类，但排查时原始状态码有用，不能丢
        r = R.classify('http_502')
        self.assertEqual(r['code'], 'http_5xx')
        self.assertIn('502', r['hint'])


class TestStrongKindBeatsMessage(unittest.TestCase):
    """明确的 kind 优先。站点正文里到处是"登录""失败"这种词。"""

    def test_http_403_with_login_word(self):
        r = R.classify('http_403', '请登录后访问')
        self.assertEqual(r['code'], 'http_403')

    def test_timeout_with_cookie_word(self):
        r = R.classify('timeout', 'cookie 已过期？')
        self.assertEqual(r['code'], 'timeout')

    def test_step_failed_with_rate_word(self):
        r = R.classify('step_failed', '操作过于频繁')
        self.assertEqual(r['code'], 'step_failed')


class TestWeakKindFallsBackToMessage(unittest.TestCase):
    """unclear 是最常见的失败 kind，真正的线索在消息里。"""

    def test_unclear_not_logged_in(self):
        r = R.classify('unclear', '未登录，请先登录')
        self.assertEqual(r['code'], 'not_logged_in')
        self.assertEqual(r['category'], 'auth')

    def test_failed_not_logged_in(self):
        r = R.classify('failed', '需要登录后才能签到')
        self.assertEqual(r['code'], 'not_logged_in')

    def test_empty_kind_rate_limited(self):
        r = R.classify('', '访问过于频繁，请稍后再试')
        self.assertEqual(r['code'], 'rate_limited')

    def test_empty_kind_captcha(self):
        r = R.classify('', '请输入验证码')
        self.assertEqual(r['code'], 'captcha')

    def test_exception_with_network_reason(self):
        r = R.classify('exception', '签到过程出错：timed out')
        self.assertEqual(r['code'], 'timeout')

    def test_expired_beats_plain_cookie(self):
        # "Cookie 已过期"要先判成过期（该重新登录），不能只说"Cookie 无效"
        r = R.classify('', 'Cookie 已过期')
        self.assertEqual(r['code'], 'cookie_expired')

    def test_unclear_without_clue_stays_unclear(self):
        r = R.classify('unclear', '')
        self.assertEqual(r['code'], 'unclear')
        self.assertEqual(r['category'], 'rule')


class TestUnknown(unittest.TestCase):
    def test_unknown_kind_is_merged_but_not_lost(self):
        r = R.classify('some_new_kind')
        self.assertEqual(r['code'], 'unknown')
        self.assertEqual(r['category'], 'other')
        self.assertIn('some_new_kind', r['hint'])

    def test_nothing_at_all(self):
        r = R.classify('', '')
        self.assertEqual(r['code'], 'unknown')

    def test_case_and_space_insensitive(self):
        self.assertEqual(R.classify('  TIMEOUT ')['code'], 'timeout')


class TestTableIntegrity(unittest.TestCase):
    """表完整性：漏一个字段就是界面上空白一格。"""

    def test_every_code_is_complete(self):
        for code, (label, cat, hint) in R._CODES.items():
            self.assertTrue(label, code)
            self.assertIn(cat, R.CATEGORIES, code)
            # 网络类的建议来自 netutil，这里可以是空的
            self.assertTrue(hint or code in R.NET_KINDS, code)

    def test_net_kinds_are_all_in_table(self):
        for code in R.NET_KINDS:
            self.assertIn(code, R._CODES, code)
            self.assertTrue(R.classify(code)['hint'], code)

    def test_every_category_has_label_and_color(self):
        for cat, (name, color) in R.CATEGORIES.items():
            self.assertTrue(name, cat)
            self.assertTrue(color.startswith('#'), cat)

    def test_keyword_targets_exist(self):
        for words, code in R._KEYWORDS:
            self.assertIn(code, R._CODES, code)
            self.assertTrue(words, code)


class TestMetaOf(unittest.TestCase):
    def test_only_requested_codes(self):
        m = R.meta_of(['timeout', 'not_logged_in'])
        self.assertEqual(set(m), {'timeout', 'not_logged_in'})

    def test_carries_color_for_frontend(self):
        m = R.meta_of(['timeout'])['timeout']
        self.assertEqual(m['color'], R.category_color('network'))
        self.assertEqual(m['category_label'], '网络与代理')

    def test_empty_and_duplicated(self):
        self.assertEqual(R.meta_of([]), {})
        self.assertEqual(len(R.meta_of(['timeout', 'timeout', ''])), 1)

    def test_unknown_code_still_renders(self):
        m = R.meta_of(['whatever'])['whatever']
        self.assertEqual(m['label'], '原因不明')


if __name__ == '__main__':
    unittest.main()
