"""凭据加密：用 Fernet（AES-128-CBC + HMAC）加密站点密码 / API Key / WebDAV 密码。

密钥来源（按优先级）：
  1) 环境变量 AUTOCHECKIN_KEY（推荐，compose 里给）
  2) 数据目录下的 secret.key（首次自动生成，权限 600）

设计原则：配置文件中永远不出现明文凭据；导出给 WebDAV 的备份也可以安全地包含密文，
但没有 key 的机器无法解开（换机时随 compose 一起提供 key 即可）。
"""

from __future__ import annotations

import base64
import os
import stat
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
    HAVE_CRYPTO = True
except ImportError:      # pragma: no cover - 容器里会装
    Fernet = None        # type: ignore
    InvalidToken = Exception  # type: ignore
    HAVE_CRYPTO = False

KEY_ENV = 'AUTOCHECKIN_KEY'
KEY_FILENAME = 'secret.key'

# 未配置密钥时的占位：允许运行，但明确标记"未加密"
PLAIN_PREFIX = 'plain:'


class SecretError(Exception):
    pass


def generate_key() -> str:
    """生成一个新的 Fernet 密钥（urlsafe base64）。"""
    if not HAVE_CRYPTO:
        raise SecretError('未安装 cryptography，无法生成密钥')
    return Fernet.generate_key().decode('ascii')


def load_or_create_key(data_dir: str, env_key: Optional[str] = None) -> str:
    """取得密钥：优先环境变量，其次数据目录里的 key 文件（没有就生成）。"""
    key = (env_key if env_key is not None else os.environ.get(KEY_ENV) or '').strip()
    if key:
        _validate(key)
        return key

    path = os.path.join(data_dir, KEY_FILENAME)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            existing = f.read().strip()
        if existing:
            _validate(existing)
            return existing

    new_key = generate_key()
    os.makedirs(data_dir, exist_ok=True)
    # 先以 600 权限创建，避免出现"短暂可读"的窗口
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, new_key.encode('ascii'))
    finally:
        os.close(fd)
    # os.open 的 mode 只在创建时生效；文件已存在时不会被收紧，这里显式再设一次
    if os.name == 'posix':
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return new_key


def _validate(key: str) -> None:
    try:
        Fernet(key.encode('ascii'))
    except Exception as e:  # noqa: BLE001
        raise SecretError('密钥格式不正确（应为 Fernet urlsafe base64）：%s' % e) from e


class SecretBox:
    """加解密入口。

    关于"没有密钥就退化为明文（带 plain: 前缀）"这条路径：
    它**不是加密**，只是 base64，等于把密码明文放在配置里。
    正常启动会由 load_or_create_key() 保证一定有密钥，所以这条路径只会在
    异常情况（如直接构造 SecretBox(None)、或未安装 cryptography）下走到。
    这里仍然保留它，但对外提供 is_weak 让上层能明确告警，
    而不是让人误以为"已经加密了"。
    """

    def __init__(self, key: Optional[str] = None):
        self.key = key
        self._fernet = None
        if key:
            if not HAVE_CRYPTO:
                raise SecretError('提供了密钥但未安装 cryptography')
            _validate(key)
            self._fernet = Fernet(key.encode('ascii'))

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    @property
    def is_weak(self) -> bool:
        """True 表示当前是"伪加密"（可逆的 base64），配置里等于明文。"""
        return self._fernet is None

    def weak_reason(self) -> str:
        if not self.is_weak:
            return ''
        if not HAVE_CRYPTO:
            return '未安装 cryptography，凭据只能以可逆形式保存'
        return '没有可用密钥，凭据以可逆形式保存（不等于加密）'

    def encrypt(self, plaintext: str) -> str:
        """加密；明文为空时返回空串（便于区分"未设置"）。"""
        if plaintext is None or plaintext == '':
            return ''
        if not self._fernet:
            # 没密钥时不假装安全，带上明确前缀
            return PLAIN_PREFIX + base64.b64encode(plaintext.encode('utf-8')).decode('ascii')
        return self._fernet.encrypt(plaintext.encode('utf-8')).decode('ascii')

    def decrypt(self, token: str) -> str:
        """解密；无法解密时抛 SecretError（不要静默返回空，否则会拿着空密码反复登录）。"""
        if not token:
            return ''
        if token.startswith(PLAIN_PREFIX):
            return base64.b64decode(token[len(PLAIN_PREFIX):]).decode('utf-8')
        if not self._fernet:
            raise SecretError('凭据是加密的，但当前没有可用密钥（请提供 %s）' % KEY_ENV)
        try:
            return self._fernet.decrypt(token.encode('ascii')).decode('utf-8')
        except InvalidToken as e:
            raise SecretError('凭据解密失败：密钥不匹配或数据损坏') from e

    def try_decrypt(self, token: str, default: str = '') -> str:
        """宽容版：解不开就返回 default。

        ⚠️ 只在"判断有没有配过"这种场合用。
        真正要拿密码去登录时请用 decrypt()，否则密钥不匹配会被静默吞掉，
        最终表现为"登录失败"，让人完全找不到原因（踩过这个坑）。
        """
        try:
            return self.decrypt(token)
        except SecretError:
            return default

    def decrypt_checked(self, token: str) -> tuple:
        """返回 (明文, 错误信息)。

        给"要用密码做事"的调用方使用：能明确区分三种情况，
        便于在界面/通知里给出准确原因，而不是笼统的"登录失败"。
            ('', '')                 字段本就为空（未配置）
            ('pw', '')               成功
            ('', '密钥不匹配…')       有密文但解不开
        """
        if not token:
            return '', ''
        try:
            return self.decrypt(token), ''
        except SecretError as e:
            return '', str(e)

    def mask(self, plaintext: str, keep: int = 3) -> str:
        """给界面显示用的打码形式，避免把密钥回显到页面上。"""
        if not plaintext:
            return ''
        if len(plaintext) <= keep:
            return '*' * len(plaintext)
        return plaintext[:keep] + '*' * (len(plaintext) - keep)
