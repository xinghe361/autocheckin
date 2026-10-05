"""站点模板同步：手动立即、自动到点（设定时刻 + 间隔天数 + 开关）。

用户需求原文：
    「没有做"从 GitHub 自动同步插件" 做一个机制 手动就是立即
      自动可以设定几点拉一次默认间隔为天 自动加上开关」

不联网：所有网络访问都在测试里被替换掉（真实的 GitHub 同步另行手工验证过）。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S                                     # noqa: E402
from app import templates as T                                   # noqa: E402
from app.plugins import PluginStore                              # noqa: E402
from app.pluginsync import (SyncError, parse_hhmm,               # noqa: E402
                            parse_source, sync_due, PluginSync)
from app.schedule import _at_local_time                          # noqa: E402
from app.service import Service                                  # noqa: E402

GOOD = {
    'id': 'remote1', 'name': '远端站点', 'homepage': 'https://a.b/',
    'flow': [{'action': 'get', 'url': '{homepage}'}],
    'success_keywords': ['ok'], 'headers': {},
}


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, 0).timestamp())


class TestParseSource(unittest.TestCase):
    def test_shorthand(self):
        self.assertEqual(parse_source('a/b'),
                         ('api', 'a/b', ''))
        self.assertEqual(parse_source('a/b/sub/dir'),
                         ('api', 'a/b', 'sub/dir'))

    def test_github_url(self):
        self.assertEqual(parse_source('https://github.com/a/b'),
                         ('api', 'a/b', ''))
        self.assertEqual(
            parse_source('https://github.com/a/b/tree/main/plug'),
            ('api', 'a/b', 'plug'))

    def test_raw_url(self):
        kind, url, _ = parse_source('https://raw.githubusercontent.com/a/b/m/x.json')
        self.assertEqual(kind, 'raw')
        self.assertTrue(url.startswith('https://'))

    def test_other_https_is_manifest(self):
        kind, _, _ = parse_source('https://example.com/p.json')
        self.assertEqual(kind, 'manifest')

    def test_http_rejected(self):
        """明文 http 必须拒绝 —— 模板内容会被拿去发请求，可被中途篡改。"""
        for bad in ('http://github.com/a/b', 'http://example.com/x.json',
                    'http://raw.githubusercontent.com/a/b/m/x.json'):
            with self.subTest(bad=bad):
                with self.assertRaises(SyncError):
                    parse_source(bad)

    def test_garbage_rejected(self):
        for bad in ('', '   ', 'nonsense', 'javascript:alert(1)',
                    'a', '///', 'ftp://x/y'):
            with self.subTest(bad=bad):
                with self.assertRaises(SyncError):
                    parse_source(bad)


    def test_credentials_in_url_rejected(self):
        """URL 里带账密/令牌必须拒绝。

        两个原因，缺一不可：
          1) **不起作用** —— 用 urllib 直接取，它不会把 URL 里的 user:pass
             变成 Basic 认证头，所以令牌等于填了个寂寞（实测过：
             以前是静默忽略，用户以为私有仓库能用）。
          2) **会泄露** —— 这个地址存进 config.json，还会进 WebDAV 备份。
        """
        for bad in ('https://user:tok@github.com/a/b',
                    'https://ghp_TOKEN@github.com/a/b',
                    'https://u:p@example.com/p.json'):
            with self.subTest(bad=bad):
                with self.assertRaises(SyncError) as cm:
                    parse_source(bad)
                self.assertIn('令牌', str(cm.exception)
                              + '账号密码')   # 提示要说清原因

    def test_shorthand_dotdot_rejected(self):
        """简写里的 .. / . 必须拒绝。

        owner/repo 会被拼进 api.github.com 的 URL 路径。虽然目前只是让 API
        404、不构成 SSRF，但"路径里能塞 .."是不该留的形状 ——
        将来若改了 URL 拼法就可能真的穿越出去。
        """
        for bad in ('../../x', '../..', 'a/..', './a', 'a/./b',
                    'a/b/../../c'):
            with self.subTest(bad=bad):
                with self.assertRaises(SyncError):
                    parse_source(bad)

    def test_plain_forms_still_work(self):
        self.assertEqual(parse_source('me/repo'), ('api', 'me/repo', ''))
        self.assertEqual(parse_source('https://github.com/me/repo'),
                         ('api', 'me/repo', ''))


class TestStripUrlSecrets(unittest.TestCase):
    """写进备份前再剥一层（防御性）。"""

    def test_removes_userinfo_and_query(self):
        from app.store import _strip_url_secrets
        self.assertEqual(_strip_url_secrets('https://u:p@h/x'),
                         'https://h/x')
        self.assertEqual(_strip_url_secrets('https://h/x?token=S'),
                         'https://h/x')
        self.assertEqual(_strip_url_secrets('https://h/x#frag'),
                         'https://h/x')

    def test_keeps_plain_values(self):
        from app.store import _strip_url_secrets
        for s in ('me/repo', 'me/repo/sub', 'https://github.com/me/repo', ''):
            with self.subTest(s=s):
                self.assertEqual(_strip_url_secrets(s), s)

    def test_at_in_path_not_treated_as_userinfo(self):
        """路径里的 @ 不能被误当成账密（否则会把路径截坏）。"""
        from app.store import _strip_url_secrets
        self.assertEqual(_strip_url_secrets('https://h/a@b/c.json'),
                         'https://h/a@b/c.json')


class TestExportDoesNotLeakSource(unittest.TestCase):
    def test_backup_source_has_no_token(self):
        import tempfile as _tf
        d = _tf.mkdtemp(prefix='ac_exp_')
        svc = Service(d, version='t', env_key=S.generate_key(), proxy='')
        cfg = svc.load_config()
        cfg.plugin_sync_source = 'https://h/p.json?token=SECRET123'
        cfg.plugin_sync_enabled = True
        svc.save_config(cfg)
        exp = svc.store.export_config(svc.load_config())
        self.assertNotIn('SECRET123', str(exp.get('plugin_sync_source')))

    def test_sync_result_in_state_has_no_source(self):
        """同步结果里不能存来源地址。

        state.json 的内容会**原样**进 WebDAV 备份（backup_now 直接传
        state.to_dict()），所以来源若带 ?token=... 就会跟着泄露。
        界面要显示来源时从配置读就行（不需要存在 state 里）。
        """
        import tempfile as _tf
        import app.pluginsync as PS
        d = _tf.mkdtemp(prefix='ac_state_')
        svc = Service(d, version='t', env_key=S.generate_key(), proxy='')
        cfg = svc.load_config()
        cfg.plugin_sync_source = 'https://h/p.json?token=SECRET123'
        svc.save_config(cfg)
        orig = PS._fetch
        PS._fetch = lambda url, proxy='', limit=None: json.dumps([GOOD]).encode()
        try:
            svc.sync_plugins()
        finally:
            PS._fetch = orig
        state = svc.load_state()
        blob = json.dumps(state.to_dict(), ensure_ascii=False)
        self.assertNotIn('SECRET123', blob, 'state 里不该出现来源里的令牌')
        self.assertNotIn('h/p.json', blob, 'state 里不该出现来源地址')
        # 但同步时间与结果概要要留着（界面/调度要用）
        res = state.get('_plugin_sync_result') or {}
        self.assertTrue(res.get('ok'))
        self.assertEqual(res.get('added'), 1)


class TestParseHhmm(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_hhmm('03:00'), (3, 0))
        self.assertEqual(parse_hhmm('9:35'), (9, 35))
        self.assertEqual(parse_hhmm('23:59'), (23, 59))
        self.assertEqual(parse_hhmm(' 7 : 5 '), (7, 5))
        self.assertEqual(parse_hhmm('3：00'), (3, 0))   # 全角冒号

    def test_invalid_falls_back(self):
        """不合法要回默认值，绝不能抛 —— 用户随便填不该让设置存不下去。"""
        for bad in ('', None, 'abc', '24:00', '99:99', '12:60', '-1:00'):
            with self.subTest(bad=bad):
                self.assertEqual(parse_hhmm(bad), (3, 0))


class TestSyncDue(unittest.TestCase):
    """到点判断：这是用户明确描述的行为，逐条钉住。"""

    def test_disabled_never_due(self):
        due, _ = sync_due(ts(2026, 6, 15, 10), 0, False, 'a/b', '03:00', 1,
                          _at_local_time)
        self.assertFalse(due, '关掉开关就不该自动同步')

    def test_no_source_never_due(self):
        due, _ = sync_due(ts(2026, 6, 15, 10), 0, True, '  ', '03:00', 1,
                          _at_local_time)
        self.assertFalse(due, '没填来源就不该自动同步')

    def test_never_synced_is_due_immediately(self):
        """刚打开开关就先拉一次 —— 否则要等到明天 3 点，很反直觉。"""
        due, _ = sync_due(ts(2026, 6, 15, 10), 0, True, 'a/b', '03:00', 1,
                          _at_local_time)
        self.assertTrue(due)

    def test_not_due_before_interval(self):
        last = ts(2026, 6, 15, 3)
        now = ts(2026, 6, 15, 23)          # 才过 20 小时，间隔是 1 天
        due, nxt = sync_due(now, last, True, 'a/b', '03:00', 1, _at_local_time)
        self.assertFalse(due)
        self.assertEqual(nxt, last + 86400)

    def test_due_after_interval_and_time_passed(self):
        last = ts(2026, 6, 14, 3)
        now = ts(2026, 6, 15, 4)           # 过了 25 小时，且今天 3 点已过
        due, _ = sync_due(now, last, True, 'a/b', '03:00', 1, _at_local_time)
        self.assertTrue(due)

    def test_waits_for_configured_hour(self):
        """间隔到了但当天那个时刻还没到 -> 等到那个时刻。"""
        last = ts(2026, 6, 14, 1)
        now = ts(2026, 6, 15, 2)           # 今天 3 点还没到
        due, nxt = sync_due(now, last, True, 'a/b', '03:00', 1, _at_local_time)
        self.assertFalse(due)
        self.assertEqual(nxt, ts(2026, 6, 15, 3))

    def test_interval_days_respected(self):
        """间隔 3 天：第 2 天不该拉，第 3 天后才拉。"""
        last = ts(2026, 6, 10, 3)
        due2, _ = sync_due(ts(2026, 6, 12, 5), last, True, 'a/b', '03:00', 3,
                           _at_local_time)
        self.assertFalse(due2, '第 2 天不该同步（间隔 3 天）')
        due3, _ = sync_due(ts(2026, 6, 13, 5), last, True, 'a/b', '03:00', 3,
                           _at_local_time)
        self.assertTrue(due3, '第 3 天后应同步')

    def test_interval_minimum_one_day(self):
        """间隔填 0 或负数按 1 天处理（防止变成每分钟都拉）。"""
        last = ts(2026, 6, 15, 3)
        due, _ = sync_due(ts(2026, 6, 15, 4), last, True, 'a/b', '03:00', 0,
                          _at_local_time)
        self.assertFalse(due)


class _FakeFetch:
    """替换 pluginsync._fetch，避免测试联网。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, url, proxy='', limit=None):
        self.calls.append(url)
        if url not in self.mapping:
            raise SyncError('测试里没有准备这个地址：%s' % url)
        v = self.mapping[url]
        if isinstance(v, Exception):
            raise v
        return v if isinstance(v, bytes) else json.dumps(v).encode()


