"""WebDAV 备份：把配置与运行状态备份到用户的 WebDAV。

与之前油猴脚本里验证过的方案保持一致：
  * Basic 认证
  * 先 MKCOL 建目录（已存在返回 405 视为正常）
  * PUT 上传；PUT 收到 409 时给出"目录不存在或没权限"的明确提示
  * 路径中的中文/空格自动百分号编码
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .httpclient import HttpConfig, HttpError, enc_json, request

# 备份目录名（英文，简短）
BACKUP_DIR = 'autocheckin'
CONFIG_FILE = 'config.json'
STATE_FILE = 'state.json'
META_FILE = 'backup.json'
# 密钥单独一个文件，**刻意与备份分开**
#
# 为什么不把密钥塞进 backup.json：
#   备份里含的是【密文】。如果密钥和密文放在同一个文件里，那等于把锁和钥匙
#   捆一起——加密就完全失去意义了。所以单独一个文件，你可以选择：
#     · 不备份密钥（最安全，但要自己保存好 key）
#     · 备份密钥（图省事，但要明白：谁拿到这个文件就能解开你所有密码）
KEY_FILE = 'encryption-key.txt'
# 备份格式版本，便于日后迁移
BACKUP_VERSION = 1


def encode_path(path: str) -> str:
    """对路径各段做百分号编码（保留 / 分隔）。"""
    segs = str(path or '').split('/')
    return '/'.join(urllib.parse.quote(s, safe='') for s in segs)


def validate_url_safety(url: str, allow_private: bool = False) -> str:
    """校验 WebDAV 地址格式。

    只允许 http/https；allow_private=False 时拒绝回环与保留地址
    （WebDAV 一般就在内网，所以默认是允许的）。
    """
    import ipaddress
    import socket

    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise ValueError('WebDAV 地址只支持 http/https，当前是 %r' % parsed.scheme)
    host = parsed.hostname or ''
    if not host:
        raise ValueError('WebDAV 地址缺少主机名')

    if allow_private:
        return url

    # 解析成 IP 后判断是否内网/回环
    try:
        infos = socket.getaddrinfo(host, parsed.port or
                                  (443 if scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)
        ips = {i[4][0] for i in infos}
    except Exception:                                           # noqa: BLE001
        ips = set()

    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(
                'WebDAV 地址指向回环/保留地址（%s）。'
                '如果你确实要连内网的 WebDAV，请在设置里打开"允许内网地址"。' % ip)
    return url


def normalize_base_url(raw: str) -> str:
    """规整 WebDAV 地址：补协议、编码路径、保证以 / 结尾。"""
    url = str(raw or '').strip()
    if not url:
        raise ValueError('WebDAV 地址为空')
    if '://' not in url:
        url = 'https://' + url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError('WebDAV 地址格式不正确：%s' % raw)
    # 路径先解码再统一编码，避免用户填了已编码的地址导致二次编码（%25）
    path = urllib.parse.unquote(parsed.path or '/')
    if not path.endswith('/'):
        path += '/'
    return '%s://%s%s' % (parsed.scheme, parsed.netloc, encode_path(path))


@dataclass
class WebdavClient:
    """最小可用的 WebDAV 客户端。

    allow_private：WebDAV 通常就在内网（用户自己的 NAS），所以默认允许；
    但会记录一次提示，让用户知道本服务会向该地址发起请求。
    置为 False 时（例如暴露到公网的环境）会拒绝回环/内网地址。
    """

    base_url: str
    username: str = ''
    password: str = ''
    proxy: str = ''
    timeout: int = 30
    verify_ssl: bool = True
    allow_private: bool = True

    def __post_init__(self):
        validate_url_safety(self.base_url, allow_private=self.allow_private)
        self.base_url = normalize_base_url(self.base_url)
        self.root = self.base_url + BACKUP_DIR + '/'

    # ------------------------------------------------------------------
    def _cfg(self, extra_headers: Optional[Dict[str, str]] = None) -> HttpConfig:
        headers: Dict[str, str] = {}
        if self.username or self.password:
            import base64
            raw = ('%s:%s' % (self.username, self.password)).encode('utf-8')
            headers['Authorization'] = 'Basic ' + base64.b64encode(raw).decode('ascii')
        if extra_headers:
            headers.update(extra_headers)
        return HttpConfig(timeout=self.timeout, verify_ssl=self.verify_ssl,
                          headers=headers)

    def url_of(self, name: str) -> str:
        return self.root + encode_path(name)

    def mkcol(self, url: str) -> bool:
        """建目录；已存在（405/301）也算成功。"""
        try:
            resp = request('MKCOL', url, cfg=self._cfg(), global_proxy=self.proxy)
        except HttpError:
            return False
        return resp.status in (200, 201, 301, 405)

    def ensure_dirs(self) -> None:
        """确保备份目录存在。"""
        self.mkcol(self.root)

    def put(self, name: str, data: bytes,
            content_type: str = 'application/json') -> None:
        resp = request('PUT', self.url_of(name), cfg=self._cfg(
            {'Content-Type': content_type}), global_proxy=self.proxy, data=data)
        if resp.status in (200, 201, 204):
            return
        if resp.status == 409:
            raise WebdavError('上传失败（409）：目标目录不存在或账号没有写权限。'
                              '请确认 WebDAV 地址正确，且已开启对该目录的读写权限。')
        if resp.status == 401:
            raise WebdavError('上传失败（401）：WebDAV 用户名或密码不正确。')
        if resp.status == 403:
            raise WebdavError('上传失败（403）：账号被拒绝访问该目录。')
        raise WebdavError('上传失败（HTTP %d）' % resp.status)

    def get(self, name: str) -> Optional[str]:
        """读取文件；不存在返回 None。"""
        resp = request('GET', self.url_of(name), cfg=self._cfg(),
                       global_proxy=self.proxy)
        if resp.status == 404:
            return None
        if resp.status >= 400:
            raise WebdavError('下载失败（HTTP %d）' % resp.status)
        return resp.text

    def get_json(self, name: str) -> Optional[Any]:
        text = self.get(name)
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as e:
            raise WebdavError('%s 不是合法 JSON（可能被中转页面污染）' % name) from e

    def put_json(self, name: str, data: Any) -> None:
        self.put(name, enc_json(data))


class WebdavError(Exception):
    pass


def backup_payload(cfg_dict: Dict[str, Any], state_dict: Dict[str, Any],
                   app_version: str = '') -> Dict[str, Any]:
    """构造备份内容。"""
    import time
    return {
        'version': BACKUP_VERSION,
        'app': 'autocheckin',
        'app_version': app_version,
        'saved_at': int(time.time()),
        'config': cfg_dict,
        'state': state_dict,
    }


def do_backup(client: WebdavClient, cfg_dict: Dict[str, Any],
              state_dict: Dict[str, Any], app_version: str = '') -> Dict[str, Any]:
    """执行一次备份，返回概要。"""
    client.ensure_dirs()
    payload = backup_payload(cfg_dict, state_dict, app_version)
    client.put_json(META_FILE, payload)
    return {
        'ok': True,
        'saved_at': payload['saved_at'],
        'url': client.url_of(META_FILE),
        'bytes': len(enc_json(payload)),
    }


def do_restore(client: WebdavClient) -> Dict[str, Any]:
    """从备份恢复，返回 {config, state, saved_at}。"""
    data = client.get_json(META_FILE)
    if not data:
        raise WebdavError('云端没有找到备份文件（%s）' % META_FILE)
    if not isinstance(data, dict) or 'config' not in data:
        raise WebdavError('备份文件格式不正确')
    return {
        'config': data.get('config') or {},
        'state': data.get('state') or {},
        'saved_at': int(data.get('saved_at') or 0),
        'version': int(data.get('version') or 0),
    }


def test_connection(client: WebdavClient) -> Dict[str, Any]:
    """测试连接：建目录 + 写入并读回一个探针文件。"""
    client.ensure_dirs()
    probe = '.write-test'
    client.put(probe, b'{"ok":true}', content_type='application/json')
    back = client.get_json(probe)
    if not back or not back.get('ok'):
        raise WebdavError('能写入但读回失败，请检查目录权限')
    return {'ok': True, 'root': client.root}


# ------------------------------------------------------------------ 密钥备份

def save_key(client: WebdavClient, key: str) -> Dict[str, Any]:
    """把加密密钥单独备份到 WebDAV。

    提醒（也会写进备份文件的开头，免得日后忘了）：
        这个文件是解开已保存凭据的唯一钥匙，请存放在只有你自己能访问的地方。
    """
    if not key or not str(key).strip():
        raise WebdavError('密钥为空，不进行备份')
    client.ensure_dirs()
    body = (
        '# 自动签到 · 凭据加密密钥（AUTOCHECKIN_KEY）\n'
        '#\n'
        '# 用途：容器环境变量 AUTOCHECKIN_KEY 填这一行的值，\n'
        '#       才能解开 config/backup 里已加密的站点密码与 API Key。\n'
        '#\n'
        '# 请妥善保管，并确保存放它的目录只有你自己能访问。\n'
        '# 如果不想把它放在云端，可以改成自己抄到密码管理器里保存。\n'
        '#\n'
        '%s\n' % str(key).strip()
    )
    client.put(KEY_FILE, body.encode('utf-8'), content_type='text/plain; charset=utf-8')
    return {'ok': True, 'url': client.url_of(KEY_FILE),
            'warning': '密钥已备份到云端，请确保该目录只有你自己能访问'}


def load_key(client: WebdavClient) -> Optional[str]:
    """从 WebDAV 取回密钥（取不到返回 None）。"""
    text = client.get(KEY_FILE)
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        return s
    return None


def key_exists(client: WebdavClient) -> bool:
    try:
        return bool(load_key(client))
    except Exception:                                       # noqa: BLE001
        return False
