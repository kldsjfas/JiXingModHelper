"""系统代理兼容性：真实 HTTP CONNECT + 验证证书的本机 TLS。"""
from __future__ import annotations

import base64
import select
import shutil
import socket
import ssl
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from astral_party_auto.core.http_transport import trusted_https_connection


class TlsTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.requests.append(dict(self.headers))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class ConnectProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_CONNECT(self):
        self.server.connects.append((self.path, dict(self.headers)))
        expected = "localhost:" + str(self.server.target_port)
        if self.path != expected:
            self.send_error(403)
            return
        with socket.create_connection(("127.0.0.1", self.server.target_port), timeout=5) as target:
            self.send_response(200)
            self.end_headers()
            self.wfile.flush()
            peers = (self.connection, target)
            while True:
                ready, _, _ = select.select(peers, [], [], 5)
                if not ready:
                    return
                for peer in ready:
                    block = peer.recv(64 * 1024)
                    if not block:
                        return
                    self.server.tunnel_bytes.append(block)
                    (target if peer is self.connection else self.connection).sendall(block)


class HttpTransportTests(unittest.TestCase):
    def test_direct_and_bypass_keep_target_certificate_verification(self):
        for proxies, bypass in (({}, False), ({"https": "http://127.0.0.1:1234"}, True)):
            with self.subTest(proxies=proxies, bypass=bypass), patch("astral_party_auto.core.http_transport.getproxies", return_value=proxies), patch("astral_party_auto.core.http_transport.proxy_bypass", return_value=bypass):
                connection = trusted_https_connection("trusted.example")
                self.assertEqual(connection.host, "trusted.example")
                self.assertTrue(connection._context.check_hostname)
                self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
                connection.close()

    def test_invalid_proxy_is_rejected_without_exposing_credentials(self):
        for proxy in ("socks5://username:private-password@127.0.0.1:80", "https://proxy.example", "http://proxy.example/path",
                      "http://proxy.example?secret=private-password", "http://proxy.example:99999", "http://proxy.example\n",
                      "http://name:private%0apassword@proxy.example"):
            with self.subTest(proxy=proxy), patch("astral_party_auto.core.http_transport.getproxies", return_value={"https": proxy}), patch("astral_party_auto.core.http_transport.proxy_bypass", return_value=False):
                with self.assertRaises(ValueError) as caught:
                    trusted_https_connection("trusted.example")
                self.assertNotIn("private-password", str(caught.exception))
                self.assertNotIn(proxy, str(caught.exception))

    def test_tls_verification_cannot_be_disabled_even_with_a_proxy(self):
        context = ssl._create_unverified_context()
        with self.assertRaisesRegex(ValueError, "验证"):
            trusted_https_connection("trusted.example", context=context)

    def test_connect_proxy_receives_no_target_bearer_and_tunnel_contains_real_tls(self):
        openssl = shutil.which("openssl")
        if openssl is None:
            candidate = Path("C:/Program Files/Git/usr/bin/openssl.exe")
            openssl = str(candidate) if candidate.exists() else None
        if openssl is None:
            self.skipTest("本机无 OpenSSL，不能生成一次性的本机 TLS 测试证书。")
        with TemporaryDirectory(prefix="http-connect-tls-") as temporary:
            root = Path(temporary)
            key, certificate = root / "test-key.pem", root / "test-cert.pem"
            configuration = root / "openssl-test.cnf"
            configuration.write_text("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
                                     "[dn]\nCN=localhost\n[v3]\nsubjectAltName=DNS:localhost\n"
                                     "basicConstraints=critical,CA:TRUE\n", encoding="ascii")
            subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-sha256", "-keyout", str(key), "-out", str(certificate), "-config", str(configuration)], check=True, shell=False,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certificate, key)
            target = ThreadingHTTPServer(("127.0.0.1", 0), TlsTarget)
            target.daemon_threads = True
            target.requests = []
            target.socket = server_context.wrap_socket(target.socket, server_side=True)
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), ConnectProxy)
            proxy.daemon_threads = True
            proxy.target_port = target.server_port
            proxy.connects, proxy.tunnel_bytes = [], []
            threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (target, proxy)]
            for thread in threads:
                thread.start()
            try:
                context = ssl.create_default_context(cafile=str(certificate))
                configured = {"https": f"http://proxy-user:test-proxy-password@127.0.0.1:{proxy.server_port}"}
                with patch("astral_party_auto.core.http_transport.getproxies", return_value=configured), patch("astral_party_auto.core.http_transport.proxy_bypass", return_value=False):
                    connection = trusted_https_connection("localhost", port=target.server_port, timeout=5, context=context)
                    try:
                        connection.request("GET", "/private-status", headers={"Authorization": "Bearer test-target-bearer", "Connection": "close"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b'{"ok":true}')
                    finally:
                        connection.close()
                self.assertEqual(proxy.connects[0][0], "localhost:" + str(target.server_port))
                connect_headers = proxy.connects[0][1]
                self.assertNotIn("Authorization", connect_headers)
                expected = "Basic " + base64.b64encode(b"proxy-user:test-proxy-password").decode()
                self.assertEqual(connect_headers["Proxy-Authorization"], expected)
                self.assertEqual(target.requests[0]["Authorization"], "Bearer test-target-bearer")
                self.assertNotIn("Proxy-Authorization", target.requests[0])
                self.assertNotIn(b"test-target-bearer", b"".join(proxy.tunnel_bytes))
            finally:
                for server, thread in zip((target, proxy), threads):
                    server.shutdown()
                    thread.join(timeout=3)
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
