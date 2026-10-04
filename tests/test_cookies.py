"""Cookie 解析模块单测。

重点验证"用户真的会粘进来的样子"能被正确识别，包括：
  * Chrome/Edge 的 Copy as cURL（含 Windows CMD 的 ^" 转义）
  * DevTools Application 面板那种一行一个键值
  * Netscape cookies.txt
  * 裸 Cookie 串、JSON
并验证敏感值不会意外外泄（to_dict 不带密文时）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cookies as C  # noqa: E402
from app.models import SiteConfig  # noqa: E402


class TestParseCookieHeader(unittest.TestCase):
    def test_basic_pairs(self):
        got = C.parse_cookie_input('a=1; b=2; c=3')[0]
        self.assertEqual(got, {'a': '1', 'b': '2', 'c': '3'})

    def test_skips_cookie_attributes(self):
        """Path/Domain/Expires 这些是属性，不是 Cookie 本身，必须剔掉。"""
        got = C.parse_cookie_input(
            'session=abc; Path=/; Domain=.x.com; Expires=Wed, 21 Oct 2026 07:28:00 GMT; '
            'HttpOnly; Secure; SameSite=Lax')[0]
        self.assertEqual(got, {'session': 'abc'})

    def test_value_containing_equals(self):
        """Discuz 的 auth 值里可能有 = 或 URL 编码，不能被切坏。"""
        got = C.parse_cookie_input(
            'v2x4_48dd_auth=abc%2Bdef%3Dghi; sid=xyz')[0]
        self.assertEqual(got['v2x4_48dd_auth'], 'abc%2Bdef%3Dghi')
        self.assertEqual(got['sid'], 'xyz')

    def test_real_chiphell_like_cookie(self):
        """Discuz 的 Cookie 形态：带 v2x4_<hash>_ 前缀，auth 值含 URL 编码。

        这里刻意用【伪造】的值，绝不拿真实账号的 Cookie 当测试数据 ——
        测试文件是要进公开仓库的，真凭据一旦写进去就等于泄露。
        """
        raw = ('v2x4_48dd_saltkey=AAAABBBB; v2x4_48dd_lastvisit=1700000000; '
               'v2x4_48dd_auth=FAKEAUTH%2BFAKE%2FTOKEN%3D%3D%2Fmore%2Bchars; '
               'v2x4_48dd_sid=sid001')
        got, fmt = C.parse_cookie_input(raw)
        self.assertEqual(fmt, 'Cookie 串')
        self.assertEqual(len(got), 4)
        self.assertIn('v2x4_48dd_auth', got)
        # URL 编码里的 %2B / %2F / %3D 不能把值切坏
        self.assertEqual(got['v2x4_48dd_auth'],
                         'FAKEAUTH%2BFAKE%2FTOKEN%3D%3D%2Fmore%2Bchars')

    def test_empty_raises(self):
        with self.assertRaises(C.CookieParseError):
            C.parse_cookie_input('')

    def test_garbage_raises(self):
        with self.assertRaises(C.CookieParseError):
            C.parse_cookie_input('这段文字里没有任何 Cookie')


class TestParseCurl(unittest.TestCase):
    def test_chrome_copy_as_curl(self):
        text = (
            "curl 'https://www.nodeseek.com/board' \\\n"
            "  -H 'accept: text/html' \\\n"
            "  -H 'cookie: session=abc123; pjwt=eyJhbGci; cf_clearance=xyz' \\\n"
            "  -H 'user-agent: Mozilla/5.0'"
        )
        got, fmt = C.parse_cookie_input(text)
        self.assertIn('cURL', fmt)
        self.assertEqual(got['session'], 'abc123')
        self.assertEqual(got['pjwt'], 'eyJhbGci')
        self.assertEqual(got['cf_clearance'], 'xyz')

    def test_windows_cmd_escaped_quotes(self):
        """Windows 的 Copy as cURL (cmd) 把引号转义成 ^" —— 必须能解。"""
        text = (
            'curl --url ^"https://www.nodeseek.com/^" ^\n'
            '  -H ^"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)^" ^\n'
            '  -H ^"cookie: session=abc; pjwt=def^"'
        )
        got, _fmt = C.parse_cookie_input(text)
        self.assertEqual(got, {'session': 'abc', 'pjwt': 'def'})

    def test_curl_without_cookie_header_raises(self):
        text = "curl 'https://a.com/' -H 'accept: text/html'"
        with self.assertRaises(C.CookieParseError):
            C.parse_cookie_input(text)


class TestParseKvLines(unittest.TestCase):
    def test_devtools_application_style(self):
        """DevTools 的 Application 面板粘出来常见"名字 + 空格 + 值"。

        值一律用占位串（FAKE...），不要把真实账号的 Cookie 写进仓库。
        """
        text = (
            'session              FAKESESSIONVALUE0001\n'
            'pjwt                 FAKEJWT0002\n'
            'smac                 FAKESMAC0003'
        )
        got, _fmt = C.parse_cookie_input(text)
        self.assertEqual(len(got), 3)
        self.assertEqual(got['session'], 'FAKESESSIONVALUE0001')
        self.assertTrue(got['pjwt'].startswith('FAKEJWT'))

    def test_tab_separated(self):
        got = C.parse_kv_lines('a\t1\nb\t2')
        self.assertEqual(got, {'a': '1', 'b': '2'})

    def test_ignores_comments(self):
        got = C.parse_kv_lines('# Netscape HTTP Cookie File\na\t1')
        self.assertEqual(got, {'a': '1'})


