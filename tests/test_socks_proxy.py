"""socks 代理支持。

urllib 原生不认 `socks5://`：把它塞进 ProxyHandler 之后，无论装没装 PySocks，
都会一路拖到 urlopen 才报 `unknown url type: socks5`（实测过 PySocks 1.7.1）。
所以这里是自己实现连接层（app/socks_proxy.py）。

测试重点：**代理参数要真的传到建连接那一步**，而不是加了个依赖就完事。
真正跑 socks 协议是 PySocks 的事，这里用"把 create_connection 换成一个
直连目标服务器的普通 socket"来验证接线是否正确。
"""

import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import httpclient as H  # noqa: E402
from app import socks_proxy as SP  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = ('path=' + self.path).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SocksCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path='/ok'):
        return 'http://127.0.0.1:%d%s' % (self.port, path)


@unittest.skipUnless(SP.available(), 'PySocks 未安装，跳过 socks 解析测试')
class TestParse(unittest.TestCase):
    def test_socks5_without_auth(self):
        self.assertEqual(SP.parse_proxy('socks5://127.0.0.1:1080'),
                         (2, '127.0.0.1', 1080, None, None))

    def test_socks5h_maps_to_socks5(self):
        """h 表示域名交给代理解析，PySocks 里由 proxy_rdns=True 表达。"""
        spec = SP.parse_proxy('socks5h://1.2.3.4:9050')
        self.assertEqual(spec[0], 2)
        self.assertEqual(spec[1], '1.2.3.4')

    def test_socks4(self):
        self.assertEqual(SP.parse_proxy('socks4://h:9050')[0], 1)

    def test_auth_is_url_decoded(self):
        spec = SP.parse_proxy('socks5://u:p%40ss@1.2.3.4:1080')
        self.assertEqual(spec[3], 'u')
        self.assertEqual(spec[4], 'p@ss', '密码里的 %40 要还原成 @')

    def test_default_port(self):
        self.assertEqual(SP.parse_proxy('socks5://h')[2], 1080)

    def test_garbage(self):
        for bad in ('', 'socks5://', 'http://1.2.3.4:80', 'socks5://h:abc',
                    'socks5://h:99999'):
            self.assertIsNone(SP.parse_proxy(bad), bad)

    def test_http_proxy_is_not_socks(self):
        """http 代理不该被 socks 这条分支接走。"""
        self.assertFalse(H._is_socks(H.normalize_proxy('10.0.0.1:7890')))
        self.assertTrue(H._is_socks(H.normalize_proxy('socks5://a:1')))


@unittest.skipUnless(SP.available(), 'PySocks 未安装，跳过 socks 接线测试')
class TestWiring(SocksCase):
    """代理参数必须真的到达建连接那一步。"""

    def setUp(self):
        self.calls = []
        self._orig = SP._socks.create_connection
        outer = self

        def fake_create_connection(dest, timeout=None, source_address=None,
                                   **kw):
            outer.calls.append({'dest': dest, 'timeout': timeout, 'kw': kw})
            # 不做 socks 协议，直接连目标 —— 验证的是接线，不是协议实现
            s = socket.create_connection(dest, timeout or 5)
            return s

        SP._socks.create_connection = fake_create_connection

    def tearDown(self):
        SP._socks.create_connection = self._orig

    def test_request_goes_through_socks_path(self):
        r = H.request('GET', self.url(), global_proxy='socks5://1.2.3.4:1080',
                      cfg=H.HttpConfig(timeout=5))
        self.assertEqual(r.status, 200)
        self.assertIn('/ok', r.text)
        self.assertEqual(len(self.calls), 1, '没有走 socks 建连接')
        call = self.calls[0]
        self.assertEqual(call['kw']['proxy_addr'], '1.2.3.4')
        self.assertEqual(call['kw']['proxy_port'], 1080)
        self.assertEqual(call['kw']['proxy_type'], 2)
        self.assertTrue(call['kw']['proxy_rdns'],
                        '域名交给代理解析，否则被污染的域名仍解析错')

    def test_auth_passed_through(self):
        H.request('GET', self.url(),
                  global_proxy='socks5://user:pa%24s@1.2.3.4:1080',
                  cfg=H.HttpConfig(timeout=5))
        kw = self.calls[0]['kw']
        self.assertEqual(kw['proxy_username'], 'user')
        self.assertEqual(kw['proxy_password'], 'pa$s')

    def test_http_proxy_still_uses_urllib_proxy_handler(self):
        """http 代理不该被新代码改道：仍然走 urllib 的 ProxyHandler。"""
        with self.assertRaises(H.HttpError):
            # 127.0.0.1:9 基本可以确定没有服务在听，一定能连失败
            H.request('GET', self.url(), global_proxy='http://127.0.0.1:9',
                      cfg=H.HttpConfig(timeout=2))
        self.assertEqual(self.calls, [], 'http 代理被错误地交给了 socks 分支')


class TestMissingDependency(SocksCase):
    def setUp(self):
        self._orig = SP._socks
        SP._socks = None

    def tearDown(self):
        SP._socks = self._orig

    def test_clear_error_when_pysocks_missing(self):
        """缺依赖时要说人话，而不是 "unknown url type: socks5"。"""
        with self.assertRaises(H.HttpError) as ctx:
            H.request('GET', self.url(), global_proxy='socks5://1.2.3.4:1080',
                      cfg=H.HttpConfig(timeout=2))
        err = ctx.exception
        self.assertEqual(err.kind, 'no_socks')
        self.assertIn('socks', err.message)
        self.assertNotIn('unknown url type', err.message)


@unittest.skipUnless(SP.available(), 'PySocks 未安装，跳过 socks 坏 URL 测试')
class TestBadUrl(SocksCase):
    def test_unparsable_url_has_its_own_kind(self):
        with self.assertRaises(H.HttpError) as ctx:
            H.request('GET', self.url(), global_proxy='socks5://h:notaport',
                      cfg=H.HttpConfig(timeout=2))
        self.assertEqual(ctx.exception.kind, 'bad_socks_url')


if __name__ == '__main__':
    unittest.main()
