"""签到引擎：把"怎么签到"抽象成可测的流程。

分层设计（关键）：
    run_http_flow(fetcher, flow, keywords)  ← 纯逻辑，注入 fetcher 即可单测
    checkin_site(site, ...)                 ← 组装真实网络/浏览器后调用上面的流程

站点分类：
    http     纯请求（V2EX）
    browser  需要浏览器（NodeSeek 的 Cloudflare、Chiphell 的验证问答）
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .httpclient import HttpConfig, HttpError, HttpResponse, request
from .models import (ACTION_ASSERT_TEXT, ACTION_CLICK, ACTION_EXTRACT, ACTION_FILL,
                     ACTION_GOTO, ACTION_WAIT, ACTION_WAIT_FOR, KIND_TEMPLATE,
                     SiteConfig)
from .notify import CheckinResult
from .templates import template_by_id


@dataclass
class FlowOutcome:
    """一次流程执行的结果（与网络/浏览器无关）。"""

    success: bool
    message: str = ''
    # 命中的关键词属于哪一类
    hit: str = ''        # success / fail / already
    error_kind: str = ''
    # 流程中被提取出来的变量（如 V2EX 的 once 令牌）
    vars: Dict[str, str] = field(default_factory=dict)
    # 便于调试：记录每步做了什么
    trace: List[str] = field(default_factory=list)


# 供流程使用的取数函数签名： (method, url, data) -> HttpResponse
Fetcher = Callable[[str, str, Optional[bytes]], HttpResponse]


def _render(text: str, variables: Dict[str, str]) -> str:
    """把 {var} 占位符替换成实际值。"""
    if not text:
        return text
    out = text
    for k, v in (variables or {}).items():
        out = out.replace('{%s}' % k, str(v))
    return out


def match_keywords(text: str, keywords: List[str]) -> Optional[str]:
    """在文本里找关键词，返回命中的那个（未命中返回 None）。"""
    if not text:
        return None
    for kw in (keywords or []):
        if kw and kw in text:
            return kw
    return None


def classify_page(text: str, success_kw: List[str], fail_kw: List[str],
                  already_kw: Optional[List[str]] = None) -> Tuple[str, str]:
    """判断页面属于 成功/失败/已领过/未知。

    优先判"失败"和"已领过"，因为它们常常同时含有"领取"这类成功词，
    例如"今天已经领取过了"里同时有"领取"和"已经领取"。
    返回 (类别, 命中的关键词)。
    """
    hit_already = match_keywords(text, already_kw or [])
    if hit_already:
        return 'already', hit_already
    hit_fail = match_keywords(text, fail_kw or [])
    if hit_fail:
        return 'fail', hit_fail
    hit_ok = match_keywords(text, success_kw or [])
    if hit_ok:
        return 'success', hit_ok
    return 'unknown', ''


def _extra_note(variables: Dict[str, str]) -> str:
    """把提取到的数值拼成一句附加说明，没有可展示的值就返回空串。

    只挑已知有意义的键，避免把内部令牌（once）之类也打印出来。
    """
    if not variables:
        return ''
    labels = (
        ('gain', '获得 %s 个鸡腿'),
        ('current', '共 %s'),
        ('log_days', '已连续登录 %s 天'),
        ('log_value', '每日奖励 %s 铜币'),
        ('jifen', '当前积分 %s'),
    )
    parts = []
    for key, fmt in labels:
        v = variables.get(key)
        if v not in (None, ''):
            parts.append(fmt % v)
    return ('｜' + '，'.join(parts)) if parts else ''


def run_http_flow(fetcher: Fetcher, flow: List[Dict[str, Any]],
                  success_kw: List[str], fail_kw: List[str],
                  already_kw: Optional[List[str]] = None,
                  initial_vars: Optional[Dict[str, str]] = None,
                  initial_text: str = '',
                  initial_status: int = 200) -> FlowOutcome:
    """按声明式流程执行一次纯请求签到。

    flow 支持的动作：
        {'action':'get',  'url': '...'}
        {'action':'post', 'url': '...', 'data': {...}}
        {'action':'extract_regex', 'pattern': '...', 'save_as': 'once', 'from': 'last'}

    initial_text / initial_status：流程为空、或没有任何请求发生时，用于判定的页面内容，
    保证这种情况下也能正常判定而不会退化成"无法判断"。
    """
    variables: Dict[str, str] = dict(initial_vars or {})
    trace: List[str] = []
    # 初始响应：让"无请求"路径也有内容可判定
    last_resp: Optional[HttpResponse] = HttpResponse(
        status=initial_status, text=initial_text, url='')

    for step in (flow or []):
        action = (step.get('action') or '').lower()
        try:
            if action == 'get':
                url = _render(step.get('url', ''), variables)
                last_resp = fetcher('GET', url, None)
                trace.append('GET %s -> %s' % (url, last_resp.status))
            elif action == 'post':
                url = _render(step.get('url', ''), variables)
                data = step.get('data')
                body = None
                if isinstance(data, dict):
                    from .httpclient import enc
                    rendered = {k: _render(str(v), variables) for k, v in data.items()}
                    body = enc(rendered)
                elif isinstance(data, str):
                    body = _render(data, variables).encode('utf-8')
                last_resp = fetcher('POST', url, body)
                trace.append('POST %s -> %s' % (url, last_resp.status))
            elif action == 'extract_regex':
                if last_resp is None:
                    continue
                pattern = step.get('pattern', '')
                m = re.search(pattern, last_resp.text or '')
                if m:
                    variables[step.get('save_as', 'v')] = m.group(1)
                    trace.append('提取 %s=%s' % (step.get('save_as'), m.group(1)))
                elif step.get('required'):
                    # 关键令牌取不到：继续往下只会拿字面量 {var} 去请求，
                    # 结果既不可信又会误导排查。直接失败并说明 HTTP 状态。
                    st = last_resp.status if last_resp else 0
                    detail = ('（HTTP %d，可能是被拦截或未登录）' % st
                              if st and (st < 200 or st >= 300)
                              else '（页面内容不含该令牌，通常意味着未登录）')
                    return FlowOutcome(
                        False, '页面里没找到预期的令牌%s' % detail, 'fail',
                        error_kind=('http_%d' % st) if st and (st < 200 or st >= 300)
                        else 'no_token',
                        vars=variables, trace=trace)
            else:
                trace.append('跳过未知动作 %s' % action)
        except HttpError as e:
            return FlowOutcome(False, str(e), e.kind, error_kind=e.kind,
                               vars=variables, trace=trace)

    text = (last_resp.text if last_resp else '') or ''
    kind, kw = classify_page(text, success_kw, fail_kw, already_kw)

    # 把流程里提取到的、值得展示的数值附在消息后面，
    # 例如 NodeSeek 的「获得 7 个鸡腿，共 408」、V2EX 的连续登录天数。
    # 这些是用户在通知里最想看到的东西，光提取不显示等于白提。
    extra = _extra_note(variables)

    if kind == 'success':
        return FlowOutcome(True, '签到成功（命中「%s」）%s' % (kw, extra),
                           'success', vars=variables, trace=trace)
    if kind == 'already':
        # 已经领过也算成功：不该因为"重复领取"而报失败并触发重试
        return FlowOutcome(True, '今天已经领过了（命中「%s」）%s' % (kw, extra),
                           'already', vars=variables, trace=trace)
    if kind == 'fail':
        return FlowOutcome(False, '站点提示需要处理（命中「%s」）%s' % (kw, extra),
                           'fail', vars=variables, trace=trace)

    status = last_resp.status if last_resp else 0
    # 无法判定时区分两种情况：HTTP 层就失败（把状态码带出来）还是页面看不懂
    if status and (status < 200 or status >= 300):
        return FlowOutcome(
            False, 'HTTP %d，且页面未命中任何已知关键词' % status,
            'unknown', error_kind='http_%d' % status, vars=variables, trace=trace)

    return FlowOutcome(False, '无法判断签到结果（HTTP %s，页面未命中任何已知关键词）' % status,
                       'unknown', error_kind='unclear', vars=variables, trace=trace)


def _http_fetcher(site: SiteConfig, proxy: str,
                  cookie_header: str = '',
                  headers_override: Optional[Dict[str, str]] = None) -> Fetcher:
    """构造真实网络取数函数。

    cookie_header：站点配置里的 Cookie 头（形如 "a=1; b=2"）。
        以前这里只建了个空 CookieJar，既不注入站点 Cookie，也没有任何凭据，
        所以需要登录的站点永远拿到未登录页面 → 必然签到失败。
        现在把它显式塞进每个请求的 Cookie 头。

    另外把站点自定义请求头一并带上：很多站点的接口会校验
    Origin / Referer / Accept-Language，缺了就返回 403
    （实测 NodeSeek 的 /api/attendance：只带 Cookie 是 403，
      补上 Origin 与 Referer 才 200）。

    headers_override：已经渲染好的自定义头（含 {cookie} 之类占位符的结果）。
        由调用方传入，避免为了替换占位符而就地修改 site.headers
        （那会把明文 Cookie/密码写回配置，见 checkin_via_http 的说明）。
    """
    from http.cookiejar import CookieJar

    jar = CookieJar()
    headers: Dict[str, str] = {}
    source = headers_override if headers_override is not None else site.headers
    # 先放站点自定义头，再放 Cookie（Cookie 不允许被用户配置覆盖，避免误配）
    for k, v in (source or {}).items():
        if k and v is not None and k.lower() != 'cookie':
            headers[str(k)] = str(v)
    if cookie_header:
        headers['Cookie'] = cookie_header
    cfg = HttpConfig(verify_ssl=site.verify_ssl, timeout=25, headers=headers)

    def fetch(method: str, url: str, data: Optional[bytes]) -> HttpResponse:
        return request(method, url, cfg=cfg, global_proxy=proxy, data=data,
                       cookie_jar=jar)

    return fetch


def checkin_via_http(site: SiteConfig, proxy: str = '',
                     password: str = '',
                     cookie_header: str = '') -> FlowOutcome:
    """纯请求站点（V2EX / NodeSeek / Chiphell 等）的签到入口。

    cookie_header：该站点的登录 Cookie 头。没有它就只能拿到未登录页面。
    """
    tpl = template_by_id(site.template) if site.kind == KIND_TEMPLATE else {}
    flow = list(tpl.get('flow') or [])
    if site.steps:
        # 自定义站点若填的是 http 步骤，也可走这条路
        flow = [s.to_dict() for s in site.steps] or flow

    mission = tpl.get('mission_url') or site.homepage
    variables = {'mission_url': mission, 'homepage': site.homepage,
                 'username': site.username, 'password': password,
                 'cookie': cookie_header}

    # 站点自定义头里的 {cookie} / {password} 占位符要替换掉
    # （有些模板把 Cookie 写在 headers 里）。
    #
    # ⚠️ 必须替换到**局部副本**，绝不能就地改 site.headers：
    #    variables 里含明文 Cookie 与密码，一旦写回 site.headers，
    #    runner 随后 save_config 就会把明文落盘 config.json，
    #    还会进 WebDAV 备份 —— 等于完全绕过 SecretBox 加密层。
    #    （这里踩过一次：就地赋值会让"加密存储"这个承诺失效。）
    rendered_headers = {
        str(k): _render(str(v), variables)
        for k, v in (site.headers or {}).items()
    }

    return run_http_flow(
        fetcher=_http_fetcher(site, proxy, cookie_header, rendered_headers),
        flow=[_render_step(s, variables) for s in flow],
        success_kw=list(site.success_keywords or tpl.get('success_keywords') or []),
        fail_kw=list(site.fail_keywords or tpl.get('fail_keywords') or []),
        already_kw=list(tpl.get('already_keywords') or []),
        initial_vars=variables,
    )


def _render_step(step: Dict[str, Any], variables: Dict[str, str]) -> Dict[str, Any]:
    out = dict(step)
    for k in ('url', 'pattern'):
        if out.get(k):
            out[k] = _render(str(out[k]), variables)
    return out


def needs_browser(site: SiteConfig) -> bool:
    """该站点是否需要浏览器。"""
    if site.need_browser:
        return True
    if site.kind == KIND_TEMPLATE:
        tpl = template_by_id(site.template)
        return bool(tpl.get('need_browser'))
    return bool(site.steps)


def check_cookie_valid(site: SiteConfig, cookie_header: str,
                       proxy: str = '') -> Tuple[int, str]:
    """检查站点的登录 Cookie 是否还有效。

    返回 (状态码, 说明)：1=有效 2=失效 0=无法判断

    这里刻意做**只读**检查：绝不执行签到动作。
    原因：像 NodeSeek 的 /api/attendance 是"一天一次"的接口，
    拿它来"检查"会真的把当天的签到用掉，或者得到"今天已签到"，
    两种情况都会被误读成"Cookie 失效"并发出错误通知。

    NodeSeek 的特殊处理：
        它的签到走 API（不在 Cloudflare 防线后），但**版块页 /board
        是被 Cloudflare 挑战拦着的**（实测 403 + Just a moment）。
        所以没法用页面内容判断登录态 —— 那就老实说"无法只读判断"，
        让用户从真正的签到结果里看。绝不能因为"页面被拦"就报 Cookie 失效。
    """
    tpl = template_by_id(site.template) if site.kind == KIND_TEMPLATE else {}
    url = tpl.get('mission_url') or site.homepage
    if not url:
        return 0, '该站点没有可用的检查地址'
    if not cookie_header:
        return 0, '还没有配置 Cookie'

    # 先做一次纯本地的结构检查：明显的残缺在这里就能发现，
    # 而且不消耗任何网络请求、也不会被站点的安全验证干扰。
    try:
        from .cookies import parse_cookie_input
        parsed, _fmt = parse_cookie_input(cookie_header)
    except Exception:                                           # noqa: BLE001
        parsed = {}
    if not parsed:
        return 2, 'Cookie 内容无法解析，请重新粘贴'
    if len(parsed) < 2:
        return 0, ('只解析出 %d 个 Cookie（%s），'
                   '可能是复制不完整 —— 登录态通常需要多个字段'
                   % (len(parsed), '、'.join(parsed)))

    # NodeSeek：无法只读校验，明确说明而不是误报失效
    is_attendance_api = 'api/attendance' in (url or '') or site.id == 'nodeseek'
    if is_attendance_api:
        return 0, ('已保存 %d 个 Cookie（%s）。该站点的签到走接口、'
                   '而版块页被 Cloudflare 挑战拦着，无法在只读的情况下判断登录态；'
                   '请以实际签到结果为准（该站点签到本身不受 Cloudflare 影响）'
                   % (len(parsed), '、'.join(list(parsed)[:4])))

    method = 'GET'
    fail_kw = list(site.fail_keywords or tpl.get('fail_keywords') or [])
    fetch = _http_fetcher(site, proxy, cookie_header)
    try:
        resp = fetch(method, url, None)
    except HttpError as e:
        return 0, '请求失败：%s' % e
    except Exception as e:                                      # noqa: BLE001
        return 0, '请求异常：%s' % e

    text = (resp.text or '')[:20000]
    status = getattr(resp, 'status', 0)

    # 命中失败关键词 → 失效
    hit = match_keywords(text, fail_kw)
    if hit:
        return 2, '登录状态已失效（页面提示「%s」）' % hit

    # 命中成功关键词 → 有效
    hit = match_keywords(text, list(site.success_keywords or
                                    tpl.get('success_keywords') or []))
    if hit:
        return 1, '登录状态有效（页面出现「%s」）' % hit

    # 网络层就没成功
    if status and (status < 200 or status >= 300):
        return 0, 'HTTP %d，无法据此判断登录状态' % status

    # Cloudflare 挑战页：这时"没命中失败词"不代表登录有效，但也不代表失效
    low = text.lower()
    if any(k in low for k in ('just a moment', 'challenge-platform',
                              'cf-challenge', '正在进行安全验证')):
        return 0, ('页面被站点的安全验证（Cloudflare）拦住，' 
                   '不能据此判断 Cookie 是否有效；以实际签到结果为准')

    return 0, '页面里既没有登录失效提示，也没有成功标志，无法判断'


def checkin_site(site: SiteConfig, proxy: str = '', password: str = '',
                 browser=None, cookie_header: str = '') -> CheckinResult:
    """统一的签到入口：自动选择 HTTP 或浏览器路径。

    browser 为 None 时，如果需要浏览器会返回失败并说明原因（由上层决定是否启用）。
    cookie_header：该站点的登录 Cookie（由上层解密后传入）。
    """
    started = time.time()
    attempt = int((site.state or {}).get('_attempt', 1) or 1)
    try:
        if needs_browser(site):
            if browser is None:
                return CheckinResult(
                    site_id=site.id, site_name=site.name, success=False,
                    message='该站点需要浏览器，但浏览器未启用/不可用',
                    error_kind='browser_unavailable', attempt=attempt,
                    duration_ms=int((time.time() - started) * 1000))
            outcome = browser.run(site, proxy=proxy, password=password,
                                  cookie_header=cookie_header)
        else:
            outcome = checkin_via_http(site, proxy=proxy, password=password,
                                      cookie_header=cookie_header)
    except HttpError as e:
        return CheckinResult(site_id=site.id, site_name=site.name, success=False,
                             message=str(e), error_kind=e.kind, attempt=attempt,
                             duration_ms=int((time.time() - started) * 1000))
    except Exception as e:  # noqa: BLE001
        return CheckinResult(site_id=site.id, site_name=site.name, success=False,
                             message='签到过程出错：%s' % e, error_kind='exception',
                             attempt=attempt,
                             duration_ms=int((time.time() - started) * 1000))

    return CheckinResult(
        site_id=site.id, site_name=site.name, success=outcome.success,
        message=outcome.message,
        error_kind='' if outcome.success else (outcome.error_kind or 'failed'),
        attempt=attempt,
        duration_ms=int((time.time() - started) * 1000),
    )
