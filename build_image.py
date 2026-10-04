"""构建（并可选推送）Docker 镜像。

用法：
    python build_image.py --name yourname/autocheckin --tag latest
    python build_image.py --name yourname/autocheckin --tag latest --push
    python build_image.py --check            # 只做构建前的自检，不构建

自检内容（构建失败往往是因为这些）：
  * docker 是否可用
  * 必需的构建上下文文件是否齐全
  * 镜像基础层是否拉得动（可选，--check 会做一次轻量探测）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))

REQUIRED_FILES = [
    'Dockerfile',
    'requirements.txt',
    'app/__init__.py',
    'app/__main__.py',
    'app/main.py',
    'app/service.py',
    'app/web.py',
]

# 绝不能进入构建上下文 / 镜像的东西
FORBIDDEN_IN_CONTEXT = ['secret.key', 'config.json', 'state.json', '.env', 'nas.env']


def run(cmd: List[str], **kw) -> int:
    print('+ ' + ' '.join(cmd))
    return subprocess.call(cmd, cwd=HERE, **kw)


def capture(cmd: List[str]) -> str:
    try:
        out = subprocess.check_output(cmd, cwd=HERE, stderr=subprocess.STDOUT)
        return out.decode('utf-8', 'replace')
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return getattr(e, 'output', b'').decode('utf-8', 'replace') if hasattr(e, 'output') else str(e)


def preflight() -> List[str]:
    """返回问题列表（空表示没问题）。"""
    problems: List[str] = []

    if shutil.which('docker') is None:
        problems.append('找不到 docker 命令（请先安装 Docker 或在本机用 docker build）')
    else:
        info = capture(['docker', 'version', '--format', '{{.Server.Version}}'])
        if not info.strip() or 'error' in info.lower():
            problems.append('docker 守护进程不可用：%s' % info.strip()[:200])

    for rel in REQUIRED_FILES:
        if not os.path.exists(os.path.join(HERE, rel)):
            problems.append('缺少构建所需文件：%s' % rel)

    for name in FORBIDDEN_IN_CONTEXT:
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            problems.append('构建目录里出现了敏感文件 %s，请删除或加入 .dockerignore' % name)

    # .dockerignore 必须挡住数据与密钥
    di = os.path.join(HERE, '.dockerignore')
    if os.path.exists(di):
        text = open(di, encoding='utf-8').read()
        for must in ('secret.key', 'config.json', 'tests/'):
            if must not in text:
                problems.append('.dockerignore 里建议排除 %s' % must)
    else:
        problems.append('缺少 .dockerignore（会把测试和密钥打进镜像）')

    try:
        sys.path.insert(0, HERE)
        import app.main as M  # noqa: F401
    except Exception as e:  # noqa: BLE001
        problems.append('应用代码 import 失败：%s' % e)

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description='构建自动签到镜像')
    ap.add_argument('--name', default='autocheckin',
                    help='镜像名，例如 yourname/autocheckin')
    ap.add_argument('--tag', default='latest', help='镜像标签')
    ap.add_argument('--push', action='store_true', help='构建后推送到仓库')
    ap.add_argument('--no-cache', action='store_true', help='不使用构建缓存')
    ap.add_argument('--check', action='store_true', help='只做构建前自检')
    ap.add_argument('--platform', default='linux/amd64',
                    help='目标平台（极空间 Z4 是 x86_64）')
    args = ap.parse_args()

    print('=== 构建前自检 ===')
    problems = preflight()
    if problems:
        for p in problems:
            print('  [问题] %s' % p)
    else:
        print('  [OK] 自检通过')

    if args.check:
        return 1 if problems else 0

    if problems:
        print('\n自检未通过，已中止构建。修好后重试，或加 --check 只看自检。')
        return 1

    full = '%s:%s' % (args.name, args.tag)
    cmd = ['docker', 'build', '-t', full, '--platform', args.platform]
    if args.no_cache:
        cmd.append('--no-cache')
    cmd.append('.')

    print('\n=== 构建镜像 %s ===' % full)
    if run(cmd) != 0:
        print('构建失败。')
        return 1

    print('\n=== 镜像信息 ===')
    print(capture(['docker', 'images', full]))

    if args.push:
        print('=== 推送 %s ===' % full)
        if run(['docker', 'push', full]) != 0:
            print('推送失败（请先 docker login）。')
            return 1
        print('已推送：%s' % full)
    else:
        print('提示：加 --push 可推送到 Docker Hub。')

    print('\n完成。在极空间上用 compose 引用 %s 即可。' % full)
    return 0


if __name__ == '__main__':
    sys.exit(main())
