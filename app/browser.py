"""浏览器层：执行"需要浏览器"的签到（Cloudflare 挑战、复杂交互、录制回放）。

与 engine.py 相同的分层思路：
    run_steps(driver, steps, keywords)  ← 纯逻辑，注入假 driver 即可单测
    BrowserRunner                       ← 真实 Playwright 驱动

两种浏览器来源：
  1) 内置：镜像内自带的 Chromium（默认，Web 界面能正常用，compose 最简单）
  2) 复用：通过 CDP 连接已存在的 Chrome（remote_cdp_url）
     注意：实测极空间上的 Kasm Chrome 把 9222 绑在容器内 127.0.0.1，
     要让本容器连上必须共享它的网络命名空间（network_mode: "container:browser"），
     而那样本容器的端口就无法再映射出来 —— 所以这是可选模式，不是默认。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .engine import FlowOutcome, classify_page
from .models import (ACTION_ASSERT_TEXT, ACTION_CLICK, ACTION_EXTRACT, ACTION_FILL,
                     ACTION_GOTO, ACTION_WAIT, ACTION_WAIT_FOR, Step)


def _safe_log(msg: str) -> None:
    """尽力打印一条日志。

    刻意不引入 logging 依赖，也不用 print 之外的机制；
    但必须保证"记录日志"这件事本身永远不会抛异常打断签到流程。
    """
    try:
        print('[浏览器] %s' % msg)
    except Exception:                                           # noqa: BLE001
        pass


@dataclass
class StepResult:
    action: str
    ok: bool
    detail: str = ''
    # extract 动作取到的值
    value: str = ''


class PageDriver:
    """页面驱动接口。真实实现包 Playwright，单测注入假实现。"""
    def goto(self, url: str, timeout_ms: int) -> None:
        raise NotImplementedError

    def click(self, selector: str, timeout_ms: int) -> None:
        raise NotImplementedError

    def fill(self, selector: str, value: str, timeout_ms: int) -> None:
        raise NotImplementedError

    def wait(self, ms: int) -> None:
        raise NotImplementedError

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        raise NotImplementedError

    def text(self) -> str:
        raise NotImplementedError

    def content(self) -> str:
        raise NotImplementedError


def _read_text(driver: PageDriver):
    """读页面文本；读不到返回 None（与"读到空字符串"区分开）。

    区分这两者很重要：拿不到页面内容时，我们其实**无法确认**签到是否成功。
    """
    try:
        return driver.text() or ''
    except Exception:  # noqa: BLE001
        return None


def run_steps(driver: PageDriver, steps: List[Step], success_kw: List[str],
              fail_kw: List[str], already_kw: Optional[List[str]] = None,
              variables: Optional[Dict[str, str]] = None) -> FlowOutcome:
    """在页面上按顺序执行步骤，然后按关键词判定结果。

    判定优先级与 http 流程一致：已领过 > 失败关键词 > 成功关键词。
    若关键词都没命中，则退化为"步骤是否全部走通"——但前提是页面内容可读，
    否则只能报"无法确认"，不能凭步骤走完就宣称成功。
    """
    variables = dict(variables or {})
    results: List[StepResult] = []
    trace: List[str] = []

    for step in (steps or []):
        action = (step.action or '').lower()
        target = _render(step.target, variables)
        value = _render(step.value, variables)
        try:
            if action == ACTION_GOTO:
                driver.goto(target, step.timeout_ms)
                results.append(StepResult(action, True, '已打开 %s' % target))
                trace.append('goto %s' % target)
            elif action == ACTION_CLICK:
                driver.click(target, step.timeout_ms)
                results.append(StepResult(action, True, '已点击 %s' % target))
                trace.append('click %s' % target)
            elif action == ACTION_FILL:
                driver.fill(target, value, step.timeout_ms)
                results.append(StepResult(action, True, '已填写 %s' % target))
                trace.append('fill %s' % target)
            elif action == ACTION_WAIT:
                ms = step.timeout_ms or 1000
                driver.wait(ms)
                results.append(StepResult(action, True, '等待 %dms' % ms))
                trace.append('wait %dms' % ms)
            elif action == ACTION_WAIT_FOR:
                driver.wait_for(target, step.timeout_ms)
                results.append(StepResult(action, True, '等到 %s' % target))
                trace.append('wait_for %s' % target)
            elif action == ACTION_EXTRACT:
                txt = _read_text(driver) or ''
                variables[step.value or 'v'] = txt
                results.append(StepResult(action, True, '已提取', value=txt[:200]))
                trace.append('extract -> %s' % (step.value or 'v'))
            elif action == ACTION_ASSERT_TEXT:
                content = _read_text(driver) or ''
                if target and target not in content:
                    raise AssertionError('页面上没有出现「%s」' % target)
                results.append(StepResult(action, True, '断言通过'))
                trace.append('assert_text %s' % target)
            else:
                results.append(StepResult(action, False, '未知动作，已跳过'))
                trace.append('未知动作 %s' % action)
        except Exception as e:  # noqa: BLE001
            results.append(StepResult(action, False, str(e)))
            trace.append('%s 失败：%s' % (action, e))
            if not step.optional:
                # 非可选步骤失败 → 整体失败，但要先看看页面是不是已经提示"已领过"
                page_text = _read_text(driver) or ''
                kind, kw = classify_page(page_text, success_kw, fail_kw, already_kw)
                if kind == 'already':
                    return FlowOutcome(True, '今天已经领过了（命中「%s」）' % kw,
                                       'already', vars=variables, trace=trace)
                return FlowOutcome(False, '%s 步骤失败：%s' % (action, e), 'fail',
                                   error_kind='step_failed',
                                   vars=variables, trace=trace)

    page_text = _read_text(driver)
    text_readable = page_text is not None
    kind, kw = classify_page(page_text or '', success_kw, fail_kw, already_kw)
    if kind == 'success':
        return FlowOutcome(True, '签到成功（命中「%s」）' % kw, 'success',
                           vars=variables, trace=trace)
    if kind == 'already':
        return FlowOutcome(True, '今天已经领过了（命中「%s」）' % kw, 'already',
                           vars=variables, trace=trace)
    if kind == 'fail':
        return FlowOutcome(False, '站点提示需要处理（命中「%s」）' % kw, 'fail',
                           vars=variables, trace=trace)

    # 关键词都没命中：只有"页面可读"时才敢用步骤结果兜底
    if not text_readable:
        return FlowOutcome(
            False, '步骤已执行，但无法读取页面内容，不能确认签到结果',
            'unknown', error_kind='page_unreadable', vars=variables, trace=trace)

    all_ok = all(r.ok for r in results)
    if all_ok and results:
        return FlowOutcome(True, '步骤全部执行成功（页面未出现明确关键词，按成功处理）',
                           'step_ok', vars=variables, trace=trace)
    if not results:
        return FlowOutcome(False, '没有可执行的步骤，且页面未命中关键词', 'unknown',
                           error_kind='no_steps', vars=variables, trace=trace)
    failed = [r for r in results if not r.ok]
    return FlowOutcome(False, '有 %d 个步骤未成功，且页面未命中关键词' % len(failed),
                       'unknown', error_kind='unclear', vars=variables, trace=trace)


def _render(text: str, variables: Dict[str, str]) -> str:
    out = text or ''
    for k, v in (variables or {}).items():
        out = out.replace('{%s}' % k, str(v))
    return out


# ------------------------------------------------------------------ Playwright


class PlaywrightDriver(PageDriver):
    """真实 Playwright 页面驱动。"""

    def __init__(self, page, timeout_default: int = 20000):
        self.page = page
        self.timeout_default = timeout_default

    def goto(self, url: str, timeout_ms: int) -> None:
        self.page.goto(url, timeout=timeout_ms or self.timeout_default,
                       wait_until='domcontentloaded')

    def click(self, selector: str, timeout_ms: int) -> None:
        self.page.click(selector, timeout=timeout_ms or self.timeout_default)

    def fill(self, selector: str, value: str, timeout_ms: int) -> None:
        self.page.fill(selector, value, timeout=timeout_ms or self.timeout_default)

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self.page.wait_for_selector(selector, timeout=timeout_ms or self.timeout_default)

    def text(self) -> str:
        try:
            return self.page.inner_text('body')
        except Exception:  # noqa: BLE001
            return ''

    def content(self) -> str:
        try:
            return self.page.content()
        except Exception:  # noqa: BLE001
            return ''


class BrowserRunner:
    """管理浏览器生命周期并执行站点签到。

    ⚠️ 线程约束（重要）
        Playwright 的**同步** API 与"创建它的那个线程"绑定：在 A 线程 start() 出来的
        playwright/browser，再到 B 线程里用就会抛
            RuntimeError: Cannot switch to a different thread
        （greenlet 不匹配，实测报错形如
            Current:  <greenlet ... otid=0xAAA>
            Expected: <greenlet ... otid=0xBBB>）

        而本项目的 BrowserRunner 是单例（Service.browser），却会被多个线程用到：
          * 调度线程   —— 定时触发签到
          * HTTP 线程  —— 网页上点「立即」
        所以必须处理"线程变了"的情形。

    做法：记录创建浏览器的线程 id（_owner_thread）。发现当前线程不是创建者时，
    先尽力关掉旧实例，再为当前线程重新创建。这样无论如何都只在本线程内使用
    Playwright 对象，既不会再报错，也不会出现两个线程同时操作一个浏览器。
    """

    def __init__(self, headless: bool = True, browser_path: str = '',
                 remote_cdp_url: str = ''):
        self.headless = headless
        self.browser_path = browser_path
        self.remote_cdp_url = remote_cdp_url
        self._pw = None
        self._browser = None
        # 创建当前 _pw/_browser 的线程 id；None 表示还没创建
        self._owner_thread: Optional[int] = None
        # 串行化：避免两个线程同时进入创建/使用流程
        self._lock = threading.RLock()

    # -- 生命周期 ------------------------------------------------------

    def _ensure_browser(self):
        with self._lock:
            me = threading.get_ident()
            # 线程变了：旧实例属于别的线程，不能再用（连 stop() 都不能在别的线程调）
            if self._browser is not None and self._owner_thread != me:
                self._drop_browser(reason='执行线程已变化')
            if self._browser is not None:
                return self._browser

            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:
                raise RuntimeError(
                    '未安装 Playwright（浏览器模式不可用）：%s' % e) from e

            self._pw = sync_playwright().start()
            self._owner_thread = me
            try:
                if self.remote_cdp_url:
                    # 复用已有 Chrome（需要能访问到它的 CDP 端口）
                    self._browser = self._pw.chromium.connect_over_cdp(self.remote_cdp_url)
                else:
                    launch_kwargs: Dict[str, Any] = {
                        'headless': self.headless,
                        'args': ['--no-sandbox', '--disable-dev-shm-usage'],
                    }
                    if self.browser_path:
                        launch_kwargs['executable_path'] = self.browser_path
                    self._browser = self._pw.chromium.launch(**launch_kwargs)
            except Exception:
                # 启动失败时别留下半成品，否则下次判断会以为"已经有浏览器"
                self._drop_browser(reason='启动失败')
                raise
            return self._browser

    def _drop_browser(self, reason: str = '') -> None:
        """丢弃当前浏览器实例。

        只有在【创建它的那个线程】里才能真正关掉它 —— 否则 Playwright 会再抛一次
        Cannot switch to a different thread。跨线程时只解除引用，让 GC 处理，
        避免为了清理反而把异常抛到调用方。
        """
        me = threading.get_ident()
        same_thread = (self._owner_thread == me)
        browser, pw = self._browser, self._pw
        self._browser = None
        self._pw = None
        self._owner_thread = None
        if not same_thread:
            # 跨线程无法安全关闭（调用 close()/stop() 会再抛同一个错），
            # 只解除引用交给 GC；这里静默处理，不打扰调用方。
            return
        try:
            if browser is not None:
                browser.close()
        except Exception as e:  # noqa: BLE001
            _safe_log('关闭浏览器时出错（忽略）：%s' % e)
        try:
            if pw is not None:
                pw.stop()
        except Exception as e:  # noqa: BLE001
            _safe_log('停止 Playwright 时出错（忽略）：%s' % e)

    def close(self) -> None:
        with self._lock:
            self._drop_browser(reason='显式关闭')

    # -- 执行 ----------------------------------------------------------

    def run(self, site, proxy: str = '', password: str = '',
            capture_excerpt: bool = False, cookie_header: str = ''):
        """执行站点签到。site 是 SiteConfig。

        整个浏览器交互过程都持有 _lock：
        否则会出现"A 线程正在用浏览器，B 线程发现线程不匹配把浏览器关掉"，
        导致 A 线程操作一个已被关闭的对象（比原来的报错更难查）。
        本方法会阻塞等待，属预期行为 —— 同一时刻只跑一个浏览器签到更安全。

        cookie_header：站点的登录 Cookie。浏览器上下文是全新的、零 Cookie，
        不注入就永远是未登录状态（这正是之前浏览器模式站点必然失败的原因）。
        """
        with self._lock:
            return self._run_locked(site, proxy, password, capture_excerpt,
                                    cookie_header)

    def _run_locked(self, site, proxy: str, password: str,
                    capture_excerpt: bool, cookie_header: str = ''):
        from .models import KIND_TEMPLATE
        from .templates import template_by_id

        tpl = template_by_id(site.template) if site.kind == KIND_TEMPLATE else {}
        steps = list(site.steps) if site.steps else []
        if not steps:
            # 内置模板没有录制步骤时，至少打开主页，让 Cloudflare 挑战在真实浏览器里通过
            homepage = site.homepage or tpl.get('homepage') or ''
            if homepage:
                steps = [Step(action=ACTION_GOTO, target=homepage, timeout_ms=45000),
                         Step(action=ACTION_WAIT, timeout_ms=6000)]
            else:
                return FlowOutcome(False, '该站点没有可执行的步骤，也没有主页地址',
                                   'unknown', error_kind='no_steps')

        # 注意：这里拿到的浏览器一定是【当前线程】创建的
        #（_ensure_browser 会在必要时先为当前线程重建）
        browser = self._ensure_browser()
        context = None
        created_context = False
        try:
            # 复用 CDP 时用已有 context，避免新建导致登录态丢失
            if self.remote_cdp_url and getattr(browser, 'contexts', None):
                ctxs = browser.contexts
                context = ctxs[0] if ctxs else browser.new_context()
                created_context = context is not ctxs[0] if ctxs else True
            else:
                context = browser.new_context(
                    user_agent=None,
                    ignore_https_errors=not site.verify_ssl,
                    locale='zh-CN',
                    timezone_id='Asia/Shanghai',
                )
                created_context = True

            # 注入站点的登录 Cookie。
            # 新建的 context 是【全新且零 Cookie】的，不注入就永远是未登录状态 ——
            # 这正是浏览器模式站点此前必然失败的根本原因。
            if cookie_header:
                try:
                    from .cookies import (domain_of, parse_cookie_input,
                                          to_playwright_cookies)
                    parsed, _fmt = parse_cookie_input(cookie_header)
                    dom = (domain_of(site.homepage)
                           or domain_of(tpl.get('mission_url') or ''))
                    pw_cookies = to_playwright_cookies(parsed, dom)
                    if pw_cookies:
                        context.add_cookies(pw_cookies)
                        _safe_log('已为「%s」注入 %d 个 Cookie'
                                  % (site.name, len(pw_cookies)))
                except Exception as e:                          # noqa: BLE001
                    # Cookie 注入失败不该让整个签到崩掉，但要说清楚
                    _safe_log('注入 Cookie 失败（%s）：%s' % (site.name, e))

            page = context.new_page()
            driver = PlaywrightDriver(page)
            outcome = run_steps(
                driver, steps,
                success_kw=list(site.success_keywords or tpl.get('success_keywords') or []),
                fail_kw=list(site.fail_keywords or tpl.get('fail_keywords') or []),
                already_kw=list(tpl.get('already_keywords') or []),
                variables={'username': site.username, 'password': password,
                           'homepage': site.homepage},
            )
            if capture_excerpt:
                try:
                    page.screenshot(full_page=False)
                except Exception:  # noqa: BLE001
                    pass
            return outcome
        finally:
            try:
                if created_context and context is not None:
                    context.close()
            except Exception:  # noqa: BLE001
                pass


def browser_available() -> bool:
    """当前环境是否**有浏览器能力**（用于自检与界面提示）。

    判据分两种情况 —— 以前只看"playwright 能否 import"，那是错的：
      * 配了 remote_cdp_url（连外部 Chrome 的调试端口）：
        只需要 playwright 包能 import。本地有没有浏览器二进制**无关**，
        因为浏览器在别人那儿（比如 NAS 上的 Chrome 容器）。
        以前这里会去查本地二进制，于是"明明配好了外部地址"却被判为
        不可用，界面/自检都显示没有浏览器（实测发现的缺陷）。
      * 没配 remote_cdp_url：需要本地浏览器二进制，因为要在本进程 launch。
        如果镜像为瘦身剔除了浏览器，只查 import 会返回 True，然后需要
        浏览器的站点会以 "Executable doesn't exist at ..." 这种难懂的错
        失败 —— 用户看到的是"启动失败"，而不是"这个镜像没带浏览器"。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    if configured_cdp_url():
        return True
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
        return bool(path) and os.path.exists(path)
    except Exception:                                           # noqa: BLE001
        # 启动 playwright 本身失败（缺系统依赖等）也算不可用
        return False


def configured_cdp_url() -> str:
    """从配置里读 remote_cdp_url；读不到就当没配。

    单独抽出来是为了让 browser_available() 在没有 service 上下文时
    （自检脚本、界面提示）也能判断。
    """
    try:
        from .store import Store
        data_dir = os.environ.get('DATA_DIR', '/data')
        cfg = Store(data_dir).load_config()
        return str(getattr(cfg, 'remote_cdp_url', '') or '')
    except Exception:                                           # noqa: BLE001
        return ''
