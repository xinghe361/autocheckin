"""内置签到站点模板（数据驱动，引擎通用）。

每个模板用声明式描述"怎么签到"，避免把站点逻辑硬编码进引擎：
    http      纯请求签到（GET/POST + 从页面提取隐藏字段 + 成功关键词）
    browser   需要浏览器（Cloudflare 挑战 / 复杂交互）

所有站点都需要"登录态（Cookie）"。在网页「站点」页给每个站点粘贴一次即可。

--------------------------------------------------------------------
实测记录（极空间 NAS，走本地代理；结论都来自真实请求，不是推测）
--------------------------------------------------------------------
v2ex      走代理可达；签到两步：取 once 令牌 → POST redeem。
          必须带登录 Cookie，否则只能拿到未登录页面。

nodeseek  ⚠️ 曾经以为"必须用浏览器"，实测推翻了这个判断：
          · GET /board（HTML 页）确实被 Cloudflare 拦（403 挑战页）
          · 但签到真正走的是 API：POST /api/attendance?random=true
            —— 这个端点【不在 Cloudflare 那道防线后面】
          · 只带 Cookie 会 403；补上 Origin + Referer + Accept-Language 才 200
          实测响应：{"success":true,"message":"运气爆棚，恭喜你签到获得了7个鸡腿",...}
          所以 NodeSeek 走纯 HTTP 即可，不需要浏览器。

chiphell  ⚠️ 同样修正过判断：
          · 曾记录"证书链不完整，需放宽校验"——实测现在正常，无需放宽
          · QD 社区有人报 HTTP 567（腾讯云 EdgeOne 的 JA3 指纹拦截），
            但那与出口 IP 有关；我们这条链路服务端头是 lighttpd，未触发
          · 签到方式：带 Cookie 访问 forum.php 即为签到，页面出现「用户组」即成功
          · 顺带用正则抽出当前积分，便于在日志里看变化
"""

from __future__ import annotations

from typing import Any, Dict, List

from .models import KIND_TEMPLATE, SiteConfig, Step

# 各站点建议的请求头。很多站点的接口会校验来源，缺了就 403。
# 用户也可以在界面上改；Cookie 由运行时注入，不写在这里。
UA_DEFAULT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0')

# --------------------------------------------------------------------- V2EX

