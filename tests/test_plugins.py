"""站点模板插件：把"支持哪些站点"从镜像里解耦。

用户需求原文：
    「站点和容器能不能解耦，这样以后我有要更新的软件就不用每次都更新容器了，
      直接在 github 的某个文件夹新增一个文件就行（容器能自动或手动监测到
      文件更新自动加入选择项，选中的才在内置站点展示出来）」

实现要点：
    * 模板本来就是纯数据，外部放 JSON（DATA_DIR/templates/*.json）即可新增
    * 插件**优先于内置**：站点改版时可用插件覆盖，不必发版
    * 校验很严格：插件内容会变成发往站点的请求与判定关键词，等于可执行配置
    * 一个坏文件不能拖垮服务（与"畸形配置不能让容器起不来"同一原则）
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S                                     # noqa: E402
from app import templates as T                                   # noqa: E402
from app.plugins import (MAX_PLUGIN_BYTES, PluginStore,          # noqa: E402
                         validate_template)
from app.service import Service                                  # noqa: E402

GOOD = {
    'id': 'mysite', 'name': '我的站', 'homepage': 'https://a.b/',
    'need_browser': False,
    'flow': [{'action': 'get', 'url': '{homepage}'},
             {'action': 'extract_regex', 'pattern': r'gain=(\d+)',
              'save_as': 'gain'}],
    'success_keywords': ['签到成功'], 'fail_keywords': ['请先登录'],
    'headers': {'User-Agent': 'x'}, 'note': '示例',
}


class TestValidateTemplate(unittest.TestCase):
    def test_accepts_good(self):
        tpl, why = validate_template(GOOD)
        self.assertEqual(why, '')
        self.assertEqual(tpl['id'], 'mysite')
        self.assertEqual(tpl['source'], 'plugin')

    def test_rejects_bad_id(self):
        """id 会被用作站点 id 并进 HTML，必须限定字符集。"""
        for bad in ('MySite', 'a/b', '../etc', 'a b', '', 'x' * 65,
                    '-lead', '.dot', '中文'):
            with self.subTest(bad=bad):
                tpl, why = validate_template(dict(GOOD, id=bad))
                self.assertEqual(tpl, {}, '不该接受 id=%r' % bad)
                self.assertTrue(why)

    def test_requires_name_and_homepage(self):
        for key in ('name', 'homepage'):
            d = {k: v for k, v in GOOD.items() if k != key}
            tpl, why = validate_template(d)
            self.assertEqual(tpl, {}, '缺少 %s 应被拒' % key)

    def test_homepage_must_be_http(self):
        for bad in ('javascript:alert(1)', 'file:///etc/passwd', 'ftp://x/y',
                    '', None):
            with self.subTest(bad=bad):
                tpl, _ = validate_template(dict(GOOD, homepage=bad))
                self.assertEqual(tpl, {})

    def test_requires_nonempty_flow(self):
        for bad in ([], None, 'nope'):
            with self.subTest(bad=bad):
                tpl, _ = validate_template(dict(GOOD, flow=bad))
                self.assertEqual(tpl, {})

    def test_rejects_unknown_action(self):
        """未知 action 必须拒绝 —— 否则会静默跳过，站点表现为"未登录"。"""
        for bad in ('exec', 'shell', 'eval', 'delete', ''):
            with self.subTest(bad=bad):
                tpl, why = validate_template(
                    dict(GOOD, flow=[{'action': bad}]))
                self.assertEqual(tpl, {})
                self.assertIn('action', why)

    def test_browser_action_requires_flag(self):
        """用了浏览器动作却不声明 need_browser 必须拒绝。

        不自动帮忙改成 True：那样用户会以为"这是纯请求站点"，
        而实际需要浏览器（当前镜像没有浏览器），查起来很费劲。
        """
        tpl, why = validate_template(
            dict(GOOD, need_browser=False,
                 flow=[{'action': 'click', 'target': '#a'}]))
        self.assertEqual(tpl, {})
        self.assertIn('need_browser', why)
        # 声明了就通过
        tpl2, why2 = validate_template(
            dict(GOOD, need_browser=True,
                 flow=[{'action': 'click', 'target': '#a'}]))
        self.assertEqual(why2, '')
        self.assertTrue(tpl2['need_browser'])

    def test_header_injection_rejected(self):
        """请求头值里的 CRLF 会造成头注入 —— 必须拒绝。"""
        for val in ('a\r\nEvil: 1', 'a\nEvil: 1', 'a\x00b'):
            with self.subTest(val=val):
                tpl, why = validate_template(
                    dict(GOOD, headers={'X-Test': val}))
                self.assertEqual(tpl, {})
                self.assertIn('换行', why)

    def test_bad_header_name_rejected(self):
        for name in ('X Y', 'X:Y', 'X\r\nY', ''):
            with self.subTest(name=name):
                tpl, _ = validate_template(dict(GOOD, headers={name: '1'}))
                self.assertEqual(tpl, {})

    def test_headers_wrong_type_rejected(self):
        tpl, _ = validate_template(dict(GOOD, headers='not-a-dict'))
        self.assertEqual(tpl, {})

    def test_flow_size_limited(self):
        many = [{'action': 'get', 'url': 'https://a/%d' % i} for i in range(200)]
        tpl, why = validate_template(dict(GOOD, flow=many))
        self.assertEqual(tpl, {})
        self.assertIn('过多', why)

    def test_only_known_fields_kept(self):
        """多余字段不该被带进流程（避免插件塞任意内容）。"""
        tpl, _ = validate_template(dict(
            GOOD, flow=[{'action': 'get', 'url': 'https://a/',
                         'evil': 'rm -rf /', 'sql': 'drop'}]))
        self.assertNotIn('evil', tpl['flow'][0])
        self.assertNotIn('sql', tpl['flow'][0])

    def test_top_level_not_object(self):
        for bad in ([1, 2], 'str', None, 5):
            with self.subTest(bad=bad):
                tpl, _ = validate_template(bad)
                self.assertEqual(tpl, {})


class TestPluginStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_plug_')
        self.ps = PluginStore(self.dir)

    def _write(self, name, obj):
        self.ps.ensure_dir()
        p = os.path.join(self.ps.dir_path(), name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj,
                                                               ensure_ascii=False))
        return p

    def test_missing_dir_is_empty_not_error(self):
        self.assertEqual(self.ps.load(force=True), [])

    def test_loads_valid(self):
        self._write('a.json', GOOD)
        self.assertEqual([t['id'] for t in self.ps.load(force=True)],
                         ['mysite'])

    def test_broken_file_does_not_break_others(self):
        """一个坏文件不能让整个插件功能失效。"""
        self._write('good.json', GOOD)
        self._write('broken.json', '{ not json')
        self._write('bad_id.json', dict(GOOD, id='UPPER'))
        ids = [t['id'] for t in self.ps.load(force=True)]
        self.assertEqual(ids, ['mysite'], '好文件应该仍然被加载')
        errs = self.ps.errors()
        self.assertEqual(len(errs), 2, '两个坏文件都该记录原因：%s' % errs)

    def test_duplicate_id_keeps_first(self):
        self._write('a.json', GOOD)
        self._write('b.json', dict(GOOD, name='另一个'))
        ids = [t['id'] for t in self.ps.load(force=True)]
        self.assertEqual(ids, ['mysite'])
        self.assertTrue(any('重复' in e['reason'] for e in self.ps.errors()))

    def test_oversize_rejected(self):
        big = dict(GOOD, note='x' * (MAX_PLUGIN_BYTES + 100))
        self._write('big.json', big)
        self.assertEqual(self.ps.load(force=True), [])
        self.assertIn('过大', self.ps.errors()[0]['reason'])

    def test_array_file_supported(self):
        """一个文件里放多个模板（数组）也支持。"""
        self._write('many.json', [GOOD, dict(GOOD, id='second',
                                             name='第二个')])
        self.assertEqual([t['id'] for t in self.ps.load(force=True)],
                         ['mysite', 'second'])

    def test_mtime_change_is_picked_up(self):
        """改了文件内容（不重启）能被重新读到 —— 这是"自动监测"的基础。"""
        p = self._write('a.json', GOOD)
        self.assertEqual(len(self.ps.load()), 1)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(dict(GOOD, id='renamed'), f)
        os.utime(p, (0, 0))          # 确保 mtime 变化（同秒内写入也能测到）
        self.assertEqual([t['id'] for t in self.ps.load()], ['renamed'])

    def test_save_and_delete(self):
        self.ps.save(dict(GOOD, id='saved'))
        self.assertEqual([t['id'] for t in self.ps.load(force=True)], ['saved'])
        self.assertTrue(self.ps.delete('saved'))
        self.assertEqual(self.ps.load(force=True), [])


class TestTemplateProvider(unittest.TestCase):
    """插件提供者：插件优先、内置兜底、异常不影响内置。"""

    def tearDown(self):
        T.set_plugin_provider(None)

    def test_no_provider_matches_old_behaviour(self):
        T.set_plugin_provider(None)
        self.assertEqual([s.id for s in T.builtin_sites()],
                         ['v2ex', 'nodeseek', 'chiphell'])
        self.assertEqual(T.template_by_id('v2ex')['name'], 'V2EX 每日铜币')

    def test_plugin_overrides_builtin(self):
        """插件能覆盖内置模板 —— 站点改版时不必等发新版镜像。"""
        T.set_plugin_provider(lambda: [dict(GOOD, id='v2ex',
                                            name='V2EX（插件修正）')])
        self.assertEqual(T.template_by_id('v2ex')['name'], 'V2EX（插件修正）')

    def test_builtin_used_when_no_plugin(self):
        T.set_plugin_provider(lambda: [dict(GOOD, id='other')])
        self.assertEqual(T.template_by_id('v2ex')['name'], 'V2EX 每日铜币')

    def test_provider_error_does_not_break_builtin(self):
        def boom():
            raise RuntimeError('boom')
        T.set_plugin_provider(boom)
        self.assertEqual(T.template_by_id('v2ex')['name'], 'V2EX 每日铜币')
        self.assertEqual(T.plugin_templates(), [])

    def test_available_templates_marks_source(self):
        T.set_plugin_provider(lambda: [dict(GOOD, id='plug')])
        got = {t['id']: t['source'] for t in T.available_templates()}
        self.assertEqual(got['plug'], 'plugin')
        self.assertEqual(got['v2ex'], 'builtin')


class TestServicePluginFlow(unittest.TestCase):
    """服务层：列出可选模板、选中才加入站点。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_svcplug_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')

    def tearDown(self):
        T.set_plugin_provider(None)

    def _drop(self, obj, name=None):
        os.makedirs(self.svc.plugin_dir(), exist_ok=True)
        p = os.path.join(self.svc.plugin_dir(),
                         name or ('%s.json' % obj['id']))
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
        return p

    def test_drop_file_then_it_appears(self):
        """丢一个文件进 templates/ 就出现在可选列表里（不用重启）。"""
        before = {t['id'] for t in self.svc.list_templates()}
        self._drop(GOOD)
        after = {t['id'] for t in self.svc.list_templates()}
        self.assertIn('mysite', after - before)

    def test_only_installed_shows_as_site(self):
        """未选中的插件模板只出现在"可选"里，不作为站点。"""
        self._drop(GOOD)
        self.svc.reload_plugins()
        ids = [s.id for s in self.svc.load_config().sites]
        self.assertNotIn('mysite', ids, '未选中的不该变成站点')
        row = [t for t in self.svc.list_templates() if t['id'] == 'mysite'][0]
        self.assertFalse(row['installed'])

    def test_install_adds_site_once(self):
        self._drop(GOOD)
        self.svc.reload_plugins()
        out = self.svc.install_template('mysite')
        self.assertTrue(out['ok'])
        self.assertFalse(out['already'])
        again = self.svc.install_template('mysite')
        self.assertTrue(again['already'], '重复安装应幂等')
        ids = [s.id for s in self.svc.load_config().sites]
        self.assertEqual(ids.count('mysite'), 1)
        # 装完后标记为已加入
        row = [t for t in self.svc.list_templates() if t['id'] == 'mysite'][0]
        self.assertTrue(row['installed'])

    def test_install_unknown_raises(self):
        with self.assertRaises(ValueError):
            self.svc.install_template('nope-not-here')

    def test_installed_site_gets_template_config(self):
        """加入时要把模板预置的关键词/请求头带上。"""
        self._drop(GOOD)
        self.svc.reload_plugins()
        self.svc.install_template('mysite')
        site = self.svc.load_config().site('mysite')
        self.assertEqual(site.template, 'mysite')
        self.assertIn('签到成功', site.success_keywords)
        self.assertIn('User-Agent', site.headers)

    def test_broken_plugin_does_not_break_listing(self):
        self._drop(GOOD, 'good.json')
        self._drop({'id': 'BAD'}, 'bad.json')
        got = self.svc.list_templates()
        self.assertIn('mysite', [t['id'] for t in got])
        self.assertTrue(self.svc.reload_plugins()['errors'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
