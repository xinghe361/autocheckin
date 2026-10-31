"""失败原因归类：把 error_kind 翻译成"人能看懂、也能按类统计"的东西。

为什么要单独一层：
  * error_kind 是给程序看的（http_403 / proxy_refused / step_failed），
    直接摊在界面上用户看不懂，也没法回答"这个月到底失败在哪一类上"。
  * 站点失败时 error_kind 常常只有 unclear，真正的线索在消息里
    （"请先登录"、"访问过于频繁"）—— 所以这里按关键词兜底再判一次。
  * 归类**读的时候算**，不写进 history.jsonl：以后调整分类规则，
    历史记录会自动按新规则重算，不需要迁移老数据。

输出固定是五段：
    code            稳定的归类标识（统计按它聚合）
    label           短标签（界面上那个小色块）
    category        大类（统计面板按它分组，避免碎成一堆）
    category_label  大类的中文名
    hint            一句话的处置建议（鼠标悬停看）
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .netutil import describe_network_error

# 大类：code -> (中文名, 主题色)
# 颜色只用来区分"该找谁"：红=凭据，橙=被站点拦，蓝=网络/代理，
# 紫=站点服务端，灰=规则/环境（这几类通常要改配置或重录）。
CATEGORIES: Dict[str, Tuple[str, str]] = {
    'auth': ('登录与凭据', '#c0392b'),
    'block': ('被站点拦截', '#d97706'),
    'network': ('网络与代理', '#2f6ae8'),
    'http': ('站点返回', '#7c3aed'),
    'rule': ('签到规则', '#6b7280'),
    'env': ('运行环境', '#6b7280'),
    'other': ('其它', '#7a828c'),
}

# code -> (label, category, hint)
_CODES: Dict[str, Tuple[str, str, str]] = {
    # -- 登录与凭据（最常见，也最该先修）
    'not_logged_in': ('未登录', 'auth',
                      '站点返回的是未登录页面；到该站点的「编辑」里更新 Cookie'),
    'cookie_expired': ('登录已过期', 'auth',
                       'Cookie 失效了；重新登录站点后复制一份新的 Cookie'),
    'cookie_error': ('Cookie 无效', 'auth',
                     'Cookie 格式不对或已过期；重新复制一份完整的 Cookie'),
    'secret_error': ('缺少凭据', 'auth',
                     '该站点没有可用的 Cookie/密钥，或密钥解不开；'
                     '检查站点 Cookie 与 SECRET_KEY 是否变过'),
    'captcha': ('遇到验证码', 'auth',
                '站点弹了验证码/人机校验，自动化过不去；'
                '换成手动复制 Cookie，或关掉该站点的自动签到'),
    'forbidden': ('没有权限', 'auth',
                  '当前账号无权执行该操作；确认账号状态或换用有权限的账号'),

    # -- 被站点拦截
    'http_403': ('被拒绝（403）', 'block',
                 '常见于风控/Cloudflare 拦截或 IP 被限制；'
                 '稍后重试，或改用代理换出口 IP'),
    'http_429': ('被限流（429）', 'block',
                 '请求太频繁；把调度时间错开、减少重试次数'),
    'rate_limited': ('被限流', 'block',
                     '请求太频繁；把调度时间错开、减少重试次数'),
    'http_451': ('地区限制（451）', 'block',
                 '该站点按地区限制了访问；需要用对应地区的代理'),

    # -- 网络与代理（hint 复用 describe_network_error，文案只有一处）
    'timeout': ('连接超时', 'network', ''),
    'refused': ('连接被拒', 'network', ''),
    'reset': ('连接被重置', 'network', ''),
    'unreachable': ('网络不可达', 'network', ''),
    'dns_error': ('域名解析失败', 'network', ''),
    'proxy_dns_error': ('代理地址解析失败', 'network', ''),
    'proxy_refused': ('代理拒绝连接', 'network', ''),
    'no_proxy': ('代理不可用', 'network',
                 '配置了走代理但代理没法用；检查「设置 → 网络」'),
    'no_socks': ('缺少 socks 支持', 'network',
                 '这个镜像没装 PySocks，socks 代理用不了；'
                 '改用 http 代理，或重建镜像带上 PySocks'),
    'bad_socks_url': ('socks 地址写错了', 'network',
                      '代理地址格式不对，示例：socks5://127.0.0.1:1080'),
    'site_timeout': ('执行超时', 'network',
                     '一次签到超过了设定的时长上限，多半是代理或站点太慢；'
                     '可在「站点 → 编辑 → 失败重试」里调大上限'),
    'tls_error': ('证书校验失败', 'network', ''),
    'http_error': ('请求失败', 'network',
                   'HTTP 层就失败了，没拿到有效响应；看消息里的状态码'),

    # -- 站点返回
    'http_5xx': ('服务端错误（5xx）', 'http',
                 '站点自己出错了；通常过几小时会恢复，不用改配置'),
    'http_4xx': ('请求被拒（4xx）', 'http',
                 '站点不接受这个请求；可能需要更新 Cookie 或站点已改版'),
    'http_3xx': ('被重定向（3xx）', 'http',
                 '多为登录态失效跳到登录页，或站点换了域名；'
                 '先更新 Cookie，仍不行就重新录制'),
    'bad_response': ('响应无法解析', 'http',
                     '返回的不是预期的内容；可能被拦截页/验证页顶掉了'),

    # -- 签到规则（脚本与站点对不上）
    'step_failed': ('步骤执行失败', 'rule',
                    '找不到按钮或点了没反应；站点改版了就重新录制一次'),
    'no_steps': ('没有可执行步骤', 'rule',
                 '规则为空或录制不完整；重新录制一次'),
    'page_unreadable': ('页面读不出来', 'rule',
                        '页面结构变了、需要登录或被拦截；先看消息再决定重录'),
    'unclear': ('判定不了结果', 'rule',
                '流程跑完了，但没找到成功/失败的标志；'
                '检查该站点的成功判定关键词'),
    'failed': ('签到失败', 'rule', '没有更具体的原因；看消息里的站点原文'),

    # -- 运行环境
    'browser_unavailable': ('浏览器不可用', 'env',
                            '容器里的 Chromium 起不来；看容器日志与内存占用'),
    'exception': ('执行异常', 'env',
                  '执行过程中抛了异常；看容器日志里的堆栈'),

    # -- 兜底
    'unknown': ('原因不明', 'other', '没有可用的失败信息'),
}

# 消息关键词兜底：只在 error_kind 缺失或本身没信息量（unclear/failed/unknown）
# 时才用 —— 明确的 kind 优先级更高，不能被消息里的字眼带偏。
_KEYWORDS: List[Tuple[Tuple[str, ...], str]] = [
    (('验证码', '人机', '滑块', 'captcha', 'cf-challenge'), 'captcha'),
    (('未登录', '请登录', '请先登录', '需要登录', '登录后', 'not logged',
      'login required', 'sign in'), 'not_logged_in'),
    # 过期/失效要在 cookie 之前判：否则"Cookie 已过期"会被 cookie 先吃掉，
    # 归成"Cookie 无效" —— 用户看到的建议就不对了（该重新登录，而不是改格式）。
    (('过期', '失效', 'expired'), 'cookie_expired'),
    (('cookie',), 'cookie_error'),
    (('频繁', '限流', 'too many', 'rate limit', '稍后再试'), 'rate_limited'),
    (('无权限', '没有权限', '权限不足', 'permission denied', 'forbidden',
      '无权'), 'forbidden'),
    (('维护', 'maintenance', '502', '503', '504', '服务不可用'), 'http_5xx'),
    (('超时', 'timeout', 'timed out'), 'timeout'),
    (('证书', 'certificate', 'ssl handshake'), 'tls_error'),
    (('拒绝连接', 'connection refused'), 'refused'),
    (('解析', 'resolve', 'getaddrinfo'), 'dns_error'),
]

# 这些 kind 本身没说明"为什么"，值得回头去消息里找线索
_WEAK_KINDS = ('', 'unclear', 'failed', 'unknown', 'exception')

# 这些 kind 的处置建议统一由 netutil.describe_network_error 提供 ——
# 通知里已经有一份给用户看的文案，这里只做归类，不重复写一遍
# （两处各写一份，改了一处另一处就开始骗人）。
NET_KINDS = ('timeout', 'refused', 'reset', 'unreachable', 'dns_error',
             'proxy_dns_error', 'proxy_refused', 'tls_error')

PROXY_KINDS = ('proxy_dns_error', 'proxy_refused', 'no_proxy')


def _result(code: str, hint: str = '') -> Dict[str, str]:
    label, cat, default_hint = _CODES.get(code, _CODES['unknown'])
    cat_name = CATEGORIES.get(cat, CATEGORIES['other'])[0]
    return {
        'code': code,
        'label': label,
        'category': cat,
        'category_label': cat_name,
        'hint': hint or default_hint,
    }


def _by_http_status(kind: str) -> str:
    """http_404 / http_502 这类按状态码归大类。"""
    try:
        st = int(kind[5:])
    except (TypeError, ValueError):
        return 'http_4xx'
    if 300 <= st < 400:
        return 'http_3xx'
    if st == 401:
        return 'not_logged_in'
    if st == 403:
        return 'http_403'
    if st == 429:
        return 'http_429'
    if st == 451:
        return 'http_451'
    if st >= 500:
        return 'http_5xx'
    if st >= 400:
        return 'http_4xx'
    return 'bad_response'


def _from_msg(msg: str) -> str:
    low = (msg or '').lower()
    if not low:
        return ''
    for words, code in _KEYWORDS:
        for w in words:
            if w in low:
                return code
    return ''


def classify(error_kind: str = '', msg: str = '') -> Dict[str, str]:
    """把一条失败记录归成 {code,label,category,category_label,hint}。

    判定顺序刻意是"先看 kind，弱 kind 再看消息"：
      * kind 明确（http_403 / timeout / step_failed）→ 直接用，不信消息。
        站点返回正文里经常出现"登录"两个字（比如"登录后签到"），
        拿它盖掉 http_403 反而更误导人。
      * kind 是空的或本身没信息量（unclear / failed / exception）→ 到消息里
        找线索。很多站点失败时 error_kind 就只有 unclear，真正的线索是
        "请先登录"这种原文，不挖出来等于没归类。
    """
    kind = str(error_kind or '').strip().lower()

    if kind and kind not in _WEAK_KINDS:
        if kind in _CODES:
            return _result(kind, describe_network_error(kind)
                           if kind in NET_KINDS else '')
        if kind.startswith('http_'):
            code = _by_http_status(kind)
            r = _result(code)
            if code in ('http_4xx', 'http_5xx', 'http_3xx', 'http_429',
                        'http_403', 'http_451'):
                r['hint'] = '%s（原始状态码 %s）' % (
                    r['hint'], kind.replace('http_', '') or '?')
            return r
        # 认不出来的 kind：不丢信息，但统计上并到"原因不明"，
        # 原始 error_kind 在明细里本来就单独显示。
        return _result('unknown', '原始标识：%s' % kind)

    code = _from_msg(msg)
    if code:
        return _result(code)
    if kind in _CODES:
        return _result(kind)
    return _result('unknown')


def meta_of(codes) -> Dict[str, Dict[str, str]]:
    """给前端用的查表：只包含这批 code，避免把整张表塞进接口。"""
    out: Dict[str, Dict[str, str]] = {}
    for c in set(codes or ()):
        c = str(c or '')
        if c and c not in out:
            r = _result(c)
            out[c] = {
                'label': r['label'],
                'category': r['category'],
                'category_label': r['category_label'],
                'hint': r['hint'],
                # 颜色也由服务端给：颜色和分类是一套东西，分两处写迟早对不上
                'color': category_color(r['category']),
            }
    return out


def category_color(cat: str) -> str:
    return CATEGORIES.get(str(cat or ''), CATEGORIES['other'])[1]