class TestParseNetscape(unittest.TestCase):
    def test_cookies_txt(self):
        text = (
            '# Netscape HTTP Cookie File\n'
            '.example.com\tTRUE\t/\tTRUE\t1791131413\tsession\tabc123\n'
            '.example.com\tTRUE\t/\tFALSE\t0\tlang\tzh-CN\n'
        )
        got, fmt = C.parse_cookie_input(text)
        self.assertIn('Netscape', fmt)
        self.assertEqual(got, {'session': 'abc123', 'lang': 'zh-CN'})


class TestParseJson(unittest.TestCase):
    def test_json_object(self):
        got, fmt = C.parse_cookie_input('{"session": "abc", "sid": "xyz"}')
        self.assertIn('JSON', fmt)
        self.assertEqual(got, {'session': 'abc', 'sid': 'xyz'})


class TestToCookieHeader(unittest.TestCase):
    def test_round_trip(self):
        src = {'a': '1', 'b': '2'}
        self.assertEqual(C.to_cookie_header(src), 'a=1; b=2')
        self.assertEqual(C.parse_cookie_input(C.to_cookie_header(src))[0], src)

    def test_empty(self):
        self.assertEqual(C.to_cookie_header({}), '')


class TestToPlaywrightCookies(unittest.TestCase):
    def test_domain_gets_leading_dot(self):
        got = C.to_playwright_cookies({'a': '1'}, 'www.example.com')
        self.assertEqual(got[0]['domain'], '.www.example.com')
        self.assertEqual(got[0]['path'], '/')
        self.assertTrue(got[0]['secure'])

    def test_domain_with_dot_kept(self):
        got = C.to_playwright_cookies({'a': '1'}, '.example.com')
        self.assertEqual(got[0]['domain'], '.example.com')

    def test_http_only_flag(self):
        got = C.to_playwright_cookies({'session': 'x', 'lang': 'zh'},
                                      'example.com',
                                      http_only_names=['session'])
        by_name = {c['name']: c for c in got}
        self.assertTrue(by_name['session'].get('httpOnly'))
        self.assertFalse(by_name['lang'].get('httpOnly'))

    def test_empty_domain_falls_back(self):
        got = C.to_playwright_cookies({'a': '1'}, '')
        self.assertTrue(got[0]['domain'])


class TestDomainOf(unittest.TestCase):
    def test_extracts_host(self):
        self.assertEqual(C.domain_of('https://www.nodeseek.com/board'),
                         'www.nodeseek.com')
        self.assertEqual(C.domain_of('https://www.chiphell.com/forum.php'),
                         'www.chiphell.com')

    def test_bad_input_is_safe(self):
        self.assertEqual(C.domain_of(''), '')
        self.assertEqual(C.domain_of('not a url'), '')


class TestSecretsNotLeaked(unittest.TestCase):
    """站点配置导出时不能带出 Cookie 密文（更不该带明文）。"""

    def test_to_dict_without_secrets_hides_cookie(self):
        s = SiteConfig(id='x', name='X', cookie_enc='ENCRYPTED_BLOB',
                       password_enc='PW_BLOB')
        d = s.to_dict(include_secrets=False)
        self.assertNotIn('cookie_enc', d)
        self.assertNotIn('password_enc', d)
        # 但要有"是否已配置"的标记供界面显示
        self.assertTrue(d['has_cookie'])
        self.assertTrue(d['has_password'])

    def test_to_dict_with_secrets_still_has_flags(self):
        s = SiteConfig(id='x', name='X', cookie_enc='BLOB')
        d = s.to_dict(include_secrets=True)
        self.assertEqual(d['cookie_enc'], 'BLOB')
        self.assertTrue(d['has_cookie'])

    def test_plaintext_cookie_never_appears(self):
        """to_dict 的键里不应该出现明文 Cookie 字段名。"""
        s = SiteConfig(id='x', name='X', cookie_enc='BLOB')
        d = s.to_dict(include_secrets=True)
        self.assertNotIn('cookie_header', d)
        self.assertNotIn('cookie', d)


class TestNewFieldsRoundTrip(unittest.TestCase):
    def test_headers_and_cookie_meta_survive(self):
        s = SiteConfig(id='x', name='X',
                       headers={'Origin': 'https://a.com', 'Referer': 'https://a.com/b'},
                       cookie_enc='BLOB', cookie_updated_at=123,
                       cookie_source='cURL 命令', cookie_status=1,
                       cookie_checked_at=456, cookie_error='')
        back = SiteConfig.from_dict(s.to_dict())
        self.assertEqual(back.headers['Origin'], 'https://a.com')
        self.assertEqual(back.headers['Referer'], 'https://a.com/b')
        self.assertEqual(back.cookie_enc, 'BLOB')
        self.assertEqual(back.cookie_updated_at, 123)
        self.assertEqual(back.cookie_source, 'cURL 命令')
        self.assertEqual(back.cookie_status, 1)
        self.assertEqual(back.cookie_checked_at, 456)

    def test_missing_new_fields_default_safely(self):
        """读旧配置文件（没有 headers / cookie 元信息）不能炸。"""
        old = {'id': 'old', 'name': '旧站点', 'kind': 'template',
               'template': 'v2ex'}
        s = SiteConfig.from_dict(old)
        self.assertEqual(s.headers, {})
        self.assertEqual(s.cookie_enc, '')
        self.assertEqual(s.cookie_status, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
