"""网络错误归类与"代理填错"识别的单测。

为什么值得专门测：
    模板 compose 里 PROXY 是个占位符（http://你的代理地址:7890），
    用户忘改就会看到 "域名解析失败（Errno -2）" —— 看起来像 DNS 故障，
    于是去查 DNS，方向完全错了（实测就这么被带偏的）。

    正确的行为是：
      * 启动时就认出占位符并明确警告
      * 真出错时把原因归到"代理地址解析不了"，而不是"目标站点 DNS 故障"
"""

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import netutil as N  # noqa: E402


class TestPlaceholderProxyDetection(unittest.TestCase):
    def test_catches_template_placeholders(self):
        for p in ('http://你的代理地址:7890',
                  'http://your-proxy:7890',
                  'http://<你的代理>:7890',
                  '你的代理地址:7890',
                  'http://请填写代理:7890',
                  'http://example.com:7890',
                  'http://xxxx:7890'):
            with self.subTest(p=p):
                self.assertTrue(N.looks_like_placeholder_proxy(p),
                                '应该认出是占位符：%s' % p)

    def test_non_ascii_host_is_placeholder(self):
        """主机名里出现非 ASCII，一定是从模板抄来没改。"""
        self.assertTrue(N.looks_like_placeholder_proxy('http://例え.jp:1080'))

    def test_real_addresses_are_not_flagged(self):
        """真实地址不能误报，否则每次启动都弹警告，用户会习惯性忽略。"""
        for p in ('http://192.168.1.10:7890',
                  'http://10.0.0.5:7890',
                  'socks5://127.0.0.1:1080',
                  'http://proxy.local:7890',
                  'http://my-proxy.lan:8118',
                  'http://proxy.example.net:8080',
                  '',
                  '   '):
            with self.subTest(p=p):
                self.assertFalse(N.looks_like_placeholder_proxy(p),
                                 '不该误报：%r' % p)


class TestProxyHost(unittest.TestCase):
    def test_extracts_host(self):
        self.assertEqual(N.proxy_host('http://192.168.1.10:7890'), '192.168.1.10')
        self.assertEqual(N.proxy_host('192.168.1.10:7890'), '192.168.1.10')
        self.assertEqual(N.proxy_host('socks5://proxy.local:1080'), 'proxy.local')

    def test_empty_is_safe(self):
        self.assertEqual(N.proxy_host(''), '')
        self.assertEqual(N.proxy_host('   '), '')


