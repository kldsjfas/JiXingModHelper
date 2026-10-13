"""使用既有系统代理的 HTTPS 连接，认证资料仅在验证后的 TLS 中传输。"""
from __future__ import annotations

import base64
import http.client
import ssl
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass


def trusted_https_connection(host: str, *, port: int | None = None, timeout: float = 20,
                             context: ssl.SSLContext | None = None):
    context = ssl.create_default_context() if context is None else context
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("HTTPS 连接必须验证服务器证书和主机名。")
    proxy = getproxies().get("https")
    if not proxy or proxy_bypass(host):
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=context)
    try:
        parts = urlsplit(proxy)
        if (parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/")
                or parts.query or parts.fragment or "\\" in proxy or any(ord(char) <= 32 for char in proxy)):
            raise ValueError
        proxy_port = 80 if parts.port is None else parts.port
        if not 1 <= proxy_port <= 65535:
            raise ValueError
        headers = {}
        if parts.username is not None or parts.password is not None:
            if parts.username is None or parts.password is None:
                raise ValueError
            login, password = unquote(parts.username), unquote(parts.password)
            if ":" in login or any(ord(char) < 32 or ord(char) == 127 for char in login + password):
                raise ValueError
            credentials = (login + ":" + password).encode("utf-8")
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
    except (TypeError, ValueError):
        raise ValueError("当前 HTTPS 系统代理配置不正确，已停止连接。") from None
    connection = http.client.HTTPSConnection(parts.hostname, port=proxy_port, timeout=timeout, context=context)
    # HTTPSConnection 在 CONNECT 成功后用 tunnel host 校验证书并协商 TLS。
    # Proxy-Authorization 只在 CONNECT 中发送，目标 Bearer 请求随后进入 TLS。
    connection.set_tunnel(host, port=port or 443, headers=headers)
    return connection
