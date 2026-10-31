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
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional

from .reasons import classify, meta_of

HISTORY_FILE = 'history.jsonl'
# 保留天数。日历是按月翻的，半年足够看出"哪些天有问题"，也不至于把盘写满。
KEEP_DAYS = 180
# 超过这个体积就压缩一次（按 KEEP_DAYS 保留）。正常一年也就几百 KB。
MAX_BYTES = 4 * 1024 * 1024
MSG_LIMIT = 300
# 单次执行最多留多少步、每步多长。截断是必须的：一条记录的步骤链会长到
# 一两 KB，180 天 × 多站点累积起来会把 history.jsonl 顶到压缩阈值。
STEPS_LIMIT = 12
STEP_LIMIT = 160
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


# 步骤链里会有 URL 和"提取到的值"，两处都可能带令牌：
#   GET https://x.com/mission/daily/redeem?once=84123 -> 302
#   提取 once=84123
# 落盘前把**像令牌的部分**打码：历史文件是"给人看发生了什么"的，
# 不是凭据仓库，留着就等于把一次性令牌写进一个要存半年的文件里。
#
# 但也不能一刀切把 query 全遮了 —— `?random=false`、`?page=2` 这类
# 是排查时最想看到的东西，遮掉等于把日志削成没用的形式。
# 所以按参数名判断（令牌类），外加"值长得像随机串就遮"兜底。
_QUERY_RE = re.compile(r'([?&][^=\s&]+=)([^\s&]*)')
_SENSITIVE_KEY_RE = re.compile(
    r'(token|once|key|secret|auth|session|sign|ticket|code|passw|pwd|'
    r'cookie|csrf|nonce|ticket|hash)', re.I)
_LONG_VALUE = 20        # 超过这个长度的 query 值一律当令牌处理
_EXTRACTED_RE = re.compile(r'(提取\s+[^=\s]+=).*')


def _mask_query(text: str) -> str:
    def repl(m):
        head, val = m.group(1), m.group(2)
        if not val:
            return m.group(0)
        if _SENSITIVE_KEY_RE.search(head) or len(val) >= _LONG_VALUE:
            return head + '***'
        return m.group(0)

    return _QUERY_RE.sub(repl, text)


def scrub_step(line: Any) -> str:
    """把一步痕迹压成单行、去掉令牌值、截断。"""
    s = str(line or '')
    for ch in ('\r\n', '\r', '\n', '\t'):
        s = s.replace(ch, ' ')
    s = ' '.join(s.split())
    s = _mask_query(s)
    # 提取出来的值可能是任何东西（令牌、积分、用户名），一律打码
    s = _EXTRACTED_RE.sub(r'\1***', s)
    if len(s) > STEP_LIMIT:
        s = s[:STEP_LIMIT] + '…'
    return s


def _clean_steps(raw: Any) -> List[str]:
    """步骤链入库前的清洗。坏数据/超长一律吞掉，绝不让它拖累签到。"""
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for line in raw:
        s = scrub_step(line)
        if s:
            out.append(s)
        if len(out) >= STEPS_LIMIT:
            break
    return out


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


