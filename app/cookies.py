"""Cookie 解析与序列化。

用户会从各种地方复制 Cookie，格式五花八门。这里统一识别并归一化，
避免用户还要先自己整理格式。

支持的输入格式（自动识别，不用用户选）：
    1. cURL 命令（Chrome/Edge 的 Copy as cURL，含 -H 'cookie: ...'）
    2. 裸 Cookie 串        a=1; b=2
    3. 键值列表（DevTools Application 面板那种两列，粘贴后一行一个）：
            session    abc123
            pjwt       eyJ...
    4. Netscape cookies.txt（curl 的 --cookie-jar 格式）
    5. 只有值的 JSON 对象  {"session": "abc"}

输出统一为：
    * parse_cookie_header() -> Dict[str, str]     名字->值
    * to_cookie_header()    -> str                "a=1; b=2"
    * to_playwright_cookies(domain) -> List[dict] 供浏览器上下文注入
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

# Cookie 名字允许的字符（RFC 6265 的 token 字符集）
_NAME_RE = re.compile(r'^[A-Za-z0-9!#$%&\'*+\-.^_`|~]+$')

# cURL 里的 -H / --header 参数（兼容 cmd 的 ^" 转义与单双引号）
_CURL_HEADER_RE = re.compile(
    r"""-H\s+(?:\^?"(?P<d>[^"]*)\^?"|'(?P<s>[^']*)')""")

# Discuz / 常见站点的 Cookie 名可能带前缀（如 v2x4_48dd_auth），这里不做限制


class CookieParseError(ValueError):
    """解析不出任何 Cookie 时抛出，便于界面给出明确提示。"""


def _clean_name(name: str) -> str:
    return (name or '').strip().lstrip(';,').strip()


def _looks_like_name(name: str) -> bool:
    if not name or not _NAME_RE.match(name):
        return False
    # 排除 HTTP 请求头名/请求方法：它们完全符合 RFC6265 的 token 语法，
    # 因此单看语法会"合法"地通过，结果把 Host=… / Accept-Language=…
    # 当成 Cookie 存下去 —— 界面显示"已配置登录"，实际签到必然失败，
    # 而报错还是"未登录"，非常难查。这里挡掉这类名字。
    return name.lower() not in _NOT_A_COOKIE_NAME


# 绝不该被当成 Cookie 名的东西（HTTP 方法 + 常见请求头名）。
# 真实站点的 Cookie 名不会叫这些。
_NOT_A_COOKIE_NAME = frozenset({
    'get', 'post', 'put', 'head', 'options', 'delete', 'patch', 'trace',
    'connect', 'host', 'accept', 'accept-encoding', 'accept-language',
    'accept-charset', 'authority', 'origin', 'referer', 'referrer',
    'user-agent', 'content-type', 'content-length', 'content-encoding',
    'connection', 'cache-control', 'pragma', 'upgrade', 'te', 'trailer',
    'transfer-encoding', 'dnt', 'if-none-match', 'if-modified-since',
    'http/1.1', 'http/2', 'sec-fetch-dest', 'sec-fetch-mode',
    'sec-fetch-site', 'sec-fetch-user', 'sec-ch-ua', 'sec-ch-ua-mobile',
    'sec-ch-ua-platform', 'x-requested-with', 'range', 'via', 'forwarded',
    'x-forwarded-for', 'x-forwarded-proto', 'x-real-ip', 'cookie', 'path',
    'domain', 'expires', 'max-age', 'samesite', 'secure', 'httponly',
})


# HTTP 请求行：`GET /path HTTP/1.1`
_HTTP_REQLINE_RE = re.compile(
    r'^\s*(?:GET|POST|PUT|HEAD|OPTIONS|DELETE|PATCH|TRACE|CONNECT)\s+\S+'
    r'\s+HTTP/\d(?:\.\d)?\s*$', re.I | re.M)