class TestClassifyProxyVsDns(unittest.TestCase):
    """核心：DNS 失败要能区分"目标站点"还是"代理地址"。"""

    def test_dns_error_without_proxy(self):
        exc = socket.gaierror(-2, 'Name or service not known')
        self.assertEqual(N.classify_network_error(exc), 'dns_error')

    def test_proxy_host_unresolvable_becomes_proxy_error(self):
        """代理主机名解析不了 -> 归为代理问题，不是 DNS 故障。"""
        exc = socket.gaierror(-2, 'Name or service not known')
        kind = N.classify_network_error(
            exc, proxy='http://nonexistent-proxy-xyz.invalid:7890')
        self.assertEqual(kind, 'proxy_dns_error')

    def test_proxy_resolvable_keeps_dns_error(self):
        """代理地址正常时，DNS 失败仍然归为目标站点的问题。"""
        exc = socket.gaierror(-2, 'Name or service not known')
        # 用回环地址，必定可解析
        kind = N.classify_network_error(exc, proxy='http://127.0.0.1:7890')
        self.assertEqual(kind, 'dns_error')

    def test_proxy_refused(self):
        exc = ConnectionRefusedError(111, 'Connection refused')
        self.assertEqual(N.classify_network_error(exc, proxy='http://127.0.0.1:1'),
                         'proxy_refused')
        self.assertEqual(N.classify_network_error(exc), 'refused')

    def test_urlerror_wrapping_is_unwrapped(self):
        """urllib 会把真实原因包在 URLError.reason 里，必须能递归看到。"""
        import urllib.error
        inner = socket.gaierror(-2, 'Name or service not known')
        exc = urllib.error.URLError(inner)
        self.assertEqual(
            N.classify_network_error(
                exc, proxy='http://nonexistent-proxy-xyz.invalid:7890'),
            'proxy_dns_error')

    def test_descriptions_mention_proxy(self):
        """给用户看的文案必须明确指向代理，不能再说 DNS。"""
        msg = N.describe_network_error('proxy_dns_error')
        self.assertIn('代理', msg)
        self.assertNotIn('检查 DNS', msg)
        msg2 = N.describe_network_error('proxy_refused')
        self.assertIn('代理', msg2)

    def test_direct_connection_failure_hints_at_proxy(self):
        """按直连跑失败时，要提示"该站点可能需要代理"。

        实测：V2EX 直连超时、NodeSeek 直连被拒，配上代理就通了。
        所以不能让用户对着一句干巴巴的"连接超时"不知道下一步做什么。
        """
        for kind in ('timeout', 'refused', 'unreachable', 'reset'):
            with self.subTest(kind=kind):
                msg = N.describe_network_error(kind, proxy='')
                self.assertIn('代理', msg,
                              '直连失败时 %s 的提示应提到代理' % kind)

    def test_proxied_failure_does_not_hint_at_proxy(self):
        """已经配了代理还失败，就不该再说"可能需要代理"——那是废话。"""
        for kind in ('timeout', 'refused', 'unreachable', 'reset'):
            with self.subTest(kind=kind):
                msg = N.describe_network_error(
                    kind, proxy='http://192.168.1.10:7890')
                self.assertNotIn('可能需要通过代理访问', msg)

    def test_dns_and_tls_never_hint_at_proxy(self):
        """DNS/TLS 类失败与"要不要代理"无关，不该附那句话。"""
        for kind in ('dns_error', 'tls_error', 'proxy_dns_error'):
            with self.subTest(kind=kind):
                msg = N.describe_network_error(kind, proxy='')
                self.assertNotIn('可能需要通过代理访问', msg)

    def test_unknown_still_unknown(self):
        self.assertEqual(N.classify_network_error(ValueError('x')), 'unknown')


class TestProxyHostResolves(unittest.TestCase):
    def test_loopback_resolves(self):
        self.assertTrue(N._proxy_host_resolves('127.0.0.1'))

    def test_bogus_does_not_resolve(self):
        self.assertFalse(
            N._proxy_host_resolves('nonexistent-proxy-xyz.invalid'))

    def test_empty_treated_as_ok(self):
        """空主机名不该被当成"解析失败"，否则没配代理时会误报。"""
        self.assertTrue(N._proxy_host_resolves(''))


class TestStartupWarning(unittest.TestCase):
    """启动参数解析时就应该拦住占位符（而不是等签到失败）。"""

    def test_settings_drops_placeholder_proxy(self):
        import app.main as M
        saved = os.environ.get('PROXY')
        os.environ['PROXY'] = 'http://你的代理地址:7890'
        try:
            s = M.build_settings([])
            self.assertEqual(s.proxy, '',
                             '占位符应被忽略，退回直连而不是拿它当代理')
        finally:
            self._restore(saved)

    def test_settings_keeps_real_proxy(self):
        """真实代理地址要保留（--proxy 路径）。

        注意：环境变量 PROXY 已不再被读取（代理只认网页设置），
        所以这里改用命令行参数验证"真实地址不被误判成占位符"。
        """
        import app.main as M
        s = M.build_settings(['--proxy', 'http://192.168.1.10:7890'])
        self.assertEqual(s.proxy, 'http://192.168.1.10:7890')

    def test_proxy_env_no_longer_used(self):
        """环境变量里的真实代理也不再生效 —— 统一由网页设置决定。"""
        import app.main as M
        saved = os.environ.get('PROXY')
        os.environ['PROXY'] = 'http://192.168.1.10:7890'
        try:
            self.assertEqual(M.build_settings([]).proxy, '')
        finally:
            self._restore(saved)

    def test_explicit_cli_proxy_also_checked(self):
        """命令行传的占位符同样要拦下。"""
        import app.main as M
        s = M.build_settings(['--proxy', 'http://your-proxy:7890'])
        self.assertEqual(s.proxy, '')

    @staticmethod
    def _restore(saved):
        if saved is None:
            os.environ.pop('PROXY', None)
        else:
            os.environ['PROXY'] = saved


if __name__ == '__main__':
    unittest.main(verbosity=2)