def tz_offset_minutes() -> int:
    """本机时区相对 UTC 的偏移（分钟，东八区为 480）。

    给前端用：记录是按容器时区分天归档的，界面上的"几月几号 几点"也必须
    按容器时区算。否则容器跑 UTC、浏览器在 +8 时，昨天 23:30 的记录会被
    显示成"今天 07:30"，和它所在的日历格子对不上。
    """
    try:
        off = datetime.now().astimezone().utcoffset()
        return int(off.total_seconds() // 60) if off else 0
    except Exception:                                           # noqa: BLE001
        return 0


def day_bounds(day: str) -> tuple:
    """'2026-10-31' -> (当天 0 点, 次日 0 点) 的本地时间戳。"""
    d = datetime.strptime(day[:10], '%Y-%m-%d')
    start = datetime(d.year, d.month, d.day)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


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
                   attempt: int = 1,
                   trace: Optional[List[str]] = None) -> Dict[str, Any]:
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
            'steps': _clean_steps(trace),
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
        # 失败原因聚合：code -> {count, last_ts, sites}
        # 按"归类"而不是按原始 error_kind 统计 —— 原始值里 http_403 和
        # 消息里写着"被拒绝"的两条其实是同一件事，分开数就没意义了。
        reason_stat: Dict[str, Dict[str, Any]] = {}
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
            # 当天最主要的原因（格子鼠标悬停时显示）
            day_reasons: Dict[str, int] = {}
            for e in items:
                if _bool(e.get('ok')):
                    continue
                code = self._reason_of(e)
                day_reasons[code] = day_reasons.get(code, 0) + 1
                st = reason_stat.setdefault(code, {'count': 0, 'last_ts': 0,
                                                   'sites': [], 'last_msg': ''})
                st['count'] += 1
                if _int(e.get('ts')) >= st['last_ts']:
                    st['last_ts'] = _int(e.get('ts'))
                    # 最近一次的原话：单站点视图里站点名是冗余的，
                    # 换成这条更有用（"Cloudflare 拦截，HTTP 403"）。
                    st['last_msg'] = _clean_msg(e.get('msg'))
                name = str(e.get('site_name') or e.get('site_id') or '')
                if name and name not in st['sites'] and len(st['sites']) < 6:
                    st['sites'].append(name)
            top = ''
            if day_reasons:
                top = max(day_reasons.items(), key=lambda kv: kv[1])[0]
            days.append({
                'date': key,
                'day': d.day,
                'status': status,
                'ok': ok_n,
                'fail': fail_n,
                'total': len(items),
                'future': key > today,
                'is_today': key == today,
                'top_reason': top,
                'items': [self._public(e) for e in items[-MAX_ITEMS_PER_DAY:]],
            })

        reasons = sorted(reason_stat.items(),
                         key=lambda kv: (-kv[1]['count'], -kv[1]['last_ts']))
        reason_list = [{
            'code': code,
            'count': st['count'],
            'last_ts': st['last_ts'],
            'sites': st['sites'],
            'last_msg': st['last_msg'],
        } for code, st in reasons]

        return {
            'month': mon,
            'site_id': str(site_id or ''),
            'today': today,
            # 服务端时区相对 UTC 的偏移（分钟）。
            # 界面上的"几点几分"必须按**容器的**时区显示：
            # 记录是按容器时区分天归档的，若用浏览器本地时间去格式化，
            # "昨天 23:30 的记录"会在 +8 的浏览器里显示成"今天 07:30"，
            # 和它所在的日历格子对不上，看起来像串了天。
            'tz_offset_minutes': tz_offset_minutes(),
            # 0=周一 … 6=周日：前端据此补前置空格，不用自己算星期
            # （浏览器时区可能和容器不同，日期算在服务端更稳）
            'first_weekday': first.weekday(),
            'days': days,
            'reasons': reason_list,
            'reason_meta': meta_of([r['code'] for r in reason_list]),
            'summary': {
                'ok': sum_ok,
                'fail': sum_fail,
                'run_days': days_ok + days_bad,
                'green_days': days_ok,
                'problem_days': days_bad,
            },
            'retention_days': KEEP_DAYS,
        }

    def day_summary(self, day: str) -> Dict[str, Any]:
        """按站点汇总某一天，供每日推送使用。

        与日历的区别：日历是"这一天每一天格子的状态"，这里是
        "这一天每个站点到底成没成、失败的是什么原因"，是给一条消息用的。
        """
        since, until = day_bounds(day)
        entries = self.load(since=since, until=until)
        sites: Dict[str, Dict[str, Any]] = {}
        reasons: Dict[str, int] = {}
        for e in entries:
            sid = str(e.get('site_id') or '')
            st = sites.setdefault(sid, {
                'site_id': sid,
                'site_name': str(e.get('site_name') or sid),
                'ok': 0, 'fail': 0, 'reason': '', 'last_msg': '',
                'last_ts': 0,
            })
            if _bool(e.get('ok')):
                st['ok'] += 1
            else:
                st['fail'] += 1
                code = self._reason_of(e)
                reasons[code] = reasons.get(code, 0) + 1
                st['reason'] = code
                st['last_msg'] = _clean_msg(e.get('msg'))
            ts = _int(e.get('ts'))
            if ts >= st['last_ts']:
                st['last_ts'] = ts

        rows = sorted(sites.values(),
                      key=lambda x: (-x['fail'], -x['ok'], x['site_name']))
        total_ok = sum(r['ok'] for r in rows)
        total_fail = sum(r['fail'] for r in rows)
        return {
            'date': day,
            'ok': total_ok,
            'fail': total_fail,
            'total': total_ok + total_fail,
            'sites': rows,
            # 失败原因按次数倒序，推送里只说前三条
            'reasons': sorted(({'code': c, 'count': n}
                               for c, n in reasons.items()),
                              key=lambda x: -x['count']),
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
        # 近 N 天里最常出现的失败原因：摘要栏一句话就能看出"最近卡在哪"
        tally: Dict[str, int] = {}
        for e in entries:
            if not _bool(e.get('ok')):
                code = self._reason_of(e)
                tally[code] = tally.get(code, 0) + 1
        top = None
        if tally:
            code, n = max(tally.items(), key=lambda kv: kv[1])
            top = {'code': code, 'label': classify(code)['label'], 'count': n}
        return {
            'days': span,
            'total': total,
            'ok': ok_n,
            'fail': fail_n,
            'rate': (round(ok_n * 100.0 / total, 1) if total else None),
            'top_reason': top,
        }

    @staticmethod
    def _reason_of(e: Dict[str, Any]) -> str:
        """一条记录的失败归类（成功记录不调用）。"""
        return classify(str(e.get('error_kind') or ''),
                        str(e.get('msg') or ''))['code']

    @classmethod
    def _public(cls, e: Dict[str, Any]) -> Dict[str, Any]:
        """给前端的字段（不额外裁剪：这里本来就不存凭据）。

        reason 只放归类 code，label/hint 由接口顶层的 reason_meta 提供 ——
        明细可能有上千条，每条重复一份中文标签既费流量，也容易和
        服务端的映射表对不上。
        """
        ok = _bool(e.get('ok'))
        return {
            'ts': _int(e.get('ts')),
            'site_id': str(e.get('site_id') or ''),
            'site_name': str(e.get('site_name') or ''),
            'ok': ok,
            'msg': _clean_msg(e.get('msg')),
            'error_kind': str(e.get('error_kind') or ''),
            'reason': '' if ok else cls._reason_of(e),
            'duration_ms': _int(e.get('duration_ms')),
            'trigger': str(e.get('trigger') or ''),
            'attempt': _int(e.get('attempt'), 1),
            # 这一步链只有展开某条明细时才显示；老记录没有这个字段，给空数组
            'steps': [str(x) for x in (e.get('steps') or [])
                      if isinstance(x, str)][:STEPS_LIMIT],
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
