"""传输层与空闲开销：压缩、以及"没变化就不写盘"。

这两个都不是功能点，是**开销**：
  * 整个界面（HTML+CSS+JS）内联在一个响应里，约 120KB，且因为带会话状态
    必须 no-store 不能缓存 —— 每次打开都重传。压缩能省掉三分之二。
  * 调度器每分钟 tick 一次，以前无条件写 state.json（临时文件+fsync+rename），
    一天 1440 次强制刷盘；NAS 上机械盘会被每分钟叫醒一次，永远不休眠。
    绝大多数 tick 一个站点都不需要补排期，写下去的是和盘上一模一样的字节。

这类东西坏了不会报错，只会慢慢变糟，所以用测试钉住。
"""

import gzip
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import scheduler as SCH  # noqa: E402
from app import secrets as S  # noqa: E402
from app import service as SV  # noqa: E402
from app import store as ST  # noqa: E402
from app import web as WB  # noqa: E402


class TransportCase(unittest.TestCase):
    PASSWORD = 'test-password-123'

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='autocheckin_transport_')
        self.svc = SV.Service(self.dir, version='9.9.9',
                              env_key=S.generate_key(), proxy='')
        self.app = WB.WebApp(self.svc, version='9.9.9')
        self.app.dispatch('POST', '/api/auth/setup', {},
                          {'password': self.PASSWORD}, token='', source='test')
        self.token = self.app._new_token
        self.httpd = WB.serve(self.app, host='127.0.0.1', port=0, block=False)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:                                       # noqa: BLE001
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def _get(self, path='/', encoding=None, cookie=True):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path))
        if encoding:
            req.add_header('Accept-Encoding', encoding)
        if cookie:
            req.add_header('Cookie', '%s=%s' % (WB.AUTH.COOKIE_NAME,
                                                self.token))
        # 显式不走代理：跑全量时前面的用例会改 os.environ['http_proxy']，
        # 环境里也可能本来就设了代理，那会把 127.0.0.1 的请求也劫走。
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as r:
            raw = r.read()
            return r.status, dict(r.headers), raw


class TestGzip(TransportCase):
    def test_page_is_compressed_when_client_accepts(self):
        st, head, raw = self._get('/', encoding='gzip')
        self.assertEqual(st, 200)
        self.assertEqual(head.get('Content-Encoding'), 'gzip')
        # 声明 Vary，否则中间代理可能把压缩版发给不支持 gzip 的客户端
        self.assertEqual(head.get('Vary'), 'Accept-Encoding')
        self.assertEqual(int(head['Content-Length']), len(raw))
        # 解开后必须和原页面完全一致
        plain = gzip.decompress(raw)
        st2, _, raw2 = self._get('/', encoding=None)
        self.assertEqual(plain, raw2)

    def test_page_is_plain_when_client_says_no_gzip(self):
        st, head, raw = self._get('/', encoding='identity')
        self.assertEqual(st, 200)
        self.assertNotIn('Content-Encoding', head)
        self.assertGreater(len(raw), 10000, '首页不可能这么小')

    def test_gzip_q0_is_a_refusal(self):
        """`gzip;q=0` 是明确的拒绝，不能因为字符串里有 gzip 就压。"""
        st, head, _ = self._get('/', encoding='gzip;q=0, identity')
        self.assertEqual(st, 200)
        self.assertNotIn('Content-Encoding', head)

    def test_compression_actually_saves_a_lot(self):
        _, head_g, raw_g = self._get('/', encoding='gzip')
        _, _, raw_p = self._get('/', encoding=None)
        ratio = len(raw_g) * 100.0 / len(raw_p)
        # 实测约 33%；留足余量，只要求"确实压得下去"
        self.assertLess(ratio, 60, '压缩率 %.1f%% 不正常' % ratio)

    def test_api_json_is_compressed_too(self):
        st, head, raw = self._get('/api/overview', encoding='gzip')
        self.assertEqual(st, 200)
        if len(raw) >= 1024:
            self.assertEqual(head.get('Content-Encoding'), 'gzip')
            import json
            self.assertIsInstance(json.loads(gzip.decompress(raw)), dict)

    def test_small_response_is_not_compressed(self):
        """小响应压了反而更大（gzip 自带约 20 字节头尾）。"""
        st, head, raw = self._get('/healthz', encoding='gzip')
        self.assertEqual(st, 200)
        self.assertLess(len(raw), 1024)
        self.assertNotIn('Content-Encoding', head)


class TestIdleWrites(unittest.TestCase):
    """空闲时不该写盘。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='autocheckin_idle_')
        self.svc = SV.Service(self.dir, version='9.9.9',
                              env_key=S.generate_key(), proxy='')
        for sid in ('v2ex', 'nodeseek'):
            self.svc.upsert_site({'id': sid, 'name': sid.upper(),
                                  'template': sid, 'kind': 'template',
                                  'daily_hour': 3, 'daily_minute': 0})
        self.now = int(time.time())
        self.saves = 0
        self._orig = ST.Store.save_state
        outer = self

        def counted(store_self, st):
            outer.saves += 1
            return outer._orig(store_self, st)

        ST.Store.save_state = counted

    def tearDown(self):
        ST.Store.save_state = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_tick_only_writes_when_it_has_to(self):
        sch = SCH.Scheduler(self.svc, now_fn=lambda: self.now)
        # 第一次要补排期，必须落盘（否则重启后又要重排）
        sch.tick()
        self.assertGreaterEqual(self.saves, 1, '首次排期没有落盘')

        # 之后什么都不该发生：排期都在，没有站点到期
        self.saves = 0
        for _ in range(30):
            sch.tick()
        self.assertEqual(self.saves, 0,
                         '空闲 tick 写了 %d 次盘' % self.saves)

    def test_ensure_scheduled_reports_what_it_did(self):
        cfg = self.svc.load_config()
        state = self.svc.load_state()
        runner = self.svc.make_runner(cfg)
        want = len([s for s in cfg.sites if s.enabled])
        self.assertGreater(want, 0)
        self.assertEqual(runner.ensure_scheduled(cfg, state, self.now), want)
        # 再调一次：都已排好，不该再报改动
        self.assertEqual(runner.ensure_scheduled(cfg, state, self.now), 0)


if __name__ == '__main__':
    unittest.main()
