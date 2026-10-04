"""并发与配置持久化的回归测试。

为什么单独一个文件：这里全部是"用户刚做的改动被静默吃掉"类的问题，
而且**都是实际踩过的坑**（审计用真实脚本复现过）。这类 bug 的症状是
"我明明改了，过一会儿又变回去了"，很难复现、很难归因，所以必须有
自动化断言把它钉住。

核心不变量：
    * 长任务（浏览器签到可能跑几分钟）结束时**不能**整包写回旧快照，
      否则会回滚期间用户做的一切：Cookie、口令、令牌吊销、排程、AI 计数
    * 用户主动删光的站点**不能**自动复活
    * AI 补丁**要**落盘（和上面相反的方向，容易过度修正）
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import secrets as S                                    # noqa: E402
from app import auth as A                                       # noqa: E402
from app.auth import AuthManager                                 # noqa: E402
from app.models import AppConfig, AiConfig, SiteConfig           # noqa: E402
from app.runner import Runner                                    # noqa: E402
from app.service import Service                                  # noqa: E402
from app.store import RuntimeState, Store                        # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ac_persist_')
        self.svc = Service(self.dir, version='t',
                           env_key=S.generate_key(), proxy='')
        self.svc.upsert_site({'id': 's1', 'name': 'S1',
                              'homepage': 'https://example.com'})
        self.svc.set_site_cookie('s1', 'session=OLD; pjwt=BBB')

    def site(self, cfg=None):
        """按 id 取站点。

        ⚠️ 不能写 `cfg.sites[0]`：全新安装会自动补上 3 个内置站点
        （v2ex/nodeseek/chiphell），下标 0 是内置站点、不是测试建的那个。
        我第一版就是这么写的，于是断言全落在错误的对象上。
        """
        cfg = cfg or self.svc.load_config()
        return [s for s in cfg.sites if s.id == 's1'][0]

    def _s(self, cfg):
        """同上，但接收已有 cfg。"""
        return [s for s in cfg.sites if s.id == 's1'][0]


class TestStaleSnapshotDoesNotRollBack(Case):
    """长任务结束的增量回写不能覆盖用户期间的修改。"""

    def test_new_cookie_survives_stale_writeback(self):
        stale = self.svc.load_config()          # 长任务手里的旧快照
        self.svc.set_site_cookie('s1', 'session=NEW; pjwt=XXX')
        fresh_enc = self.site().cookie_enc

        self._s(stale).state = {'consecutive_failures': 3}
        self.svc.store.merge_site_runtime(stale.sites)

        self.assertEqual(self.site().cookie_enc, fresh_enc,
                         '新 Cookie 被旧快照覆盖了')
        self.assertEqual(self.svc.box.decrypt(fresh_enc),
                         'session=NEW; pjwt=XXX')

    def test_password_change_survives_stale_writeback(self):
        """改口令不能被回滚 —— 这是安全回归。

        改回去会怎样：用户改了口令（想让旧会话失效）之后，只要有长任务
        结束写回旧快照，`auth_password_enc` 就被回滚，**旧口令重新可用**。
        """
        am = AuthManager(self.svc)
        am.set_password('old-pw-111')
        stale = self.svc.load_config()
        am.set_password('new-pw-222')           # 长任务期间用户改口令

        self.svc.store.merge_site_runtime(stale.sites)

        after = AuthManager(self.svc)
        self.assertTrue(after.password_matches('new-pw-222'),
                        '新口令失效了')
        self.assertFalse(after.password_matches('old-pw-111'),
                         '旧口令被回滚成可用（安全回归）')

    def test_token_revocation_survives_stale_writeback(self):
        """吊销脚本令牌不能被回滚。

        手动把令牌哈希写进配置（这才是"已签发"的状态），
        然后模拟：长任务持旧快照 → 用户吊销 → 长任务写回。
        改回去会怎样：吊销被回滚，**已吊销的令牌重新有效**。
        """
        am = AuthManager(self.svc)
        am.set_password('pw-123456')
        tok = A.new_api_token()
        cfg = self.svc.load_config()
        cfg.api_token_hashes = [A.hash_api_token(tok)]
        self.svc.save_config(cfg)
        self.assertTrue(AuthManager(self.svc).api_token_valid(tok),
                        '前置条件：令牌应先有效')

        stale = self.svc.load_config()            # 长任务手里的旧快照
        AuthManager(self.svc).revoke_api_tokens()  # 用户吊销
        self.assertFalse(AuthManager(self.svc).api_token_valid(tok))

        self.svc.store.merge_site_runtime(stale.sites)

        self.assertFalse(AuthManager(self.svc).api_token_valid(tok),
                         '已吊销的令牌被回滚成有效（安全回归）')

    def test_user_config_edit_survives(self):
        """用户在长任务期间改的站点设置不能被覆盖。"""
        stale = self.svc.load_config()
        cfg = self.svc.load_config()
        self.site(cfg).name = '用户改的名字'
        self.site(cfg).retry_count = 9
        self.svc.save_config(cfg)

        self._s(stale).state = {'consecutive_failures': 1}
        self._s(stale).name = '旧名字'
        self._s(stale).retry_count = 0
        self.svc.store.merge_site_runtime(stale.sites)

        got = self.site()
        self.assertEqual(got.name, '用户改的名字', '用户改的名字被覆盖')
        self.assertEqual(got.retry_count, 9, '用户改的重试次数被覆盖')
        self.assertEqual(got.state.get('consecutive_failures'), 1,
                         '运行期状态应写入')

    def test_deleted_site_does_not_resurrect_via_writeback(self):
        """长任务手里的站点若已被用户删除，回写不能让它复活。

        注意 setUp 里是全新安装，所以还有 3 个内置站点在 —— 断言要看
        "s1 没了"，而不是"一个站点都不剩"（我第一版就写错了）。
        """
        stale = self.svc.load_config()
        self.svc.delete_site('s1')

        self._s(stale).state = {'consecutive_failures': 5}
        self.svc.store.merge_site_runtime(stale.sites)

        ids = [s.id for s in self.svc.load_config().sites]
        self.assertNotIn('s1', ids, '已删除的站点被回写复活了')


class TestStateDeltaDoesNotClobber(Case):
    """state 的增量写回不能回滚排程与 AI 计数。"""

    def test_next_run_at_not_rolled_back(self):
        """排程回滚会导致同一天签到两次。"""
        r = Runner(self.svc.store, box=self.svc.box,
                   global_proxy_mode='direct')
        r.now_fn = lambda: 1000
        st = self.svc.load_state()
        st.sites.setdefault('s1', {})['next_run_at'] = 5000
        self.svc.store.update_state(lambda s: s.sites.update(st.sites))

        # 一个持有旧快照的执行体（旧排程 = 1001）跑完
        stale = RuntimeState()
        stale.sites = {'s1': {'next_run_at': 1001}}
        stale.last_run_at = 1000
        r._persist_state_delta(stale, [], 1000)

        self.assertEqual(self.svc.load_state().next_run_at('s1'), 5000,
                         '排程被旧快照回滚（会重复签到）')

    def test_ai_call_count_never_decreases(self):
        """AI 计数回滚会让每日上限失效、多烧 token。"""
        r = Runner(self.svc.store, box=self.svc.box,
                   global_proxy_mode='direct')
        # 别的执行体已经写到 4
        self.svc.store.update_state(
            lambda s: s.ai_calls.update({'s1': {'date': '2026-01-01',
                                                'count': 4}}))
        # 长任务手里的旧计数是 1
        stale = RuntimeState()
        stale.ai_calls = {'s1': {'date': '2026-01-01', 'count': 1}}
        r._persist_state_delta(stale, [], 1000)

        self.assertEqual(
            self.svc.load_state().ai_calls_today('s1', '2026-01-01'), 4,
            'AI 计数被回滚（每日上限失效）')

    def test_broken_next_run_at_does_not_stall_scheduler(self):
        """state.json 被写坏时不能让调度整体停摆。

        改回去会怎样：due_sites 里裸 int('abc') 抛 ValueError，冒泡到
        调度循环的宽泛 except → 每轮都失败 → 所有站点永远不再被检查。
        """
        st = RuntimeState()
        st.sites = {'a': {'next_run_at': 'abc'}, 'b': {'next_run_at': 0}}
        try:
            got = st.due_sites(1000)
        except Exception as e:                                  # noqa: BLE001
            self.fail('due_sites 抛异常会让调度停摆：%s' % e)
        self.assertEqual(sorted(got), ['a', 'b'])


class TestBuiltinSitesDoNotResurrect(Case):
    """删光站点后内置模板不能自己回来。

    历史行为是"站点为空就补内置"，这导致用户删掉不想跑的站点后，
    它们下次加载又回来继续签到（国内直连 v2ex/nodeseek 不可达 →
    天天失败 → 烧 AI 额度）。这里把"用户删空"与"从没配置过"分开。
    """

    def test_deleted_builtins_stay_deleted(self):
        """用户删光内置站点后不能再自己回来（本项目的核心诉求）。"""
        svc = Service(tempfile.mkdtemp(prefix='ac_builtin_'), version='t',
                      env_key=S.generate_key(), proxy='')
        svc.save_config(svc.load_config())
        self.assertEqual(len(svc.list_sites()), 3)
        for s in list(svc.load_config().sites):
            svc.delete_site(s.id)
        self.assertEqual(svc.list_sites(), [], '内置站点自动复活了')
        self.assertEqual(svc.load_config().sites, [])

    def test_legacy_empty_config_gets_builtins(self):
        """老配置（站点为空、从没管理过）仍应补内置 —— 升级行为不变。

        这是"从没配置过"的那一半：文件里 sites 为空且没有管理标记。
        """
        d = tempfile.mkdtemp(prefix='ac_legacy_')
        Store(d).save_config(AppConfig())        # 站点为空且未标记
        svc = Service(d, version='t', env_key=S.generate_key(), proxy='')
        self.assertEqual(len(svc.list_sites()), 3)

    def test_pristine_default_recognised(self):
        """默认配置（3 个内置站点原封不动）判定为"没动过"。"""
        from app.models import default_config
        store = Store(tempfile.mkdtemp(prefix='ac_pristine_'))
        # default_config() 才带内置站点；裸 AppConfig() 的 sites 是空的，
        # 那代表"站点为空"而不是"未动过的默认"，两者要分清（我第一版搞混了）
        self.assertTrue(store.is_pristine(default_config()))
        self.assertFalse(store.is_pristine(AppConfig()),
                         '站点为空的配置不等于"未动过的默认配置"')
        cfg = default_config()
        cfg.sites_initialized = True
        self.assertFalse(store.is_pristine(cfg), '标记过就不算未动过')

    def test_flag_persists_across_reload(self):
        cfg = self.svc.load_config()
        cfg.sites = []                            # 删空
        cfg.sites_initialized = True
        self.svc.save_config(cfg)
        self.assertTrue(self.svc.load_config().sites_initialized)
        self.assertEqual(self.svc.list_sites(), [])


class TestAiPatchPersists(Case):
    """AI 补丁必须落盘（与"不覆盖用户"是相反方向，别过度修正）。"""

    def _runner(self):
        r = Runner(self.svc.store, box=self.svc.box,
                   global_proxy_mode='direct')
        r.now_fn = lambda: 1000
        return r

    def test_patch_fields_written_back(self):
        stale = self.svc.load_config()
        self._s(stale).need_browser = True
        self._s(stale).success_keywords = ['已领取']
        self.svc.store.merge_site_patch(stale.sites)
        got = self.site()
        self.assertTrue(got.need_browser, 'AI 补丁没落盘')
        self.assertEqual(got.success_keywords, ['已领取'])

    def test_patch_does_not_touch_non_patch_fields(self):
        """补丁回写只碰白名单字段，用户的其它设置不能被带动。

        注意 retry_count **在**白名单里（AI 可以建议改重试次数），
        所以这里要挑真正不在白名单的字段来验证 —— 我第一版拿了
        retry_count，等于在测一个本来就该被改的东西。
        """
        cfg = self.svc.load_config()
        self.site(cfg).name = '用户的名字'            # 不在白名单
        self.site(cfg).enabled = False                # 不在白名单
        self.site(cfg).proxy_url = 'http://用户设的:1'  # 不在白名单
        self.svc.save_config(cfg)

        stale = self.svc.load_config()
        self._s(stale).name = 'AI 想改的名字'
        self._s(stale).enabled = True
        self._s(stale).proxy_url = 'http://attacker:8080'
        self._s(stale).need_browser = True          # 在白名单
        self._s(stale).retry_count = 5              # 在白名单
        self.svc.store.merge_site_patch(stale.sites)

        got = self.site()
        self.assertEqual(got.name, '用户的名字', '补丁改了不在白名单的 name')
        self.assertFalse(got.enabled, '补丁改了不在白名单的 enabled')
        self.assertEqual(got.proxy_url, 'http://用户设的:1',
                         '补丁改了不在白名单的 proxy_url')
        self.assertTrue(got.need_browser, '白名单字段应该写入')
        self.assertEqual(got.retry_count, 5, '白名单字段应该写入')

    def test_patch_does_not_touch_credentials(self):
        before = self.site().cookie_enc
        stale = self.svc.load_config()
        self._s(stale).cookie_enc = 'ATTACKER'
        self._s(stale).password_enc = 'ATTACKER'
        self._s(stale).need_browser = True
        self.svc.store.merge_site_patch(stale.sites)
        got = self.site()
        self.assertEqual(got.cookie_enc, before, '补丁动了凭据')
        self.assertNotEqual(got.password_enc, 'ATTACKER')


class TestDisabledSiteDoesNotBurnAi(Case):
    """停用的站点不消耗 AI 额度（界面与 README 都这么承诺）。"""

    def test_disabled_site_skips_ai(self):
        from app.deepseek import AiSuggestion
        from app.notify import CheckinResult

        cfg = self.svc.load_config()
        site = self.site(cfg)
        site.enabled = False
        site.ai_enabled = True
        site.state = {'consecutive_failures': 3}   # 正好是阈值整数倍
        cfg.ai = AiConfig(enabled=True, ai_after_failures=3,
                          max_calls_per_day=0,
                          api_key_enc=self.svc.box.encrypt('k'))
        self.svc.save_config(cfg)

        r = Runner(self.svc.store, box=self.svc.box,
                   global_proxy_mode='direct')
        r.now_fn = lambda: 1000
        called = []
        r.analyze_fn = lambda *a, **k: (called.append(1),
                                        AiSuggestion(True, analysis='x'))[1]

        out = r._maybe_analyze(self.site(), self.svc.load_config(),
                               self.svc.load_state(),
                               CheckinResult('s1', 'S1', False, 'x'), 1000)
        self.assertEqual(called, [], '停用站点仍然调用了 AI（烧额度）')
        self.assertIn('停用', out)


class TestRestorePinsControlPlane(Case):
    """从备份恢复时，控制面字段必须保持本机值。

    为什么：`ai.base_url` 决定"把 API Key 发到哪里"。如果它由备份决定，
    攻击者只要让你恢复一份自己的备份，容器就会把**真实的 DeepSeek Key**
    发到他的服务器（审计实测：Authorization: Bearer sk-REAL... 落到假服务器）。
    proxy 可做全量劫持；browser_path 是"按指定路径启动程序"= 代码执行面。
    """

    def _local(self):
        cfg = self.svc.load_config()
        cfg.ai.base_url = 'https://api.deepseek.com'
        cfg.ai.api_key_enc = 'LOCAL_KEY'
        cfg.proxy = 'http://local-proxy:1'
        self.svc.save_config(cfg)
        return self.svc.load_config()

    EVIL = {
        'ai': {'base_url': 'http://attacker.example/v1',
               'api_key_enc': 'ATTACKER_KEY'},
        'proxy': 'http://attacker:8080',
        'remote_cdp_url': 'http://u:p@attacker:9222',
        'browser_path': '/tmp/attacker/evil',
        'sites': [{'id': 'restored', 'name': 'R'}],
    }

    def test_control_plane_kept_from_local(self):
        local = self._local()
        r = self.svc.store.import_config(self.EVIL, keep_auth_from=local)
        self.assertEqual(r.ai.base_url, 'https://api.deepseek.com',
                         'AI 地址被备份劫持 → Key 会被发到攻击者服务器')
        self.assertEqual(r.ai.api_key_enc, 'LOCAL_KEY')
        self.assertEqual(r.proxy, 'http://local-proxy:1',
                         '代理被备份劫持 → 全量流量可被中间人')
        self.assertEqual(r.remote_cdp_url, '', 'CDP 地址被备份带入')
        self.assertEqual(r.browser_path, '', 'browser_path 被备份带入（代码执行面）')

    def test_site_data_still_restored(self):
        r = self.svc.store.import_config(self.EVIL,
                                         keep_auth_from=self._local())
        self.assertEqual([s.id for s in r.sites], ['restored'],
                         '站点数据应该正常恢复')

    def test_auth_still_kept_from_local(self):
        local = self._local()
        local.auth_password_enc = 'LOCAL_PW'
        local.session_hashes = ['LOCAL_SESS']
        r = self.svc.store.import_config(
            dict(self.EVIL, auth_password_enc='plain:ATTACKER',
                 session_hashes=['ATTACKER']), keep_auth_from=local)
        self.assertEqual(r.auth_password_enc, 'LOCAL_PW')
        self.assertEqual(r.session_hashes, ['LOCAL_SESS'])

    def test_backup_omits_control_plane(self):
        """备份本身也不该带这些字段（双保险）。"""
        cfg = self._local()
        out = self.svc.store.export_config(cfg)
        self.assertEqual(out.get('remote_cdp_url'), '')
        self.assertEqual(out.get('browser_path'), '')
        self.assertEqual((out.get('ai') or {}).get('base_url'), '')
        self.assertEqual((out.get('webdav') or {}).get('username'), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
