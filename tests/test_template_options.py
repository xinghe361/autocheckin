"""模板可选项：NodeSeek 的签到方式（试试手气 / 鸡腿×5）。

这一层最容易出的错不是"没实现"，而是**取值没校验**：
这些值最后会拼进请求 URL（`?random={attendance}`），
所以从模板声明到落盘到回放，每一环都得只认白名单里的取值。
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.engine as E  # noqa: E402
from app import secrets as S  # noqa: E402
from app import templates as T  # noqa: E402
from app.httpclient import HttpResponse  # noqa: E402
from app.models import SiteConfig  # noqa: E402
from app.service import Service  # noqa: E402


def nodeseek_template():
    return T.template_by_id('nodeseek')


class TestDeclaredOptions(unittest.TestCase):
    def test_nodeseek_declares_two_attendance_choices(self):
        specs = T.option_specs(nodeseek_template())
        self.assertEqual(len(specs), 1)
        sp = specs[0]
        self.assertEqual(sp['key'], 'attendance')
        self.assertEqual(sp['default'], 'true', '默认必须是站内默认的「试试手气」')
        self.assertEqual([c['value'] for c in sp['choices']], ['true', 'false'])
        for c in sp['choices']:
            self.assertTrue(c['label'], '每个选项都要有给用户看的文字')

    def test_flow_uses_the_placeholder(self):
        """流程里不能再写死 random=true，否则选项形同虚设。"""
        flow = nodeseek_template()['flow']
        urls = [s.get('url', '') for s in flow]
        self.assertTrue(any('{attendance}' in u for u in urls), urls)
        self.assertFalse(any('random=true' in u for u in urls),
                         '还有写死的 random=true：%s' % urls)

    def test_other_templates_have_no_options(self):
        """V2EX / Chiphell 没有可选项，界面就不该出现那一栏。"""
        for tid in ('v2ex', 'chiphell'):
            self.assertEqual(T.option_specs(T.template_by_id(tid)), [], tid)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tpl = nodeseek_template()

    def test_missing_falls_back_to_default(self):
        self.assertEqual(T.resolve_options(self.tpl, None)['attendance'], 'true')
        self.assertEqual(T.resolve_options(self.tpl, {})['attendance'], 'true')

    def test_valid_value_kept(self):
        got = T.resolve_options(self.tpl, {'attendance': 'false'})
        self.assertEqual(got['attendance'], 'false')

    def test_unknown_value_falls_back(self):
        for bad in ('随便写', 'TRUE', 'false; DROP TABLE', '', '  ', '1'):
            got = T.resolve_options(self.tpl, {'attendance': bad})
            self.assertEqual(got['attendance'], 'true', '非法值 %r 没被挡住' % bad)

    def test_unknown_key_dropped(self):
        """不认识的键不能原样留在配置里 —— 它会被当成占位符变量。"""
        got = T.resolve_options(self.tpl, {'attendance': 'false', 'evil': 'x'})
        self.assertEqual(set(got), {'attendance'})

    def test_non_dict_input_is_safe(self):
        for weird in ('false', 123, ['a'], object()):
            self.assertEqual(T.resolve_options(self.tpl, weird)['attendance'],
                             'true')

    def test_broken_specs_are_ignored(self):
        """插件模板来自外部 JSON，写歪了不能把界面搞崩。"""
        broken = {'options': [
            'not-a-dict',
            {'key': ''},                          # 没 key
            {'key': 'k1'},                        # 没 choices
            {'key': 'k2', 'choices': []},         # 空 choices
            {'key': 'k3', 'choices': [{'label': 'x'}]},   # choice 没 value
            {'key': 'k4', 'choices': [{'value': 'a'}, {'value': 'a'}]},
        ]}
        specs = T.option_specs(broken)
        self.assertEqual([s['key'] for s in specs], ['k4'],
                         '只应保留 k4，且重复取值要去重')
        self.assertEqual(len(specs[0]['choices']), 1)

    def test_bad_default_falls_back_to_first_choice(self):
        tpl = {'options': [{'key': 'k', 'default': '不存在',
                            'choices': [{'value': 'a'}, {'value': 'b'}]}]}
        self.assertEqual(T.option_specs(tpl)[0]['default'], 'a')


class TestEngineUrl(unittest.TestCase):
    """真正重要的是最后发出去的请求地址。"""

    def setUp(self):
        self.site = [x for x in T.builtin_sites() if x.id == 'nodeseek'][0]
        self.seen = []
        self._orig = E._http_fetcher

        def fake_fetch(method, url, data):
            self.seen.append(url)
            return HttpResponse(status=200,
                                text='{"success":true,"message":"恭喜你签到获得了5个鸡腿"}',
                                url=url)

        E._http_fetcher = (lambda site, proxy, cookie='',
                           headers_override=None: fake_fetch)

    def tearDown(self):
        E._http_fetcher = self._orig

    def url_for(self, options):
        self.site.options = options
        self.seen.clear()
        E.checkin_via_http(self.site, cookie_header='c=1')
        self.assertTrue(self.seen, '没有发出请求')
        return self.seen[0]

    def test_default_is_random(self):
        self.assertIn('random=true', self.url_for({}))

    def test_fixed_five(self):
        self.assertIn('random=false', self.url_for({'attendance': 'false'}))

    def test_back_to_random(self):
        self.assertIn('random=true', self.url_for({'attendance': 'true'}))

    def test_no_placeholder_left_in_url(self):
        """占位符没被替换的话，请求会带着字面量 {attendance} 发出去。"""
        self.assertNotIn('{', self.url_for({'attendance': 'false'}))

    def test_garbage_value_does_not_reach_the_wire(self):
        url = self.url_for({'attendance': 'false&x=1'})
        self.assertIn('random=true', url)
        self.assertNotIn('x=1', url)

    def test_mission_url_placeholder_also_rendered(self):
        """mission_url 自己带占位符，引用 {mission_url} 的步骤要拿到渲染后的地址。"""
        site = self.site
        site.options = {'attendance': 'false'}
        tpl = nodeseek_template()
        self.assertIn('{attendance}', tpl['mission_url'])
        from app.engine import _render
        rendered = _render(tpl['mission_url'],
                           T.resolve_options(tpl, site.options))
        self.assertIn('random=false', rendered)
        self.assertNotIn('{', rendered)


class TestServiceOptions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_opt_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _ns(self):
        return [s for s in self.svc.list_sites() if s['id'] == 'nodeseek'][0]

    def test_list_exposes_specs_and_value(self):
        ns = self._ns()
        self.assertEqual([s['key'] for s in ns['option_specs']], ['attendance'])
        self.assertEqual(ns['options']['attendance'], 'true')

    def test_save_and_persist(self):
        self.svc.upsert_site({'id': 'nodeseek',
                              'options': {'attendance': 'false'}})
        self.assertEqual(self._ns()['options']['attendance'], 'false')
        # 换一个 Service 实例（重新读盘）也得是改后的值
        again = Service(self.dir, version='t', env_key=None, proxy='')
        got = [s for s in again.list_sites() if s['id'] == 'nodeseek'][0]
        self.assertEqual(got['options']['attendance'], 'false')

    def test_invalid_value_normalised_on_write(self):
        self.svc.upsert_site({'id': 'nodeseek',
                              'options': {'attendance': '<script>'}})
        self.assertEqual(self._ns()['options']['attendance'], 'true')

    def test_absent_options_does_not_clear(self):
        """只改名字时不带 options —— 不能把用户选好的值冲回默认。"""
        self.svc.upsert_site({'id': 'nodeseek',
                              'options': {'attendance': 'false'}})
        self.svc.upsert_site({'id': 'nodeseek', 'name': 'NodeSeek'})
        self.assertEqual(self._ns()['options']['attendance'], 'false')

    def test_non_template_site_has_no_options(self):
        sites = {s['id']: s for s in self.svc.list_sites()}
        self.assertEqual(sites['v2ex']['option_specs'], [])
        self.assertEqual(sites['v2ex']['options'], {})


class TestModelRoundTrip(unittest.TestCase):
    def test_options_survive_serialisation(self):
        s = SiteConfig(id='nodeseek', name='NodeSeek', kind='template',
                       template='nodeseek', options={'attendance': 'false'})
        back = SiteConfig.from_dict(s.to_dict())
        self.assertEqual(back.options, {'attendance': 'false'})

    def test_empty_option_values_dropped(self):
        """空串等于"没设置"，不能存成一个空取值把默认值盖掉。"""
        s = SiteConfig.from_dict({'id': 'x', 'name': 'X',
                                  'options': {'attendance': '', 'k': 'v'}})
        self.assertEqual(s.options, {'k': 'v'})

    def test_garbage_options_ignored(self):
        for bad in (None, 'x', [1, 2], 5):
            s = SiteConfig.from_dict({'id': 'x', 'name': 'X', 'options': bad})
            self.assertEqual(s.options, {})

    def test_old_config_without_options_still_loads(self):
        s = SiteConfig.from_dict({'id': 'nodeseek', 'name': 'NodeSeek',
                                  'kind': 'template', 'template': 'nodeseek'})
        self.assertEqual(s.options, {})
        self.assertEqual(T.site_options(s), {'attendance': 'true'})


class TestSiteApiExposesSpecs(unittest.TestCase):
    """编辑表单取的是 /api/site，不是列表接口。

    这条用例是踩过才补的：option_specs 一开始只加在 list_sites 里，
    接口返回正常、单测也绿，但界面上那一栏永远不出现 ——
    因为表单根本不读列表接口。
    """

    def setUp(self):
        from app import web as WB
        self.dir = tempfile.mkdtemp(prefix='ac_optapi_')
        self.svc = Service(self.dir, version='t', env_key=S.generate_key(),
                           proxy='')
        self.app = WB.WebApp(self.svc, version='t')
        self.app.dispatch('POST', '/api/auth/setup', {},
                          {'password': 'test-password-1'}, token='',
                          source='test')
        self.token = self.app._new_token

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _get(self, path, query):
        got = self.app.dispatch('GET', path, query, {}, token=self.token,
                                source='test')
        return got[-1]

    def test_site_detail_carries_option_specs(self):
        d = self._get('/api/site', {'id': ['nodeseek']})
        site = d['site']
        self.assertEqual([s['key'] for s in site['option_specs']],
                         ['attendance'])
        self.assertEqual(site['options']['attendance'], 'true')
        # 顺带确认没把凭据带出来
        self.assertNotIn('cookie_enc', site)
        self.assertNotIn('password_enc', site)

    def test_site_detail_of_plain_template_has_no_specs(self):
        d = self._get('/api/site', {'id': ['v2ex']})
        self.assertEqual(d['site']['option_specs'], [])

    def test_list_endpoint_also_carries_them(self):
        """列表接口也要有：站点卡片上要显示当前选的是哪个。"""
        got = self.app.dispatch('GET', '/api/sites', {}, {}, token=self.token,
                                source='test')
        sites = {s['id']: s for s in got[-1]['sites']}
        self.assertEqual([s['key'] for s in sites['nodeseek']['option_specs']],
                         ['attendance'])


if __name__ == '__main__':
    unittest.main()
