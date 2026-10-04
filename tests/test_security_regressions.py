"""安全回归测试：钉住这一轮审计发现并修掉的问题。

为什么单独一个文件：这些都是"看起来只是代码风格、实际是安全边界"的地方，
很容易在后续重构里被无意改回去（比如又用 self 存每请求状态、
又把某类凭据放进备份）。每条都写明"改回去会怎样"。
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOSTILE_ID = "x'),alert(1),('y"


def _find_node():
    """找一个可用的 node（用于跑页面 JS）。找不到就返回 None。"""
    import shutil
    p = shutil.which('node')
    if p:
        return p
    cand = os.path.join(os.path.expanduser('~'), '.dsh', 'dsh-runtimes',
                        'dsh-primary-runtime', 'dependencies', 'node', 'bin',
                        'node.exe')
    return cand if os.path.exists(cand) else None

from app import secrets as S                                    # noqa: E402
from app import service as SV                                    # noqa: E402
from app import web as WB                                        # noqa: E402
from app.auth import AuthManager                                 # noqa: E402
from app.models import AppConfig, NotifyConfig, SiteConfig        # noqa: E402
from app.secrets import SecretBox, SecretError                    # noqa: E402
from app.store import Store                                       # noqa: E402


class TestNoSharedRequestState(unittest.TestCase):
    """每请求状态绝不能放在 WebApp 实例上。

    改回去会怎样：WebApp 是单例、HTTP 服务一请求一线程，
    共享字段会让并发请求互相串用 —— 实测一个没有令牌的请求只要
    和一个带合法令牌的请求并发，就能被对方的令牌授权而返回 200。
    """

    def setUp(self):
        self.svc = SV.Service(tempfile.mkdtemp(prefix='ac_sec_'),
                              version='t', env_key=S.generate_key(), proxy='')
        self.am = AuthManager(self.svc)
        self.am.set_password('correct-pw')
        self.token = self.am.login('correct-pw')
        self.app = WB.WebApp(self.svc, version='t')
        self.app.auth = self.am

    def test_no_token_request_is_rejected(self):
        with self.assertRaises(WB.ApiError) as ctx:
            self.app.dispatch('GET', '/api/sites', {}, None, token='')
        self.assertEqual(ctx.exception.status, 401)

    def test_concurrent_requests_cannot_share_token(self):
        """并发时无令牌请求不能借到别人的令牌。"""
        leaked = []
        barrier = threading.Barrier(2)

        def attacker():
            barrier.wait()
            for _ in range(150):
                try:
                    self.app.dispatch('GET', '/api/sites', {}, None, token='')
                    leaked.append(1)
                except WB.ApiError:
                    pass

        def victim():
            barrier.wait()
            for _ in range(150):
                try:
                    self.app.dispatch('GET', '/api/sites', {}, None,
                                      token=self.token)
                except WB.ApiError:
                    pass

        ts = [threading.Thread(target=attacker), threading.Thread(target=victim)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(leaked, [], '并发下无令牌请求被授权了（认证绕过回归）')

    def test_instance_has_no_per_request_token_attrs(self):
        for name in ('_current_token', '_current_source', '_script_token',
                     '_via_script', '_do_logout'):
            self.assertFalse(hasattr(self.app, name),
                             '不该再有共享的每请求字段 %s' % name)

    def test_handler_write_to_context_survives(self):
        """端点往 current_request() 里写的东西必须能被 HTTP 层读到。

        改回去会怎样：登录成功后写 new_token 会落进另一个临时字典，
        Set-Cookie 发不出去，用户登不进来。
        """
        st, ct, out = self.app.dispatch('POST', '/api/auth/login', {},
                                       {'password': 'correct-pw'})
        self.assertEqual(st, 200)
        self.assertTrue(self.app.current_request().get('new_token'),
                        '登录令牌必须留在本次请求的上下文里')


class TestBackupExcludesSecrets(unittest.TestCase):
    """备份不能带明文凭据，也不能带认证状态。"""

    def setUp(self):
        self.store = Store(tempfile.mkdtemp(prefix='ac_bak_'))
        self.cfg = AppConfig()
        self.cfg.proxy = 'http://user:pass@10.0.0.1:7890'
        self.cfg.notify = NotifyConfig(
            pushplus_token='PP_SECRET',
            wecom_webhook='https://qyapi.weixin.qq.com/WECOM_SECRET',
            tg_bot_token='TG_SECRET', tg_chat_id='12345',
            proxy_url='http://u:p@10.0.0.2:1',
            channel_proxy={'telegram': {'mode': 'custom',
                                        'url': 'http://a:b@1.2.3.4:9'}})
        self.cfg.auth_password_enc = 'ENC_PW'
        self.cfg.session_hashes = ['HASH_OF_A_LIVE_SESSION']
        self.cfg.api_token_hashes = ['HASH_OF_A_SCRIPT_TOKEN']
        site = SiteConfig(id='s1', name='S1',
                          proxy_url='http://u2:p2@site-proxy:8080',
                          headers={'Authorization': 'Bearer SITE_SECRET'})
        self.cfg.sites = [site]
        self.out = repr(self.store.export_config(self.cfg))

    def test_plaintext_credentials_stripped(self):
        for label, needle in (('pushplus token', 'PP_SECRET'),
                              ('企业微信 webhook', 'WECOM_SECRET'),
                              ('TG bot token', 'TG_SECRET'),
                              ('TG chat id', '12345'),
                              ('全局代理账密', 'user:pass'),
                              ('通知级代理账密', 'u:p'),
                              ('渠道代理账密', 'a:b'),
                              ('站点代理账密', 'u2:p2'),
                              ('站点自定义头凭据', 'SITE_SECRET')):
            with self.subTest(label=label):
                self.assertNotIn(needle, self.out,
                                 '备份里不该出现%s' % label)

    def test_auth_state_stripped(self):
        """认证状态不进备份。

        改回去会怎样：能写备份文件的人（中间人/被攻陷的网盘）塞一条自己
        令牌的哈希或自选口令，用户点一次「从云端恢复」他就成了管理员。
        """
        for label, needle in (('登录口令密文', 'ENC_PW'),
                              ('会话哈希', 'HASH_OF_A_LIVE_SESSION'),
                              ('脚本令牌哈希', 'HASH_OF_A_SCRIPT_TOKEN')):
            with self.subTest(label=label):
                self.assertNotIn(needle, self.out, '备份里不该出现%s' % label)

    def test_encrypted_site_credentials_kept(self):
        """站点密文凭据要保留 —— 换机恢复靠它。"""
        site = SiteConfig(id='s1', name='S1', password_enc='ENC_SITE_PW')
        self.cfg.sites = [site]
        out = repr(self.store.export_config(self.cfg))
        self.assertIn('ENC_SITE_PW', out)

    def test_import_keeps_local_auth(self):
        """从备份恢复时必须保留本机现有认证状态。"""
        local = AppConfig()
        local.auth_required = True
        local.auth_password_enc = 'LOCAL_PW'
        local.session_hashes = ['LOCAL_SESSION']
        local.api_token_hashes = ['LOCAL_SCRIPT']

        malicious = {
            'auth_required': True,
            'auth_password_enc': 'plain:' + base64.b64encode(b'pwned').decode(),
            'session_hashes': ['ATTACKER_SESSION_HASH'],
            'api_token_hashes': ['ATTACKER_SCRIPT_HASH'],
            'sites': [{'id': 'x', 'name': 'X'}],
        }
        restored = self.store.import_config(malicious, keep_auth_from=local)
        self.assertEqual(restored.auth_password_enc, 'LOCAL_PW')
        self.assertEqual(restored.session_hashes, ['LOCAL_SESSION'])
        self.assertEqual(restored.api_token_hashes, ['LOCAL_SCRIPT'])
        # 但站点数据要正常恢复
        self.assertEqual([s.id for s in restored.sites], ['x'])


class TestPlainPrefixRequiresWeakMode(unittest.TestCase):
    """`plain:` 只在真的没有密钥时才接受。"""

    def test_rejected_when_key_present(self):
        """有真密钥时拒绝 plain: 值。

        改回去会怎样：任何能写配置/备份的人写一个
        auth_password_enc = "plain:" + base64(自选口令) 就能凭空设口令。
        """
        box = SecretBox(S.generate_key())
        self.assertTrue(box.enabled)
        with self.assertRaises(SecretError):
            box.decrypt('plain:' + base64.b64encode(b'chosen-pw').decode())

    def test_accepted_in_weak_mode(self):
        weak = SecretBox(None)
        self.assertTrue(weak.is_weak)
        self.assertEqual(
            weak.decrypt('plain:' + base64.b64encode(b'pw').decode()), 'pw')

    def test_normal_roundtrip_still_works(self):
        box = SecretBox(S.generate_key())
        self.assertEqual(box.decrypt(box.encrypt('真实密码')), '真实密码')

    def test_malformed_token_raises_secret_error(self):
        """畸形密文要包成 SecretError，不能让 ValueError 穿出去。

        改回去会怎样：HTTP 层把它当"用户输入错误"返回 400 并回显 stdlib
        原文；Cookie 巡检循环会整体中断，导致每个 tick 重跑一遍全站巡检。
        """
        box = SecretBox(S.generate_key())
        for bad in ('plain:!!!not-base64!!!', 'not-a-fernet-token!', '~~~~'):
            with self.subTest(bad=bad[:16]):
                with self.assertRaises(SecretError):
                    box.decrypt(bad)


class TestSetupCannotBeHijacked(unittest.TestCase):
    """配置损坏时不能开放 setup 入口。"""

    def _svc(self):
        return SV.Service(tempfile.mkdtemp(prefix='ac_setup_'),
                          version='t', env_key=S.generate_key(), proxy='')

    def test_fresh_install_needs_setup(self):
        svc = self._svc()
        self.assertTrue(AuthManager(svc).needs_setup(),
                        '全新安装（没有 config.json）应该允许初始化')

    def test_corrupted_config_does_not_allow_setup(self):
        """config.json 存在但坏了 -> 不开放 setup。

        改回去会怎样：局域网第一个访问者就能设口令接管容器，
        还会用默认配置覆盖用户原有的站点与凭据。
        """
        svc = self._svc()
        with open(svc.store.config_path, 'w', encoding='utf-8') as f:
            f.write('{"sites": [')          # 半截 JSON
        am = AuthManager(svc)
        self.assertFalse(am.needs_setup(),
                         '配置损坏时绝不能开放首次设置')

    def test_existing_config_without_password_does_not_allow_setup(self):
        """已初始化过、只是口令字段为空 -> 也不开放 setup。"""
        svc = self._svc()
        cfg = svc.load_config()
        cfg.auth_password_enc = ''
        svc.save_config(cfg)
        self.assertFalse(AuthManager(svc).needs_setup())

    def test_corrupted_config_already_setup_fails_cleanly(self):
        """损坏配置下调用 setup 要被拒，而不是被接管。"""
        svc = self._svc()
        with open(svc.store.config_path, 'w', encoding='utf-8') as f:
            f.write('{ broken')
        app = WB.WebApp(svc, version='t')
        with self.assertRaises(WB.ApiError) as ctx:
            app.dispatch('POST', '/api/auth/setup', {},
                         {'password': 'attacker-pw'})
        self.assertEqual(ctx.exception.status, 403)


class TestUserscriptOriginSanitized(unittest.TestCase):
    """油猴脚本里的容器地址必须来自白名单化的 host。"""

    def setUp(self):
        self.app = WB.WebApp(SV.Service(tempfile.mkdtemp(prefix='ac_js_'),
                                        version='t',
                                        env_key=S.generate_key(), proxy=''),
                             version='t')

    def test_injection_payload_rejected(self):
        evil = "http://nas.local';GM_xmlhttpRequest({url:'http://evil/'});"\
               "//"
        origin = self.app._public_origin({'host': [evil]})
        self.assertNotIn("'", origin)
        self.assertNotIn(';', origin)
        self.assertNotEqual(origin, evil)

    def test_generated_script_has_no_injection(self):
        evil = "http://nas.local';alert(1);//"
        js = WB.build_userscript(self.app._public_origin({'host': [evil]}))
        # CONTAINER 那行必须是一段合法的单引号字符串字面量
        m = re.search(r"var CONTAINER = ('[^']*');", js)
        self.assertIsNotNone(m, 'CONTAINER 应是单引号字面量')
        self.assertNotIn('alert(1)', m.group(1))

    def test_valid_hosts_accepted(self):
        cases = {
            'http://nas.local:28999': 'http://nas.local:28999',
            'nas.local:28999': 'http://nas.local:28999',
            'https://10.0.0.5': 'https://10.0.0.5',
            '192.168.1.10:8080': 'http://192.168.1.10:8080',
        }
        for raw, expect in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.app._public_origin({'host': [raw]}),
                                 expect)

    def test_bad_input_falls_back_to_default(self):
        for bad in ('', 'http://a b', 'http://h:99999', 'javascript:alert(1)'):
            with self.subTest(bad=bad):
                got = self.app._public_origin({'host': [bad]})
                self.assertTrue(got.startswith('http://127.0.0.1'),
                                '非法输入应退回本机默认值，得到 %r' % got)


class TestSiteIdSanitized(unittest.TestCase):
    """站点 id 必须在数据入口就收敛到安全字符集。

    改回去会怎样：id 会被拼进页面的内联事件属性（JS 字符串上下文），
    HTML 实体转义挡不住引号逃逸 —— 实测 id = `x'),alert(1),('y`
    点一下按钮就执行，可升级为密钥外泄。
    """

    def test_from_dict_sanitizes_dangerous_id(self):
        s = SiteConfig.from_dict({'id': "x'),alert(1),('y", 'name': 'N'})
        self.assertTrue(re.match(r'^[A-Za-z0-9][A-Za-z0-9_\-]*$', s.id), s.id)

    def test_from_dict_sanitizes_attribute_breakout(self):
        s = SiteConfig.from_dict({'id': 'x" onfocus="alert(1)//'})
        self.assertTrue(re.match(r'^[A-Za-z0-9][A-Za-z0-9_\-]*$', s.id), s.id)

    def test_legal_ids_unchanged(self):
        for good in ('v2ex', 'node-seek_2', 'a1'):
            s = SiteConfig.from_dict({'id': good, 'name': good})
            self.assertEqual(s.id, good)

    def test_id_never_empty(self):
        for bad in ('', '   ', '"', '../../x'):
            s = SiteConfig.from_dict({'id': bad})
            self.assertTrue(s.id, 'id 不能为空：%r -> %r' % (bad, s.id))


class TestRedirectStripsCredentials(unittest.TestCase):
    """跨源跳转不能把凭据带给第三方（详见 test_redirect_safety.py）。"""

    def test_origin_comparison(self):
        from app.httpclient import _origin_of
        self.assertNotEqual(_origin_of('http://h:1/a'), _origin_of('http://h:2/a'))
        self.assertNotEqual(_origin_of('http://a.com/'), _origin_of('https://a.com/'))
        self.assertEqual(_origin_of('https://a.com/x'), 'https://a.com:443')


class TestAtomicWriteTempName(unittest.TestCase):
    """临时文件名要带 pid+线程号，避免并发写同一个 tmp 写出混合内容。

    改回去会怎样：两个线程各持同一 tmp 的文件偏移量，可能写出混合 JSON，
    被判为损坏配置 -> 回退默认值 -> 触发"未初始化"分支。
    """

    @unittest.skipIf(os.name != 'posix',
                     'Windows 上并发 os.replace 同一路径会有共享冲突；'
                     '容器运行在 Linux，只在 POSIX 上断言')
    def test_concurrent_writes_do_not_corrupt(self):
        import json
        from app.store import atomic_write_json, read_json
        path = os.path.join(tempfile.mkdtemp(prefix='ac_atomic_'), 'c.json')
        errs = []

        def writer(n):
            try:
                for i in range(40):
                    atomic_write_json(path, {'writer': n, 'i': i}, private=True)
            except Exception as e:                              # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [], '并发写不应抛错：%s' % errs[:2])
        got = read_json(path, None)
        self.assertIsInstance(got, dict, '并发写后必须仍是合法 JSON')
        self.assertIn('writer', got)


class TestNoInlineEventInjection(unittest.TestCase):
    """不能再把数据拼进内联事件属性。

    为什么单独守：内联事件属性（onclick/onchange/onfocus…）里是 **JS 字符串
    上下文**，HTML 实体转义挡不住引号逃逸 —— 浏览器会先把 &#39; 解码回 '
    再交给 JS 解析器。实测 id = `x'),alert(1),('y` 点一下即执行。
    这个坑我第一次修的时候漏了 6 处 onchange，所以要有自动守卫。
    """

    APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'app')

    def _web_source(self):
        with open(os.path.join(self.APP, 'web.py'), encoding='utf-8') as f:
            return f.read()

    def test_site_form_survives_hostile_id(self):
        """最强的守卫：直接把真实输出拿出来，断言它**没被改写结构**。

        为什么不用正则扫源码：真实坏写法里的引号是 Python 转义引号（\\'），
        形态太多变，我连写了三版正则都抓不全 —— 假守卫比没有更糟。
        所以改成断言最终生成物。

        做法：把整个 <script> 喂给 node，但**先 stub 掉 document/window** ——
        脚本末尾有顶层 `document.querySelectorAll(...)`，裸 node 里没有 DOM，
        否则会 ReferenceError（我第一版就踩了，测试变成假失败）。
        """
        src = self._web_source()
        m = re.search(r'<script[^>]*>(.*?)</script>', src, re.S)
        self.assertIsNotNone(m, '没找到页面脚本')
        js = m.group(1)

        # 只执行"函数定义 + 顶层 var/const/let"，跳过所有顶层语句 ——
        # 否则脚本末尾的 boot() 会去 fetch('/api/meta')（node 里没有该接口），
        # 测试会变成假失败。这样既能调用 siteFormHtml，又不触发任何副作用。
        runner = (
            'const fs=require("fs");\n'
            # 最小 DOM/浏览器 stub：抽出来的函数体里会引用 document 等。
            # 不 stub 就会 ReferenceError（我前两版都栽在这）。
            'const __el={className:"",style:{},value:"",checked:false,'
            'textContent:"",innerHTML:"",dataset:{},options:[],'
            'setAttribute:function(){},getAttribute:function(){return "";},'
            'appendChild:function(){},addEventListener:function(){},'
            'querySelector:function(){return __el;},'
            'querySelectorAll:function(){return {forEach:function(){}};},'
            'closest:function(){return null;},classList:{add:function(){},'
            'remove:function(){},toggle:function(){},contains:function(){'
            'return false;}}};\n'
            'global.document={getElementById:function(){return __el;},'
            'querySelector:function(){return __el;},'
            'querySelectorAll:function(){return {forEach:function(){}};},'
            'createElement:function(){return __el;},'
            'addEventListener:function(){},body:__el,readyState:"complete"};\n'
            'global.window={addEventListener:function(){},'
            'location:{origin:"http://x",href:"http://x/",reload:function(){}}};\n'
            'global.location=global.window.location;\n'
            'global.localStorage={getItem:function(){return null;},'
            'setItem:function(){},removeItem:function(){}};\n'
            'global.alert=function(){};\n'
            'global.navigator={userAgent:"node"};\n'
            'const src=fs.readFileSync(process.argv[2],"utf8");\n'
            'const parts=[];\n'
            # 函数定义（含单个前导注释块）
            'const fnRe=/(?:\\/\\*[\\s\\S]*?\\*\\/\\s*)?function\\s+[A-Za-z_$][\\w$]*'
            '\\s*\\([^)]*\\)\\s*\\{[\\s\\S]*?\\n\\}/g;\n'
            'let m; while((m=fnRe.exec(src))) parts.push(m[0]);\n'
            # 顶层变量声明（可能多行，取到分号）
            'const varRe=/^(?:var|let|const)\\s+[A-Za-z_$][\\w$]*\\s*=[\\s\\S]*?;$/gm;\n'
            'while((m=varRe.exec(src))) parts.push(m[0]);\n'
            'const code=parts.join("\\n");\n'
            'const siteArg=%s;\n'
            'eval(code);\n'
            'const out=siteFormHtml(siteArg, {has_cookie:false,names:[]});\n'
            'console.log(JSON.stringify(out));\n') % json.dumps({
                'id': HOSTILE_ID, 'name': 'n', 'enabled': True,
                'need_browser': False, 'verify_ssl': True, 'mode': 'daily',
                'daily_hour': 3, 'daily_minute': 0, 'jitter_enabled': False,
                'jitter_seconds': 0, 'retry_enabled': True, 'retry_count': 3,
                'retry_interval_minutes': 30, 'ai_enabled': True,
                'success_interval_minutes': 1440, 'proxy_mode': 'inherit',
                'headers': {}, 'steps': []})

        node = _find_node()
        if not node:
            self.skipTest('没有可用的 node，跳过（JS 层检查由 docs/check_webjs.py 覆盖）')
        tmpdir = tempfile.mkdtemp(prefix='ac_xss_')
        with open(os.path.join(tmpdir, 'web.js'), 'w', encoding='utf-8') as f:
            f.write(js)
        run_file = os.path.join(tmpdir, 'run.js')
        with open(run_file, 'w', encoding='utf-8') as f:
            f.write(runner)

        r = subprocess.run([node, run_file, os.path.join(tmpdir, 'web.js')],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60)
        self.assertEqual(r.returncode, 0,
                         'JS 执行失败：%s' % (r.stderr or '')[:300])
        html = json.loads((r.stdout or '""').strip().splitlines()[-1] or '""')
        self.assertTrue(html, 'siteFormHtml 没产出内容')

        # ⚠️ 重要更正（我在这条测试上错了两次，如实记下）：
        # siteFormHtml 是**客户端渲染函数**，不是安全边界 —— 它拿到什么 id
        # 就渲染什么，不做清洗（实测：直接把恶意 id 传给它，确实会原样出现在
        # 属性里）。真正的边界有两层，分别在下面两个测试里验证：
        #   1) 服务端 SiteConfig.from_dict 用 safe_site_id 收敛 id（数据入口）
        #   2) 前端 esc() 对属性值做实体转义（防结构破坏）
        # 所以这里**不再**断言"恶意 id 不会出现"，只断言：
        #   * 函数能正常执行（不抛异常）
        #   * 控件走 data-* 而不是内联事件拼 id（纵深防御）
        #   * 关键控件存在（data-sid 在，委托监听才能工作）
        self.assertNotIn("swSync('", html,
                         '不该再用内联事件拼 id（JS 字符串上下文）')
        self.assertNotIn("swSiteProxy('", html)
        self.assertIn('data-sid=', html, '控件必须带 data-sid 供委托监听')
        self.assertIn('data-needs-sync=', html)
        # 属性值必须被 esc() 实体转义过（引号不能以裸形态出现）
        self.assertNotIn("data-sid=\"x'", html, '属性值里的引号必须被转义')
        # id 用 esc 转义后，单引号会变成 &#39;
        self.assertIn('&#39;', html, 'id 里的引号应被转义成实体')

    def test_server_side_is_the_real_boundary(self):
        """真正的边界：数据入口就收敛 id。

        这才是有意义的断言 —— 只要这里成立，'恶意 id' 根本到不了前端。
        """
        from app.models import SiteConfig, safe_site_id
        for hostile in (HOSTILE_ID, 'x" onfocus="alert(1)//',
                        "a'),fetch('http://evil/'),('b", '../../etc',
                        '<script>alert(1)</script>', ''):
            with self.subTest(hostile=hostile[:24]):
                sid = safe_site_id(hostile, 'fb')
                self.assertTrue(
                    re.match(r'^[A-Za-z0-9][A-Za-z0-9_\-]*$', sid),
                    'id 必须收敛到安全字符集：%r -> %r' % (hostile, sid))
                cfg = SiteConfig.from_dict({'id': hostile, 'name': 'n'})
                self.assertTrue(
                    re.match(r'^[A-Za-z0-9][A-Za-z0-9_\-]*$', cfg.id),
                    'from_dict 必须清洗 id：%r -> %r' % (hostile, cfg.id))

    def test_homepage_link_is_scheme_checked(self):
        """渲染成链接的地址必须走 safeLink（只允许 http/https）。"""
        src = self._web_source()
        self.assertIn('function safeLink(', src)
        # 站点主页那处必须用 safeLink，而不是裸 href + esc
        m = re.search(r"手动粘贴怎么拿[^\n]*", src)
        self.assertIsNotNone(m)
        self.assertIn('safeLink(s.homepage)', m.group(0))

    def test_safe_http_url(self):
        from app.service import safe_http_url as f
        self.assertEqual(f('https://a.com/x'), 'https://a.com/x')
        self.assertEqual(f('http://a.com'), 'http://a.com')
        self.assertEqual(f('www.a.com'), 'http://www.a.com')
        for bad in ('javascript:alert(1)', 'JavaScript:alert(1)',
                    'data:text/html,x', 'vbscript:x', '//evil.com/x',
                    'file:///etc/passwd', '', '   '):
            with self.subTest(bad=bad):
                self.assertEqual(f(bad), '', '危险协议必须丢弃：%r' % bad)

    def test_upsert_site_rejects_javascript_homepage(self):
        """数据入口就要挡掉 —— 录制的 start_url 也走这条路。"""
        svc = SV.Service(tempfile.mkdtemp(prefix='ac_home_'),
                         version='t', env_key=S.generate_key(), proxy='')
        svc.upsert_site({'id': 'x1', 'name': 'X',
                         'homepage': 'javascript:alert(document.domain)'})
        site = [s for s in svc.load_config().sites if s.id == 'x1'][0]
        self.assertNotIn('javascript', site.homepage.lower())


if __name__ == '__main__':
    unittest.main(verbosity=2)
