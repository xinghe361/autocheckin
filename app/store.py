"""配置与运行状态的持久化。

两个文件，职责分开（这点很重要）：
    config.json  站点配置 + 通知/WebDAV/AI 设置（含**加密后**的凭据）
    state.json   运行状态：上次成功时刻、连败次数、下次运行时间、AI 每日调用计数

为什么分开：
  * state.json 可以安全地放进每日备份并跨机器使用
  * config.json 含密文凭据，是否外发由用户决定

写入一律"先写临时文件再原子替换"，避免断电/崩溃留下半个 JSON 把配置弄坏。
"""

from __future__ import annotations

import json
import os
import threading
import stat
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import AppConfig, default_config

CONFIG_FILE = 'config.json'
STATE_FILE = 'state.json'


def atomic_write_json(path: str, data: Any, private: bool = False) -> None:
    """原子写 JSON：临时文件 + fsync + replace。

    private=True 时把权限收成 600。注意 os.open 的 mode 只在**创建**时生效，
    文件已存在时不会改变权限，所以这里额外显式 chmod 一次。
    （Windows 上 os.chmod 只影响只读位，因此仅在 POSIX 上做严格断言。）

    临时文件名带 pid 与线程 id：固定名的话，两个线程同时写会各自持有
    同一个文件的偏移量，可能写出**混合内容** —— 而混合内容会被判为损坏配置，
    进而回退默认值（那会触发"未初始化"分支，见 AuthManager.needs_setup）。
    """
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = '%s.tmp.%d.%d' % (path, os.getpid(), threading.get_ident())
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    mode = (stat.S_IRUSR | stat.S_IWUSR) if private else 0o644
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, payload.encode('utf-8'))
        os.fsync(fd)
    finally:
        os.close(fd)

    if private and os.name == 'posix':
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def read_json(path: str, default: Any = None,
              broken_flag: Optional[list] = None) -> Any:
    """读 JSON；文件不存在或损坏时返回 default（不让坏文件把程序卡死）。

    broken_flag：传入一个 list 时，如果文件存在但解析失败，会往里 append
    一次（用于区分"全新安装、没有文件"和"文件坏了、曾经初始化过"）。
    为什么要区分：前者应该引导用户设口令，后者**绝不能**开放首次设置 ——
    否则配置一坏，局域网第一个访问者就能设口令接管容器。
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        if broken_flag is not None:
            broken_flag.append(True)
        # 损坏的配置：备份一份现场，方便排查，然后回退默认值。
        #
        # ⚠️ 改名会把"曾经初始化过"的唯一直接证据挪走。所以同时写一个
        # **持久化哨兵**：只要哨兵在，重启后也仍然知道这台实例初始化过，
        # 不会把"配置坏了"误判成"全新安装"而开放 setup。
        # （之前只用内存标记，重启即丢 → 一次重启就能被匿名接管，实测复现过。）
        try:
            broken = path + '.broken.%d' % int(time.time())
            os.replace(path, broken)
            _write_unreadable_marker(path)
        except OSError:
            pass
        return default


def unreadable_marker_path(config_path: str) -> str:
    return config_path + '.unreadable'


def _write_unreadable_marker(config_path: str) -> None:
    """落一个持久哨兵，记录"配置曾经存在但读不出来"。"""
    try:
        with open(unreadable_marker_path(config_path), 'w',
                  encoding='utf-8') as f:
            f.write('config was unreadable at %d\n' % int(time.time()))
        if os.name == 'posix':
            try:
                os.chmod(unreadable_marker_path(config_path),
                         stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
    except OSError:
        pass


def was_ever_initialized(config_path: str) -> bool:
    """这台实例是否**曾经**初始化过（用于判断能不能开放首次设置）。

    三种证据任意一种成立即为"初始化过"：
      1. config.json 还在
      2. 有 config.json.broken.* 残留（说明曾有配置、只是坏了被改名）
      3. 有持久哨兵 config.json.unreadable

    为什么不能只看第 1 条：坏文件会被改名，重启后第 1 条就不成立了，
    于是被误判成全新安装 —— 局域网第一个访问者可以匿名设口令接管。
    """
    if os.path.exists(config_path):
        return True
    if os.path.exists(unreadable_marker_path(config_path)):
        return True
    directory = os.path.dirname(os.path.abspath(config_path)) or '.'
    base = os.path.basename(config_path) + '.broken.'
    try:
        for name in os.listdir(directory):
            if name.startswith(base):
                return True
    except OSError:
        pass
    return False


@dataclass
class RuntimeState:
    """所有站点的运行状态 + 全局计数。"""

    sites: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 每个站点每天调用 AI 的次数：{site_id: {'date': 'YYYY-MM-DD', 'count': n}}
    ai_calls: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_run_at: int = 0
    last_backup_at: int = 0
    # 杂项时间戳等（如 Cookie 上次巡检时间）。独立成 dict 是为了
    # 以后加新的全局时间戳时不必再改序列化代码。
    meta: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def site(self, site_id: str) -> Dict[str, Any]:
        return self.sites.setdefault(site_id, {})

    def get(self, key: str, default: Any = None) -> Any:
        return (self.meta or {}).get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self.meta is None:
            self.meta = {}
        self.meta[key] = value

    def next_run_at(self, site_id: str) -> int:
        try:
            return int(self.site(site_id).get('next_run_at') or 0)
        except (TypeError, ValueError):
            return 0

    def set_next_run_at(self, site_id: str, ts: int) -> None:
        self.site(site_id)['next_run_at'] = int(ts)

    def due_sites(self, now: int) -> list:
        """到点的站点 id 列表（next_run_at 已过或未设置）。

        必须复用 next_run_at() 的容错解析，不能自己裸 int() ——
        state.json 被写坏（例如 next_run_at 变成 "abc"）时，裸 int()
        会抛 ValueError，一路冒泡到调度循环的宽泛 except，
        结果是**每一轮都失败、所有站点永远不再被检查**（调度整体停摆）。
        """
        out = []
        for sid in list(self.sites.keys()):
            if self.next_run_at(sid) <= now:
                out.append(sid)
        return out

    # -- AI 每日计数 ---------------------------------------------------
    def ai_calls_today(self, site_id: str, today: str) -> int:
        rec = self.ai_calls.get(site_id) or {}
        if rec.get('date') != today:
            return 0
        try:
            return int(rec.get('count') or 0)
        except (TypeError, ValueError):
            return 0

    def bump_ai_calls(self, site_id: str, today: str) -> int:
        rec = self.ai_calls.get(site_id) or {}
        if rec.get('date') != today:
            rec = {'date': today, 'count': 0}
        rec['count'] = int(rec.get('count') or 0) + 1
        self.ai_calls[site_id] = rec
        return rec['count']

    def ai_calls_total_today(self, today: str) -> int:
        """今天所有站点合计调用了多少次 AI（用于全局上限）。"""
        total = 0
        for rec in (self.ai_calls or {}).values():
            if isinstance(rec, dict) and rec.get('date') == today:
                try:
                    total += int(rec.get('count') or 0)
                except (TypeError, ValueError):
                    pass
        return total

    # -- 站点级别的通用标记 ---------------------------------------------
    def get_site_flag(self, site_id: str, key: str, default: Any = None) -> Any:
        return self.site(site_id).get(key, default)

    def set_site_flag(self, site_id: str, key: str, value: Any) -> None:
        self.site(site_id)[key] = value

    def clear_site_flag(self, site_id: str, key: str) -> None:
        self.site(site_id).pop(key, None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'sites': self.sites,
            'ai_calls': self.ai_calls,
            'last_run_at': self.last_run_at,
            'last_backup_at': self.last_backup_at,
            'meta': self.meta or {},
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> 'RuntimeState':
        d = d or {}
        return RuntimeState(
            sites=dict(d.get('sites') or {}),
            ai_calls=dict(d.get('ai_calls') or {}),
            last_run_at=int(d.get('last_run_at') or 0),
            last_backup_at=int(d.get('last_backup_at') or 0),
            meta=dict(d.get('meta') or {}),
        )


class Store:
    """读写 config.json / state.json 的门面。

    ⚠️ 并发要点（这里踩过一次很贵的坑）：
    config/state 都是"读全量-改内存-写全量"，而服务是多线程的
    （HTTP 一请求一线程 + 调度线程 + 后台任务）。如果两个执行体各自
    读一份快照、改完再整包写回，后写的会**静默覆盖**先写的：
      * 长任务（浏览器签到可能几分钟）结束时写回旧快照
        → 期间用户保存的 Cookie、改的口令、吊销的令牌全部被回滚
      * 调度线程写 next_run_at 与执行体写回互相覆盖 → 同一天签到两次
      * AI 每日计数被覆盖 → 上限失效，多烧 token

    所以这里提供两件事：
      1. 一把可重入锁 —— 让"读改写"成为一个整体
      2. update_config/update_state —— **在锁内重新读盘再改再写**，
         而不是拿调用方手里的旧快照去覆盖
    调用方能不用 save_config/save_state 就别用。
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.config_path = os.path.join(data_dir, CONFIG_FILE)
        # 由 load_config 设置：文件存在但解析不出来（损坏/半截）
        self.config_unreadable = False
        self.state_path = os.path.join(data_dir, STATE_FILE)
        # 可重入：update_* 内部会调用 load_*/save_*，同一个线程要能重复进入
        self._lock = threading.RLock()

    # -- 锁内读改写 ------------------------------------------------------
    def update_config(self, mutator) -> AppConfig:
        """在锁内**重新读盘**、交给 mutator 改、再写回。

        mutator(cfg) 的返回值若为 False 表示"不改了"，此时不写盘。
        这样就不会用调用方手里的旧快照覆盖别人的修改。
        """
        with self._lock:
            cfg = self.load_config()
            if mutator(cfg) is False:
                return cfg
            self.save_config(cfg)
            return cfg

    def update_state(self, mutator) -> RuntimeState:
        """状态版 update_config（同样锁内重读-改-写）。"""
        with self._lock:
            st = self.load_state()
            if mutator(st) is False:
                return st
            self.save_state(st)
            return st

    # -- 配置 ----------------------------------------------------------
    def load_config(self) -> AppConfig:
        # 关键：read_json 在文件损坏时会把它改名成 .broken.<ts>（所以事后
        # "文件是否存在"已经不可靠 —— 文件已经不见了）。因此用 broken_flag
        # 直接拿"解析失败"这个事实，并且**标记是粘性的**：
        # 一旦发现损坏就一直是 True，不能因为下次读时文件已被改名消失、
        # 又倒回去当成"全新安装"（那样配置一坏就会开放首次设置，
        # 局域网第一个访问者即可设口令接管 —— 实测踩过这个坑）。
        with self._lock:
            broken: list = []
            raw = read_json(self.config_path, None, broken_flag=broken)
            if broken:
                self.config_unreadable = True
            if raw is None or not isinstance(raw, dict):
                if os.path.exists(self.config_path):
                    self.config_unreadable = True
                return default_config()
            return AppConfig.from_dict(raw)

    def save_config(self, cfg: AppConfig) -> None:
        # 含密文凭据 → 权限收紧
        with self._lock:
            atomic_write_json(self.config_path,
                              cfg.to_dict(include_secrets=True), private=True)

    def export_config(self, cfg: AppConfig) -> Dict[str, Any]:
        """给 WebDAV 备份用的导出。

        含各站点的密文凭据（换机恢复时用得上），但**主动剥掉两类东西**：

        1) 明文凭据：
           * 通知渠道的 5 个字段是明文存的（pushplus token / Server酱 key /
             企微 webhook / TG bot token / chat id）—— 拿到备份就能冒充你
             发消息、接管你的 Telegram bot。
           * 全局代理 URL 里常带 user:pass。
           * 站点级独立代理地址、站点自定义请求头（可能放 Authorization）。

        2) **认证状态**（auth_password_enc / session_hashes / api_token_hashes）：
           备份若把它当"用户数据"整包恢复，那么任何能写备份文件的人
           （明文 http 上的中间人、被攻陷的网盘账号、拿到的他人备份）
           只要在里面塞一条**自己令牌的哈希**或一个自选口令，
           用户点一次「从云端恢复」攻击者就成了管理员。
           认证信息本来也不该跟着备份走 —— 恢复后用户重新设一次口令即可。

        注意：这是**唯一**的备份入口，所以在这里剥最稳妥 ——
        靠调用方记得传 include_secrets=False 是不可靠的
        （而且 include_secrets=False 本来也不管 notify 这几个字段，
          之前就是这么漏出去的）。
        """
        data = cfg.to_dict(include_secrets=True)
        data['proxy'] = ''
        # 认证状态不进备份，恢复时保留本机现有的认证信息
        for k in ('auth_password_enc', 'session_hashes', 'api_token_hashes'):
            data.pop(k, None)
        notify = data.get('notify')
        if isinstance(notify, dict):
            for k in ('pushplus_token', 'serverchan_key', 'wecom_webhook',
                      'tg_bot_token', 'tg_chat_id'):
                notify.pop(k, None)
            # 渠道级代理地址同样可能带账密
            notify['proxy_url'] = ''
            for v in (notify.get('channel_proxy') or {}).values():
                if isinstance(v, dict):
                    v['url'] = ''
        # 站点级：独立代理地址常带 user:pass；自定义头可能放 Authorization/Cookie
        for site in (data.get('sites') or []):
            if not isinstance(site, dict):
                continue
            site['proxy_url'] = ''
            if site.get('headers'):
                site['headers'] = {}
            # 站点账号名常是邮箱/用户名，恢复它没必要
            site.pop('username', None)
            # state 只留调度白名单键（别的键可能是错误详情/响应片段）
            st = site.get('state')
            if isinstance(st, dict):
                site['state'] = {k: v for k, v in st.items()
                                 if k in _STATE_KEEP_KEYS}
        # 控制面字段不进备份：
        #   remote_cdp_url 常带 CDP 认证账密；browser_path 是宿主机绝对路径，
        #   恢复它等于让 Playwright 按备份里的路径启动可执行文件（=代码执行）。
        data['remote_cdp_url'] = ''
        data['browser_path'] = ''
        # webdav 凭据由本机保留（恢复时也覆盖回去），不必进备份
        wd = data.get('webdav')
        if isinstance(wd, dict):
            wd['username'] = ''
        # AI 的 base_url 决定了把 Key 发到哪里。恢复时本机优先（见 import_config），
        # 备份里也不留它，避免"换台机器恢复就把 Key 发到别人服务器"。
        ai = data.get('ai')
        if isinstance(ai, dict):
            ai['base_url'] = ''
        return data

    def import_config(self, data: Dict[str, Any],
                      keep_auth_from: Optional[AppConfig] = None) -> AppConfig:
        """从备份导入配置。

        keep_auth_from 非空时，**用它的认证字段覆盖备份里的值** ——
        备份不含认证状态（见 export_config），但旧备份或被人篡改的备份
        可能带着 session_hashes / 自选口令，那等于让"能写备份的人"变成管理员。
        所以恢复时一律以本机现有认证为准。
        """
        cfg = AppConfig.from_dict(data or {})
        if keep_auth_from is not None:
            cfg.auth_required = keep_auth_from.auth_required
            cfg.auth_password_enc = keep_auth_from.auth_password_enc
            cfg.session_hashes = list(keep_auth_from.session_hashes or [])
            cfg.api_token_hashes = list(keep_auth_from.api_token_hashes or [])
            # 控制面字段也一律以本机为准（不只认证）。
            #
            # 为什么必须在这里钉住，而不是只靠调用方记得覆盖：这些字段决定
            # "数据往哪去"，一旦由备份决定就等于把容器交给备份的作者：
            #   ai.base_url    → 把**真实的 DeepSeek Key** 发到攻击者服务器
            #                    （实测：Authorization: Bearer sk-REAL... 落到假服务器）
            #   proxy          → 全部流量可被劫持
            #   remote_cdp_url → 连到外部调试端口（常带账密）
            #   browser_path   → 按备份指定路径启动程序 = 代码执行面
            cfg.ai.base_url = keep_auth_from.ai.base_url
            cfg.ai.api_key_enc = keep_auth_from.ai.api_key_enc
            cfg.proxy = keep_auth_from.proxy
            cfg.proxy_mode = keep_auth_from.proxy_mode
            cfg.remote_cdp_url = keep_auth_from.remote_cdp_url
            cfg.browser_path = keep_auth_from.browser_path
            cfg.sites_initialized = bool(cfg.sites) or \
                keep_auth_from.sites_initialized
        return cfg

    def is_pristine(self, cfg: AppConfig) -> bool:
        """配置是否还是"刚装好、没动过"的默认状态？

        用于决定"站点为空时要不要补内置站点"。两种"空"要分开：
          * 从没配置过（默认的 3 个内置站点原封不动）→ 补回来是合理的，
            用户删了内置站点后重启也能恢复（老行为，别破坏）
          * 用户显式管理过站点（新增/删除过）→ **绝不能**自动补，
            否则删掉的站点会自己回来继续签到、烧 AI 额度
        """
        if cfg.sites_initialized:
            return False
        from .templates import builtin_sites
        cur = sorted((s.id, s.name) for s in cfg.sites)
        prv = sorted((s.id, s.name) for s in builtin_sites())
        return cur == prv

    def merge_site_patch(self, sites: List[Any],
                         fields: Optional[List[str]] = None) -> None:
        """把 AI 补丁改过的**允许字段**增量写回盘上。

        为什么单独一个方法：AI 分析跑在长任务里，手里是几分钟前的快照。
        整包写回会覆盖用户期间的修改；完全不写回又会让补丁丢失
        （重启后还按旧配置签到）。所以只回写补丁允许改的那些字段。

        字段来源默认取 deepseek 的白名单，保证"能自动改什么"只有一处定义。
        """
        if fields is None:
            from .deepseek import ALLOWED_PATCH_FIELDS
            fields = sorted(ALLOWED_PATCH_FIELDS)

        with self._lock:
            cfg = self.load_config()
            by_id = {str(s.id): s for s in cfg.sites}
            touched = False
            for src in sites:
                dst = by_id.get(str(getattr(src, 'id', '')))
                if dst is None:
                    continue
                for name in fields:
                    val = getattr(src, name, None)
                    if val is None:
                        continue
                    if getattr(dst, name, None) != val:
                        # 列表/字典要深拷贝，避免和调用方共享可变对象
                        if isinstance(val, list):
                            val = list(val)
                        elif isinstance(val, dict):
                            val = dict(val)
                        setattr(dst, name, val)
                        touched = True
            if touched:
                self.save_config(cfg)

    # -- 状态 ----------------------------------------------------------
    def load_state(self) -> RuntimeState:
        with self._lock:
            return RuntimeState.from_dict(read_json(self.state_path, {}))

    def save_state(self, st: RuntimeState) -> None:
        # 状态里会有失败明细（含上游响应片段、代理地址），收紧到 600。
        # 与 docs/SECURITY.md 的"运行状态不含凭据"保持一致：
        # 密码确实不在里面，但别人的报错正文不该是全局可读的。
        with self._lock:
            atomic_write_json(self.state_path, st.to_dict(), private=True)

    # -- 供后台任务用的"只回写我动过的部分" -----------------------------
    def merge_site_runtime(self, sites: List[Any]) -> AppConfig:
        """只把站点的**运行期字段**回写到盘上（绝不动用户配置）。

        为什么需要：长任务跑几分钟，期间用户可能改了站点设置、加了站点、
        删了站点、保存了新 Cookie。如果结束时整包写回旧快照，这些改动全部
        丢失（实测：新 Cookie 消失、改口令被回滚、吊销的令牌又生效）。
        所以这里在锁内重读盘，然后**只**覆盖运行期字段：
            state / cookie_status / cookie_checked_at / cookie_error
        其余（enabled、name、homepage、steps、headers、proxy、口令密文…）
        一律保持盘上的值 —— 那才是用户的最新意图。

        ⚠️ 刻意**不**回写 `cookie_enc`：它是凭据，只能由 service 里
        明确的"保存 Cookie"操作写入。运行期回写它等于让一个旧快照
        把用户刚粘的新 Cookie 覆盖掉。

        站点按 id 匹配；盘上已不存在（用户删了）就跳过，不能让它复活。
        只覆盖**调用方确实提供了**的键，避免把"没跑到"误当成"清空"。
        """
        _RUNTIME = ('state', 'cookie_status', 'cookie_checked_at',
                    'cookie_error')

        with self._lock:
            cfg = self.load_config()
            by_id = {str(s.id): s for s in cfg.sites}
            touched = False
            for src in sites:
                dst = by_id.get(str(getattr(src, 'id', '')))
                if dst is None:
                    continue                      # 用户删掉了 → 不复活
                # src.state 为空 dict 时视为"没跑过"，不要清掉盘上的状态
                src_state = getattr(src, 'state', None) or {}
                if src_state:
                    dst.state = dict(src_state)
                    touched = True
                for name in _RUNTIME[1:]:
                    val = getattr(src, name, None)
                    if val in (None, '', 0):
                        continue                  # 没提供 → 不动盘上的值
                    if getattr(dst, name, None) != val:
                        setattr(dst, name, val)
                        touched = True
            if touched:
                self.save_config(cfg)
            return cfg
