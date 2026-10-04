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
    return bool(name) and bool(_NAME_RE.match(name))


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
    """把 cookie 字典合成请求用的 Cookie 头字符串。"""
    return '; '.join('%s=%s' % (k, v) for k, v in (cookies or {}).items())


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
