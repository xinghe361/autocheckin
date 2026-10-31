"""执行历史：给界面上的「日历」看每次签到到底成没成。

为什么单独一个文件，而不是塞进 state.json：
  * state.json 是**整包读写**（锁内重读-改-写全量），而历史是只增不改的。
    塞进去会让每次写 state 的体积越滚越大，还更容易和并发写回打架。
  * JSONL 追加写不需要先读旧内容，天然抗并发、抗半截写入 ——
    写到一半断电最多丢最后一行，读的时候跳过坏行即可，不会"整个文件坏掉"。
  * 用户能直接 cat / 下载下来用 Excel 看，出问题能自己核对。

存储：DATA_DIR/history.jsonl，一行一次"尝试"（失败重试也算一条）：
    {"ts": 1761900000, "site_id": "v2ex", "site_name": "V2EX",
     "ok": true, "msg": "签到成功，获得 8 铜币", "error_kind": "",
     "duration_ms": 812, "trigger": "schedule", "attempt": 1}

⚠️ msg 里可能有上游响应片段（和 state.json 里的失败明细同源），
   所以文件权限收到 600，与 state.json 一致。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional

HISTORY_FILE = 'history.jsonl'
# 保留天数。日历是按月翻的，半年足够看出"哪些天有问题"，也不至于把盘写满。
KEEP_DAYS = 180
# 超过这个体积就压缩一次（按 KEEP_DAYS 保留）。正常一年也就几百 KB。
MAX_BYTES = 4 * 1024 * 1024
MSG_LIMIT = 300
# 单日明细最多返回多少条（防止某站点疯狂重试把页面撑爆）
MAX_ITEMS_PER_DAY = 60

STATUS_OK = 'ok'
STATUS_FAIL = 'fail'
STATUS_NONE = 'none'


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'ok', 'y')
    return bool(v)


def _clean_msg(raw: Any) -> str:
    """压成单行并截断。

    为什么要压：消息会被塞进 JSONL 的一行里，换行会让文件看起来像多行记录
    （虽然 json 能解析，但 grep/Excel 打开就乱了），前端也要自己再处理一遍。
    """
    s = str(raw or '')
    for ch in ('\r\n', '\r', '\n', '\t'):
        s = s.replace(ch, ' ')
    s = ' '.join(s.split())
    if len(s) > MSG_LIMIT:
        s = s[:MSG_LIMIT] + '…'
    return s


def day_of(ts: int) -> str:
    """时间戳 -> 本地日期字符串（按容器时区，与调度/界面显示一致）。"""
    try:
        return datetime.fromtimestamp(int(ts)).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ''


def month_of(ts: int) -> str:
    return day_of(ts)[:7]


def parse_month(raw: str, now_ts: int) -> str:
    """把 '2026-10' 解析成合法月份；非法就退回当前月。"""
    s = str(raw or '').strip()
    if len(s) == 7 and s[4] == '-' and s[:4].isdigit() and s[5:].isdigit():
        y, m = int(s[:4]), int(s[5:])
        if 2000 <= y <= 2100 and 1 <= m <= 12:
            return s
    return month_of(now_ts)


def month_bounds(month: str) -> tuple:
    """'2026-10' -> (该月 1 日 0 点的本地时间戳, 下月 1 日 0 点的本地时间戳)。"""
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1)
    end = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    return int(start.timestamp()), int(end.timestamp())


class History:
    """history.jsonl 的读写门面（追加写 + 容错读）。"""

    def __init__(self, data_dir: str, now_fn: Optional[Callable[[], int]] = None):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, HISTORY_FILE)
        self.now_fn = now_fn or (lambda: int(time.time()))
        self._lock = threading.Lock()
        self._appended = 0

    # -- 写 -------------------------------------------------------------
    def make_entry(self, site_id: str, site_name: str, ok: bool,
                   msg: str = '', error_kind: str = '', duration_ms: int = 0,
                   ts: Optional[int] = None, trigger: str = 'schedule',
                   attempt: int = 1) -> Dict[str, Any]:
        return {
            'ts': _int(ts if ts is not None else self.now_fn()),
            'site_id': str(site_id or ''),
            'site_name': str(site_name or ''),
            'ok': bool(ok),
            'msg': _clean_msg(msg),
            'error_kind': str(error_kind or '')[:60],
            'duration_ms': _int(duration_ms),
            'trigger': str(trigger or 'schedule')[:20],
            'attempt': max(1, _int(attempt, 1)),
        }

    def append(self, entries: List[Dict[str, Any]]) -> int:
        """追加若干条。任何异常都不外抛 —— 记历史失败绝不能影响签到本身。"""
        if not entries:
            return 0
        lines = ''.join(json.dumps(e, ensure_ascii=False) + '\n'
                        for e in entries if isinstance(e, dict))
        if not lines:
            return 0
        with self._lock:
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                             0o600)
                try:
                    os.write(fd, lines.encode('utf-8'))
                finally:
                    os.close(fd)
            except OSError:
                return 0
            self._appended += len(entries)
            if self._appended >= 50 or self._too_big():
                # 压缩不是为了省空间，是为了让"半年"这个保留期真的生效：
                # 否则文件会一直长（每天每站点几条，一年也就几百 KB，
                # 但重试多、站点多的时候会失控）。
                self._appended = 0
                self._compact()
            return len(entries)

    def _too_big(self) -> bool:
        try:
            return os.path.getsize(self.path) > MAX_BYTES
        except OSError:
            return False

    def _compact(self) -> int:
        """重写文件，只留 KEEP_DAYS 内的记录。返回丢弃的条数。

        先写临时文件再 replace：压缩过程中崩了也不会留下半个文件
        （半个文件虽然读得动，但会静默丢掉后半段历史）。
        """
        cutoff = self.now_fn() - KEEP_DAYS * 86400
        keep: List[str] = []
        dropped = 0
        for line in self._raw_lines():
            try:
                e = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(e, dict):
                continue
            if _int(e.get('ts')) >= cutoff:
                keep.append(line)
            else:
                dropped += 1
        if not dropped:
            return 0
        tmp = self.path + '.compact'
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(''.join(l + '\n' for l in keep))
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return 0
        return dropped

    # -- 读 -------------------------------------------------------------
    def _raw_lines(self) -> Iterator[str]:
        try:
            with open(self.path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield line
        except OSError:
            return

    def iter_entries(self) -> Iterator[Dict[str, Any]]:
        """逐条产出，坏行直接跳过（手改错了一个字符不该让整页报错）。"""
        for line in self._raw_lines():
            try:
                e = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(e, dict):
                yield e

    def load(self, site_id: str = '', since: int = 0,
             until: int = 0) -> List[Dict[str, Any]]:
        sid = str(site_id or '')
        out = []
        for e in self.iter_entries():
            ts = _int(e.get('ts'))
            if since and ts < since:
                continue
            if until and ts >= until:
                continue
            if sid and str(e.get('site_id') or '') != sid:
                continue
            out.append(e)
        return out

    # -- 日历 -----------------------------------------------------------
    def calendar(self, site_id: str = '', month: str = '') -> Dict[str, Any]:
        """一个月的日历数据：每天什么状态 + 当天的明细。

        状态只有三种，刻意不做"部分成功"这种灰色地带：
          * ok   —— 当天有记录且**没有一次失败**（全绿）
          * fail —— 当天有失败（哪怕后来重试成功了也算"有问题"，
                    因为用户要看的正是"哪天不对劲"）
          * none —— 当天没跑过
        """
        now = self.now_fn()
        mon = parse_month(month, now)
        since, until = month_bounds(mon)
        today = day_of(now)

        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for e in self.load(site_id=site_id, since=since, until=until):
            d = day_of(_int(e.get('ts')))
            if not d:
                continue
            buckets.setdefault(d, []).append(e)

        first = datetime(int(mon[:4]), int(mon[5:7]), 1)
        if int(mon[5:7]) == 12:
            nxt = datetime(int(mon[:4]) + 1, 1, 1)
        else:
            nxt = datetime(int(mon[:4]), int(mon[5:7]) + 1, 1)
        days_in_month = (nxt - first).days

        days = []
        sum_ok = sum_fail = days_ok = days_bad = 0
        for i in range(days_in_month):
            d = date(first.year, first.month, 1) + timedelta(days=i)
            key = d.isoformat()
            items = sorted(buckets.get(key, []), key=lambda e: _int(e.get('ts')))
            ok_n = sum(1 for e in items if _bool(e.get('ok')))
            fail_n = len(items) - ok_n
            if not items:
                status = STATUS_NONE
            elif fail_n:
                status = STATUS_FAIL
            else:
                status = STATUS_OK
            sum_ok += ok_n
            sum_fail += fail_n
            if items:
                if status == STATUS_OK:
                    days_ok += 1
                else:
                    days_bad += 1
            days.append({
                'date': key,
                'day': d.day,
                'status': status,
                'ok': ok_n,
                'fail': fail_n,
                'total': len(items),
                'future': key > today,
                'is_today': key == today,
                'items': [self._public(e) for e in items[-MAX_ITEMS_PER_DAY:]],
            })

        return {
            'month': mon,
            'site_id': str(site_id or ''),
            'today': today,
            # 0=周一 … 6=周日：前端据此补前置空格，不用自己算星期
            # （浏览器时区可能和容器不同，日期算在服务端更稳）
            'first_weekday': first.weekday(),
            'days': days,
            'summary': {
                'ok': sum_ok,
                'fail': sum_fail,
                'run_days': days_ok + days_bad,
                'green_days': days_ok,
                'problem_days': days_bad,
            },
            'retention_days': KEEP_DAYS,
        }

    def recent(self, site_id: str = '', days: int = 30) -> Dict[str, Any]:
        """最近 N 天的成功率速览（日历下面那行小字用）。"""
        now = self.now_fn()
        span = max(1, _int(days, 30))
        since = now - span * 86400
        entries = self.load(site_id=site_id, since=since)
        ok_n = sum(1 for e in entries if _bool(e.get('ok')))
        fail_n = len(entries) - ok_n
        total = ok_n + fail_n
        return {
            'days': span,
            'total': total,
            'ok': ok_n,
            'fail': fail_n,
            'rate': (round(ok_n * 100.0 / total, 1) if total else None),
        }

    @staticmethod
    def _public(e: Dict[str, Any]) -> Dict[str, Any]:
        """给前端的字段（不额外裁剪：这里本来就不存凭据）。"""
        return {
            'ts': _int(e.get('ts')),
            'site_id': str(e.get('site_id') or ''),
            'site_name': str(e.get('site_name') or ''),
            'ok': _bool(e.get('ok')),
            'msg': _clean_msg(e.get('msg')),
            'error_kind': str(e.get('error_kind') or ''),
            'duration_ms': _int(e.get('duration_ms')),
            'trigger': str(e.get('trigger') or ''),
            'attempt': _int(e.get('attempt'), 1),
        }


# 每个数据目录一个实例：Runner 是「每次执行新建一个」，如果各自持有
# History 就各自拿一把锁，压缩（重写文件）时可能和追加写撞上。
_INSTANCES: Dict[str, History] = {}
_INSTANCES_LOCK = threading.Lock()


def get_history(data_dir: str) -> History:
    key = os.path.abspath(data_dir or '.')
    with _INSTANCES_LOCK:
        h = _INSTANCES.get(key)
        if h is None:
            h = History(key)
            _INSTANCES[key] = h
        return h