V2EX = {
    'id': 'v2ex',
    'name': 'V2EX',
    'homepage': 'https://www.v2ex.com/',
    'mission_url': 'https://www.v2ex.com/mission/daily',
    'need_browser': False,
    'verify_ssl': True,
    # V2EX 的每日领币是三步：取页面里的 once 令牌 → 带令牌请求 redeem → 再取一次页面确认结果。
    'flow': [
        {'action': 'get', 'url': '{mission_url}'},
        # 令牌是必填的：取不到就说明页面没正常返回（未登录 / 被 Cloudflare 拦截），
        # 继续往下走只会带着字面量 {once} 去请求，结果无法判断、还会误导排查方向。
        {'action': 'extract_regex', 'pattern': r'/mission/daily/redeem\?once=(\d+)',
         'save_as': 'once', 'required': True},
        # 必须是 GET：实测 POST 会返回 405 Method Not Allowed（这个端点只收 GET）。
        # 这里踩过一次，POST 一直 405，签到永远不会成功。
        {'action': 'get', 'url': 'https://www.v2ex.com/mission/daily/redeem?once={once}'},
        # 关键：redeem 返回 302，body 很短，判定靠的是"最后一个响应"。
        # 若不重新取一次页面，就会去 302 的空 body 里找成功关键词，永远判失败。
        # 重取之后：已领到 -> 页面出现「每日登录奖励已领取」；
        # 还能领 -> 页面仍显示「领取 X 铜币」。
        {'action': 'get', 'url': '{mission_url}'},
    ],
    'success_keywords': ['每日登录奖励已领取', '已领取', '领取成功', '已经领取'],
    'fail_keywords': ['需要先登录', '请先登录', '登录以继续'],
    'already_keywords': ['已经领取', '已领取过'],
    'headers': {
        'User-Agent': UA_DEFAULT,
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/avif,image/webp,*/*;q=0.8'),
        'Accept-Language': 'zh-CN,zh;q=0.9',
    },
    'note': '需要在「站点」页粘贴一次登录 Cookie（V2EX 的登录态在 Cookie 里）。',
}

# ----------------------------------------------------------------- NodeSeek

NODESEEK = {
    'id': 'nodeseek',
    'name': 'NodeSeek',
    'homepage': 'https://www.nodeseek.com/board',
    # 签到走 API，不是网页。random=true 就是"试试手气"（随机鸡腿数）；
    # 想改成固定 5 鸡腿，把 url 里的 random=true 换成 random=false。
    'mission_url': 'https://www.nodeseek.com/api/attendance?random=true',
    'need_browser': False,          # 实测：API 端点不在 Cloudflare 防线后面
    'verify_ssl': True,
    'flow': [
        {'action': 'post',
         'url': 'https://www.nodeseek.com/api/attendance?random=true'},
        # 顺带把获得数量与余额抽出来，便于在通知里看到"这次拿了多少、总共多少"。
        # 不是必需项（extract 失败不影响判定），所以不加 required。
        {'action': 'extract_regex', 'pattern': r'"gain":\s*(\d+)', 'save_as': 'gain'},
        {'action': 'extract_regex', 'pattern': r'"current":\s*(\d+)',
         'save_as': 'current'},
    ],
    # 实测响应长这样：
    #   {"success":true,"message":"运气爆棚，恭喜你签到获得了7个鸡腿","gain":7,"current":408}
    # 失效时（实测）：
    #   {"message":"USER NOT FOUND","status":404,"success":false}
    #   {"message":"請先登入","status":404,"success":false}
    'success_keywords': ['鸡腿', '已完成签到', '恭喜你签到'],
    # 注意：判定是纯文本包含匹配，所以不能写死空格（JSON 里 "status":404 与
    # "status": 404 两种都常见）。踩过的坑：只写 '"status":404' 会漏掉带空格的
    # 响应，导致"Cookie 失效"被误判。这里把两种都列上，另外靠 '"success":false'
    # 兜底，避免只依赖某一个字段。
    'fail_keywords': ['未登录', '请先登录', '請先登入', '登录失效', '登录已过期',
                      '"success":false', '"success": false',
                      '"status":404', '"status": 404',
                      'USER NOT FOUND', 'NOT LOGGED IN'],
    'already_keywords': ['已完成签到', '今天已经签到', '已签到'],
    # ⚠️ Origin 与 Referer 是必需的：缺了会 403（实测确认，不是 Cloudflare 拦的）
    'headers': {
        'User-Agent': UA_DEFAULT,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Content-Type': 'application/json',
        'Origin': 'https://www.nodeseek.com',
        'Referer': 'https://www.nodeseek.com/board',
    },
    'note': '走 API 纯请求签到。必须配 Cookie；Origin/Referer 已按实测预置。',
}

# ----------------------------------------------------------------- Chiphell

CHIPHELL = {
    'id': 'chiphell',
    'name': 'Chiphell',
    'homepage': 'https://www.chiphell.com/',
    'mission_url': 'https://www.chiphell.com/forum.php',
    'need_browser': False,          # 实测：纯请求可达，无需浏览器
    'verify_ssl': True,             # 实测：证书正常，无需放宽校验
    'flow': [
        {'action': 'get', 'url': 'https://www.chiphell.com/forum.php'},
        # 顺带把当前积分抽出来，方便在日志/通知里看到变化
        {'action': 'extract_regex', 'pattern': r'积分:\s*(\d+)',
         'save_as': 'jifen', 'from': 'last'},
    ],
    # 实测：带登录 Cookie 访问 forum.php 即算签到，
    # 页面里会出现「用户组: 大天使」这类信息（未登录则只有"登录"入口）。
    'success_keywords': ['用户组'],
    'fail_keywords': ['请先登录', '需要登录', '立即登录', '注册会员'],
    'already_keywords': [],
    'headers': {
        'User-Agent': UA_DEFAULT,
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/webp,*/*;q=0.8'),
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': 'https://www.chiphell.com/portal.php',
        'Upgrade-Insecure-Requests': '1',
    },
    'note': '带 Cookie 访问 forum.php 即为签到（Discuz 的访问计分）。',
}


BUILTIN: List[Dict[str, Any]] = [V2EX, NODESEEK, CHIPHELL]

# 内置模板**旧版本用过**的显示名 -> 现在用的名字。
#
# 用户要求："把每个站点签到任务的名称直接以站点名称当名字，
# 不要加每日铜币什么的"。模板名改了只影响**新建**的站点；
# 已经存在的站点名字是存在 config.json 里的，所以要迁移一次。
#
# 只在"站点用的正是该内置模板、且名字一字不差等于旧默认名"时才改 ——
# 这样用户自己改过的名字绝不会被覆盖。
TEMPLATE_NAME_MIGRATION = {
    'v2ex': ('V2EX 每日铜币', 'V2EX'),
    'nodeseek': ('NodeSeek 试试手气', 'NodeSeek'),
    'chiphell': ('Chiphell 每日邪恶值', 'Chiphell'),
}


