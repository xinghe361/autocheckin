"""从 GitHub（或任意 https 地址）同步站点模板插件。

用户需求原文：
    「没有做"从 GitHub 自动同步插件" 做一个机制 手动就是立即
      自动可以设定几点拉一次默认间隔为天 自动加上开关」

两种触发：
    * 手动：界面上「立即同步」→ 立刻拉一次
    * 自动：后台调度每 tick 问一次，内部自限（到设定的时刻、且间隔天数已到）

支持的来源写法：
    xinghe361/autocheckin                    # 仓库根
    xinghe361/autocheckin/site-plugins       # 仓库下的文件夹（推荐）
    https://github.com/xinghe361/autocheckin/tree/main/site-plugins
    https://raw.githubusercontent.com/.../x.json    # 单个文件的直链

安全要点（这些校验是必须的，不是形式）：
    * **只允许 https**：同步内容会成为发往站点的请求模板，明文传输可被中途改。
    * 拉下来的每个模板都过一遍 plugins.validate_template —— 那条校验本来就
      限定动作白名单、拒绝请求头 CRLF 注入、拒绝非法 id，所以远端即使被
      篡改，最坏也只是"一个不合规的模板被拒绝"，而不是执行任意内容。
    * 文件数与单文件大小都设上限，避免一个恶意/错误的仓库把数据目录塞满。
    * **只删除"上次同步写进来的、这次远端已没有"的文件**；用户自己手放的
      模板绝不碰（用 .sync_state.json 记录来源）。

用标准库实现，不引入新依赖。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .plugins import (PLUGIN_ID_RE, RESERVED_IDS, PluginStore,
                      validate_template)

# 一次同步最多取多少个文件（防止把数据目录塞满）
MAX_FILES = 50
# 单个文件大小上限（与本地插件一致）
MAX_BYTES = 256 * 1024
# 网络超时
TIMEOUT = 20
# 同步记录文件名（放在插件目录里，标注哪些是同步来的）
STATE_NAME = '.sync_state.json'

# GitHub 相关主机
GH_HOSTS = ('github.com', 'www.github.com')
RAW_HOSTS = ('raw.githubusercontent.com',)

UA = 'autocheckin-plugin-sync'

# 首次安装时「模板来源」默认填的项目地址。
# 只是给界面预填 + 手动点「立即同步」时的兜底；自动同步仍需用户自己打开开关。
DEFAULT_TEMPLATE_SOURCE = 'xinghe361/autocheckin/site-plugins'


class SyncError(Exception):
    """同步失败（网络/来源格式/远端返回异常）。"""


# ---------------------------------------------------------------------------
# 来源解析
# ---------------------------------------------------------------------------
# GitHub 的 owner / repo 名：字母数字开头，可有 . - _
# 为什么要卡：这两个值会被拼进 api.github.com 的 URL 路径。
# 不卡的话 `../../x` 这种也能匹配上（实测），虽然只是让 API 404、
# 不构成 SSRF，但"路径里能塞 .."是不该留的形状。
OWNER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$')


def _check_owner_repo(owner: str, repo: str) -> None:
    for label, v in (('owner', owner), ('repo', repo)):
        if not v or v in ('.', '..') or not OWNER_RE.match(v):
            raise SyncError('%s 不合法：%r（应为字母数字开头，'
                            '只含字母数字与 . - _）' % (label, v))


def _check_path(path: str) -> str:
    """目录路径也检查一遍：`. ` 与 `..` 段会让 URL 被规范化到别的路径。

    实测：`a/b/../../c` 里的 owner/repo 是合法的，但路径拼出来是
    `.../contents/../../c`，被 URL 规范化成 `repos/c` —— 请求跑到了
    完全不同的地方。所以路径段也要卡。
    """
    segs = [x for x in str(path or '').split('/') if x]
    for x in segs:
        if x in ('.', '..'):
            raise SyncError('目录路径里不能有 . 或 .. 段：%r' % path)
    return '/'.join(segs)


def parse_source(src: str) -> Tuple[str, str, str]:
    """把用户填的来源解析成 (kind, owner_repo, path)。

      kind='api'      -> 用 GitHub API 列目录（kind, 'owner/repo', 'path'）
      kind='raw'      -> 直接取这个 URL 的内容（raw, url, ''）
      kind='manifest' -> 直接取这个 URL，期望是模板/模板数组

    允许的写法见模块开头。非法写法抛 SyncError。
    """
    s = (src or '').strip()
    if not s:
        raise SyncError('没有填来源地址')

    # 简写：owner/repo[/path]
    if not s.lower().startswith(('http://', 'https://')):
        if not re.match(r'^[\w.\-]+/[\w.\-]+', s):
            raise SyncError('来源格式不对，应形如 owner/repo 或 '
                            'https://github.com/owner/repo/tree/main/目录')
        parts = s.strip('/').split('/')
        owner, repo = parts[0], parts[1]
        _check_owner_repo(owner, repo)
        path = '/'.join(parts[2:])
        if path.startswith('tree/'):          # 容忍 owner/repo/tree/main/x
            seg = path.split('/')
            path = '/'.join(seg[2:]) if len(seg) > 2 else ''
        return 'api', '%s/%s' % (owner, repo), _check_path(path)

    u = urllib.parse.urlparse(s)
    if u.scheme != 'https':
        # 只允许 https：模板会变成发往站点的请求，明文可被中途篡改
        raise SyncError('只支持 https 地址（http 明文传输可被中途篡改）')
    if u.username or u.password:
        # ⚠️ 拒绝 URL 里带账密。两个原因：
        #   1) **根本不起作用**：这里用 urllib 直接取，它不会把 URL 里的
        #      user:pass 变成 Basic 认证头，所以令牌等于填了个寂寞。
        #   2) **会泄露**：这个地址会存进 config.json，进而进入 WebDAV 备份；
        #      带 token 的 URL 一旦备份外泄就等于交出仓库访问权。
        # 实测发现：以前是静默忽略账密，用户以为私有仓库能用，其实不能。
        raise SyncError('不要在地址里写账号密码或令牌（既不起作用，也会'
                        '随配置和备份泄露）。当前不支持私有仓库；'
                        '公开仓库直接写 owner/repo 即可')
    host = (u.hostname or '').lower()

    if host in GH_HOSTS:
        seg = [x for x in u.path.split('/') if x]
        if len(seg) < 2:
            raise SyncError('GitHub 地址至少要包含 owner/repo')
        owner, repo = seg[0], seg[1]
        _check_owner_repo(owner, repo)
        path = ''
        if len(seg) > 3 and seg[2] in ('tree', 'blob'):
            # /owner/repo/tree/<branch>/<path...>
            path = '/'.join(seg[4:])
        elif len(seg) > 2:
            path = '/'.join(seg[2:])
        return 'api', '%s/%s' % (owner, repo), _check_path(path)

    if host in RAW_HOSTS:
        return 'raw', s, ''

    # 其它 https 地址：当作"直接给一个模板或模板数组的文件"
    return 'manifest', s, ''


# ---------------------------------------------------------------------------
# 取内容
# ---------------------------------------------------------------------------
def _fetch(url: str, proxy: str = '', limit: int = MAX_BYTES
           ) -> bytes:
    """下载一个 URL，带大小上限与超时。只在需要时才用代理。"""
    handlers: List[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {'http': proxy, 'https': proxy}))
    else:
        # 显式禁用环境变量代理，避免容器里 NO_PROXY 之类的意外影响
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'application/vnd.github+json, application/json, text/plain',
    })
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            if r.geturl().lower().startswith('http://'):
                # 跟随重定向后掉回明文 -> 拒绝
                raise SyncError('重定向到了非 https 地址，已拒绝')
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise SyncError('HTTP %s：%s' % (e.code, url)) from e
    except urllib.error.URLError as e:
        raise SyncError('网络错误：%s' % e.reason) from e
    except OSError as e:
        raise SyncError('网络错误：%s' % e) from e
    if len(data) > limit:
        raise SyncError('远端文件过大（> %d KB）' % (limit // 1024))
    return data


def _list_repo_files(owner_repo: str, path: str, proxy: str,
                     with_sha: bool = False) -> List[Tuple]:
    """用 GitHub API 列目录，返回 [(文件名, 下载地址[, sha]), ...]（只取 .json）。

    with_sha=True 时带上 git blob sha —— 「检查更新」拿它和本地记录比对，
    不用把每个文件都下载一遍。
    """
    api = 'https://api.github.com/repos/%s/contents/%s' % (
        owner_repo, urllib.parse.quote(path.strip('/')))
    raw = _fetch(api, proxy, limit=1024 * 1024)
    try:
        items = json.loads(raw.decode('utf-8'))
    except ValueError as e:
        raise SyncError('GitHub 返回的不是 JSON（仓库或目录不存在？）：%s'
                        % e) from e
    if isinstance(items, dict):
        # 单个文件（用户直接填了文件路径）
        items = [items]
    if not isinstance(items, list):
        raise SyncError('GitHub 返回结构异常')
    out: List[Tuple] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get('type') != 'file':
            continue
        name = str(it.get('name') or '')
        if not name.lower().endswith('.json'):
            continue
        if name == STATE_NAME:
            continue
        url = str(it.get('download_url') or '')
        if not url:
            continue
        out.append((name, url, str(it.get('sha') or '')) if with_sha
                   else (name, url))
    if not out:
        raise SyncError('这个目录里没有 .json 模板文件')
    if len(out) > MAX_FILES:
        raise SyncError('文件太多（%d 个，上限 %d）' % (len(out), MAX_FILES))
    return sorted(out)


def git_blob_sha(data: bytes) -> str:
    """算 git blob 的 sha1（和 GitHub API 返回的 sha 同一种算法）。

    用途：不需要下载文件就能判断"远端这份和我本地这份是不是同一内容"。
    """
    h = hashlib.sha1()
    h.update(b'blob %d\0' % len(data))
    h.update(data)
    return h.hexdigest()



# ---------------------------------------------------------------------------
# 同步
# ---------------------------------------------------------------------------
class PluginSync:
    """把远端模板同步到本地插件目录。"""

    def __init__(self, store: PluginStore, proxy_fn=None):
        self.store = store
        # proxy_fn 用函数而不是字符串：代理是运行时可能改的设置
        self.proxy_fn = proxy_fn or (lambda: '')

    # -- 记录哪些文件是同步来的 ---------------------------------------
    def _state_path(self) -> str:
        return os.path.join(self.store.dir, STATE_NAME)

    def _read_state(self) -> Dict[str, Any]:
        try:
            with open(self._state_path(), 'r', encoding='utf-8') as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember_remote(self, shas: Dict[str, str]) -> None:
        """把远端当前各文件的 sha 记下来（不动 files/shas/at）。

        用途：模板列表要能直接显示"已是最新 / 有新版本"，而「检查更新」是
        用户主动点的、不该顺手改同步状态。单独存一份只读快照最安全 ——
        写进 shas 会污染 prune 的"上次同步写过哪些文件"的依据。
        """
        if not shas:
            return
        prev = self._read_state()
        merged = prev.get('remote_shas') if isinstance(prev.get('remote_shas'), dict) else {}
        merged = dict(merged)
        merged.update({k: v for k, v in shas.items() if v})
        prev['remote_shas'] = merged
        prev['checked_at'] = int(time.time())
        try:
            self.store.ensure_dir()
            tmp = self._state_path() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(prev, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path())
        except OSError:
            pass

    def state_snapshot(self):
        """一次读盘拿到 (synced_shas, remote_shas)。

        两个列表都在同一个 .sync_state.json 里，分开取会把文件解析两遍 ——
        而模板列表在每次页面加载（/api/meta、/api/templates）时都会走到。
        """
        d = self._read_state()
        synced = d.get('shas') if isinstance(d.get('shas'), dict) else {}
        remote = d.get('remote_shas') if isinstance(d.get('remote_shas'), dict) else {}
        return synced, remote

    def _write_state(self, files: List[str], source: str,
                     shas: Optional[Dict[str, str]] = None) -> None:
        self.store.ensure_dir()
        tmp = self._state_path() + '.tmp'
        payload = {'files': sorted(files), 'source': source,
                   'at': int(time.time())}
        if shas:
            # 远端每个文件的 git blob sha：下次「检查更新」不用再下载一遍
            payload['shas'] = {k: v for k, v in shas.items() if v}
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path())

    # -- 主流程 --------------------------------------------------------
    def sync(self, source: str, prune: bool = True,
             only: Optional[List[str]] = None) -> Dict[str, Any]:
        """拉取并写入模板。返回概要；失败抛 SyncError。

        only：只更新这几个模板（id 或文件名）。「更新模板」用它做单条更新，
        不去碰其它文件 —— 用户可能手改过别的模板。
        """
        kind, a, b = parse_source(source)
        proxy = self.proxy_fn() or ''

        shas: Dict[str, str] = {}
        items: List[Tuple[str, str]] = []
        if kind == 'api':
            listed = _list_repo_files(a, b, proxy, with_sha=True)
            items = [(n, u) for (n, u, _sha) in listed]
            shas = {n: sha for (n, _u, sha) in listed}
        elif kind == 'raw':
            items = [(os.path.basename(urllib.parse.urlparse(a).path)
                      or 'plugin.json', a)]
        else:
            items = [(os.path.basename(urllib.parse.urlparse(a).path)
                      or 'plugin.json', a)]

        if only:
            want = set()
            for x in only:
                x = str(x or '').strip()
                if x:
                    want.add(x if x.endswith('.json') else x + '.json')
            items = [(n, u) for (n, u) in items if n in want]

        self.store.ensure_dir()
        written: List[str] = []
        updated: List[str] = []
        unchanged: List[str] = []     # 远端有、本地已一致（要留在 state 里，别当被删）
        errors: List[Dict[str, str]] = []
        fetched = 0          # 成功下载并解析的文件数
        ok_files = 0         # 其中产出有效模板的文件数

        for name, url in items:
            try:
                raw = _fetch(url, proxy)
            except SyncError as e:
                errors.append({'file': name, 'reason': str(e)})
                continue
            try:
                data = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as e:
                errors.append({'file': name, 'reason': 'JSON 解析失败：%s' % e})
                continue
            fetched += 1
            arr = data if isinstance(data, list) else [data]
            for one in arr:
                tpl, why = validate_template(one, name)
                if why:
                    errors.append({'file': name, 'reason': why})
                    continue
                if tpl['id'] in RESERVED_IDS:
                    # 示例文档不落盘：它只用于抄格式，写进 templates/
                    # 既没用又会让目录看起来多一个"模板"
                    continue
                fn = '%s.json' % tpl['id']
                path = os.path.join(self.store.dir, fn)
                existed = os.path.exists(path)
                body = json.dumps(tpl, ensure_ascii=False, indent=2)
                # 内容一模一样就不落盘、也不计为"更新"。
                # 我们写出去的是**重新序列化**的 JSON，字节和远端不同，
                # 所以不能靠比对远端 sha 判断"要不要写"；只能比本地内容。
                # 不做这一步的后果就是：点一百次「更新」，一百次都"更新了"，
                # 用户完全看不出到底变没变（实测反馈）。
                same = False
                if existed:
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            same = (f.read() == body)
                    except OSError:
                        same = False
                if not same:
                    tmp = path + '.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        f.write(body)
                    os.replace(tmp, path)
                    (updated if existed else written).append(fn)
                else:
                    unchanged.append(fn)
                ok_files += 1

        # 区分两种失败，别混为一谈：
        #   * **一个文件都没下载到** -> 来源写错 / 仓库私有 / 网络不通，
        #     这是整体失败，必须抛出去。否则界面和自动同步的日志都显示
        #     "一切正常"，用户根本不知道模板没同步过来（实测踩过这个歧义）。
        #   * **下载到了但内容不合规** -> 只是那个文件的问题，记进 errors
        #     让用户看到是哪个文件、为什么，不整体失败（否则一个坏文件会把
        #     其它好文件一起带下来）。
        if items and fetched == 0 and errors:
            raise SyncError('一个文件都没能下载：%s'
                            % errors[0].get('reason', '未知原因'))

        # 清理：只删"上次同步写过、这次远端没有"的文件；用户手放的不动。
        #
        # ⚠️ only 模式必须跳过清理：这次只下载了 1~2 个文件，"远端没有"
        # 其实只是"这次没请求"，照删会把其它模板全删光（实测踩到过）。
        removed: List[str] = []
        if prune and not only:
            prev = self._read_state().get('files') or []
            # 这次远端仍然给了的文件一律保留（含"内容没变所以没写"的那些），
            # 只有"上次写过、这次远端确实不再提供"的才删。
            keep = set(written) | set(updated) | set(unchanged)
            for fn in prev:
                if fn in keep:
                    continue
                base = fn[:-5] if fn.endswith('.json') else fn
                if not PLUGIN_ID_RE.match(base):
                    continue          # 记录异常时不删，宁可不清理
                p = os.path.join(self.store.dir, fn)
                try:
                    os.remove(p)
                    removed.append(fn)
                except OSError:
                    pass

        # 只更新部分文件时，要把旧记录合并进来 —— 否则 state 里就只剩这一两个
        # 文件，之后 prune 会以为别的都不归它管（文件删不掉了）。
        # ⚠️ 必须把"内容没变、跳过写入"的文件也算进来：否则这次一个文件都没写，
        # files_now 就是空，紧接着的清理会把它们全当成"远端已删"删掉
        #（实测踩到：连续同步两次，第二次模板目录被清空）。
        files_now = sorted(set(written) | set(updated) | set(unchanged))
        if only:
            prev = self._read_state()
            files_now = sorted(set(prev.get('files') or []) | set(files_now))
            old_shas = prev.get('shas') if isinstance(prev.get('shas'), dict) else {}
            merged = dict(old_shas)
            merged.update(shas)
            shas = merged
        self._write_state(files_now, source, shas)
        self._remember_remote(shas)
        got = self.store.load(force=True)
        return {
            'ok': True,
            'source': source,
            'added': written,
            'updated': updated,
            'removed': removed,
            'errors': errors,
            'total': len(got),
            'at': int(time.time()),
        }

    # -- 检查更新 ------------------------------------------------------
    def check_updates(self, source: str) -> Dict[str, Any]:
        """远端有没有比本地更新的模板？只看 sha，不下载文件。

        返回 {'updates': [{id,file}], 'same': n, 'unknown': n, 'at': ts}
          * updates：远端 sha 与本地记录不一致（或本地没有记录，多为老版本
            同步过来的）→ 建议更新
          * same   ：一致
          * unknown：远端有、本地根本没有这个文件（没装，不是"更新"）
        """
        kind, a, b = parse_source(source)
        proxy = self.proxy_fn() or ''
        state = self._read_state()
        known = state.get('shas') if isinstance(state.get('shas'), dict) else {}

        if kind != 'api':
            # 单文件直链：没有 sha 可比，直接下载一次比内容
            url = a
            raw = _fetch(url, proxy)
            try:
                remote_obj = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as e:
                raise SyncError('远端内容不是 JSON：%s' % e) from e
            name = os.path.basename(urllib.parse.urlparse(url).path) or 'plugin.json'
            path = os.path.join(self.store.dir, name)
            if not os.path.isfile(path):
                return {'updates': [], 'same': 0, 'unknown': 1,
                        'missing': [name], 'at': int(time.time())}
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    local_obj = json.load(f)
            except (OSError, ValueError):
                local_obj = None
            same = local_obj == remote_obj
            return {'updates': [] if same else [{'id': name[:-5], 'file': name}],
                    'same': 1 if same else 0, 'unknown': 0, 'missing': [],
                    'at': int(time.time())}

        listed = _list_repo_files(a, b, proxy, with_sha=True)
        updates: List[Dict[str, str]] = []
        missing: List[str] = []
        same = 0
        unknown = 0
        for name, _url, sha in listed:
            tid = name[:-5] if name.endswith('.json') else name
            if tid in RESERVED_IDS:
                continue
            path = os.path.join(self.store.dir, name)
            if not os.path.isfile(path):
                missing.append(name)
                unknown += 1
                continue
            if sha and known.get(name) == sha:
                same += 1
                continue
            # 没有记录（老版本同步的）或 sha 变了 → 都算"可更新"
            updates.append({'id': tid, 'file': name})
        self._remember_remote({n: sha for (n, _u, sha) in listed if sha})
        return {'updates': updates, 'same': same, 'unknown': unknown,
                'missing': missing, 'source': source,
                'at': int(time.time())}


# ---------------------------------------------------------------------------
# 到点判断
# ---------------------------------------------------------------------------
def parse_hhmm(s: str, default: Tuple[int, int] = (3, 0)
               ) -> Tuple[int, int]:
    """解析 'HH:MM'；不合法就用默认值（宁可回到默认也不抛）。"""
    m = re.match(r'^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$', str(s or ''))
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return default
    return h, mi


def sync_due(now: int, last_at: int, enabled: bool, source: str,
             hhmm: str, interval_days: int,
             at_local_time) -> Tuple[bool, int]:
    """判断现在是否该自动同步。

    返回 (是否到期, 下一个到期时间戳)。

    规则（按用户描述：设定几点、默认间隔为天）：
      * 关闭 / 没填来源 -> 永不到期
      * 从没同步过 -> **立刻**到期（刚打开开关就先拉一次，符合直觉）
      * 否则：要同时满足"间隔天数已到"和"今天的那个时刻已过"
    """
    if not enabled or not source.strip():
        return False, 0
    if last_at <= 0:
        return True, 0
    days = max(1, int(interval_days or 1))
    h, mi = parse_hhmm(hhmm)
    # 间隔没到 -> 算下次时间
    gap = days * 86400
    if now - last_at < gap:
        return False, last_at + gap
    # 间隔到了，但还要等当天的那个时刻
    today = at_local_time(now, h, mi)
    if now >= today:
        return True, 0
    return False, today
