"""netutil 单测：验证强制 IPv4 与错误分类（不依赖真实网络）。

注意测试卫生：force_ipv4 会替换全局 socket.getaddrinfo，
所以每个用例前后都必须把 netutil._REAL_GETADDRINFO 与 socket.getaddrinfo
都恢复成"导入时的真实实现"，否则用例之间会互相污染（这个坑踩过一次）。
"""

import os
import socket
import ssl
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import netutil  # noqa: E402

# 在测试模块导入时固定真实实现，作为所有用例的还原目标
TRUE_GETADDRINFO = netutil._REAL_GETADDRINFO


def fake_addrinfo(entries):
    """构造一个假的 getaddrinfo，返回给定的 (family, ip) 组合。"""
    def _f(host, port, *a, **k):
        out = []
        for fam, ip in entries:
            sockaddr = (ip, port) if fam == socket.AF_INET else (ip, port, 0, 0)
            out.append((fam, socket.SOCK_STREAM, 6, '', sockaddr))
        return out
    return _f


class NetutilTestCase(unittest.TestCase):
    def setUp(self):
        netutil._REAL_GETADDRINFO = TRUE_GETADDRINFO
        socket.getaddrinfo = TRUE_GETADDRINFO
        netutil.force_ipv4._depth = 0

    def tearDown(self):
        netutil._REAL_GETADDRINFO = TRUE_GETADDRINFO
        socket.getaddrinfo = TRUE_GETADDRINFO
        netutil.force_ipv4._depth = 0


class TestForceIPv4(NetutilTestCase):
    def test_filters_out_ipv6_when_ipv4_present(self):
        """同时有 v4/v6 时，只返回 v4（这是 v2ex / nodeseek 的真实情况）。"""
        netutil._REAL_GETADDRINFO = fake_addrinfo([
            (socket.AF_INET6, '2a03:2880:f12c:183::25de'),
            (socket.AF_INET, '103.6.220.1'),
        ])
        got = netutil.ipv4_only_getaddrinfo('example.com', 443)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], socket.AF_INET)
        self.assertEqual(got[0][4][0], '103.6.220.1')

    def test_falls_back_when_no_ipv4(self):
        """纯 IPv6 站点不能直接被过滤成空，否则连错误信息都拿不到。"""
        netutil._REAL_GETADDRINFO = fake_addrinfo([
            (socket.AF_INET6, '2a03:2880:f12c:183::25de'),
        ])
        got = netutil.ipv4_only_getaddrinfo('v6only.example.com', 443)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], socket.AF_INET6)

    def test_prefers_ipv4_order(self):
        """即使 v6 排在前，结果里也只剩 v4。"""
        netutil._REAL_GETADDRINFO = fake_addrinfo([
            (socket.AF_INET6, '::1'),
            (socket.AF_INET6, '::2'),
            (socket.AF_INET, '9.9.9.9'),
            (socket.AF_INET, '8.8.8.8'),
        ])
        got = netutil.ipv4_only_getaddrinfo('dual.example.com', 443)
        self.assertEqual([r[4][0] for r in got], ['9.9.9.9', '8.8.8.8'])

    def test_context_manager_installs_and_restores(self):
        netutil._REAL_GETADDRINFO = fake_addrinfo([
            (socket.AF_INET, '1.2.3.4'), (socket.AF_INET6, '::1')])
        self.assertIs(socket.getaddrinfo, TRUE_GETADDRINFO, '前置状态应干净')
        with netutil.force_ipv4():
            self.assertIs(socket.getaddrinfo, netutil.ipv4_only_getaddrinfo)
            got = socket.getaddrinfo('x', 443)
            self.assertEqual([r[0] for r in got], [socket.AF_INET])
        self.assertIs(socket.getaddrinfo, TRUE_GETADDRINFO, '退出后必须还原全局函数')

    def test_nested_context_restores_only_at_outermost(self):
        """嵌套使用时，内层退出不能提前还原（否则外层失效）。"""
        netutil._REAL_GETADDRINFO = fake_addrinfo([(socket.AF_INET, '1.2.3.4')])
        with netutil.force_ipv4():
            with netutil.force_ipv4():
                self.assertIs(socket.getaddrinfo, netutil.ipv4_only_getaddrinfo)
            self.assertIs(socket.getaddrinfo, netutil.ipv4_only_getaddrinfo,
                          '内层退出时不该还原')
        self.assertIs(socket.getaddrinfo, TRUE_GETADDRINFO)
        self.assertEqual(netutil.force_ipv4._depth, 0)

    def test_exception_still_restores(self):
        netutil._REAL_GETADDRINFO = fake_addrinfo([(socket.AF_INET, '1.2.3.4')])
        try:
            with netutil.force_ipv4():
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        self.assertIs(socket.getaddrinfo, TRUE_GETADDRINFO, '异常路径也必须还原')
        self.assertEqual(netutil.force_ipv4._depth, 0)

    def test_restore_target_is_immune_to_global_mutation(self):
        """即使 _REAL_GETADDRINFO 被外部改成假的，退出时也要回到真实实现。

        这是修过的一个真实脆弱点：原先还原时读的是可变全局变量。
        """
        netutil._REAL_GETADDRINFO = fake_addrinfo([(socket.AF_INET, '1.2.3.4')])
        with netutil.force_ipv4():
            # 上下文内部被第三方（或测试）改动
            netutil._REAL_GETADDRINFO = fake_addrinfo([(socket.AF_INET, '5.6.7.8')])
        self.assertIs(socket.getaddrinfo, TRUE_GETADDRINFO,
                      '不该还原成一个假的实现')


