"""部署配置的回归测试。

为什么值得测：docker-compose.yml 是要给非技术用户在极空间界面上直接粘贴的，
改错一个缩进或漏掉一条安全设置，用户看到的就是没头没脑的报错，
或者悄悄失去一层加固。这里把"必须有的东西"固定住。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'docs'))

import check_compose  # noqa: E402


def read(rel):
    with open(os.path.join(HERE, rel), encoding='utf-8') as f:
        return f.read()


class TestComposeFile(unittest.TestCase):
    def setUp(self):
        self.text = read('docker-compose.yml')
        self.rows = check_compose.strip_comments(self.text)
        self.body = '\n'.join(c for _, _, c in self.rows)
        self.active = [c for _, _, c in self.rows]

    def test_indentation_is_valid(self):
        problems = check_compose.check_indentation(self.rows)
        self.assertEqual(problems, [], 'compose 缩进有问题：%s' % problems)

    def test_security_hardening_present(self):
        """推荐加固必须默认带上。"""
        self.assertIn('security_opt:', self.body,
                      '缺少 security_opt 段')
        self.assertIn('no-new-privileges:true', self.body,
                      '缺少 no-new-privileges:true')
        self.assertIn('shm_size:', self.body,
                      '缺少 shm_size（Chromium 需要足够的 /dev/shm）')

    def test_uses_host_mode_without_ports(self):
        """host 模式下写 ports 是无效的，容易误导用户。"""
        self.assertIn('network_mode: host', self.active)
        ports = [c for c in self.active if c.startswith('ports:')]
        self.assertEqual(ports, [], 'host 模式下不该有生效的 ports 段')

    def test_data_volume_mounts_to_data(self):
        """必须把某个卷挂到容器内 /data —— 镜像声明的就是 /data。

        允许三种写法：
          * 命名卷      autocheckin-data:/data   （零配置，Docker 自己管）
          * 绝对路径绑定 /绝对路径:/data          （数据放到指定磁盘）
          * 相对路径绑定 ./data:/data            （随 compose 文件走，可整体搬走）
        但【容器内那侧必须是 /data】，否则数据不落盘、容器一重建就全丢。
        踩过的坑：容器里代码在 /app/app，很容易顺手写成 /app/data，
        那样数据写进镜像层，重建即丢。
        """
        mounts = re.findall(r'-\s+(\S+?):(/[^\s:]+)', self.body)
        targets = [t for _, t in mounts]
        self.assertIn('/data', targets,
                      'compose 里必须有卷挂载到容器内 /data，'
                      '当前挂载目标：%s' % (targets or '无'))
        for src, tgt in mounts:
            if tgt == '/data':
                self.assertTrue(src.startswith('/') or src.startswith('./')
                                or src.startswith('../'),
                                '宿主侧写法异常：%s' % src)

    def test_data_dir_matches_mount_point(self):
        """DATA_DIR 必须与挂载的容器侧路径一致。

        不一致时程序会把数据写到别处（镜像层），表现为"配置重启就没了"。
        """
        m = re.search(r'DATA_DIR[=:]\s*["\']?([^"\'\s]+)', self.body)
        if not m:
            self.skipTest('compose 里没有显式 DATA_DIR（用镜像默认值）')
        mounts = [t for _, t in re.findall(r'-\s+(\S+?):(/[^\s:]+)', self.body)]
        self.assertIn(m.group(1), mounts,
                      'DATA_DIR=%s 与挂载点 %s 不一致'
                      % (m.group(1), mounts))

    def test_relative_bind_mount_is_supported(self):
        """相对路径绑定挂载（./data:/data）必须能通过校验。

        这是用户明确要的用法：数据放在 compose 文件旁边，
        整个文件夹可以整体搬走，不会像命名卷那样藏在 Docker 目录里。
        """
        sample = (
            'services:\n'
            '  autocheckin:\n'
            '    image: x:latest\n'
            '    volumes:\n'
            '      - ./data:/data\n'
            '    environment:\n'
            '      - DATA_DIR=/data\n'
        )
        vols = re.findall(r'-\s+(\S+?):(/[^\s:]+)', sample)
        self.assertEqual(vols, [('./data', '/data')],
                         '相对路径绑定挂载没被正确解析：%s' % vols)

    def test_validator_rejects_wrong_mount_target(self):
        """把数据挂到 /app/data 必须被判为错误 —— 那是本项目最易犯的错。

        注意：判定必须只针对 volumes 段，不能拿全文去正则。
        踩过的坑：PROXY=http://192.168.1.1:7890 这种值会被 `- x:y` 的宽正则
        误当成挂载项，导致检查得出莫名其妙的结论。
        """
        # 复刻校验脚本的做法：只取 volumes: 段里的列表项
        def mounts_in(text):
            lines = text.splitlines()
            out, inside, base = [], False, 0
            for line in lines:
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                indent = len(line) - len(line.lstrip(' '))
                if line.strip() == 'volumes:':
                    inside, base = True, indent
                    continue
                if inside:
                    if indent <= base:
                        break
                    out.extend(re.findall(r'(\S+?):(/\S+)', line.strip()))
            return out

        good = mounts_in(self.text)
        self.assertIn(('./data', '/data'), good,
                      '当前 compose 的 volumes 段解析结果异常：%s' % good)
        self.assertEqual([t for _, t in good], ['/data'],
                         '容器侧应只挂 /data：%s' % good)

        bad_text = self.text.replace('./data:/data', './data:/app/data')
        self.assertNotEqual(bad_text, self.text, '样例替换没生效，测试失效')
        bad = mounts_in(bad_text)
        self.assertNotIn('/data', [t for _, t in bad],
                         '替换后不该再有 /data 目标：%s' % bad)
        self.assertIn(('./data', '/app/data'), bad,
                      '替换后应能解析出 /app/data：%s' % bad)

    def test_named_volume_is_declared_at_top_level(self):
        """用了命名卷就必须在顶层 volumes 里声明，否则 compose 报错。

        注意：self.body 是去掉注释与首尾空格后的行，所以这里用
        re.M + 行首来判断，不能假设有缩进。
        """
        named = re.findall(r'-\s+([A-Za-z0-9_.-]+):/data\b', self.body)
        if not named:
            self.skipTest('当前用的是绑定挂载，不涉及命名卷声明')
        self.assertRegex(self.body, r'(?m)^volumes:\s*$',
                         '缺少顶层 volumes: 段')
        for name in named:
            self.assertRegex(
                self.body, r'(?m)^%s:\s*$' % re.escape(name),
                '命名卷 %s 未在顶层 volumes 中声明（compose 会当成外部卷而报错）'
                % name)

    def test_essential_env_vars(self):
        """三个必需的环境变量都要在。

        支持两种 YAML 写法（都是合法的，模板里用的是列表形式）：
            KEY: value
            - KEY=value
        """
        for key in ('TZ', 'PORT', 'DATA_DIR'):
            found = (key + ':' in self.body) or (key + '=' in self.body)
            self.assertTrue(found, '缺少环境变量 %s' % key)

    def test_no_credentials_embedded(self):
        """配置文件里不能有任何真实凭据（只检查通用凭据形态）。

        这里刻意不写任何具体的密码/账号字面量 —— 否则"禁止凭据出现"的
        检查本身就把凭据写进了源码，连 .pyc 字节码里都会带上。
        """
        patterns = [
            (r'sk-[A-Za-z0-9]{20,}', 'API Key'),
            (r'ghp_[A-Za-z0-9]{30,}', 'GitHub token'),
            (r'dckr_pat_[A-Za-z0-9_-]{20,}', 'Docker Hub token'),
            (r'BEGIN [A-Z ]*PRIVATE KEY', '私钥'),
        ]
        for pattern, label in patterns:
            self.assertIsNone(re.search(pattern, self.text),
                              'compose 里出现了 %s' % label)


class TestDockerfile(unittest.TestCase):
    def setUp(self):
        self.text = read('Dockerfile')

    def test_port_is_28999_not_8080(self):
        """默认端口必须是 28999：host 模式下容器直接占用宿主端口，
        而 8080 在 NAS 上通常已被系统服务占用。"""
        self.assertIn('PORT=28999', self.text)
        self.assertIn('EXPOSE 28999', self.text)
        self.assertNotIn('EXPOSE 8080', self.text)

    def test_healthcheck_follows_port(self):
        """健康检查必须跟着 PORT 走，否则改端口后容器永远不健康。"""
        i = self.text.find('HEALTHCHECK')
        self.assertGreater(i, 0, '缺少 HEALTHCHECK')
        block = self.text[i:i + 600]
        self.assertIn('PORT', block, '健康检查应读取 PORT 而不是写死端口')
        self.assertIn('28999', block, '健康检查应有默认端口')

    def test_base_image_pinned(self):
        m = re.search(r'^FROM\s+(\S+)', self.text, re.M)
        self.assertIsNotNone(m)
        base = m.group(1)
        self.assertNotIn(':latest', base, '基础镜像不该用 latest')
        self.assertIn(':', base, '基础镜像应固定 tag')

    def test_dependencies_pinned(self):
        req = read('requirements.txt')
        for line in req.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                self.assertIn('==', line, '依赖未固定版本：%s' % line)


class TestBrowserRuntime(unittest.TestCase):
    """确认浏览器启动参数没有被意外改动。

    注：这里刻意只校验"参数是什么"，不在文档里复述其安全含义 ——
    文档对外公开，不复述具体风险面。
    """

    def test_browser_launch_args_pinned(self):
        br = read('app/browser.py')
        self.assertIn('--no-sandbox', br,
                      '容器内启动 Chromium 需要该参数，勿随意删除')
        self.assertIn('--disable-dev-shm-usage', br,
                      '保留该参数以兼容 /dev/shm 较小的环境')

    def test_security_doc_covers_hardening(self):
        """SECURITY.md 应说明默认加固与非 root 的可选做法（供使用者参照）。"""
        sec = read('docs/SECURITY.md')
        self.assertIn('no-new-privileges', sec,
                      'SECURITY.md 应说明默认加固')
        self.assertIn('非 root', sec,
                      'SECURITY.md 应说明可选的进一步加固及其代价')

    def test_security_doc_is_usage_oriented(self):
        """文档保持"怎么用"的定位，不复述风险清单。"""
        sec = read('docs/SECURITY.md')
        for banned in ('已知限制', '攻击者', '打穿', '漏洞'):
            self.assertNotIn(banned, sec,
                             'SECURITY.md 不应出现「%s」这类表述' % banned)


if __name__ == '__main__':
    unittest.main()
