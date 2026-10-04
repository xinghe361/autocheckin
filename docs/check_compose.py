"""校验 docker-compose.yml 的结构（不依赖 pyyaml，用缩进解析）。

为什么值得单独做：compose 文件写错一个缩进，用户拿到的就是
"yaml: line N: did not find expected key" 这种没头没脑的报错，
而这个项目是要给非技术用户在极空间界面上直接粘贴的。
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def strip_comments(text: str):
    """去掉整行注释，返回 [(行号, 缩进, 内容)]。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        out.append((i, indent, line.strip()))
    return out


def check_indentation(rows):
    """同一层级里，缩进必须一致；子键必须比父键更深。"""
    problems = []
    stack = []          # [(indent, key)]
    for lineno, indent, content in rows:
        if indent % 2 != 0:
            problems.append('第 %d 行缩进是 %d（不是 2 的倍数）：%s'
                            % (lineno, indent, content))
        # 列表项
        if content.startswith('- '):
            if not stack:
                problems.append('第 %d 行出现列表项但没有父键：%s' % (lineno, content))
            continue
        m = re.match(r'^([\w.\-]+):(.*)$', content)
        if not m:
            problems.append('第 %d 行不是合法的 key: value：%s' % (lineno, content))
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack and indent <= stack[-1][0]:
            problems.append('第 %d 行缩进未比父键更深：%s' % (lineno, content))
        stack.append((indent, m.group(1)))
    return problems


def main() -> int:
    path = os.path.join(HERE, 'docker-compose.yml')
    text = open(path, encoding='utf-8').read()
    rows = strip_comments(text)

    print('=== docker-compose.yml 校验 ===')
    print('  有效配置行数: %d' % len(rows))

    problems = check_indentation(rows)
    if problems:
        for p in problems:
            print('  !! %s' % p)
    else:
        print('  缩进与键值结构: OK')

    # 关键字段必须在
    body = '\n'.join(c for _, _, c in rows)
    required = {
        'services:': '顶层 services',
        'autocheckin:': '服务名',
        'image:': '镜像',
        'security_opt:': '安全加固',
        'no-new-privileges:true': 'no-new-privileges 值',
        'shm_size:': '/dev/shm 大小',
        'volumes:': '数据卷',
        'environment:': '环境变量',
        'PORT:': '端口',
    }
    print('\n=== 关键字段 ===')
    missing = []
    for key, label in required.items():
        ok = key in body
        print('  %s %s' % ('[OK]  ' if ok else '[缺]  ', label))
        if not ok:
            missing.append(key)

    # host 模式不应出现 ports
    print('\n=== 模式一致性 ===')
    active_rows = [c for _, _, c in rows]
    has_network_host = 'network_mode: host' in active_rows
    has_ports = any(c.startswith('ports:') for c in active_rows)
    if has_network_host and has_ports:
        print('  !! 同时出现 network_mode: host 与 ports:（host 模式下 ports 无效）')
        missing.append('ports-conflict')
    elif has_network_host:
        print('  OK host 模式，且未被注释掉的部分没有 ports')
    else:
        print('  [提示] 未启用 host 模式')

    # 数据卷必须是绝对路径（相对路径在极空间上容易出意外）
    vol = re.search(r'- (/[^:]+):/data', body)
    if vol:
        print('  OK 数据卷是绝对路径: %s' % vol.group(1))
    else:
        print('  !! 数据卷未使用绝对路径挂载到 /data')

    print()
    bad = bool(problems or missing)
    print('结论：%s' % ('有问题，见上' if bad else '可以安全使用'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