def looks_like_header_block(text: str) -> bool:
    """看起来像"整段 HTTP 头"（哪怕没带请求行）吗？

    用户从 DevTools 里复制时，经常只复制 Headers 面板的内容 ——
    没有 `GET /x HTTP/1.1` 那一行。这种输入里
    `Host: a.com`、`Accept-Language: zh-CN` 在语法上都是合法的
    name=value，会被裸串解析器当成 Cookie 存下来（界面显示"已配置登录"，
    实际没登录，签到必然失败且报错是"未登录"）。

    判定要求**所有**非空行都是 `名字: 值` 形状，且至少两行 ——
    那样就不可能是一条正常 Cookie 串（`a=1; b=2` 不含冒号）。
    cURL 命令明显不是这个形状，所以不会被误判。
    """
    lines = [l.strip() for l in (text or '').splitlines() if l.strip()]
    if not lines:
        return False
    # 第一行以 `Cookie:` / `cookies:` 开头 → 那就是一个头行，必须按头解析。
    # 单行的 `Cookie:  session=abc`（冒号后多个空格）如果走裸串解析器，
    # 会被当成名为 Cookie 的 cookie，真正的 session 反而丢掉。
    first_name = (lines[0].split(':', 1)[0].strip()
                  if ':' in lines[0] else '')
    if first_name.lower() in ('cookie', 'cookies'):
        return True
    if len(lines) < 2:
        return False
    named = 0
    for line in lines:
        if ':' not in line:
            return False
        name = line.split(':', 1)[0].strip()
        # 头名必须是 token 形状；排除 `https://…` 这种带协议的
        if not _NAME_RE.match(name):
            return False
        named += 1
    return named == len(lines)


def looks_like_http_message(text: str) -> bool:
    """看起来像粘贴了整段 HTTP 报文（带请求行）吗？"""
    return bool(_HTTP_REQLINE_RE.search(text or ''))


def has_cookie_header(text: str) -> bool:
    """有没有 `Cookie:` / `cookies:` 开头的头行。"""
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        if line.split(':', 1)[0].strip().lower() in ('cookie', 'cookies'):
            return True
    return False


def parse_http_message(text: str) -> Dict[str, str]:
    """从整段 HTTP 报文里**只**取 Cookie 头；没有就返回空 dict。

    关键是"只取 Cookie 行"，而不是把每一行都当 Cookie ——
    否则 Host / Accept-Language 会被当成 Cookie 存下来，
    界面显示"已配置登录"但实际没登录（实测踩过）。
    """
    out: Dict[str, str] = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        # 头行形如 `Cookie: a=1; b=2`；请求行/响应的 `HTTP/1.1 200` 也会
        # 被 split 出冒号，所以下面用名字白名单限定为 cookie 头
        name, value = line.split(':', 1)
        if name.strip().lower() in ('cookie', 'cookies'):
            out.update(_parse_pairs(value))
    return out


def parse_curl(text: str) -> Dict[str, str]:
    """从 cURL 命令里取出 cookie 头。

    Chrome/Edge 的 Copy as cURL 会把 Cookie 放在 -H 'cookie: ...' 或
    --header 'Cookie: ...' 里；Windows CMD 版还会把双引号转义成 ^"。
    """
    out: Dict[str, str] = {}
    # 先规范化：去掉 cmd 的 ^ 转义
    norm = text.replace('^"', '"').replace('^\'', "'")
    for m in _CURL_HEADER_RE.finditer(norm):
        raw = m.group('d') if m.group('d') is not None else m.group('s')
        raw = (raw or '').strip()
        if not raw or ':' not in raw:
            continue
        k, v = raw.split(':', 1)
        if k.strip().lower() in ('cookie', 'cookies'):
            out.update(_parse_pairs(v))
    if not out:
        # 有的 Copy as cURL 把 cookie 写成 --cookie "a=1; b=2"
        m = re.search(r'--cookie\s+(?:"([^"]*)"|\'([^\']*)\')', norm)
        if m:
            out.update(_parse_pairs(m.group(1) or m.group(2) or ''))
    return out


