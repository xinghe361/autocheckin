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

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .plugins import (PLUGIN_ID_RE, PluginStore, validate_template)

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


def _list_repo_files(owner_repo: str, path: str, proxy: str
                     ) -> List[Tuple[str, str]]:
    """用 GitHub API 列目录，返回 [(文件名, 下载地址), ...]（只取 .json）。"""
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
    out: List[Tuple[str, str]] = []
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
        out.append((name, url))
    if not out:
        raise SyncError('这个目录里没有 .json 模板文件')
    if len(out) > MAX_FILES:
        raise SyncError('文件太多（%d 个，上限 %d）' % (len(out), MAX_FILES))
    return sorted(out)


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

    def _write_state(self, files: List[str], source: str) -> None:
        self.store.ensure_dir()
        tmp = self._state_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'files': sorted(files), 'source': source,
                       'at': int(time.time())}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path())

    # -- 主流程 --------------------------------------------------------
    def sync(self, source: str, prune: bool = True) -> Dict[str, Any]:
        """拉取并写入模板。返回概要；失败抛 SyncError。"""
        kind, a, b = parse_source(source)
        proxy = self.proxy_fn() or ''

        items: List[Tuple[str, str]] = []
        if kind == 'api':
            items = _list_repo_files(a, b, proxy)
        elif kind == 'raw':
            items = [(os.path.basename(urllib.parse.urlparse(a).path)
                      or 'plugin.json', a)]
        else:
            items = [(os.path.basename(urllib.parse.urlparse(a).path)
                      or 'plugin.json', a)]

        self.store.ensure_dir()
        written: List[str] = []
        updated: List[str] = []
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
                fn = '%s.json' % tpl['id']
                path = os.path.join(self.store.dir, fn)
                existed = os.path.exists(path)
                tmp = path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(tpl, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
                ok_files += 1
                (updated if existed else written).append(fn)

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

        # 清理：只删"上次同步写过、这次远端没有"的文件；用户手放的不动
        removed: List[str] = []
        if prune:
            prev = self._read_state().get('files') or []
            keep = set(written) | set(updated)
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

        self._write_state(sorted(set(written) | set(updated)), source)
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
