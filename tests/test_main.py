"""main 单测：参数与环境变量解析优先级、时区应用、进程级启动冒烟。"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from app import main as M  # noqa: E402

ENV_KEYS = ('PORT', 'HOST', 'DATA_DIR', 'TZ', 'PROXY', 'TICK_SECONDS',
            'NO_SCHEDULER', 'LOG_LEVEL', 'AUTOCHECKIN_KEY', 'APP_VERSION',
            'DISABLE_BROWSER')


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestBuildSettings(EnvCase):
    def test_defaults(self):
        s = M.build_settings([])
        # 默认内部端口是 28999（不是常见的 8080）：
        # host 网络模式下容器直接占用宿主端口，而 8080 在很多 NAS 上已被占用。
        self.assertEqual(s.port, 28999)
        self.assertEqual(s.host, '0.0.0.0')
        self.assertEqual(s.data_dir, '/data')
        self.assertEqual(s.timezone, 'Asia/Shanghai')
        self.assertEqual(s.proxy, '')
        self.assertEqual(s.tick_seconds, 60)
        self.assertFalse(s.no_scheduler)

    def test_cli_overrides_env(self):
        os.environ['PORT'] = '9999'
        os.environ['PROXY'] = 'http://from-env:1'
        s = M.build_settings(['--port', '7777', '--proxy', 'http://from-cli:2'])
        self.assertEqual(s.port, 7777, '命令行应优先于环境变量')
        self.assertEqual(s.proxy, 'http://from-cli:2')

    def test_env_used_when_no_cli(self):
        os.environ['PORT'] = '9999'
        os.environ['DATA_DIR'] = '/custom/data'
        os.environ['PROXY'] = 'http://env:1'
        os.environ['TZ'] = 'UTC'
        s = M.build_settings([])
        self.assertEqual(s.port, 9999)
        self.assertEqual(s.data_dir, '/custom/data')
        self.assertEqual(s.timezone, 'UTC')

    def test_proxy_env_is_ignored(self):
        """代理只能从网页设置来 —— 环境变量 PROXY 必须被忽略。

        为什么：两处都能设就会互相打架，而"网页里改完却不生效"是最难查的
        一类问题。现在只有一个权威来源，行为可预测。
        """
        os.environ['PROXY'] = 'http://from-env:1'
        s = M.build_settings([])
        self.assertEqual(s.proxy, '',
                         '环境变量 PROXY 不该再影响代理配置')
        # 但 --proxy 命令行参数保留（调试用）
        s2 = M.build_settings(['--proxy', 'http://from-cli:2'])
        self.assertEqual(s2.proxy, 'http://from-cli:2')

    def test_bad_env_port_falls_back(self):
        os.environ['PORT'] = 'not-a-number'
        self.assertEqual(M.build_settings([]).port, 28999)

    def test_tick_seconds_minimum(self):
        self.assertEqual(M.build_settings(['--tick-seconds', '1']).tick_seconds, 5)
        self.assertEqual(M.build_settings(['--tick-seconds', '30']).tick_seconds, 30)

    def test_no_scheduler_flag(self):
        self.assertTrue(M.build_settings(['--no-scheduler']).no_scheduler)

    def test_no_scheduler_env(self):
        os.environ['NO_SCHEDULER'] = 'true'
        self.assertTrue(M.build_settings([]).no_scheduler)
        os.environ['NO_SCHEDULER'] = '0'
        self.assertFalse(M.build_settings([]).no_scheduler)

    def test_env_key_detected(self):
        os.environ['AUTOCHECKIN_KEY'] = 'abc'
        s = M.build_settings([])
        self.assertEqual(s.env_key, 'abc')
        self.assertIn('环境变量', s.summary())

    def test_proxy_stripped(self):
        s = M.build_settings(['--proxy', '  http://p:1  '])
        self.assertEqual(s.proxy, 'http://p:1')

    def test_summary_mentions_key_points(self):
        s = M.build_settings(['--port', '1234'])
        text = s.summary()
        self.assertIn('1234', text)
        self.assertIn('直连', text)

    def test_version_flag_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            M.build_settings(['--version'])
        self.assertEqual(ctx.exception.code, 0)

    def test_unknown_flag_errors(self):
        with self.assertRaises(SystemExit):
            M.build_settings(['--nope'])


class TestTimezone(EnvCase):
    def test_apply_timezone(self):
        saved = os.environ.get('TZ')
        try:
            self.assertTrue(M.apply_timezone('UTC'))
            self.assertEqual(os.environ.get('TZ'), 'UTC')
        finally:
            if saved is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = saved

    def test_apply_bogus_timezone_does_not_raise(self):
        # 某些平台不会因非法时区抛错，这里只要求不崩
        saved = os.environ.get('TZ')
        try:
            M.apply_timezone('Not/AZone')
        finally:
            if saved is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = saved


class TestCreateService(unittest.TestCase):
    def test_service_created_with_data_dir(self):
        d = tempfile.mkdtemp(prefix='autocheckin_main_')
        try:
            s = M.Settings(data_dir=d, env_key='', version='1.2.3')
            svc = M.create_service(s)
            self.assertEqual(svc.version, '1.2.3')
            # 密钥文件应已生成
            from app import secrets as S
            self.assertTrue(os.path.exists(os.path.join(d, S.KEY_FILENAME)))
            self.assertEqual(len(svc.list_sites()), 3)
        finally:
            shutil.rmtree(d, ignore_errors=True)


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestProcessSmoke(unittest.TestCase):
    """真的把程序拉起来，验证能监听、能响应、能优雅退出。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='autocheckin_proc_')
        self.port = _free_port()
        env = dict(os.environ)
        env.update({
            'DATA_DIR': self.dir,
            'PORT': str(self.port),
            'TZ': 'Asia/Shanghai',
            'DISABLE_BROWSER': '1',
            'PYTHONPATH': HERE,
            'PYTHONIOENCODING': 'utf-8',
        })
        self.proc = subprocess.Popen(
            [sys.executable, '-m', 'app', '--no-scheduler'],
            cwd=HERE, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.base = 'http://127.0.0.1:%d' % self.port
        # 带 CookieJar：认证改成基于会话 Cookie 后，需要保持登录态
        import http.cookiejar
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar))
        self.password = 'proc-smoke-pw'

    def _login(self):
        """完成口令初始化（或登录），之后 self.opener 就带着会话 Cookie。"""
        payload = json.dumps({'password': self.password}).encode('utf-8')
        req = urllib.request.Request(
            self.base + '/api/auth/setup', data=payload,
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with self.opener.open(req, timeout=10) as r:
                if r.status == 200:
                    return
        except urllib.error.HTTPError:
            pass
        req = urllib.request.Request(
            self.base + '/api/auth/login', data=payload,
            headers={'Content-Type': 'application/json'}, method='POST')
        with self.opener.open(req, timeout=10) as r:
            self.assertEqual(r.status, 200, '登录失败')

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _wait_ready(self, timeout=25):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode('utf-8', 'replace')
                raise AssertionError('进程提前退出（码 %s）：\n%s'
                                     % (self.proc.returncode, out[-2000:]))
            try:
                with self.opener.open(self.base + '/healthz', timeout=3) as r:
                    if r.status == 200:
                        return True
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.4)
        raise AssertionError('等待就绪超时：%s' % last)

    def test_process_starts_serves_and_stops(self):
        self._wait_ready()

        # 页面匿名可看，但接口需要登录
        with self.opener.open(self.base + '/', timeout=10) as r:
            html = r.read().decode('utf-8')
        self.assertIn('自动签到', html)
        self.assertIn('tab-add', html)

        # 未登录时受保护接口应被拒绝
        try:
            self.opener.open(self.base + '/api/overview', timeout=10)
            self.fail('未登录竟然能读 /api/overview')
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (401, 403))

        self._login()

        with self.opener.open(self.base + '/api/overview', timeout=10) as r:
            ov = json.loads(r.read().decode('utf-8'))
        self.assertEqual(ov['site_count'], 3)
        self.assertEqual(ov['enabled_count'], 3)

        # 无调度模式：站点应该还没排过期
        with self.opener.open(self.base + '/api/sites', timeout=10) as r:
            sites = json.loads(r.read().decode('utf-8'))['sites']
        self.assertEqual(sites[0]['next_run_at'], 0,
                         '--no-scheduler 时不该自动排程')

        # 优雅退出。
        # 注意平台差异：Windows 的 Popen.terminate() 是 TerminateProcess（强制结束，
        # 退出码会是 1），而容器跑在 Linux 上，SIGTERM 会走我们的优雅关闭逻辑。
        self.proc.terminate()
        self.proc.wait(timeout=15)
        if os.name == 'posix':
            self.assertIn(self.proc.returncode, (0, -15, 143),
                          '应以正常码退出，实际 %s' % self.proc.returncode)
        else:
            self.assertIsNotNone(self.proc.returncode, '进程应已结束')

    def test_sigterm_graceful_shutdown_on_posix(self):
        """只在 POSIX 上验证：SIGTERM 应触发优雅退出（容器里就是这个路径）。"""
        if os.name != 'posix':
            self.skipTest('Windows 无 SIGTERM 语义，容器里才需要验证')
        import signal as _sig
        self._wait_ready()
        self.proc.send_signal(_sig.SIGTERM)
        self.proc.wait(timeout=20)
        self.assertIn(self.proc.returncode, (0, 143),
                      '收到 SIGTERM 应优雅退出，实际 %s' % self.proc.returncode)

    def test_settings_persist_across_restart(self):
        self._wait_ready()
        self._login()
        req = urllib.request.Request(
            self.base + '/api/settings',
            data=json.dumps({'proxy': 'http://persist:1'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with self.opener.open(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

        # 重启后配置应还在
        self.proc.terminate()
        self.proc.wait(timeout=15)
        env = dict(os.environ)
        env.update({'DATA_DIR': self.dir, 'PORT': str(self.port),
                    'DISABLE_BROWSER': '1', 'PYTHONPATH': HERE})
        self.proc = subprocess.Popen(
            [sys.executable, '-m', 'app', '--no-scheduler'],
            cwd=HERE, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._wait_ready()
        self._login()
        with self.opener.open(self.base + '/api/settings', timeout=10) as r:
            cfg = json.loads(r.read().decode('utf-8'))
        self.assertEqual(cfg['proxy'], 'http://persist:1',
                         '重启后配置应保持')


if __name__ == '__main__':
    unittest.main(verbosity=2)
