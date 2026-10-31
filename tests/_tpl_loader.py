"""测试辅助：从 site-plugins/*.json 加载模板。

内置模板（BUILTIN）已被移除，三个站点（v2ex/nodeseek/chiphell）与其它站点
统一放在仓库的 site-plugins/*.json 里。测试要验证这些真实模板的正确性，
所以直接读 JSON，而不是依赖已经不存在的内置常量。
"""

import json
import os
import shutil

SITE_PLUGINS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'site-plugins')


def load(sid: str):
    """按站点 id 读取 site-plugins/<sid>.json，返回模板 dict。"""
    path = os.path.join(SITE_PLUGINS, '%s.json' % sid)
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def all_ids():
    """site-plugins 下所有模板的 id 列表。"""
    out = []
    for name in sorted(os.listdir(SITE_PLUGINS)):
        if name.lower().endswith('.json'):
            out.append(name[:-5])
    return out


def seed_plugins(svc, *ids):
    """把 site-plugins 下的模板复制到 Service 的插件目录并重载。

    运行时插件读的是 DATA_DIR/templates/*.json（由同步或手动放入），
    测试里的 data_dir 是临时目录、本来没有模板。这个函数把仓库里的
    真实模板塞进去，模拟"用户已同步安装"，再 reload 让 provider 生效。
    """
    tpl_dir = svc.plugin_dir()
    os.makedirs(tpl_dir, exist_ok=True)
    for sid in (ids or all_ids()):
        shutil.copy(os.path.join(SITE_PLUGINS, '%s.json' % sid),
                    os.path.join(tpl_dir, '%s.json' % sid))
    svc.reload_plugins()

