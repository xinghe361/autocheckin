"""站点模板插件：把"支持哪些站点"从镜像里解耦出来。

背景（用户需求原文）：
    「站点和容器能不能解耦，这样以后我有要更新的软件就不用每次都更新容器了，
      直接在 github 的某个文件夹新增一个文件就行（容器能自动或手动监测到
      文件更新自动加入选择项，选中的才在内置站点展示出来）」

做法：模板本来就是**纯数据**（id / flow / success_keywords / headers…），
所以把内置的那三个留在代码里，另外从**数据目录下的 templates/** 读外部 JSON。
放一个 .json 文件进去（或同步进去）就多一个可选模板，**不用重建镜像**。

安全要点（这些校验是必须的，不是形式）：
    * 插件内容会变成**发往站点的请求**与**判定关键词**，等于可执行的配置。
      所以：id 必须匹配 ^[a-z0-9][a-z0-9_\\-]{0,63}$（它会被用作站点 id、
      也进 DOM），action 必须是已知动作白名单，字段类型必须对。
    * 单个文件有大小上限，避免有人塞个几百 MB 的文件进数据目录。
    * 解析失败只跳过该文件并记录原因，**不能让一个坏文件把整个服务弄挂**
      （与"畸形配置不能让容器起不来"同一个原则）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 插件目录名（在数据目录下）
PLUGIN_DIRNAME = 'templates'

# 单个插件文件大小上限：模板是纯文本配置，256KB 足够宽裕
MAX_PLUGIN_BYTES = 256 * 1024

# 一个插件最多多少步/多少关键词，避免异常文件拖垮界面
MAX_FLOW_STEPS = 60
MAX_KEYWORDS = 60

# id 允许的字符：小写字母/数字开头，后面可有 _ 和 -
# 必须限制：id 会被用作站点 id、进 HTML 属性、也用于按 id 查模板
PLUGIN_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_\-]{0,63}$')

# 纯请求流程实际支持的动作（与 engine.HTTP_FLOW_ACTIONS 保持一致）
HTTP_ACTIONS = frozenset({'get', 'post', 'extract_regex'})
# 浏览器动作（模板若用这些，必须声明 need_browser=True）
BROWSER_ACTIONS = frozenset({'goto', 'click', 'fill', 'wait', 'wait_for',
                             'extract', 'assert_text'})
ALLOWED_ACTIONS = HTTP_ACTIONS | BROWSER_ACTIONS

# 头部名/值的字符限制：防止把 CRLF 注入请求头
_HEADER_NAME_RE = re.compile(r'^[A-Za-z0-9!#$%&\'*+\-.^_`|~]+$')
_BAD_HEADER_CHARS = ('\r', '\n', '\x00')


def _err(msg: str) -> str:
    return msg


def validate_template(data: Any, source: str = '') -> Tuple[Dict[str, Any], str]:
    """校验并规范化一个模板。返回 (模板, 错误原因)；错误时模板为 {}。"""
    if not isinstance(data, dict):
        return {}, '顶层不是 JSON 对象'

    def bad(msg: str) -> Tuple[Dict[str, Any], str]:
        return {}, ('%s%s' % (('%s：' % source) if source else '', msg))

    tid = data.get('id')
    if not isinstance(tid, str) or not PLUGIN_ID_RE.match(tid):
        return bad('id 必须是小写字母/数字开头、只含小写字母数字下划线连字符，'
                   '长度 1-64（收到 %r）' % (tid,))
    name = data.get('name')
    if not isinstance(name, str) or not name.strip():
        return bad('缺少 name（站点显示名）')
    homepage = data.get('homepage')
    if not isinstance(homepage, str) or not homepage.startswith('http'):
        return bad('homepage 必须是 http/https 开头的地址')

    flow = data.get('flow')
    if not isinstance(flow, list) or not flow:
        return bad('缺少 flow（至少一步）')
    if len(flow) > MAX_FLOW_STEPS:
        return bad('flow 步骤过多（上限 %d）' % MAX_FLOW_STEPS)

    clean_flow: List[Dict[str, Any]] = []
    for i, step in enumerate(flow):
        if not isinstance(step, dict):
            return bad('第 %d 步不是对象' % (i + 1))
        action = step.get('action')
        if action not in ALLOWED_ACTIONS:
            return bad('第 %d 步的 action 不受支持：%r（可用：%s）'
                       % (i + 1, action, '、'.join(sorted(ALLOWED_ACTIONS))))
        one: Dict[str, Any] = {'action': action}
        # 逐字段按类型收敛 —— 只接受已知字段，避免把任意内容带进流程
        if 'url' in step:
            if not isinstance(step['url'], str):
                return bad('第 %d 步的 url 不是字符串' % (i + 1))
            one['url'] = step['url']
        if 'pattern' in step:
            if not isinstance(step['pattern'], str):
                return bad('第 %d 步的 pattern 不是字符串' % (i + 1))
            one['pattern'] = step['pattern']
        if 'save_as' in step:
            one['save_as'] = str(step['save_as'])
        if 'data' in step:
            if not isinstance(step['data'], (dict, str)):
                return bad('第 %d 步的 data 既不是对象也不是字符串' % (i + 1))
            one['data'] = step['data']
        if step.get('required'):
            one['required'] = True
        if action in ('click', 'fill', 'goto', 'wait_for'):
            one['target'] = str(step.get('target') or '')
        if action == 'fill':
            one['value'] = str(step.get('value') or '')
        if action in ('wait',):
            one['timeout_ms'] = int(step.get('timeout_ms') or 3000)
        clean_flow.append(one)

    need_browser = bool(data.get('need_browser', False))
    used = {s['action'] for s in clean_flow}
    browser_used = used & BROWSER_ACTIONS
    if browser_used and not need_browser:
        # 不自动改，直接报错：让作者明确声明，避免"以为走纯请求"却需要浏览器
        return bad('流程里用了浏览器动作 %s，必须写 "need_browser": true'
                   % '、'.join(sorted(browser_used)))

    def kw(key: str) -> List[str]:
        v = data.get(key)
        if not isinstance(v, list):
            return []
        out = []
        for x in v[:MAX_KEYWORDS]:
            if isinstance(x, str) and x.strip():
                out.append(x)
        return out

    headers: Dict[str, str] = {}
    raw_headers = data.get('headers')
    if isinstance(raw_headers, dict):
        for k, v in list(raw_headers.items())[:40]:
            ks, vs = str(k), str(v)
            if not _HEADER_NAME_RE.match(ks):
                return bad('请求头名不合法：%r' % ks)
            if any(c in vs for c in _BAD_HEADER_CHARS):
                return bad('请求头 %s 的值里有换行/空字符' % ks)
            headers[ks] = vs
    elif raw_headers is not None:
        return bad('headers 必须是对象')

    tpl: Dict[str, Any] = {
        'id': tid,
        'name': name.strip()[:80],
        'homepage': homepage,
        'need_browser': need_browser,
        'verify_ssl': bool(data.get('verify_ssl', True)),
        'flow': clean_flow,
        'success_keywords': kw('success_keywords'),
        'fail_keywords': kw('fail_keywords'),
        'already_keywords': kw('already_keywords'),
        'headers': headers,
        'note': str(data.get('note') or '')[:400],
        # 标记来源，界面上可以区分"内置"与"插件"
        'source': 'plugin',
    }
    if isinstance(data.get('mission_url'), str):
        tpl['mission_url'] = data['mission_url']
    return tpl, ''


class PluginStore:
    """从数据目录读插件模板；带缓存，按 mtime 判断是否需要重读。

    为什么要缓存 + mtime：这个会被 /api/meta 和每次签到调用，
    每次都扫目录 + 解析 JSON 太浪费；但用户丢进新文件后必须能很快看到，
    所以只看目录与文件的 mtime，变了才重读。
    """

    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, PLUGIN_DIRNAME)
        self._lock = threading.RLock()
        self._cache: List[Dict[str, Any]] = []
        self._errors: List[Dict[str, str]] = []
        self._stamp: Optional[Tuple] = None

    # -- 读 ------------------------------------------------------------
    def _dir_stamp(self) -> Tuple:
        """目录里所有 .json 的 (名字, 修改时间, 大小) 快照。"""
        try:
            names = sorted(n for n in os.listdir(self.dir)
                           if n.lower().endswith('.json'))
        except OSError:
            return ()
        items = []
        for n in names:
            p = os.path.join(self.dir, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            items.append((n, int(st.st_mtime), st.st_size))
        return tuple(items)

    def load(self, force: bool = False) -> List[Dict[str, Any]]:
        """返回所有**有效**的插件模板（按 id 排序）。"""
        with self._lock:
            stamp = self._dir_stamp()
            if not force and stamp == self._stamp:
                return list(self._cache)
            templates: List[Dict[str, Any]] = []
            errors: List[Dict[str, str]] = []
            seen = set()
            for name, _mtime, size in stamp:
                path = os.path.join(self.dir, name)
                if size > MAX_PLUGIN_BYTES:
                    errors.append({'file': name,
                                   'reason': '文件过大（> %d KB）'
                                             % (MAX_PLUGIN_BYTES // 1024)})
                    continue
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        raw = json.load(f)
                except (OSError, ValueError) as e:
                    errors.append({'file': name,
                                   'reason': '读取/解析失败：%s' % e})
                    continue
                items = raw if isinstance(raw, list) else [raw]
                for it in items:
                    tpl, why = validate_template(it, name)
                    if why:
                        errors.append({'file': name, 'reason': why})
                        continue
                    if tpl['id'] in seen:
                        errors.append({'file': name,
                                       'reason': 'id 重复：%s（已忽略这个）'
                                                 % tpl['id']})
                        continue
                    seen.add(tpl['id'])
                    tpl['file'] = name
                    templates.append(tpl)
            templates.sort(key=lambda t: t['id'])
            self._cache, self._errors, self._stamp = templates, errors, stamp
            return list(self._cache)

    def errors(self) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._errors)

    def by_id(self, tid: str) -> Dict[str, Any]:
        for t in self.load():
            if t['id'] == tid:
                return t
        return {}

    # -- 写（界面上的"从文件夹导入"用；也方便用户直接丢文件）-----------
    def dir_path(self) -> str:
        return self.dir

    def ensure_dir(self) -> str:
        os.makedirs(self.dir, exist_ok=True)
        return self.dir

    def save(self, template: Dict[str, Any]) -> str:
        """把（已校验的）模板写成 <id>.json，返回文件路径。"""
        self.ensure_dir()
        path = os.path.join(self.dir, '%s.json' % template['id'])
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        self.load(force=True)
        return path

    def delete(self, tid: str) -> bool:
        path = os.path.join(self.dir, '%s.json' % tid)
        try:
            os.remove(path)
        except OSError:
            return False
        self.load(force=True)
        return True
