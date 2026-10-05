"""签到调度引擎（纯逻辑，无外部依赖，便于单测）。

需求对应：
  * 每个站点可选「每天固定时刻 + ±1 小时内随机偏移」
  * 可选「以上次签到成功时刻为基准」：今天 03:21 成功 → 明天 03:21+随机
  * 签到失败可配置重试次数与重试间隔
  * 连续失败达到阈值 → 触发 DeepSeek 分析

时间统一用「本地时区的时间戳（秒）」表示，避免 naive/aware 混用出错。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# 随机偏移上限：需求写的是「±1 小时之内」
JITTER_MAX_SECONDS = 3600

# 调度模式
MODE_DAILY = 'daily'          # 每天固定时刻
MODE_SUCCESS_BASED = 'success_based'  # 以上次成功时刻为基准

# 一次签到尝试的结果
RESULT_SUCCESS = 'success'
RESULT_FAILED = 'failed'


@dataclass
class SiteSchedule:
    """单个站点的调度配置与运行状态。"""

    site_id: str

    # --- 配置 ---
    mode: str = MODE_DAILY
    # 每天固定时刻（本地时间），mode=MODE_DAILY 时使用
    daily_hour: int = 3
    daily_minute: int = 0

    # 与"上次签到成功时间"的间隔（分钟），mode=MODE_SUCCESS_BASED 时使用。
    # 默认 1440 分钟 = 24 小时，即"每天一次"。
    # 为什么要有这个字段：以前这里硬编码 +86400，界面上却显示
    # "上次成功后 XX 分钟"（取的是重试间隔），显示与实际不符，
    # 用户也没法真的调间隔。
    success_interval_minutes: int = 1440
    # 【上次签到成功时间】模式的**初始基准时刻**（当天 00:00 起的分钟数）。
    # 用户在界面上选这个模式时，把那一刻填的时刻锁定成基准。
    # -1 = 没设置过（老配置/新站点）。
    # 基准取 max(初始锚点, 上次成功时间)：首次成功前用锚点，
    # 成功之后自然被真实成功时间取代。
    success_anchor_minutes: int = -1

    # 是否启用随机偏移
    jitter_enabled: bool = False
    # 偏移范围（秒），默认 ±1 小时；可在 [-JITTER_MAX_SECONDS, JITTER_MAX_SECONDS] 内配置
    jitter_seconds: int = JITTER_MAX_SECONDS

    # 失败重试
    retry_enabled: bool = True
    retry_count: int = 3            # 最多重试几次
    retry_interval_minutes: int = 30  # 每次重试间隔

    # 连续失败多少次后交给 DeepSeek 分析
    ai_after_failures: int = 3
    ai_enabled: bool = True

    # --- 状态（持久化） ---
    last_success_at: Optional[int] = None     # 上次签到成功的时间戳
    last_attempt_at: Optional[int] = None     # 上次尝试的时间戳
    consecutive_failures: int = 0             # 连续失败次数（成功后清零）
    retries_done: int = 0                     # 本轮已重试次数
    ai_analyzed_for_streak: int = 0           # 已经为哪一档连败触发过 AI，避免重复触发


def clamp_jitter(seconds: int) -> int:
    """把偏移量限制在 1 小时以内，并保证非负。"""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        s = JITTER_MAX_SECONDS
    if s < 0:
        s = 0
    return min(s, JITTER_MAX_SECONDS)


def apply_jitter(base_ts: int, site: SiteSchedule, rng: random.Random) -> int:
    """在基准时间上叠加随机偏移（未启用则原样返回）。

    偏移取 [-jitter, +jitter] 内的均匀整数秒。
    """
    if not site.jitter_enabled:
        return int(base_ts)
    j = clamp_jitter(site.jitter_seconds)
    if j == 0:
        return int(base_ts)
    delta = rng.randint(-j, j)
    return int(base_ts) + delta


def next_daily_time(now: int, site: SiteSchedule, rng: random.Random) -> int:
    """模式一：每天固定时刻（+可选随机偏移）。

    规则：
      * 基准是「今天/明天的 daily_hour:daily_minute」
      * 叠加随机偏移后，如果结果已经过去，就顺延到明天同一基准再叠加偏移
      * 结果一定 > now
    """
    base_today = _at_local_time(now, site.daily_hour, site.daily_minute)
    candidate = apply_jitter(base_today, site, rng)
    if candidate > now:
        return candidate
    # 今天这个点已经过了（或加偏移后落在过去），顺延一天
    base_tomorrow = base_today + 86400
    return apply_jitter(base_tomorrow, site, rng)


def anchor_base_ts(now: int, anchor_minutes: int) -> int:
    """把锁定的基准时刻换算成"**第一个**该签到的时刻"。

    用户要求（第一次）："如果时刻已过就顺延下一天"。
    所以语义是「今天的那个时刻；已经过了就用明天那个」，而不是
    "锚点 + 一个间隔"（那样第一次会白等一整天）。

    注意与成功后的区别：成功之后的基准是"实际成功时间 + 间隔"，
    由调用方另行处理。
    """
    minutes = max(0, min(1439, int(anchor_minutes)))
    candidate = _at_local_time(now, minutes // 60, minutes % 60)
    if candidate <= now:
        candidate += 86400          # 今天这个点已过 -> 顺延到明天
    return int(candidate)


def next_success_based_time(now: int, site: SiteSchedule,
                            rng: random.Random) -> int:
    """模式二：以【上次签到成功时刻】为基准，每隔 N 分钟一次（+可选随机偏移）。

    基准的取法（优先级从高到低）：
      1. 上次真实签到成功时间（一旦成功过，就用它）
      2. 界面里锁定的"初始基准时刻"（success_anchor_minutes）
      3. 都没有（老配置/从没设置过）-> 现在 + 间隔（保持旧行为不变）

    基准确定后：候选 = 基准 + 间隔 N 次，直到落在未来；再叠加随机偏移。

    例：初始基准 09:35、间隔 1440 分钟
      -> 首次排在 09:35（今天或明天，看当前时间）
      -> 09:35 签到成功后，基准换成真实成功时刻（如 09:41）
      -> 下次就是 09:41 + 1440 分钟 = 明天 09:41
    """
    interval = max(60, int(getattr(site, 'success_interval_minutes', 1440) or 1440))
    interval_seconds = interval * 60

    # ⚠️ 不能用 `... or -1`：0 是合法值（基准 00:00），而 `0 or -1` 会变成 -1，
    # 于是"午夜基准"被当成"没设置"，静默退回旧行为（实测踩过）。
    raw_anchor = getattr(site, 'success_anchor_minutes', -1)
    anchor = -1 if raw_anchor is None else int(raw_anchor)
    last_success = getattr(site, 'last_success_at', None)
    if last_success:
        # 成功过：基准就是真实成功时间（锚点自动被取代）
        base = int(last_success) + interval_seconds
    elif anchor >= 0:
        # 还没成功过、但锁定了初始基准：
        # 第一次就排在"基准当天"（今天该时刻已过则顺延明天），
        # 而不是"锚点 + 一个间隔"—— 后者会让第一次白等一整天。
        #
        # ⚠️ 这里必须单独处理"叠加抖动后仍然过去"的情形：
        # 那种情况下要顺延到**下一个基准点（+1 天）**，不能交给下面
        # 那个按 `interval` 推进的循环 —— 间隔是 1440 分钟时，
        # 从 00:00 会一次跳到次日 14:00，把整段都跳过（实测踩过）。
        base = anchor_base_ts(now, anchor)
        guard = 0
        while apply_jitter(base, site, rng) <= now and guard < 400:
            base += 86400
            guard += 1
        return apply_jitter(base, site, rng)
    else:
        # 从没成功过、也没设置初始基准 -> 保持旧行为
        base = int(now) + interval_seconds

    candidate = apply_jitter(base, site, rng)
    # 偏移可能把它拉到过去（例如上次成功在 23 小时前、偏移 -1h）；
    # 这种情况下按间隔继续顺延，直到落到未来，保证不会立即重复触发。
    guard = 0
    while candidate <= now and guard < 400:
        base += interval_seconds
        candidate = apply_jitter(base, site, rng)
        guard += 1
    return candidate


def next_run_at(now: int, site: SiteSchedule, rng: Optional[random.Random] = None) -> int:
    """计算下一次该签到的时间戳。"""
    rng = rng or random.Random()
    if site.mode == MODE_SUCCESS_BASED:
        return next_success_based_time(now, site, rng)
    return next_daily_time(now, site, rng)


def next_retry_at(now: int, site: SiteSchedule) -> Optional[int]:
    """签到失败后，下一次重试的时间；不能再重试则返回 None。

    重试计数语义：on_failure 会先把本次失败计入 retries_done，所以这里用
    `retries_done <= retry_count` 判断。这样可以保证 retry_count=N 时
    恰好重试 N 次（1 次正常尝试 + N 次重试）。
    """
    if not site.retry_enabled:
        return None
    if site.retries_done > max(0, int(site.retry_count)):
        return None
    interval = max(1, int(site.retry_interval_minutes)) * 60
    return int(now) + interval


def should_ask_ai(site: SiteSchedule, after_failures: Optional[int] = None) -> bool:
    """连续失败是否达到了需要 AI 分析的程度。

    语义：在 after_failures 的**整数倍**处各分析一次。
    例如阈值 3 → 在连败 3、6、9… 次时分析。

    为什么不"每多失败一次就分析"：
    站点若持续坏掉（连败 4、5、6…），逐次分析会天天烧 token 却给不出新结论。
    按倍数走既能有限次尝试，又能随连败加重再要一次新建议。

    after_failures 现在由**全局设置**提供（用户要求：统一逻辑，
    站点只负责重试次数）。传 None 时回退到站点上的同名字段，
    兼容旧调用方；0 表示不启用 AI 分析。
    """
    if not site.ai_enabled:
        return False
    raw = after_failures
    if raw is None:
        raw = getattr(site, 'ai_after_failures', 3)
    try:
        threshold = int(raw)
    except (TypeError, ValueError):
        threshold = 3
    if threshold <= 0:
        return False        # 0 = 不启用 AI 分析
    if site.consecutive_failures < threshold:
        return False
    if site.consecutive_failures % threshold != 0:
        return False
    # 同一档位只分析一次
    return site.ai_analyzed_for_streak != site.consecutive_failures


def on_attempt(site: SiteSchedule, now: int) -> None:
    """记录一次「尝试」发生。"""
    site.last_attempt_at = int(now)


def on_success(site: SiteSchedule, now: int) -> None:
    """签到成功：清零连败与重试计数，记录成功时刻。"""
    site.last_success_at = int(now)
    site.consecutive_failures = 0
    site.retries_done = 0
    site.ai_analyzed_for_streak = 0


def on_failure(site: SiteSchedule) -> None:
    """签到失败：连败 +1，重试计数 +1。"""
    site.consecutive_failures += 1
    site.retries_done += 1


def mark_ai_analyzed(site: SiteSchedule) -> None:
    """标记这一档连败已经做过 AI 分析。"""
    site.ai_analyzed_for_streak = site.consecutive_failures


def plan_next(site: SiteSchedule, now: int, succeeded: bool,
              rng: Optional[random.Random] = None) -> int:
    """一次签到结束后，决定下一次什么时候跑。

    成功 → 按调度模式排下一次正常签到（并清空重试）
    失败 → 优先按重试策略排重试；重试用尽则回到正常调度
    """
    on_attempt(site, now)
    if succeeded:
        on_success(site, now)
        return next_run_at(now, site, rng)

    on_failure(site)
    retry_at = next_retry_at(now, site)
    if retry_at is not None:
        return retry_at
    # 重试用尽：回到正常节奏，同时把重试计数清零，给下一轮留出额度
    site.retries_done = 0
    return next_run_at(now, site, rng)


def _at_local_time(ts: int, hour: int, minute: int) -> int:
    """把时间戳所在「本地日期」的 hour:minute 换回时间戳。"""
    d = datetime.fromtimestamp(ts)
    target = d.replace(hour=int(hour) % 24, minute=int(minute) % 60,
                       second=0, microsecond=0)
    return int(target.timestamp())
