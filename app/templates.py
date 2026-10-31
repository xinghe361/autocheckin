"""站点模板（数据驱动，引擎通用）。

每个模板用声明式描述"怎么签到"，避免把站点逻辑硬编码进引擎：
    http      纯请求签到（GET/POST + 从页面提取隐藏字段 + 成功关键词）
    browser   需要浏览器（Cloudflare 挑战 / 复杂交互）

模板**全部来自外部 JSON**（数据目录 templates/*.json，或 site-plugins/ 同步），
本模块不再内置任何站点。所有站点都需要"登录态（Cookie）"，
在网页「站点」页给每个站点粘贴一次即可。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .models import KIND_TEMPLATE, SiteConfig, Step

# 各站点建议的请求头。很多站点的接口会校验来源，缺了就 403。
# 用户也可以在界面上改；Cookie 由运行时注入，不写在这里。
UA_DEFAULT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0')

# ---------------------------------------------------------------------------
# 插件模板：把"支持哪些站点"从镜像里解耦出来
#
# 模板本来就是纯数据，所以外部只要放一个 JSON 文件（数据目录 templates/*.json）
# 就能多一个可选模板，**不用重建镜像**。实现见 app/plugins.py。
#
# 这里用"提供者回调"的方式挂钩子，而不是让 templates 直接依赖 plugins：
#   * 避免循环依赖（plugins 校验时要用到动作白名单）
#   * 单测里可以注入假提供者
#   * 没挂提供者时没有任何模板（站点模板全部来自外部 JSON）
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
    """按 id 找模板（只查插件，不再有内置兜底）。"""
    if tid:
        for t in plugin_templates():
            if t.get('id') == tid:
                return t
    return {}


def available_templates() -> List[Dict[str, Any]]:
    """所有可选模板（现在只有插件），按 id 排序。

    供界面"可选站点"列表使用：用户勾选哪个才把它加进站点。
    """
    return sorted(plugin_templates(), key=lambda t: t.get('id', ''))


def option_specs(tpl: Dict[str, Any]) -> List[Dict[str, Any]]:
    """模板声明的"可选项"，供界面渲染下拉、供引擎校验取值。

    只有形状正确的才认：要 key、要非空 choices、每个 choice 要有 value。
    插件模板是外部 JSON，写歪了不能把界面和签到流程搞崩 ——
    认不出来的那一项直接当不存在，其余项照常工作。
    """
    raw = (tpl or {}).get('options')
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    seen_keys = set()
    for spec in raw:
        if not isinstance(spec, dict):
            continue
        key = str(spec.get('key') or '').strip()
        if not key or key in seen_keys:
            continue
        choices_raw = spec.get('choices')
        if not isinstance(choices_raw, list):
            continue
        choices: List[Dict[str, str]] = []
        for c in choices_raw:
            if not isinstance(c, dict):
                continue
            val = c.get('value')
            if val is None:
                continue
            val = str(val)
            if not val or any(x['value'] == val for x in choices):
                continue
            choices.append({'value': val,
                            'label': str(c.get('label') or val)})
        if not choices:
            continue
        default = str(spec.get('default') or choices[0]['value'])
        if default not in [c['value'] for c in choices]:
            # 默认值写错时退回第一项，而不是让界面出现"没有选中"的下拉
            default = choices[0]['value']
        seen_keys.add(key)
        out.append({
            'key': key,
            'label': str(spec.get('label') or key),
            'default': default,
            'choices': choices,
            'note': str(spec.get('note') or ''),
        })
    return out


def resolve_options(tpl: Dict[str, Any], values: Any) -> Dict[str, str]:
    """把站点上存的值对齐到模板声明：认不出的键/取值一律丢掉，缺的补默认值。

    为什么必须校验而不是照单全收：这些值最终会**拼进请求 URL**
    （NodeSeek 就是 `?random={attendance}`）。放任任意字符串进去，
    等于把一份用户可写的配置当成 URL 片段使用；只认白名单里的取值，
    拼进去的就永远是 true/false 这种已知常量。
    """
    specs = option_specs(tpl)
    src = values if isinstance(values, dict) else {}
    out: Dict[str, str] = {}
    for spec in specs:
        allowed = [c['value'] for c in spec['choices']]
        got = str(src.get(spec['key']) or '').strip()
        out[spec['key']] = got if got in allowed else spec['default']
    return out


def site_option_specs(site: Any) -> List[Dict[str, Any]]:
    """某个站点该显示哪些可选项（只有内置/插件模板站点才有）。"""
    if getattr(site, 'kind', '') != KIND_TEMPLATE:
        return []
    return option_specs(template_by_id(getattr(site, 'template', '')))


def site_options(site: Any) -> Dict[str, str]:
    """某个站点当前生效的可选项取值（没设过就是模板默认值）。"""
    if getattr(site, 'kind', '') != KIND_TEMPLATE:
        return {}
    return resolve_options(template_by_id(getattr(site, 'template', '')),
                           getattr(site, 'options', None))


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
