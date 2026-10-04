"""httpclient 单测：代理选择、请求往返、错误处理、DNS 覆盖。

用本机起一个临时 HTTP 服务做真实往返，不依赖外网。
"""

import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import httpclient as H  # noqa: E402
from app import netutil  # noqa: E402

TRUE_GETADDRINFO = netutil._REAL_GETADDRINFO


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='text/plain; charset=utf-8'):
        raw = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/ok':
            self._send(200, 'hello 世界')
        elif self.path == '/json':
            self._send(200, json.dumps({'a': 1, 'b': '中文'}), 'application/json')
        elif self.path == '/404':
            self._send(404, 'nope')
        elif self.path == '/500':
            self._send(500, 'boom')
        elif self.path == '/echo-ua':
            self._send(200, self.headers.get('User-Agent', ''))
        elif self.path == '/echo-header':
            self._send(200, self.headers.get('X-Custom', ''))
        else:
            self._send(200, 'root')

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n)
        self._send(200, 'got:' + body.decode('utf-8'))


class ClientTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        netutil._REAL_GETADDRINFO = TRUE_GETADDRINFO
        socket.getaddrinfo = TRUE_GETADDRINFO
        netutil.force_ipv4._depth = 0

    def tearDown(self):
        netutil._REAL_GETADDRINFO = TRUE_GETADDRINFO
        socket.getaddrinfo = TRUE_GETADDRINFO
        netutil.force_ipv4._depth = 0


class TestProxyHandling(ClientTestCase):
    """代理是全局的（用户已做分流），这里只验证地址规整与注入。"""

    def test_normalize_adds_scheme(self):
        self.assertEqual(H.normalize_proxy('10.0.0.1:7890'),
                         'http://10.0.0.1:7890')

    def test_normalize_keeps_scheme(self):
        self.assertEqual(H.normalize_proxy('http://a.b:1'), 'http://a.b:1')
        self.assertEqual(H.normalize_proxy('socks5://a.b:1080'), 'socks5://a.b:1080')

    def test_normalize_empty(self):
        self.assertEqual(H.normalize_proxy(''), '')
        self.assertEqual(H.normalize_proxy('   '), '')
        self.assertEqual(H.normalize_proxy(None), '')

    def test_opener_has_proxy_when_configured(self):
        import urllib.request
        opener = H.build_opener(H.HttpConfig(), 'http://10.0.0.1:7890')
        proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(proxies)
        self.assertEqual(proxies[0].proxies.get('http'), 'http://10.0.0.1:7890')
        self.assertEqual(proxies[0].proxies.get('https'), 'http://10.0.0.1:7890')

    def test_no_proxy_env_neutralizes_env_proxy(self):
        """未配代理时，即使环境变量里有代理也要真正直连。

        实现方式：临时摘掉代理环境变量并清 getproxies 的 lru_cache。
        """
        import os
        import urllib.request
        old = {}
        for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
            old[k] = os.environ.get(k)
            os.environ[k] = 'http://should-not-be-used:1'
        try:
            self.assertTrue(urllib.request.getproxies(), '前置：环境变量代理应生效')
            with H._no_proxy_env():
                self.assertEqual(urllib.request.getproxies(), {},
                                 '上下文内不应解析出任何代理')
            self.assertTrue(urllib.request.getproxies(), '退出后环境变量应还原')
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    
    def test_no_proxy_env_restores_on_exception(self):
        import os
        import urllib.request
        old = os.environ.get('http_proxy')
        os.environ['http_proxy'] = 'http://x:1'
        try:
            try:
                with H._no_proxy_env():
                    raise RuntimeError('boom')
            except RuntimeError:
                pass
            self.assertEqual(os.environ.get('http_proxy'), 'http://x:1')
        finally:
            if old is None:
                os.environ.pop('http_proxy', None)
            else:
                os.environ['http_proxy'] = old
    
    def test_direct_request_ignores_env_proxy(self):
        """端到端：环境变量指向坏代理时，未配代理的请求仍应成功。"""
        import os
        import urllib.request
        old = {}
        for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
            old[k] = os.environ.get(k)
            os.environ[k] = 'http://127.0.0.1:1'
        try:
            r = H.get(self.base + '/ok')          # 不传 global_proxy
            self.assertEqual(r.status, 200)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    
    def test_proxy_is_normalized_before_injection(self):
        import urllib.request
        opener = H.build_opener(H.HttpConfig(), '10.0.0.1:7890')
        proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxies[0].proxies.get('http'), 'http://10.0.0.1:7890')

    def test_verify_ssl_false_relaxes_context(self):
        import ssl
        opener = H.build_opener(H.HttpConfig(verify_ssl=False), '')
        https = [h for h in opener.handlers if type(h).__name__ == 'HTTPSHandler']
        self.assertTrue(https)
        self.assertEqual(https[0]._context.verify_mode, ssl.CERT_NONE)

    def test_verify_ssl_true_keeps_verification(self):
        """build_opener 总会加一个默认 HTTPSHandler，所以要比对它的上下文。"""
        import ssl
        opener = H.build_opener(H.HttpConfig(verify_ssl=True), '')
        https = [h for h in opener.handlers if type(h).__name__ == 'HTTPSHandler']
        self.assertTrue(https)
        self.assertEqual(https[0]._context.verify_mode, ssl.CERT_REQUIRED)


