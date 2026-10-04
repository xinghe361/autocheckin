"""自检里的"疑似真实 Cookie 值"探测器的单测。

背景：我曾经为了写"真实格式"的测试用例，把用户的真实 Cookie 复制进
tests/test_cookies.py —— 测试文件会进公开仓库，等于泄露。
所以加了这个探测器，并在这里固定它的行为：

    * 真实会话值（长、大小写数字混合）必须被抓出来
    * 测试里常用的假值（abc / xxx / FAKE...）不能误报，
      否则大家会习惯性忽略这个检查，等于没有
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selfcheck import COOKIE_VALUE_RE  # noqa: E402

RE = re.compile(COOKIE_VALUE_RE)


class TestLooksLikeRealCookieValue(unittest.TestCase):
    def test_catches_long_mixed_case_values(self):
        """看起来像真实会话值：长 + 大小写混合 + 含数字。

        样例必须完全虚构 —— 这个文件虽然被 selfcheck 豁免扫描，
        但豁免是为了"能从源码里写出类似真凭据的形态"，不是允许放真凭据。
        下面这些值是随手编的，不对应任何真实账号。
        """
        samples = [
            'session=Zq8Xm2KpLd4Rt6Vw9Yb1Nc3Hj5Gf7Aa',
            'pjwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9',
            'cookie: session=AbCdEf123456GhIjKl',
            '"session": "AbCdEf123456GhIjKl"',
            'smac=1700000000-MkQ3nB7xZ2pL9vC4aW6tR8yH1jE5sU0g',
        ]
        for s in samples:
            with self.subTest(s=s):
                self.assertTrue(RE.search(s), '应该抓出来：%s' % s)

    def test_cf_clearance_detected(self):
        s = ('cf_clearance=Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv'
             '-1700000000-1.2.1.1-Wx9Yz8Ab7Cd6Ef5Gh4Ij3Kl2Mn1Op0Qr')
        self.assertTrue(re.search(r'\bcf_clearance=[A-Za-z0-9._\-]{40,}', s))

    def test_does_not_flag_obvious_placeholders(self):
        """测试里的假值不能被误报，否则这个检查会被人习惯性无视。"""
        safe = [
            'session=abc',
            'a=1; b=2; c=3',
            'session=FAKESESSIONVALUE0001',   # 全大写 -> 熵低
            'sid=sid001',
            'token=xxxxxxxxxxxxxxxxxxxx',
            'session=AAAAAAAAAAAAAAAAAAAA',
            'auth=1111111111111111111111',
            'session=<your-session-here>',
            'sid=spMQrZ',                     # 短
        ]
        for s in safe:
            with self.subTest(s=s):
                self.assertFalse(RE.search(s), '不该抓出来：%s' % s)

    def test_empty_and_garbage_are_safe(self):
        for s in ('', 'hello world', 'no cookies here'):
            self.assertFalse(RE.search(s))


class TestRepoHasNoRealCredentials(unittest.TestCase):
    """对当前仓库做一次真实扫描：不应命中任何"疑似真实 Cookie"。

    豁免本文件自己：探测器要证明"能抓到真实值"，就必须在源码里写出
    看起来像真实值的样例（自指问题）。这些样例是刻意编的，不是谁的凭据。
    """

    # 允许包含凭据样例的文件（只有探测器自己的测试）
    ALLOWLIST = ('test_selfcheck_secrets.py',)

    def test_repository_is_clean(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d not in ('__pycache__', '.git', 'node_modules')]
            for fn in files:
                if fn in self.ALLOWLIST:
                    continue
                if not re.search(r'\.(py|md|json|txt|ya?ml|sh|toml|cfg|ini)$', fn):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    text = open(p, encoding='utf-8').read()
                except Exception:                               # noqa: BLE001
                    continue
                for m in RE.finditer(text):
                    rel = os.path.relpath(p, root)
                    offenders.append('%s: %s' % (rel, m.group(0)[:40]))
        self.assertEqual(
            offenders, [],
            '仓库里发现疑似真实 Cookie 值，请替换成占位串：\n  '
            + '\n  '.join(offenders[:10]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
