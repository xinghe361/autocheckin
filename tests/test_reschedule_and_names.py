"""两个真实缺陷的回归测试：

1) 改了调度设置后，已排的 next_run_at 没有作废 —— 导致"设置看起来没生效"。
   实测症状：站点从"每天固定 03:00"改成"以【上次签到成功时间】为基准、
   锚点 09:35"之后，界面与实际调度仍停在 03:00，锚点要等第一次签到成功才生效。
   根因：runner.ensure_scheduled() 只在 next_run_at **为空**时才计算。

2) 内置站点的显示名带"每日铜币"这类后缀。用户要求直接用站点本名，
   并且**已有站点也要跟着改**（名字存在 config.json 里，改模板不影响旧站点）。
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S                                     # noqa: E402
from app import templates as T                                   # noqa: E402
from app.models import SiteConfig                                # noqa: E402
from app.service import SCHEDULE_FIELDS, Service                 # noqa: E402

NOW = int(datetime(2026, 10, 5, 23, 59).timestamp())


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')


class TestRescheduleOnConfigChange(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_resched_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')
        self.svc.upsert_site({
            'id': 'chiphell', 'name': 'Chiphell', 'template': 'chiphell',
            'kind': 'template', 'homepage': 'https://www.chiphell.com/',
            'mode': 'daily', 'daily_hour': 3, 'daily_minute': 0,
            'enabled': True})

    def _pin_next(self, ts):
        st = self.svc.load_state()
        st.set_next_run_at('chiphell', ts)
        self.svc.store.save_state(st)

    def _next(self):
        return self.svc.load_state().next_run_at('chiphell')

    def test_mode_change_reschedules_immediately(self):
        """改模式必须立刻按新配置重算（而不是沿用旧的 03:00）。

        这里用"锚点 09:35"来断言结果落在锚点附近 —— 若沿用旧值就会是 03:00。
        """
        old = int(datetime(2026, 10, 6, 3, 0).timestamp())
        self._pin_next(old)
        self.svc.upsert_site({'id': 'chiphell', 'template': 'chiphell',
                              'kind': 'template', 'mode': 'success_based',
                              'success_anchor_minutes': 9 * 60 + 35,
                              'success_interval_minutes': 1440,
                              'jitter_enabled': True, 'jitter_seconds': 3600})
        got = self._next()
        self.assertNotEqual(got, old, '不能沿用改设置前的旧时间')
        self.assertGreater(got, 0, '应立刻算出新时间，而不是留空')
        anchor = int(datetime(2026, 10, 6, 9, 35).timestamp())
        self.assertLessEqual(abs(got - anchor), 3600,
                             '应落在锚点 09:35 ± 60 分钟内')

    def test_anchor_change_reschedules(self):
        self.svc.upsert_site({'id': 'chiphell', 'template': 'chiphell',
                              'kind': 'template', 'mode': 'success_based',
                              'success_anchor_minutes': 9 * 60 + 35,
                              'success_interval_minutes': 1440})
        before = self._next()
        self.svc.upsert_site({'id': 'chiphell',
                              'success_anchor_minutes': 23 * 60})
        self.assertNotEqual(self._next(), before, '改锚点应重算')

    def test_scheduling_fields_trigger_recompute(self):
        for patch in ({'success_interval_minutes': 720},
                      {'jitter_enabled': True},
                      {'jitter_seconds': 1800},
                      {'daily_hour': 9},
                      {'daily_minute': 30}):
            with self.subTest(patch=patch):
                self._pin_next(NOW + 3600)
                self.svc.upsert_site(dict({'id': 'chiphell'}, **patch))
                self.assertNotEqual(self._next(), NOW + 3600,
                                    '这些字段变化都该重算：%s' % patch)

    def test_disable_clears_schedule(self):
        self._pin_next(NOW + 3600)
        self.svc.upsert_site({'id': 'chiphell', 'enabled': False})
        self.assertEqual(self._next(), 0, '停用后不该留着排期')

    def test_unrelated_change_does_not_reschedule(self):
        """改不相关字段不该把下次时间往后推（否则会被无限顺延）。"""
        pinned = int(datetime(2026, 10, 6, 3, 0).timestamp())
        self._pin_next(pinned)
        self.svc.upsert_site({'id': 'chiphell', 'name': '我的名字',
                              'homepage': 'https://www.chiphell.com/',
                              'success_keywords': ['ok']})
        self.assertEqual(self._next(), pinned,
                         '只改名字/关键词不该重新排期')

    def test_rescheduled_result_lands_on_anchor(self):
        """重算结果必须落在锚点 ± 随机范围内，且不在过去。"""
        self._pin_next(int(datetime(2026, 10, 6, 3, 0).timestamp()))
        self.svc.upsert_site({'id': 'chiphell', 'template': 'chiphell',
                              'kind': 'template', 'mode': 'success_based',
                              'success_anchor_minutes': 9 * 60 + 35,
                              'success_interval_minutes': 1440,
                              'jitter_enabled': True, 'jitter_seconds': 3600})
        got = self._next()
        anchor = int(datetime(2026, 10, 6, 9, 35).timestamp())
        self.assertLessEqual(abs(got - anchor), 3600,
                             '实际 %s' % fmt(got))
        self.assertGreater(got, NOW, '不能排到过去')

    def test_schedule_fields_list_is_sane(self):
        """字段名必须都是 SiteConfig 上真实存在的属性（防止写错名字）。"""
        s = SiteConfig(id='x', name='X')
        for k in SCHEDULE_FIELDS:
            self.assertTrue(hasattr(s, k), 'SiteConfig 没有字段 %s' % k)


class TestPlainSiteNames(unittest.TestCase):
    def test_builtin_templates_use_plain_names(self):
        names = {t['id']: t['name'] for t in T.BUILTIN}
        self.assertEqual(names['v2ex'], 'V2EX')
        self.assertEqual(names['nodeseek'], 'NodeSeek')
        self.assertEqual(names['chiphell'], 'Chiphell')

    def test_no_decorative_suffix(self):
        for t in T.BUILTIN:
            for bad in ('每日', '铜币', '邪恶值', '试试手气', '签到'):
                with self.subTest(t=t['id'], bad=bad):
                    self.assertNotIn(bad, t['name'],
                                     '站点名不该带装饰性后缀')

    def test_migration_renames_old_defaults(self):
        class C:
            pass
        c = C()
        c.sites = [SiteConfig(id='v2ex', name='V2EX 每日铜币', template='v2ex'),
                   SiteConfig(id='nodeseek', name='NodeSeek 试试手气',
                              template='nodeseek'),
                   SiteConfig(id='chiphell', name='Chiphell 每日邪恶值',
                              template='chiphell')]
        self.assertEqual(T.migrate_site_names(c), 3)
        self.assertEqual([s.name for s in c.sites],
                         ['V2EX', 'NodeSeek', 'Chiphell'])

    def test_migration_keeps_custom_names(self):
        """用户自己改过的名字绝不能被覆盖。"""
        class C:
            pass
        c = C()
        c.sites = [SiteConfig(id='v2ex', name='我的 V2EX', template='v2ex'),
                   SiteConfig(id='x', name='V2EX 每日铜币', template=''),
                   SiteConfig(id='y', name='V2EX 每日铜币', template='other')]
        self.assertEqual(T.migrate_site_names(c), 0)
        self.assertEqual([s.name for s in c.sites],
                         ['我的 V2EX', 'V2EX 每日铜币', 'V2EX 每日铜币'])

    def test_migration_is_idempotent(self):
        class C:
            pass
        c = C()
        c.sites = [SiteConfig(id='v2ex', name='V2EX 每日铜币', template='v2ex')]
        T.migrate_site_names(c)
        self.assertEqual(T.migrate_site_names(c), 0, '第二次不该再改')

    def test_load_config_applies_migration(self):
        """界面读到的名字必须已经是新名字（不只是文件里改了）。"""
        d = tempfile.mkdtemp(prefix='ac_names_')
        svc = Service(d, version='t', env_key=S.generate_key(), proxy='')
        svc.upsert_site({'id': 'v2ex', 'name': 'V2EX 每日铜币',
                         'template': 'v2ex', 'kind': 'template',
                         'homepage': 'https://www.v2ex.com/'})
        self.assertEqual(svc.load_config().site('v2ex').name, 'V2EX')
        # 自定义名字不受影响
        svc.upsert_site({'id': 'v2ex', 'name': '我自己起的'})
        self.assertEqual(svc.load_config().site('v2ex').name, '我自己起的')


if __name__ == '__main__':
    unittest.main(verbosity=2)