class TestRealRequests(ClientTestCase):
    def test_get_ok_and_utf8(self):
        r = H.get(self.base + '/ok')
        self.assertEqual(r.status, 200)
        self.assertTrue(r.ok)
        self.assertEqual(r.text, 'hello 世界')

    def test_json_helper(self):
        r = H.get(self.base + '/json')
        self.assertEqual(r.json(), {'a': 1, 'b': '中文'})

    def test_404_is_returned_not_raised(self):
        r = H.get(self.base + '/404')
        self.assertEqual(r.status, 404)
        self.assertFalse(r.ok)

    def test_500_is_returned_not_raised(self):
        r = H.get(self.base + '/500')
        self.assertEqual(r.status, 500)

    def test_post_body(self):
        r = H.post(self.base + '/', data=b'abc=1')
        self.assertEqual(r.text, 'got:abc=1')

    def test_enc_form(self):
        self.assertEqual(H.enc({'a': '中 文', 'b': 2}), 'a=%E4%B8%AD+%E6%96%87&b=2'.encode())

    def test_enc_json_keeps_unicode(self):
        self.assertIn('中文', H.enc_json({'k': '中文'}).decode('utf-8'))

    def test_custom_user_agent(self):
        r = H.get(self.base + '/echo-ua', cfg=H.HttpConfig(user_agent='MyUA/1.0'))
        self.assertEqual(r.text, 'MyUA/1.0')

    def test_custom_headers(self):
        r = H.get(self.base + '/echo-header',
                  cfg=H.HttpConfig(headers={'X-Custom': 'v123'}))
        self.assertEqual(r.text, 'v123')

    def test_default_user_agent_is_browser_like(self):
        r = H.get(self.base + '/echo-ua')
        self.assertIn('Mozilla/5.0', r.text)

    def test_connection_refused_raises_httperror(self):
        # 找一个几乎肯定没人监听的端口
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        free_port = s.getsockname()[1]
        s.close()
        with self.assertRaises(H.HttpError) as ctx:
            H.get('http://127.0.0.1:%d/' % free_port, cfg=H.HttpConfig(timeout=3))
        self.assertIn(ctx.exception.kind, ('refused', 'reset', 'unknown'))
        self.assertTrue(ctx.exception.message)

    def test_timeout_raises_httperror_with_readable_kind(self):
        # 10.255.255.1 是不可路由地址，用来触发超时
        with self.assertRaises(H.HttpError) as ctx:
            H.get('http://10.255.255.1:9/', cfg=H.HttpConfig(timeout=1))
        self.assertTrue(ctx.exception.kind)
        self.assertTrue(str(ctx.exception))


class TestDnsOverrideContext(ClientTestCase):
    """dns_override 是通用能力（备用手段），保留但不再按站点使用。"""

    def test_patch_redirects_named_host(self):
        with H.netutil.dns_override({'example.com': '9.9.9.9'}):
            got = socket.getaddrinfo('example.com', 443)
            self.assertEqual(got[0][4][0], '9.9.9.9')
            self.assertEqual(got[0][0], socket.AF_INET)

    def test_patch_passes_through_other_hosts(self):
        with H.netutil.dns_override({'example.com': '9.9.9.9'}):
            got = socket.getaddrinfo('127.0.0.1', 80)
            self.assertTrue(any(r[4][0] == '127.0.0.1' for r in got))

    def test_restored_after_exit(self):
        before = socket.getaddrinfo
        with H.netutil.dns_override({'example.com': '9.9.9.9'}):
            self.assertIsNot(socket.getaddrinfo, before)
        self.assertIs(socket.getaddrinfo, before, '退出后必须还原')

    def test_restored_on_exception(self):
        before = socket.getaddrinfo
        try:
            with H.netutil.dns_override({'example.com': '9.9.9.9'}):
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        self.assertIs(socket.getaddrinfo, before)

    def test_real_request_through_override(self):
        """真发一次请求：把假域名映射到本机，并确认结束后还原。"""
        before = socket.getaddrinfo
        with H.netutil.dns_override({'fake.local': '127.0.0.1'}):
            r = H.get('http://fake.local:%d/ok' % self.port)
            self.assertEqual(r.status, 200)
        self.assertIs(socket.getaddrinfo, before, '请求结束后必须还原')

    def test_nested_overrides_merge(self):
        with H.netutil.dns_override({'a.local': '127.0.0.1'}):
            with H.netutil.dns_override({'b.local': '127.0.0.2'}):
                self.assertEqual(socket.getaddrinfo('a.local', 80)[0][4][0], '127.0.0.1')
                self.assertEqual(socket.getaddrinfo('b.local', 80)[0][4][0], '127.0.0.2')


class TestHttpErrorText(unittest.TestCase):
    def test_str_includes_detail(self):
        e = H.HttpError('timeout', '连接超时', 'timed out')
        self.assertIn('连接超时', str(e))
        self.assertIn('timed out', str(e))

    def test_str_without_detail(self):
        e = H.HttpError('refused', '连接被拒绝')
        self.assertEqual(str(e), '连接被拒绝')


if __name__ == '__main__':
    unittest.main(verbosity=2)
