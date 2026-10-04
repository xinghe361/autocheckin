"""复现「Cannot switch to a different thread」：同一个 BrowserRunner 被两个线程使用。

诊断假设：BrowserRunner 缓存了 playwright 实例与 browser，但 Playwright 的同步 API
与创建它的线程绑定。调度线程先建好浏览器，HTTP 接口线程（手动点"立即签到"）再去用，
就会抛 greenlet 线程不匹配的错误。

本测试用一个"假的 playwright"精确模拟这条约束：
    * sync_playwright().start() 记录当前线程；
    * 之后任何方法在别的线程被调用 -> 抛 RuntimeError('Cannot switch to a different thread')。
这样不依赖真的 Playwright，也能验证 BrowserRunner 有没有违规复用。
"""
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from app import browser as B
from app.models import Step


class FakePage:
    def __init__(self, owner_thread):
        self._owner = owner_thread
        self._text = '恭喜，签到成功'

    def _check_thread(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError(
                'Cannot switch to a different thread\n\tCurrent:  x\n\tExpected: y')

    def goto(self, url, **kw):
        self._check_thread()

    def click(self, sel, **kw):
        self._check_thread()

    def fill(self, sel, val, **kw):
        self._check_thread()

    def wait_for_timeout(self, ms):
        self._check_thread()

    def wait_for_selector(self, sel, **kw):
        self._check_thread()

    def inner_text(self, sel):
        self._check_thread()
        return self._text

    def content(self):
        self._check_thread()
        return '<html>%s</html>' % self._text


class FakeContext:
    def __init__(self, owner):
        self._owner = owner

    def new_page(self):
        return FakePage(self._owner)

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, owner):
        self._owner = owner

    def new_context(self, **kw):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Cannot switch to a different thread')
        return FakeContext(self._owner)

    def is_connected(self):
        return True

    def close(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Cannot switch to a different thread')


class FakeChromium:
    def __init__(self, owner):
        self._owner = owner

    def launch(self, **kw):
        return FakeBrowser(self._owner)

    def connect_over_cdp(self, url):
        return FakeBrowser(self._owner)


class FakePlaywright:
    def __init__(self, owner):
        self._owner = owner
        self.chromium = FakeChromium(owner)

    def stop(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Cannot switch to a different thread')


class FakeSyncPlaywright:
    """模拟 playwright.sync_api.sync_playwright()。"""
    def start(self):
        return FakePlaywright(threading.get_ident())


def install_fake_playwright():
    """把假的 playwright.sync_api 注入 sys.modules。

    注意：app.browser._ensure_browser() 里是【函数内】`from playwright.sync_api import
    sync_playwright`，所以直接改模块属性打不到它，必须替换 sys.modules 里的模块。
    """
    import types
    fake_pkg = types.ModuleType('playwright')
    fake_api = types.ModuleType('playwright.sync_api')
    fake_api.sync_playwright = lambda: FakeSyncPlaywright()
    fake_pkg.sync_api = fake_api
    _saved['playwright'] = sys.modules.get('playwright')
    _saved['playwright.sync_api'] = sys.modules.get('playwright.sync_api')
    sys.modules['playwright'] = fake_pkg
    sys.modules['playwright.sync_api'] = fake_api


_saved = {}


def remove_fake_playwright():
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


class TestThreadAffinity(unittest.TestCase):
    def setUp(self):
        install_fake_playwright()
        self.steps = [Step(action='goto', target='https://example.com/'),
                      Step(action='click', target='.sign')]

    def tearDown(self):
        remove_fake_playwright()

    def _run_in_thread(self, runner, site, results, key):
        try:
            r = runner.run(site, '', '')
            results[key] = r
        except Exception as e:                                  # noqa: BLE001
            results[key] = 'ERROR: %s' % e

    def test_same_thread_is_fine(self):
        """同一线程连续两次：应该没问题（基线）。"""
        runner = B.BrowserRunner(headless=True)
        site = _site()
        r1 = runner.run(site, '', '')
        r2 = runner.run(site, '', '')
        self.assertIsInstance(r1, object)
        self.assertFalse(isinstance(r1, str))
        self.assertFalse(isinstance(r2, str))

    def test_two_threads_must_not_raise(self):
        """这条就是用户遇到的场景：调度线程跑一次，另一个线程再跑一次。

        修复前：第二次会抛 RuntimeError('Cannot switch to a different thread')。
        修复后：应当自动为当前线程重建浏览器，正常完成。
        """
        runner = B.BrowserRunner(headless=True)
        site = _site()
        results = {}

        t1 = threading.Thread(target=self._run_in_thread,
                              args=(runner, site, results, 'first'))
        t1.start()
        t1.join(timeout=30)

        # 第二次在【主线程】跑 —— 与第一次不同线程
        self._run_in_thread(runner, site, results, 'second')

        self.assertNotIsInstance(results.get('first'), str,
                                 '第一次就失败了：%s' % results.get('first'))
        second = results.get('second')
        self.assertNotIsInstance(
            second, str,
            '跨线程第二次调用失败了（就是用户报的 bug）：%s' % second)

    def test_thread_switch_rebuilds_browser(self):
        """换线程后应当重建浏览器，而不是继续用旧对象。"""
        runner = B.BrowserRunner(headless=True)
        site = _site()
        results = {}
        t1 = threading.Thread(target=self._run_in_thread,
                              args=(runner, site, results, 'first'))
        t1.start()
        t1.join(timeout=30)

        owner_before = getattr(runner, '_owner_thread', None)
        self.assertIsNotNone(owner_before, '应记录创建浏览器的线程 id')

        # 主线程再跑一次
        self._run_in_thread(runner, site, results, 'second')
        owner_after = getattr(runner, '_owner_thread', None)
        self.assertEqual(owner_after, threading.get_ident(),
                         '换线程后 _owner_thread 应更新为当前线程')
        self.assertNotEqual(owner_before, owner_after)

    def test_close_from_other_thread_is_safe(self):
        """在别的线程调 close() 不能把异常抛出来。

        跨线程无法真正 close/stop（Playwright 会再抛同一个错），
        所以实现里只解除引用；这里确认它不会炸。
        """
        runner = B.BrowserRunner(headless=True)
        site = _site()
        runner.run(site, '', '')          # 主线程创建

        err = []

        def closer():
            try:
                runner.close()
            except Exception as e:                              # noqa: BLE001
                err.append(e)

        t = threading.Thread(target=closer)
        t.start()
        t.join(timeout=10)
        self.assertEqual(err, [], '跨线程 close() 不该抛异常：%s' % err)

    def test_concurrent_runs_do_not_close_each_others_browser(self):
        """并发场景：一个线程在用时，另一个线程不能把它正在用的浏览器关掉。

        这是修复过程中发现的第二个问题 —— 如果锁只覆盖"创建"而不覆盖"使用"，
        B 线程会因为线程不匹配把浏览器关掉，A 线程就会操作已关闭的对象。
        这里让两个线程同时跑，断言都成功、且没有任何异常。
        """
        runner = B.BrowserRunner(headless=True)
        site = _site()
        results = {}
        barrier = threading.Barrier(2)

        def worker(key):
            try:
                barrier.wait(timeout=10)
                results[key] = runner.run(site, '', '')
            except Exception as e:                              # noqa: BLE001
                results[key] = 'ERROR: %s' % e

        ts = [threading.Thread(target=worker, args=(k,)) for k in ('a', 'b')]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=40)

        for k in ('a', 'b'):
            self.assertNotIsInstance(results.get(k), str,
                                     '并发线程 %s 失败：%s'
                                     % (k, results.get(k)))


def _site():
    """构造一个用浏览器执行的站点（字段名按 models.Step / SiteConfig 的真实定义）。"""
    from app.models import SiteConfig, KIND_CUSTOM
    return SiteConfig(
        id='nodeseek', name='NodeSeek 试试手气', kind=KIND_CUSTOM,
        need_browser=True, homepage='https://www.nodeseek.com/board',
        steps=[Step(action='goto', target='https://www.nodeseek.com/board'),
               Step(action='click', target='.roll')],
        success_keywords=['签到成功', '领取成功'],
        fail_keywords=['失败'],
    )


if __name__ == '__main__':
    unittest.main(verbosity=2)
