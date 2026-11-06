"""模板自愈与"模板缺失"报错。

背景（线上踩到的）：1.4.14 把内置模板外置成 site-plugins/*.json 之后，
老用户升级上来，config.json 里还引用 v2ex/nodeseek/chiphell，
但数据目录 templates/ 是空的。于是：
    template_by_id() 返回 {} → 流程为空 → 一个请求都不发
    → 结果必然是 unclear（"判定不了结果"）
    → 界面再据此提示"疑似站点改版"，把人引去改关键词、跑 AI，完全治错方向。

这里守住三件事：
    1) 启动时把"配置里引用、但文件已缺失"的模板补回来（绝不覆盖已有文件）；
    2) 补不回来时报 template_missing，并且归类不算"签到规则"类；
    3) 这种站点不该被标成"疑似站点改版"。
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


class SelfHealCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_selfheal_')
        self.key = S.generate_key()

    def _svc(self):
        return SV.Service(self.dir, version='t', env_key=self.key, proxy='')

    def _tpl_path(self, sid='v2ex'):
        return os.path.join(self.dir, 'templates', '%s.json' % sid)

    def _write_site(self):
        svc = self._svc()
        cfg = svc.load_config()
        cfg.sites.append(_tpl_site())
        svc.save_config(cfg)

    def _wipe_templates(self):
        import shutil
        shutil.rmtree(os.path.join(self.dir, 'templates'), ignore_errors=True)


class TestRestoreOnStartup(SelfHealCase):
    def test_missing_template_is_restored(self):
        """升级后 templates/ 空着：重启一次就该自动补回来。"""
        self._write_site()
        self._wipe_templates()

        svc = self._svc()                       # 相当于容器重启

        self.assertTrue(os.path.isfile(self._tpl_path()), '模板没被补回')
        self.assertTrue(svc.load_config().site('v2ex'))
        from app import templates as T
        tpl = T.template_by_id('v2ex')
        self.assertTrue(tpl.get('flow'), '补回来的模板应当有可执行流程')

    def test_existing_file_is_never_overwritten(self):
        """用户手改过/同步到新版的文件，绝不能被内置的那份冲掉。"""
        self._write_site()
        os.makedirs(os.path.join(self.dir, 'templates'), exist_ok=True)
        custom = dict(load('v2ex'))
        custom['name'] = '我自己改过的 V2EX'
        with open(self._tpl_path(), 'w', encoding='utf-8') as f:
            json.dump(custom, f, ensure_ascii=False)

        self._svc()

        with open(self._tpl_path(), encoding='utf-8') as f:
            got = json.load(f)
        self.assertEqual(got['name'], '我自己改过的 V2EX')

    def test_only_referenced_templates_are_restored(self):
        """只补站点真正引用的，不要把"可选站点"列表塞满。"""
        self._write_site()
        self._wipe_templates()

        svc = self._svc()

        # 落盘只补被引用的那个；但「可选站点」还要列出镜像自带的其它模板
        # （它们还没落盘，点「加入」时才复制）。
        files = sorted(os.listdir(os.path.join(self.dir, 'templates')))
        self.assertEqual(files, ['v2ex.json'])
        rows = {t['id']: t for t in svc.list_templates()}
        self.assertIn('v2ex', rows)
        self.assertTrue(rows['v2ex']['installed'], '配置里已有该站点')
        others = [t for t in rows.values() if t['id'] != 'v2ex']
        self.assertTrue(others, '镜像自带模板应出现在可选列表里')
        self.assertTrue(all(t['source'] == 'image' for t in others))
        self.assertNotIn('example', rows, '示例文档不该出现在可选站点')

    def test_no_sites_means_no_restore(self):
        """零站点的新装用户：保持零模板（模板靠同步获取，符合产品设计）。"""
        self._svc()
        self.assertFalse(os.path.isdir(os.path.join(self.dir, 'templates'))
                         and os.listdir(os.path.join(self.dir, 'templates')))


class TestTemplateMissing(SelfHealCase):
    def test_reports_template_missing_not_unclear(self):
        """模板补不回来时，必须明确说"缺模板"，而不是"判定不了结果"。"""
        self._write_site()
        self._wipe_templates()
        with mock.patch.dict(os.environ,
                             {'AUTOCHECKIN_BUNDLED_PLUGINS': '/nonexistent'}):
            svc = self._svc()                   # 没有内置目录可补
            site = svc.load_config().site('v2ex')
            out = checkin_via_http(site, cookie_header='a=1')

        self.assertEqual(out.error_kind, 'template_missing')
        self.assertIn('模板', out.message)
        self.assertIn('不是站点改版', out.message)

    def test_category_is_not_rule(self):
        """归类不能落进 rule，否则会触发"疑似站点改版"。"""
        r = R.classify('template_missing', '')
        self.assertEqual(r['code'], 'template_missing')
        self.assertNotEqual(r['category'], 'rule')

    def test_not_flagged_as_stale_template(self):
        """模板本来就缺着时，不该再说"疑似站点改版"。"""
        self._write_site()
        self._wipe_templates()
        with mock.patch.dict(os.environ,
                             {'AUTOCHECKIN_BUNDLED_PLUGINS': '/nonexistent'}):
            svc = self._svc()
            site = svc.load_config().site('v2ex')
            site.state = {'consecutive_failures': 5}
            hist = svc.history()
            with mock.patch.object(hist, 'recent_failures',
                                   return_value=[{'code': 'unclear', 'ts': 1}] * 6):
                self.assertFalse(svc._template_stale(site))

    def test_rule_failures_still_flagged(self):
        """反过来：模板确实在、但连续判定失败，仍要提示改版。"""
        self._write_site()
        svc = self._svc()                       # 模板会自动补回
        site = svc.load_config().site('v2ex')
        site.state = {'consecutive_failures': 5}
        hist = svc.history()
        with mock.patch.object(hist, 'recent_failures',
                               return_value=[{'code': 'unclear', 'ts': 1}] * 6):
            self.assertTrue(svc._template_stale(site),
                            '模板在却一直判定失败，才是真的改版信号')


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
        calls = []

        def fetcher(method, url, data):
            calls.append(url)
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


class TestRestorePathSafety(SelfHealCase):
    def test_traversal_template_id_is_ignored(self):
        """template 字段可手改：含 ../ 时绝不能拿来拼路径写文件。"""
        svc = self._svc()
        cfg = svc.load_config()
        cfg.sites.append(SiteConfig(id='bad', name='bad', kind=KIND_TEMPLATE,
                                    template='../../evil'))
        svc.save_config(cfg)
        self._wipe_templates()

        self._svc()

        self.assertFalse(os.path.exists(os.path.join(self.dir, '..', 'evil.json')),
                         '模板 id 里的 ../ 逃出了数据目录')


class TestFailureIndex(SelfHealCase):
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


class TestTemplateFieldFallback(SelfHealCase):
    """kind=template 但 template 字段空着的老配置。"""

    def test_restore_uses_site_id_when_template_empty(self):
        svc = self._svc()
        cfg = svc.load_config()
        cfg.sites.append(SiteConfig(id='v2ex', name='V2EX',
                                    kind=KIND_TEMPLATE, template='',
                                    homepage='https://www.v2ex.com/'))
        svc.save_config(cfg)
        self._wipe_templates()

        self._svc()

        self.assertTrue(os.path.isfile(self._tpl_path()), '应按站点 id 补回模板')

    def test_engine_resolves_by_site_id(self):
        from app.engine import _tpl_of
        self._write_site()
        svc = self._svc()                       # 重启一次，模板被补回
        self.assertTrue(_tpl_of(svc.load_config().site('v2ex')).get('flow'))

        bare = SiteConfig(id='v2ex', name='V2EX', kind=KIND_TEMPLATE,
                          template='', homepage='https://www.v2ex.com/')
        self.assertTrue(_tpl_of(bare).get('flow'),
                        'template 空时要用站点 id 找到模板')


class TestBundledDirectInstall(SelfHealCase):
    """「可选站点」直接列镜像自带模板，点加入即可（不必先同步）。"""

    def test_listed_before_sync(self):
        svc = self._svc()
        rows = {t['id']: t for t in svc.list_templates()}
        self.assertTrue(rows, '全新安装也应看到镜像自带的模板')
        self.assertTrue(all(t['source'] == 'image' for t in rows.values()))
        self.assertFalse(any(t['installed'] for t in rows.values()))
        self.assertNotIn('example', rows, '示例文档不是可装模板')

    def test_install_without_sync(self):
        svc = self._svc()
        self.assertFalse(os.path.exists(self._tpl_path()))
        out = svc.install_template('v2ex')
        self.assertTrue(out['ok'])
        self.assertTrue(os.path.isfile(self._tpl_path()), '加入时应落盘')
        rows = {t['id']: t for t in svc.list_templates()}
        self.assertTrue(rows['v2ex']['installed'])
        self.assertEqual(rows['v2ex']['source'], 'plugin', '落盘后由插件目录读取')

    def test_example_cannot_be_installed(self):
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.install_template('example')


if __name__ == '__main__':
    unittest.main()
