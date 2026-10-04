"""把项目推送到 GitHub（带完整的预检，避免推到一半才发现问题）。

用法:
    # 第一次：配置身份 + 指定仓库地址
    python push_to_github.py --repo https://github.com/你的用户名/autocheckin.git

    # 之后再次推送
    python push_to_github.py --repo <地址> --message "更新说明"

    # 只做检查，不真正推送
    python push_to_github.py --repo <地址> --check
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))


def run(args: List[str], check: bool = False, capture: bool = True
        ) -> Tuple[int, str]:
    p = subprocess.run(args, cwd=HERE, capture_output=capture,
                       text=True, encoding='utf-8', errors='replace')
    out = (p.stdout or '') + (p.stderr or '')
    if check and p.returncode != 0:
        raise SystemExit('命令失败：%s\n%s' % (' '.join(args), out))
    return p.returncode, out.strip()


def preflight(repo: str) -> List[str]:
    problems: List[str] = []

    if shutil.which('git') is None:
        problems.append('找不到 git 命令')
        return problems

    if not re.match(r'^(https://github\.com/[\w.\-]+/[\w.\-]+(\.git)?'
                    r'|git@github\.com:[\w.\-]+/[\w.\-]+(\.git)?)$', repo or ''):
        problems.append('仓库地址看起来不对：%s\n'
                        '   正确格式示例：https://github.com/用户名/autocheckin.git'
                        % repo)

    # 绝不能提交的东西
    forbidden = ['secret.key', 'config.json', 'state.json', 'nas.env', '.env']
    for name in forbidden:
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            problems.append('仓库目录里存在敏感文件 %s，请先删除' % name)

    # .gitignore 必须挡住数据与密钥
    gi = os.path.join(HERE, '.gitignore')
    if not os.path.exists(gi):
        problems.append('缺少 .gitignore')
    else:
        text = open(gi, encoding='utf-8').read()
        for must in ('secret.key', 'config.json', 'state.json', '__pycache__'):
            if must not in text:
                problems.append('.gitignore 建议包含 %s' % must)

    # 关键文件必须在
    for rel in ('README.md', 'Dockerfile', 'requirements.txt',
                'app/main.py', '.github/workflows/build.yml'):
        if not os.path.exists(os.path.join(HERE, rel)):
            problems.append('缺少关键文件 %s' % rel)

    # git 身份（首次提交需要）
    code, name = run(['git', 'config', '--global', 'user.name'])
    code2, email = run(['git', 'config', '--global', 'user.email'])
    if not name:
        problems.append('未配置 git 用户名：git config --global user.name "你的名字"')
    if not email:
        problems.append('未配置 git 邮箱：git config --global user.email "你的邮箱"')

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description='推送项目到 GitHub')
    ap.add_argument('--repo', required=True, help='GitHub 仓库地址')
    ap.add_argument('--branch', default='main')
    ap.add_argument('--message', default='自动签到：首个可用版本')
    ap.add_argument('--check', action='store_true', help='只检查不推送')
    ap.add_argument('--tag', default='', help='推送后额外打一个 tag，如 v1.0.0')
    args = ap.parse_args()

    print('=== 推送前检查 ===')
    problems = preflight(args.repo)
    if problems:
        for p in problems:
            print('  [问题] %s' % p)
    else:
        print('  [OK] 检查通过')

    if args.check:
        return 1 if problems else 0
    if problems:
        print('\n检查未通过，已中止。修好后重试。')
        return 1

    # 初始化仓库
    if not os.path.isdir(os.path.join(HERE, '.git')):
        print('\n=== 初始化 git 仓库 ===')
        code, out = run(['git', 'init', '-b', args.branch])
        print(out or '  已初始化')

    # 确保远程地址正确
    code, out = run(['git', 'remote', 'get-url', 'origin'])
    if code != 0:
        run(['git', 'remote', 'add', 'origin', args.repo])
        print('  已添加远程 origin')
    elif out.strip() != args.repo:
        run(['git', 'remote', 'set-url', 'origin', args.repo])
        print('  已更新远程地址为 %s' % args.repo)

    print('\n=== 暂存文件 ===')
    run(['git', 'add', '-A'])
    code, out = run(['git', 'status', '--porcelain'])
    staged = [l for l in out.splitlines() if l.strip()]
    print('  待提交 %d 项' % len(staged))
    # 二次确认：真的没有敏感文件被暂存
    bad = [l for l in staged
           if re.search(r'secret\.key|config\.json|state\.json|\.env$', l)]
    if bad:
        print('  !! 检测到敏感文件被暂存，已中止：')
        for l in bad:
            print('     %s' % l)
        run(['git', 'reset'])
        return 1

    if staged:
        print('\n=== 提交 ===')
        code, out = run(['git', 'commit', '-m', args.message])
        print(out[-800:])
        if code != 0:
            print('提交失败')
            return 1
    else:
        print('\n没有新改动需要提交。')

    print('\n=== 推送到 %s ===' % args.repo)
    print('（首次推送会要求登录 GitHub：用户名 + Personal Access Token）')
    code, out = run(['git', 'push', '-u', 'origin', args.branch], capture=False)
    if code != 0:
        print('\n推送失败。常见原因：')
        print('  1) 仓库还没在 GitHub 上创建 → 先去 GitHub 点 New repository 建一个空的')
        print('  2) 需要 Token 而不是密码 → 见下方说明')
        return 1
    print('推送成功。')

    if args.tag:
        print('\n=== 打标签 %s 并推送（会触发 GitHub Actions 构建镜像）===' % args.tag)
        run(['git', 'tag', args.tag])
        code, out = run(['git', 'push', 'origin', args.tag], capture=False)
        print('标签推送%s' % ('成功' if code == 0 else '失败'))

    print('\n完成。接下来去 GitHub 仓库页面配置 Secrets（见 README 或我给你的步骤）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