def migrate_site_names(cfg) -> int:
    """把内置站点的旧默认名换成新名字。返回改了几个。

    只做精确匹配（template 对得上 + 名字等于旧默认名），不做模糊猜测 ——
    用户自定义的名字必须原样保留。
    """
    changed = 0
    for site in (getattr(cfg, 'sites', None) or []):
        tid = getattr(site, 'template', '') or ''
        pair = TEMPLATE_NAME_MIGRATION.get(tid)
        if not pair:
            continue
        old, new = pair
        if getattr(site, 'name', '') == old:
            site.name = new
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# 插件模板：把"支持哪些站点"从镜像里解耦出来
#
# 模板本来就是纯数据，所以外部只要放一个 JSON 文件（数据目录 templates/*.json）
# 就能多一个可选模板，**不用重建镜像**。实现见 app/plugins.py。
#
# 这里用"提供者回调"的方式挂钩子，而不是让 templates 直接依赖 plugins：
#   * 避免循环依赖（plugins 校验时要用到动作白名单）
#   * 单测里可以注入假提供者
#   * 没挂提供者时行为与以前完全一致（只有那三个内置模板）
# 服务启动时会调用 set_plugin_provider() 挂上真实的插件存储。
# ---------------------------------------------------------------------------
_plugin_provider = None


def set_plugin_provider(fn) -> None:
    """挂上插件提供者：fn() -> List[模板 dict]。传 None 表示取消。"""
    global _plugin_provider
    _plugin_provider = fn


def plugin_templates() -> List[Dict[str, Any]]:
    """当前可用的插件模板（没有提供者或出错时返回空表，绝不让它抛出去）。"""
    if _plugin_provider is None:
        return []
    try:
        got = _plugin_provider() or []
    except Exception:                                           # noqa: BLE001
        # 插件加载失败不能影响内置功能
        return []
    return [t for t in got if isinstance(t, dict) and t.get('id')]


def template_by_id(tid: str) -> Dict[str, Any]:
    """按 id 找模板。

    顺序：**插件优先，内置兜底** —— 这样用户可以用插件覆盖某个内置模板
    （比如站点改版导致判定关键词失效时，不必等我改代码发版）。
    """
    if tid:
        for t in plugin_templates():
            if t.get('id') == tid:
                return t
    for t in BUILTIN:
        if t['id'] == tid:
            return t
    return {}


def available_templates() -> List[Dict[str, Any]]:
    """所有可选模板（内置 + 插件），id 去重、插件优先。

    供界面"可选站点"列表使用：用户勾选哪个才把它加进站点。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for t in BUILTIN:
        out[t['id']] = dict(t, source='builtin')
    for t in plugin_templates():
        out[t['id']] = dict(t, source='plugin')
    return sorted(out.values(), key=lambda t: t['id'])


def builtin_sites() -> List[SiteConfig]:
    """把内置模板转成 SiteConfig 列表（默认全部启用）。"""
    out: List[SiteConfig] = []
    for t in BUILTIN:
        out.append(SiteConfig(
            id=t['id'],
            name=t['name'],
            enabled=True,
            kind=KIND_TEMPLATE,
            template=t['id'],
            homepage=t['homepage'],
            need_browser=bool(t['need_browser']),
            verify_ssl=bool(t.get('verify_ssl', True)),
            success_keywords=list(t.get('success_keywords') or []),
            fail_keywords=list(t.get('fail_keywords') or []),
            # 把模板里预置的请求头带进站点配置，用户可在界面上改
            headers=dict(t.get('headers') or {}),
        ))
    return out


def generic_custom_site(site_id: str, name: str, homepage: str,
                        steps: List[Step] | None = None) -> SiteConfig:
    """新建站点（用户录制/手填）的初始配置。"""
    return SiteConfig(
        id=site_id,
        name=name,
        enabled=True,
        kind='custom',
        template='',
        homepage=homepage,
        # 不硬编码 need_browser：交给 engine.needs_browser() 依据步骤里的动作
        # （click/fill/goto 等浏览器动作）自动判断。硬编码成 True 会让
        # "纯 get/post 步骤"的自定义站点也被迫走浏览器，
        # 把 checkin_via_http 里那套支持自定义步骤的分支架空。
        verify_ssl=True,
        steps=list(steps or []),
        success_keywords=[],
        fail_keywords=[],
        headers={'User-Agent': UA_DEFAULT, 'Accept-Language': 'zh-CN,zh;q=0.9'},
    )