class TestResolveHelpers(NetutilTestCase):
    def test_resolve_ipv4_dedupes_and_filters(self):
        netutil._REAL_GETADDRINFO = fake_addrinfo([
            (socket.AF_INET6, '::1'),
            (socket.AF_INET, '1.1.1.1'),
            (socket.AF_INET, '1.1.1.1'),
            (socket.AF_INET, '2.2.2.2'),
        ])
        self.assertEqual(netutil.resolve_ipv4('x'), ['1.1.1.1', '2.2.2.2'])

    def test_resolve_ipv4_empty_on_dns_failure(self):
        def boom(*a, **k):
            raise socket.gaierror('nope')
        netutil._REAL_GETADDRINFO = boom
        self.assertEqual(netutil.resolve_ipv4('x'), [])

    def test_has_ipv6_only(self):
        netutil._REAL_GETADDRINFO = fake_addrinfo([(socket.AF_INET6, '::1')])
        self.assertTrue(netutil.has_ipv6_only('v6only'))
        netutil._REAL_GETADDRINFO = fake_addrinfo([
            (socket.AF_INET6, '::1'), (socket.AF_INET, '1.1.1.1')])
        self.assertFalse(netutil.has_ipv6_only('dual'))
        netutil._REAL_GETADDRINFO = fake_addrinfo([(socket.AF_INET, '1.1.1.1')])
        self.assertFalse(netutil.has_ipv6_only('v4only'))

    def test_has_ipv6_only_false_on_dns_error(self):
        def boom(*a, **k):
            raise socket.gaierror('nope')
        netutil._REAL_GETADDRINFO = boom
        self.assertFalse(netutil.has_ipv6_only('x'))


class TestSslContext(NetutilTestCase):
    def test_verify_true_checks(self):
        ctx = netutil.build_ssl_context(verify=True)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_verify_false_relaxes(self):
        ctx = netutil.build_ssl_context(verify=False)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)


class TestErrorClassification(unittest.TestCase):
    def test_certificate(self):
        e = ssl.SSLCertVerificationError('certificate verify failed')
        self.assertEqual(netutil.classify_network_error(e), 'tls_error')

    def test_timeout(self):
        self.assertEqual(
            netutil.classify_network_error(socket.timeout('timed out')), 'timeout')
        self.assertEqual(
            netutil.classify_network_error(TimeoutError('connect timeout')), 'timeout')

    def test_dns(self):
        self.assertEqual(
            netutil.classify_network_error(socket.gaierror('Name or service not known')),
            'dns_error')

    def test_refused_and_reset(self):
        self.assertEqual(
            netutil.classify_network_error(ConnectionRefusedError('Connection refused')),
            'refused')
        self.assertEqual(
            netutil.classify_network_error(ConnectionResetError('Connection reset by peer')),
            'reset')

    def test_unreachable(self):
        self.assertEqual(
            netutil.classify_network_error(OSError('Network is unreachable')),
            'unreachable')

    def test_unknown(self):
        self.assertEqual(netutil.classify_network_error(ValueError('x')), 'unknown')

    def test_every_kind_has_description(self):
        for kind in ('tls_error', 'timeout', 'refused', 'dns_error',
                     'unreachable', 'reset'):
            desc = netutil.describe_network_error(kind)
            self.assertTrue(desc)
            self.assertNotEqual(desc, '未知网络错误',
                                '%s 缺少专门说明' % kind)

    def test_unknown_falls_back_to_generic(self):
        """未知类别回退到通用文案是预期行为，不算缺陷。"""
        self.assertEqual(netutil.describe_network_error('unknown'), '未知网络错误')
        self.assertEqual(netutil.describe_network_error('no-such-kind'), '未知网络错误')


if __name__ == '__main__':
    unittest.main(verbosity=2)