class TestPluginSync(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_sync_')
        self.store = PluginStore(self.dir)
        self.sync = PluginSync(self.store)
        import app.pluginsync as PS
        self._orig = PS._fetch
        self.calls = []
        self.fetch = _FakeFetch({})
        PS._fetch = self.fetch

    def tearDown(self):
        import app.pluginsync as PS
        PS._fetch = self._orig
        T.set_plugin_provider(None)

    def test_sync_single_file(self):
        self.fetch.mapping['https://example.com/p.json'] = [GOOD]
        rep = self.sync.sync('https://example.com/p.json')
        self.assertEqual(rep['added'], ['remote1.json'])
        self.assertEqual(rep['errors'], [])
        self.assertEqual([t['id'] for t in self.store.load(force=True)],
                         ['remote1'])

    def test_sync_github_folder(self):
        """模拟 GitHub API 列目录 + 逐个下载 raw。"""
        self.fetch.mapping[
            'https://api.github.com/repos/a/b/contents/plug'] = [
            {'type': 'file', 'name': 'one.json',
             'download_url': 'https://raw.githubusercontent.com/a/b/m/one.json'},
            {'type': 'file', 'name': 'readme.md',      # 非 json，应被跳过
             'download_url': 'https://raw.githubusercontent.com/a/b/m/r.md'},
        ]
        self.fetch.mapping[
            'https://raw.githubusercontent.com/a/b/m/one.json'] = [GOOD]
        rep = self.sync.sync('a/b/plug')
        self.assertEqual(rep['added'], ['remote1.json'])
        # 不该去下载 md
        self.assertNotIn('https://raw.githubusercontent.com/a/b/m/r.md',
                         self.fetch.calls)

    def test_second_sync_is_update_not_add(self):
        self.fetch.mapping['https://example.com/p.json'] = [GOOD]
        self.sync.sync('https://example.com/p.json')
        rep = self.sync.sync('https://example.com/p.json')
        self.assertEqual(rep['added'], [])
        self.assertEqual(rep['updated'], ['remote1.json'])

    def test_invalid_remote_template_rejected(self):
        """远端内容再离谱也只能"被拒绝"，不会写进本地。"""
        bad = dict(GOOD, id='BAD')                       # 大写 id
        self.fetch.mapping['https://example.com/p.json'] = [bad]
        rep = self.sync.sync('https://example.com/p.json')
        self.assertEqual(rep['added'], [])
        self.assertTrue(rep['errors'])

    def test_header_injection_from_remote_rejected(self):
        bad = dict(GOOD, headers={'X': 'a\r\nEvil: 1'})
        self.fetch.mapping['https://example.com/p.json'] = [bad]
        rep = self.sync.sync('https://example.com/p.json')
        self.assertEqual(rep['added'], [])
        self.assertTrue(any('换行' in e['reason'] for e in rep['errors']))

    def test_prune_removes_only_previously_synced(self):
        """远端删掉的文件要清理，但用户手放的文件绝不能被删。"""
        self.store.ensure_dir()
        mine = os.path.join(self.store.dir, 'mine.json')
        with open(mine, 'w', encoding='utf-8') as f:
            json.dump(dict(GOOD, id='mine'), f)
        self.fetch.mapping['https://example.com/p.json'] = [GOOD]
        self.sync.sync('https://example.com/p.json')
        # 远端这次不再提供 remote1
        self.fetch.mapping['https://example.com/p.json'] = [
            dict(GOOD, id='remote2')]
        rep = self.sync.sync('https://example.com/p.json')
        self.assertEqual(rep['removed'], ['remote1.json'])
        ids = {t['id'] for t in self.store.load(force=True)}
        self.assertIn('mine', ids, '用户手放的模板被误删了')
        self.assertIn('remote2', ids)

    def test_all_fetches_failing_raises(self):
        """一个文件都下载不到 -> 整体失败（不能报成功）。

        否则来源写错/仓库私有/网络不通时，界面和自动同步日志都显示
        "一切正常"，用户不知道模板没同步过来。
        """
        self.fetch.mapping['https://example.com/p.json'] = SyncError('boom')
        with self.assertRaises(SyncError):
            self.sync.sync('https://example.com/p.json')

    def test_partial_failure_still_succeeds_for_good_files(self):
        """部分下载失败时，好的文件仍要写进来，失败的在 errors 里列出。"""
        self.fetch.mapping['https://api.github.com/repos/a/b/contents/x'] = [
            {'type': 'file', 'name': 'ok.json',
             'download_url': 'https://raw.githubusercontent.com/a/b/m/ok.json'},
            {'type': 'file', 'name': 'bad.json',
             'download_url': 'https://raw.githubusercontent.com/a/b/m/bad.json'},
        ]
        self.fetch.mapping[
            'https://raw.githubusercontent.com/a/b/m/ok.json'] = [GOOD]
        self.fetch.mapping[
            'https://raw.githubusercontent.com/a/b/m/bad.json'] = SyncError('nope')
        rep = self.sync.sync('a/b/x')
        self.assertEqual(rep['added'], ['remote1.json'])
        self.assertTrue(any(e['file'] == 'bad.json' for e in rep['errors']))

    def test_file_count_limit(self):
        many = [{'type': 'file', 'name': 'f%d.json' % i,
                 'download_url': 'https://raw.githubusercontent.com/a/b/m/f%d.json' % i}
                for i in range(80)]
        self.fetch.mapping['https://api.github.com/repos/a/b/contents/x'] = many
        with self.assertRaises(SyncError):
            self.sync.sync('a/b/x')


class TestServiceSync(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_svcsync_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')
        import app.pluginsync as PS
        self._orig = PS._fetch
        self.fetch = _FakeFetch({'https://example.com/p.json': [GOOD]})
        PS._fetch = self.fetch

    def tearDown(self):
        import app.pluginsync as PS
        PS._fetch = self._orig
        T.set_plugin_provider(None)

    def test_manual_sync_is_immediate(self):
        """手动同步不看时刻、不看间隔 —— 点了就拉。"""
        rep = self.svc.sync_plugins('https://example.com/p.json')
        self.assertTrue(rep['ok'])
        self.assertEqual(rep['added'], ['remote1.json'])

    def test_empty_source_raises(self):
        with self.assertRaises(SyncError):
            self.svc.sync_plugins('')

    def test_auto_skips_when_disabled(self):
        r = self.svc.maybe_sync_plugins()
        self.assertTrue(r.get('skipped'))
        self.assertEqual(self.fetch.calls, [], '关闭时不该发任何请求')

    def test_auto_runs_once_then_respects_interval(self):
        cfg = self.svc.load_config()
        cfg.plugin_sync_enabled = True
        cfg.plugin_sync_source = 'https://example.com/p.json'
        cfg.plugin_sync_interval_days = 1
        self.svc.save_config(cfg)
        r1 = self.svc.maybe_sync_plugins()
        self.assertTrue(r1.get('ok'), '首次应立刻同步：%s' % r1)
        n = len(self.fetch.calls)
        r2 = self.svc.maybe_sync_plugins()
        self.assertTrue(r2.get('skipped'), '间隔没到不该再拉')
        self.assertEqual(len(self.fetch.calls), n, '不该多发请求')

    def test_auto_failure_does_not_raise(self):
        """同步失败绝不能让调度崩掉（否则签到也跟着停）。"""
        cfg = self.svc.load_config()
        cfg.plugin_sync_enabled = True
        cfg.plugin_sync_source = 'https://example.com/bad.json'
        self.svc.save_config(cfg)
        self.fetch.mapping['https://example.com/bad.json'] = SyncError('boom')
        r = self.svc.maybe_sync_plugins()
        self.assertFalse(r.get('ok'))
        self.assertIn('boom', str(r.get('error')))

    def test_status_reports_config(self):
        cfg = self.svc.load_config()
        cfg.plugin_sync_enabled = True
        cfg.plugin_sync_source = 'a/b/plug'
        cfg.plugin_sync_time = '09:35'
        cfg.plugin_sync_interval_days = 2
        self.svc.save_config(cfg)
        st = self.svc.plugin_sync_status()
        self.assertTrue(st['enabled'])
        self.assertEqual(st['source'], 'a/b/plug')
        self.assertEqual(st['time'], '09:35')
        self.assertEqual(st['interval_days'], 2)

    def test_installed_after_sync_needs_explicit_install(self):
        """同步进来的模板只是"可选"，必须显式加入才是站点。"""
        self.svc.sync_plugins('https://example.com/p.json')
        ids = [s.id for s in self.svc.load_config().sites]
        self.assertNotIn('remote1', ids, '同步不该自动变成签到站点')
        self.svc.install_template('remote1')
        ids = [s.id for s in self.svc.load_config().sites]
        self.assertIn('remote1', ids)


if __name__ == '__main__':
    unittest.main(verbosity=2)
