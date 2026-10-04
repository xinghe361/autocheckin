"""跨域跳转的头剥离测试（安全回归）。

背景：urllib 的默认 HTTPRedirectHandler 会把原请求的**所有**头复制到跳转后的
请求上（requests 库会剥 Authorization，urllib 不会）。于是任意被签到站点
只要 302 指向别的域名，你的登录 Cookie / Authorization 就落到第三方手里。
已实测复现过，所以这里必须钉住。

同时要保证**同源**跳转不剥头（很多站点登录后会跳回自己，剥了就会失败）。
"""

import http.server
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.httpclient import HttpConfig, request, _origin_of              # noqa: E402

SENSITIVE = {
    'Cookie': 'session=SUPERSECRET; pjwt=TOKEN',
    'Authorization': 'Bearer MYKEY',
    'X-Api-Key': 'K123',
    'User-Agent': 'ac-test',
}


class _Echo(http.server.BaseHTTPRequestHandler):
    """收到请求就把头记下来（供断言）。"""

    def do_GET(self):
        self.server.seen.append(dict(self.headers.items()))
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _Redirect(http.server.BaseHTTPRequestHandler):
    """跳到 self.server.target。"""

    def do_GET(self):
        self.send_response(302)
        self.send_header('Location', self.server.target)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *a):
        pass


def _start(handler_cls, **attrs):
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
    srv.seen = []
    for k, v in attrs.items():
        setattr(srv, k, v)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _fetch(url, headers=None):
    """走项目真实的请求路径（request 会把 cfg.headers 挂到请求上）。"""
    return request('GET', url,
                   cfg=HttpConfig(timeout=10, headers=headers or {}),
                   global_proxy='')


class TestCrossOriginRedirect(unittest.TestCase):
    def test_headers_stripped_on_cross_origin(self):
        """跨源跳转（不同端口也算）必须剥掉凭据类头。"""
        echo = _start(_Echo)
        redir = _start(_Redirect,
                       target='http://127.0.0.1:%d/target' % echo.server_address[1])
        try:
            _fetch('http://127.0.0.1:%d/start' % redir.server_address[1], SENSITIVE)
        finally:
            echo.shutdown(); redir.shutdown()

        got = echo.seen[0] if echo.seen else {}
        lower = {k.lower(): v for k, v in got.items()}
        for name in ('cookie', 'authorization', 'x-api-key'):
            self.assertNotIn(name, lower,
                             '跨源跳转不该带上 %s（实际带上了：%s）'
                             % (name, lower.get(name)))
        # 正常头应该还在，否则说明剥过头了
        self.assertIn('user-agent', lower)

    def test_headers_kept_without_redirect(self):
        """不跳转时凭据必须照常发送（不能因为加了剥头逻辑就漏发）。"""
        echo = _start(_Echo)
        try:
            _fetch('http://127.0.0.1:%d/direct' % echo.server_address[1],
                   SENSITIVE)
        finally:
            echo.shutdown()
        lower = {k.lower(): v for k, v in (echo.seen[0] if echo.seen else {}).items()}
        self.assertEqual(lower.get('cookie'), SENSITIVE['Cookie'])
        self.assertEqual(lower.get('authorization'), SENSITIVE['Authorization'])
        self.assertEqual(lower.get('x-api-key'), SENSITIVE['X-Api-Key'])

    def test_headers_kept_on_same_origin_redirect(self):
        """同源跳转必须保留凭据。

        很多站点登录后会 302 回自己的另一个路径，剥了头就会登录失败。
        """
        seen = []

        class Same(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/hop':
                    self.send_response(302)
                    self.send_header('Location', '/land')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                seen.append(dict(self.headers.items()))
                body = b'ok'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Same)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            _fetch('http://127.0.0.1:%d/hop' % srv.server_address[1], SENSITIVE)
        finally:
            srv.shutdown()
        lower = {k.lower(): v for k, v in (seen[0] if seen else {}).items()}
        self.assertEqual(lower.get('cookie'), SENSITIVE['Cookie'],
                         '同源跳转不该剥 Cookie')
        self.assertEqual(lower.get('authorization'), SENSITIVE['Authorization'])


class TestOriginOf(unittest.TestCase):
    def test_ports_matter(self):
        self.assertNotEqual(_origin_of('http://h:1/a'), _origin_of('http://h:2/a'))

    def test_default_ports(self):
        self.assertEqual(_origin_of('https://a.com/x'), 'https://a.com:443')
        self.assertEqual(_origin_of('http://a.com/x'), 'http://a.com:80')
        self.assertEqual(_origin_of('https://a.com:443/y'), 'https://a.com:443')

    def test_case_insensitive_host(self):
        self.assertEqual(_origin_of('https://A.COM/x'), _origin_of('https://a.com/y'))

    def test_scheme_matters(self):
        self.assertNotEqual(_origin_of('http://a.com/x'),
                            _origin_of('https://a.com/x'))

    def test_no_host(self):
        self.assertEqual(_origin_of('/just/a/path'), '')

    def test_bad_port_does_not_raise(self):
        """端口非法不能让判定函数抛异常（抛了就没人剥头了）。"""
        self.assertTrue(_origin_of('http://h:abc/x').startswith('http://h:'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