def _parse_pairs(text: str) -> Dict[str, str]:
    """解析 "a=1; b=2" 形式的串；自动跳过属性（path/domain/expires 等）。"""
    out: Dict[str, str] = {}
    if not text:
        return out
    # 分号分隔；但值里可能含分号（少见），这里按标准做法简单切分
    for part in text.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        k, v = part.split('=', 1)
        k = _clean_name(k)
        v = v.strip()
        if not _looks_like_name(k):
            continue
        # 跳过 Cookie 属性（它们不是 cookie 本身）
        if k.lower() in ('path', 'domain', 'expires', 'max-age', 'samesite',
                         'secure', 'httponly', 'version', 'comment'):
            continue
        out[k] = v
    return out


def parse_kv_lines(text: str) -> Dict[str, str]:
    """解析"一行一个键值"的列表（DevTools Application 面板粘出来常见）。

    形式可能是：
        name    value
        name=value
        name: value
    """
    out: Dict[str, str] = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        k = v = None
        if '=' in line and not re.match(r'^\S+\s{2,}', line):
            k, v = line.split('=', 1)
        elif '\t' in line:
            k, v = line.split('\t', 1)
        else:
            m = re.match(r'^(\S+)\s+(.*)$', line)
            if m:
                k, v = m.group(1), m.group(2)
        if k is None:
            continue
        k = _clean_name(k).rstrip(':')
        v = (v or '').strip()
        if _looks_like_name(k):
            out[k] = v
    return out


def parse_netscape(text: str) -> Dict[str, str]:
    """解析 Netscape cookies.txt（每行 7 列，制表符分隔）。"""
    out: Dict[str, str] = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        cols = line.split('\t')
        if len(cols) >= 7:
            name, value = cols[5].strip(), cols[6].strip()
            if _looks_like_name(name):
                out[name] = value
    return out


def parse_json_obj(text: str) -> Dict[str, str]:
    """解析 {"name": "value"} 形式的 JSON。"""
    out: Dict[str, str] = {}
    t = (text or '').strip()
    if not (t.startswith('{') and t.endswith('}')):
        return out
    try:
        data = json.loads(t)
    except Exception:                                           # noqa: BLE001
        return out
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        k = _clean_name(str(k))
        if _looks_like_name(k):
            out[k] = str(v)
    return out


