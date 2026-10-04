"""后台调度循环：按分钟醒来，把到期的站点交给 Runner 执行。

与用户约定的行为一致：
  * 打开/启动时**不立即签到**，先排程
  * 每站点可配 ±1 小时随机偏移
  * 可选"以上次成功时刻为基准"
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

TICK_SECONDS = 60


class Scheduler:
    """极简调度器：单线程循环 + 可注入的时钟，便于测试。"""

    def __init__(self, service, interval: int = TICK_SECONDS,
                 now_fn: Callable[[], float] = None, log_fn: Callable = None):
        self.service = service
        self.interval = max(5, int(interval))
        self.now_fn = now_fn or time.time
        self.log = log_fn or (lambda *a: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.ticks = 0
        self.last_tick_at = 0.0
        self.last_error = ''

    # ------------------------------------------------------------------
    def tick(self) -> Optional[dict]:
        """执行一次："到点就签到"。返回执行概要；无到期站点返回 None。

        每次 tick 还会顺手做一次"登录 Cookie 效期检查"（内部自限为每天一次），
        失效时推一条通知。这样失效能被主动发现，而不是等签到失败才发现。
        """
        self.ticks += 1
        self.last_tick_at = self.now_fn()
        try:
            # 登录态巡检：内部按 24 小时自限，所以每分钟调它也不会真的天天查
            try:
                self.service.maybe_check_cookies()
            except Exception as e:                              # noqa: BLE001
                # 巡检失败绝不能影响正常签到
                self.log('Cookie 巡检出错（已忽略）：%s' % e)

            cfg = self.service.load_config()
            state = self.service.load_state()
            runner = self.service.make_runner(cfg)
            runner.ensure_scheduled(cfg, state, int(self.now_fn()))
            self.service.store.save_state(state)

            due = runner.due_sites(cfg, state, int(self.now_fn()))
            if not due:
                return None
            self.log('有 %d 个站点到期，开始执行' % len(due))
            report = runner.run_once(cfg, state)
            self.last_error = ''
            return {
                'summary': report.summary(),
                'sites': [r.site_id for r in report.runs],
            }
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.log('调度出错：%s' % e)
            return None

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        # 启动时先排程、不立即执行
        self.tick()
        while not self._stop.wait(self.interval):
            self.tick()

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='autocheckin-scheduler')
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {
            'running': self.running,
            'interval_seconds': self.interval,
            'ticks': self.ticks,
            'last_tick_at': int(self.last_tick_at) if self.last_tick_at else 0,
            'last_error': self.last_error,
        }
