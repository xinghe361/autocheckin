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
import stat
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .models import AppConfig, default_config

CONFIG_FILE = 'config.json'
STATE_FILE = 'state.json'


def atomic_write_json(path: str, data: Any, private: bool = False) -> None:
    """原子写 JSON：临时文件 + fsync + replace。

    private=True 时把权限收成 600。注意 os.open 的 mode 只在**创建**时生效，
    文件已存在时不会改变权限，所以这里额外显式 chmod 一次。
    （Windows 上 os.chmod 只影响只读位，因此仅在 POSIX 上做严格断言。）
    """
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = path + '.tmp'
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


def read_json(path: str, default: Any = None) -> Any:
    """读 JSON；文件不存在或损坏时返回 default（不让坏文件把程序卡死）。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        # 损坏的配置：备份一份现场，方便排查，然后回退默认值
        try:
            broken = path + '.broken.%d' % int(time.time())
            os.replace(path, broken)
        except OSError:
            pass
        return default


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
        """到点的站点 id 列表（next_run_at 已过或未设置）。"""
        out = []
        for sid, st in self.sites.items():
            nxt = st.get('next_run_at')
            if not nxt or int(nxt) <= now:
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

    # -- 每日尝试预算 ---------------------------------------------------
    # 用户要求：每个站点每天最多试 N 次，用完就等第二天，
    # 避免"一直重试、一直问 AI"把 token 额度烧光。
    def day_attempts(self, site_id: str, today: str) -> int:
        rec = self.site(site_id).get('day') or {}
        if rec.get('date') != today:
            return 0
        try:
            return int(rec.get('attempts') or 0)
        except (TypeError, ValueError):
            return 0

    def bump_day_attempts(self, site_id: str, today: str) -> int:
        st = self.site(site_id)
        rec = st.get('day') or {}
        if rec.get('date') != today:
            # 新的一天：计数与"当天已问过 AI"一起清零
            rec = {'date': today, 'attempts': 0, 'ai_done': False}
        rec['attempts'] = int(rec.get('attempts') or 0) + 1
        st['day'] = rec
        return rec['attempts']

    def day_ai_done(self, site_id: str, today: str) -> bool:
        """今天这个站点是否已经问过 AI 了。"""
        rec = self.site(site_id).get('day') or {}
        return rec.get('date') == today and bool(rec.get('ai_done'))

    def mark_day_ai_done(self, site_id: str, today: str) -> None:
        st = self.site(site_id)
        rec = st.get('day') or {}
        if rec.get('date') != today:
            rec = {'date': today, 'attempts': 0}
        rec['ai_done'] = True
        st['day'] = rec

    def day_exhausted(self, site_id: str, today: str, limit: int) -> bool:
        """今天这个站点是否已用完尝试次数。"""
        if limit <= 0:
            return False
        return self.day_attempts(site_id, today) >= limit

    def clear_day_problem(self, site_id: str) -> None:
        """清掉"当天不再尝试"的标记（用户手动操作时用）。"""
        st = self.site(site_id)
        st.pop('day', None)
        for k in ('day_problem', 'day_problem_reason', 'day_problem_at'):
            st.pop(k, None)

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
    """读写 config.json / state.json 的门面。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.config_path = os.path.join(data_dir, CONFIG_FILE)
        self.state_path = os.path.join(data_dir, STATE_FILE)

    # -- 配置 ----------------------------------------------------------
    def load_config(self) -> AppConfig:
        raw = read_json(self.config_path, None)
        if not raw:
            cfg = default_config()
            return cfg
        return AppConfig.from_dict(raw)

    def save_config(self, cfg: AppConfig) -> None:
        # 含密文凭据 → 权限收紧
        atomic_write_json(self.config_path, cfg.to_dict(include_secrets=True),
                          private=True)

    def export_config(self, cfg: AppConfig) -> Dict[str, Any]:
        """给 WebDAV 备份用的导出（含密文，但不含密钥）。"""
        return cfg.to_dict(include_secrets=True)

    def import_config(self, data: Dict[str, Any]) -> AppConfig:
        return AppConfig.from_dict(data or {})

    # -- 状态 ----------------------------------------------------------
    def load_state(self) -> RuntimeState:
        return RuntimeState.from_dict(read_json(self.state_path, {}))

    def save_state(self, st: RuntimeState) -> None:
        atomic_write_json(self.state_path, st.to_dict(), private=False)