def parse_cookie_input(text: str) -> Tuple[Dict[str, str], str]:
    """自动识别格式并解析。

    返回 (cookies, 识别到的格式名)。
    解析不出任何 Cookie 时抛 CookieParseError。
    """
    raw = (text or '').strip()
    if not raw:
        raise CookieParseError('内容是空的，请粘贴 Cookie 或 Copy as cURL 的结果')

    # 0) 头部区内容（DevTools 里"整段复制"很常见）
    #    必须**先**处理：否则 `Host: a.com` / `Accept-Language: zh-CN`
    #    这些行在语法上也是合法的 name=value，会被当成 Cookie 存下来 ——
    #    界面显示"已配置登录"，实际存的是请求头，签到必然失败、
    #    报错还是"未登录"，极难排查（实测复现过）。
    #    另外单独一行 `Cookie:  session=abc`（冒号后多个空格）也会被
    #    裸串解析器误判成名为 Cookie 的键，导致真正的 session 丢失。
    if looks_like_http_message(raw) or looks_like_header_block(raw):
        got = parse_http_message(raw)
        if got:
            return got, 'HTTP 报文里的 Cookie 头'
        raise CookieParseError(
            '这段内容看起来是 HTTP 请求/响应头，但里面**没有 Cookie 行**。'
            '请确认：① 复制的是已登录状态下的请求；② 请求头里确实有 '
            'Cookie: 开头的那一行。（不能把 Host、Accept-Language 这些'
            '当成 Cookie，那样签到只会一直失败。）')

    # 1) cURL
    #    注意：如果确认是 cURL 却取不到 cookie 头，必须直接报错。
    #    否则会继续往下走，把 "accept: text/html" 这类**别的请求头**当成 Cookie
    #    解析出来 —— 那是静默给出错误数据，比直接报错危险得多。
    is_curl = bool(re.search(r'\bcurl\b', raw, re.I)) or '-H ' in raw \
        or '--header' in raw or '--url' in raw
    if is_curl:
        got = parse_curl(raw)
        if got:
            return got, 'cURL 命令'
        raise CookieParseError(
            '这段 cURL 里没有 Cookie 头。请在开发者工具的 Network 面板里，'
            '选中那条请求的 Headers，把以 cookie: 开头的一行复制出来。'
            '（如果那条请求本来就没带 Cookie，说明当时是未登录状态。）')

    # 2) Netscape cookies.txt（有制表符且列数够）
    if '\t' in raw:
        got = parse_netscape(raw)
        if got:
            return got, 'cookies.txt（Netscape 格式）'

    # 3) JSON 对象
    got = parse_json_obj(raw)
    if got:
        return got, 'JSON 对象'

    # 4) 裸 Cookie 串（含 "=" 且以 "; " 或 ";" 分隔，且不是多行键值表）
    if '=' in raw:
        # 多行的走键值列表解析（更准），单行的走 Cookie 串
        if '\n' in raw.strip():
            kv = parse_kv_lines(raw)
            pairs = _parse_pairs(raw.replace('\n', '; '))
            # 取解析出条目更多的那一种
            got = kv if len(kv) >= len(pairs) else pairs
        else:
            got = _parse_pairs(raw)
        if got:
            return got, 'Cookie 串'

    # 5) 兜底：键值列表
    got = parse_kv_lines(raw)
    if got:
        return got, '键值列表'

    raise CookieParseError(
        '没能从这段内容里解析出 Cookie。请确认复制的是完整的一行，'
        '至少要包含 name=value（例如 session=abc123）。')


def to_cookie_header(cookies: Dict[str, str]) -> str:
    """把 cookie 字典合成请求用的 Cookie 头字符串。

    这里会拒绝含 CR/LF/NUL 的名称或值：那些字符能切断 HTTP 头，
    构成头注入（请求走私的原料）。Python 的 http.client 目前也会拒绝
    这类头值，但那是 stdlib 的兜底 —— 不该当作我们自己的防线，
    而且显式拒绝能给出清晰原因，而不是一个莫名的发送失败。
    """
    parts = []
    for k, v in (cookies or {}).items():
        name, value = str(k), str(v)
        for label, s in (('名称', name), ('值', value)):
            if any(c in s for c in ('\r', '\n', '\x00')):
                raise CookieParseError(
                    'Cookie %s里含有换行或空字符，无法安全发送'
                    '（通常是复制时带入了多余内容）' % label)
        parts.append('%s=%s' % (name, value))
    return '; '.join(parts)


def to_playwright_cookies(cookies: Dict[str, str], domain: str,
                          path: str = '/',
                          secure: bool = True,
                          http_only_names: Optional[List[str]] = None
                          ) -> List[dict]:
    """转成 Playwright 的 add_cookies 需要的结构。

    domain 传 'www.example.com' 或 '.example.com' 都行；会自动带上点，
    这样对子域也生效（大多数站点的登录 Cookie 都在主域上）。
    """
    d = (domain or '').strip()
    if d and not d.startswith('.'):
        d = '.' + d.lstrip('.')
    http_only = set(http_only_names or [])
    out: List[dict] = []
    for k, v in (cookies or {}).items():
        item = {
            'name': k, 'value': v,
            'domain': d or '.localhost',
            'path': path,
            'secure': bool(secure),
            'sameSite': 'Lax',
        }
        if k in http_only:
            item['httpOnly'] = True
        out.append(item)
    return out


def domain_of(url: str) -> str:
    """从 URL 取出域名（用于构造 cookie domain）。"""
    from urllib.parse import urlsplit
    try:
        host = urlsplit(url or '').hostname or ''
    except Exception:                                           # noqa: BLE001
        host = ''
    return host
