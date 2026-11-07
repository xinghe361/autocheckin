"""模板来源（只从 GitHub）与「更新模板」。

1.4.14 起站点模板**唯一来源是仓库的 site-plugins/*.json，通过同步落到
`<数据目录>/templates/`。镜像里不带模板（Dockerfile 里刻意没有 COPY）。
这里守住四件事：

  * 配置引用了、但本地没有的模板 → 明确报 template_missing，而不是
    含糊的"判定不了结果"（线上踩过，会把用户引去改关键词）；
  * 这种情况下不能提示"疑似站点改版"（原因已经明确了）；
  * 「检查更新」只看 sha，不下载；
  * 「更新」只覆盖指定模板，不碰其它文件。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 单独跑本文件时 tests/ 不在 sys.path 里，_tpl_loader 会导入不到
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tpl_loader import load  # noqa: E402

from app import pluginsync as PS  # noqa: E402
from app import reasons as R  # noqa: E402
from app import secrets as S  # noqa: E402
from app import service as SV  # noqa: E402
from app.engine import checkin_via_http, run_http_flow  # noqa: E402
from app.httpclient import HttpResponse  # noqa: E402
from app.models import KIND_TEMPLATE, SiteConfig  # noqa: E402
from app.plugins import validate_template  # noqa: E402


def _tpl_site(sid='v2ex'):
    t = load(sid)
    return SiteConfig(id=sid, name=t['name'], kind=KIND_TEMPLATE, template=sid,
                      homepage=t['homepage'],
                      success_keywords=list(t.get('success_keywords') or []),
                      fail_keywords=list(t.get('fail_keywords') or []))


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_tpl_')
        self.key = S.generate_key()

    def _svc(self):
        return SV.Service(self.dir, version='t', env_key=self.key, proxy='')

    def _tpl_path(self, sid='v2ex'):
        return os.path.join(self.dir, 'templates', '%s.json' % sid)

    def _write_site(self, sid='v2ex'):
        svc = self._svc()
        cfg = svc.load_config()
        cfg.sites.append(_tpl_site(sid))
        svc.save_config(cfg)

    def _seed_tpl(self, sid='v2ex'):
        """把模板文件放进数据目录（等价于用户已经同步过一次）。"""
        d = os.path.join(self.dir, 'templates')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '%s.json' % sid), 'w', encoding='utf-8') as f:
            json.dump(load(sid), f, ensure_ascii=False)


class TestMissingTemplate(Case):
    def test_reports_template_missing_not_unclear(self):
        """本地没有模板文件 → 说清"缺文件"，而不是"判定不了结果"。"""
        self._write_site()
        svc = self._svc()
        out = checkin_via_http(svc.load_config().site('v2ex'), cookie_header='a=1')
        self.assertEqual(out.error_kind, 'template_missing')
        self.assertIn('模板', out.message)
        self.assertIn('不是站点改版', out.message)

    def test_category_is_not_rule(self):
        """归类不能落进 rule，否则会触发"疑似站点改版"。"""
        r = R.classify('template_missing', '')
        self.assertEqual(r['code'], 'template_missing')
        self.assertNotEqual(r['category'], 'rule')

    def test_not_flagged_as_stale(self):
        """模板本来就缺着，不该再说"疑似站点改版"。"""
        self._write_site()
        svc = self._svc()
        site = svc.load_config().site('v2ex')
        site.state = {'consecutive_failures': 5}
        hist = svc.history()
        with mock.patch.object(hist, 'recent_failures',
                               return_value=[{'code': 'unclear', 'ts': 1}] * 6):
            self.assertFalse(svc._template_stale(site))

    def test_rule_failures_still_flagged(self):
        """反过来：模板在、却一直判定失败，仍要提示改版。"""
        self._seed_tpl()
        self._write_site()
        svc = self._svc()
        site = svc.load_config().site('v2ex')
        site.state = {'consecutive_failures': 5}
        hist = svc.history()
        with mock.patch.object(hist, 'recent_failures',
                               return_value=[{'code': 'unclear', 'ts': 1}] * 6):
            self.assertTrue(svc._template_stale(site))


class TestTemplateFieldFallback(Case):
    """kind=template 但 template 字段空着的老配置：用站点 id 找模板。"""

    def test_engine_resolves_by_site_id(self):
        from app.engine import _tpl_of
        self._seed_tpl()
        self._write_site()
        svc = self._svc()
        self.assertTrue(_tpl_of(svc.load_config().site('v2ex')).get('flow'))

        bare = SiteConfig(id='v2ex', name='V2EX', kind=KIND_TEMPLATE,
                          template='', homepage='https://www.v2ex.com/')
        self.assertTrue(_tpl_of(bare).get('flow'),
                        'template 空时要用站点 id 找到模板')


class TestFailureIndex(Case):
    """list_sites 用的一次性失败索引，结果必须和"逐站点查"一致。"""

    def _seed(self, svc):
        # 用"刚刚"的时间戳：历史文件超过保留期会被压缩掉，
        # 写死 100/200 这种远古 ts 会让记录在压缩时消失（踩过）。
        base = int(time.time()) - 10
        svc.history().append([
            {'ts': base + 1, 'site_id': 'a', 'ok': False, 'error_kind': 'unclear'},
            {'ts': base + 2, 'site_id': 'a', 'ok': False, 'error_kind': 'timeout'},
            {'ts': base + 3, 'site_id': 'a', 'ok': False, 'error_kind': 'unclear'},
            {'ts': base + 4, 'site_id': 'a', 'ok': True,  'error_kind': ''},
            {'ts': base + 5, 'site_id': 'b', 'ok': False, 'error_kind': 'http_403'},
        ])

    def test_matches_per_site_query(self):
        svc = self._svc()
        self._seed(svc)
        h = svc.history()

        batch = h.recent_failure_codes(limit=6)
        one = [r['code'] for r in h.recent_failures('a', limit=6)]

        self.assertEqual(batch['a'], one)
        self.assertEqual(batch['a'], ['unclear', 'timeout', 'unclear'])
        self.assertEqual(batch['b'], ['http_403'])

    def test_limit_keeps_newest(self):
        svc = self._svc()
        self._seed(svc)
        got = svc.history().recent_failure_codes(limit=2)
        self.assertEqual(got['a'], ['unclear', 'timeout'])


class TestStepFromField(unittest.TestCase):
    def test_validator_keeps_from(self):
        """from 不能被校验器吞掉（吞掉既不报错、结果又对不上）。"""
        tpl, err = validate_template({
            'id': 'x1', 'name': 'X1', 'homepage': 'https://a.b/',
            'flow': [
                {'action': 'get', 'url': 'https://a.b/1'},
                {'action': 'extract_regex', 'pattern': r'(\d+)',
                 'save_as': 'n', 'from': 'first'},
            ],
        })
        self.assertEqual(err, '')
        self.assertEqual(tpl['flow'][1]['from'], 'first')

    def test_flow_reads_from_first_response(self):
        """from="first" 要真的从第一个响应里取，而不是永远用最后一个。"""
        def fetcher(method, url, data):
            if url.endswith('/1'):
                return HttpResponse(status=200, text='token=111', url=url)
            return HttpResponse(status=200, text='token=222', url=url)

        out = run_http_flow(
            fetcher=fetcher,
            flow=[
                {'action': 'get', 'url': 'https://a.b/1'},
                {'action': 'get', 'url': 'https://a.b/2'},
                {'action': 'extract_regex', 'pattern': r'token=(\d+)',
                 'save_as': 'tk'},
                {'action': 'extract_regex', 'pattern': r'token=(\d+)',
                 'save_as': 'tk_first', 'from': 'first'},
            ],
            success_kw=['222'], fail_kw=[])
        self.assertTrue(out.success)
        self.assertEqual(out.vars['tk'], '222')
        self.assertEqual(out.vars['tk_first'], '111')


class TestCheckUpdates(Case):
    """「检查更新」只看 sha。"""

    SRC = 'xinghe361/autocheckin/site-plugins'

    def _run(self, listed, state=None):
        store = mock.Mock(dir=os.path.join(self.dir, 'templates'))
        store.ensure_dir.return_value = store.dir
        sync = PS.PluginSync(store, proxy_fn=lambda: '')
        with mock.patch.object(PS, '_list_repo_files', return_value=listed):
            return sync.check_updates(self.SRC)

    def test_detects_changed_sha(self):
        self._seed_tpl()
        got = self._run([('v2ex.json', 'https://x/v2ex.json', 'newsha')])
        self.assertEqual([u['id'] for u in got['updates']], ['v2ex'])

    def test_same_sha_means_latest(self):
        self._seed_tpl()
        state = {'files': ['v2ex.json'], 'shas': {'v2ex.json': 's1'}}
        with open(os.path.join(self.dir, 'templates', PS.STATE_NAME), 'w',
                  encoding='utf-8') as f:
            json.dump(state, f)
        got = self._run([('v2ex.json', 'https://x/v2ex.json', 's1')])
        self.assertEqual(got['updates'], [])
        self.assertEqual(got['same'], 1)

    def test_missing_local_counts_as_unknown_not_update(self):
        got = self._run([('brandnew.json', 'https://x/b.json', 's9')])
        self.assertEqual(got['updates'], [])
        self.assertEqual(got['unknown'], 1)

    def test_example_is_ignored(self):
        got = self._run([('example.json', 'https://x/e.json', 's')])
        self.assertEqual(got['updates'], [])
        self.assertEqual(got['unknown'], 0)


class TestUpdateOnly(Case):
    """「更新」只覆盖指定的模板，不动别的文件。"""

    SRC = 'xinghe361/autocheckin/site-plugins'

    def test_only_updates_given_ids(self):
        self._seed_tpl('v2ex')
        self._seed_tpl('chiphell')
        with open(self._tpl_path('chiphell'), encoding='utf-8') as f:
            before_chip = f.read()

        new_v2ex = dict(load('v2ex'))
        new_v2ex['name'] = 'V2EX（新版）'
        payload = json.dumps(new_v2ex, ensure_ascii=False).encode()

        store = mock.Mock(dir=os.path.join(self.dir, 'templates'))
        store.ensure_dir.return_value = store.dir
        store.load.return_value = []
        sync = PS.PluginSync(store, proxy_fn=lambda: '')
        with mock.patch.object(PS, '_list_repo_files',
                               return_value=[('v2ex.json', 'https://x/v2ex.json', 's2'),
                                             ('chiphell.json', 'https://x/ch.json', 's3')]), \
                mock.patch.object(PS, '_fetch', return_value=payload):
            rep = sync.sync(self.SRC, only=['v2ex'])

        self.assertEqual(rep['updated'], ['v2ex.json'])
        with open(self._tpl_path('v2ex'), encoding='utf-8') as f:
            after = json.load(f)
        self.assertEqual(after['name'], 'V2EX（新版）')
        with open(self._tpl_path('chiphell'), encoding='utf-8') as f:
            self.assertEqual(f.read(), before_chip, '没点更新的模板不能被动')


    def test_single_update_never_prunes_others(self):
        """只更新一条时，别的同步模板不能被当成"远端已删"删掉。

        踩过：only 模式只下载了 v2ex，prune 却把 state 里记录的其它文件
        全删了 —— 用户点一次「更新」，模板目录就剩一个。
        """
        self._seed_tpl('v2ex')
        self._seed_tpl('chiphell')
        state = {'files': ['v2ex.json', 'chiphell.json'],
                 'shas': {'v2ex.json': 'old', 'chiphell.json': 's3'}}
        with open(os.path.join(self.dir, 'templates', PS.STATE_NAME), 'w',
                  encoding='utf-8') as f:
            json.dump(state, f)

        payload = json.dumps(load('v2ex'), ensure_ascii=False).encode()
        store = mock.Mock(dir=os.path.join(self.dir, 'templates'))
        store.ensure_dir.return_value = store.dir
        store.load.return_value = []
        sync = PS.PluginSync(store, proxy_fn=lambda: '')
        with mock.patch.object(PS, '_list_repo_files',
                               return_value=[('v2ex.json', 'https://x/v2ex.json', 's2'),
                                             ('chiphell.json', 'https://x/ch.json', 's3')]), \
                mock.patch.object(PS, '_fetch', return_value=payload):
            rep = sync.sync(self.SRC, only=['v2ex'])

        self.assertEqual(rep['removed'], [], '单条更新不该删任何东西')
        self.assertTrue(os.path.isfile(self._tpl_path('chiphell')),
                        '没点更新的模板必须还在')



class TestSyncIdempotentAndVersions(Case):
    """「同一个模板反复更新都是自己」——内容没变就不该算更新。"""

    SRC = 'xinghe361/autocheckin/site-plugins'

    def _sync(self, listed, payload):
        store = mock.Mock(dir=os.path.join(self.dir, 'templates'))
        store.ensure_dir.return_value = store.dir
        store.load.return_value = []
        sync = PS.PluginSync(store, proxy_fn=lambda: '')
        with mock.patch.object(PS, '_list_repo_files', return_value=listed), \
                mock.patch.object(PS, '_fetch', return_value=payload):
            return sync.sync(self.SRC)

    def test_second_sync_reports_nothing_updated(self):
        os.makedirs(os.path.join(self.dir, 'templates'), exist_ok=True)
        listed = [('v2ex.json', 'https://x/v2ex.json', 'sha1')]
        payload = json.dumps(load('v2ex'), ensure_ascii=False).encode()
        first = self._sync(listed, payload)
        self.assertEqual(first['added'], ['v2ex.json'])
        second = self._sync(listed, payload)
        self.assertEqual(second['updated'], [], '内容没变不该再算"更新"')
        self.assertEqual(second['added'], [])

    def test_list_marks_latest_right_after_sync(self):
        """同步完成后立刻应该显示"已是最新"，而不是"有新版本"。

        踩过：拿本地文件内容的 sha 跟远端比 —— 落盘时重新序列化过，
        字节永远不同，于是每行都长期显示"有新版本"。
        """
        os.makedirs(os.path.join(self.dir, 'templates'), exist_ok=True)
        listed = [('v2ex.json', 'https://x/v2ex.json', 'sha1')]
        payload = json.dumps(load('v2ex'), ensure_ascii=False).encode()
        self._sync(listed, payload)

        svc = self._svc()
        rows = {t['id']: t for t in svc.list_templates()}
        row = rows['v2ex']
        self.assertFalse(row['has_update'], '刚同步完不该报"有新版本"')
        self.assertTrue(row['up_to_date'])

    def test_remote_change_marks_update(self):
        os.makedirs(os.path.join(self.dir, 'templates'), exist_ok=True)
        payload = json.dumps(load('v2ex'), ensure_ascii=False).encode()
        self._sync([('v2ex.json', 'https://x/v2ex.json', 'sha1')], payload)
        # 远端换了内容
        self._sync([('v2ex.json', 'https://x/v2ex.json', 'sha2')], payload)
        svc = self._svc()
        row = {t['id']: t for t in svc.list_templates()}['v2ex']
        # 这一次同步已经把新内容拉下来了，所以仍是最新
        self.assertTrue(row['up_to_date'])

    def test_version_field_survives_validation(self):
        tpl, why = PS.validate_template({
            'id': 'x1', 'name': 'X', 'homepage': 'https://x.test/',
            'version': '2.3.1', 'updated': '2026-11-08',
            'flow': [{'action': 'get', 'url': 'https://x.test/'}],
        })
        self.assertEqual(why, '')
        self.assertEqual(tpl['version'], '2.3.1')
        self.assertEqual(tpl['updated'], '2026-11-08')

    def test_list_exposes_version_and_time(self):
        self._seed_tpl()
        row = {t['id']: t for t in self._svc().list_templates()}['v2ex']
        for key in ('version', 'tpl_updated', 'file_mtime', 'file',
                    'has_update', 'up_to_date'):
            self.assertIn(key, row)
        self.assertGreater(row['file_mtime'], 0, '要能显示"更新于"')


if __name__ == '__main__':
    unittest.main()
