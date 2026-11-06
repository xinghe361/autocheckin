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

        files = sorted(os.listdir(os.path.join(self.dir, 'templates')))
        self.assertEqual(files, ['v2ex.json'])
        ids = [t['id'] for t in svc.list_templates()]
        self.assertEqual(ids, ['v2ex'])

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


if __name__ == '__main__':
    unittest.main()
