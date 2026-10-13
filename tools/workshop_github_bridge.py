"""维护者在可信分支运行的工坊审查桥接；投稿内容永远不会被执行。"""
from __future__ import annotations

import base64
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from astral_party_auto.core.http_transport import trusted_https_connection

from astral_party_auto.modkit.workshop import (MAX_PACKAGE_BYTES, MAX_CATALOG_BYTES, inspect_package, validate_catalog, validate_metadata,
                                              _bad_number, _json_shape, _unique_object)

REPOSITORY = "kldsjfas/JiXingModHelper"
SERVICE_URL = "https://xn--killer-gx2m.top/jixing-workshop"
CATALOG_BRANCH = "workshop"
CATALOG_PATH = "catalog.json"
API_ROOT = "/repos/" + REPOSITORY
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_JSON_BYTES = 4 * 1024 * 1024
BLOCK_BYTES = 64 * 1024
REVIEW_TUNNEL_PORT = 18443


class ApiFailure(RuntimeError):
    def __init__(self, status, source_host=""):
        self.status = status
        allowed_hosts = {"api.github.com", "uploads.github.com", urlsplit(SERVICE_URL).hostname}
        self.source_host = source_host if source_host in allowed_hosts else ""
        source = "（" + self.source_host + "）" if self.source_host else ""
        super().__init__(f"受信服务{source}请求失败（HTTP {status}），未输出远端内容。")


class TrustedHttp:
    def __init__(self, origin, token, *, origin_ip="", review_tunnel_port=""):
        parts = urlsplit(origin)
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.port or parts.query or parts.fragment
                or not parts.hostname or "\\" in origin or any(ord(char) <= 32 for char in origin)):
            raise ValueError("桥接只能连接固定的 HTTPS 来源。")
        if not isinstance(token, str) or not token or any(ord(char) <= 32 or ord(char) >= 127 for char in token):
            raise ValueError("缺少受保护的桥接凭证。")
        if not isinstance(review_tunnel_port, str) or review_tunnel_port not in ("", str(REVIEW_TUNNEL_PORT)):
            raise ValueError("审核隧道只能使用工作流固定的本机端口。")
        if review_tunnel_port:
            if origin != SERVICE_URL:
                raise ValueError("审核隧道只允许固定工坊服务，不能改变 GitHub 连接。")
            if origin_ip:
                raise ValueError("审核隧道与源站直连不能同时配置。")
        self.host, self.prefix, self.token = parts.hostname, parts.path.rstrip("/"), token
        self._origin_ip = ""
        self._review_tunnel = bool(review_tunnel_port)
        if origin_ip:
            if origin != SERVICE_URL:
                raise ValueError("源站直连只允许固定工坊服务，不能改变 GitHub 连接。")
            try:
                if not isinstance(origin_ip, str) or origin_ip != origin_ip.strip():
                    raise ValueError
                address = ipaddress.ip_address(origin_ip)
                if (not address.is_global or address.is_private or address.is_loopback or address.is_multicast
                        or address.is_reserved or address.is_link_local or address.is_unspecified
                        or getattr(address, "ipv4_mapped", None) is not None or getattr(address, "scope_id", None) is not None):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("工坊源站直连配置必须是有效的公网地址。") from None
            self._origin_ip = address.compressed

    def _connection(self):
        if not self._origin_ip and not self._review_tunnel:
            return trusted_https_connection(self.host, timeout=30)
        connection = http.client.HTTPSConnection(self.host, timeout=30, context=ssl.create_default_context())

        def connect_service(address, timeout=30, source_address=None):
            if address != (self.host, 443):
                raise ValueError("审核连接不能改变受信服务来源。")
            target = ("127.0.0.1", REVIEW_TUNNEL_PORT) if self._review_tunnel else (self._origin_ip, 443)
            return socket.create_connection(target, timeout, source_address)

        # 仅 TCP 走固定隧道或维护者的源站；TLS 仍校验原域名，
        # SNI、Host 和认证请求都沿用同一个受信服务。
        connection._create_connection = connect_service
        return connection

    def request(self, method, path, value=None, *, file=None, size=0, sha256="", output=None, binary=False):
        if not path.startswith("/") or "\\" in path or any(ord(char) <= 32 for char in path):
            raise ValueError("桥接请求路径不正确。")
        connection = self._connection()
        try:
            headers = {"Authorization": "Bearer " + self.token, "User-Agent": "JiXing-Workshop-Review",
                       "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28", "Connection": "close"}
            body = None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
            if file is not None:
                headers.update({"Content-Type": "application/zip", "Content-Length": str(size)})
            elif body is not None:
                headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
            connection.putrequest(method, self.prefix + path)
            for key, item in headers.items():
                connection.putheader(key, item)
            connection.endheaders()
            if file is not None:
                count, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 300
                with Path(file).open("rb") as stream:
                    while block := stream.read(BLOCK_BYTES):
                        count += len(block)
                        if count > size or time.monotonic() > deadline:
                            raise ValueError("发布包超过大小或传输时间限制。")
                        digest.update(block)
                        connection.send(block)
                if count != size or digest.hexdigest() != sha256:
                    raise ValueError("发布包在传输期间变化。")
            elif body is not None:
                connection.send(body)
            response = connection.getresponse()
            if response.status not in (200, 201, 204):
                # 包括所有跳转：绝不把任何 Bearer 凭证转发到其它来源。
                raise ApiFailure(response.status, self.host)
            if output is not None:
                if response.getheader("Content-Type", "").split(";")[0] not in ("application/zip", "application/octet-stream"):
                    raise ValueError("审核包响应类型不正确。")
                count, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 300
                with Path(output).open("xb") as stream:
                    while block := response.read(BLOCK_BYTES):
                        count += len(block)
                        if count > size or time.monotonic() > deadline:
                            raise ValueError("下载的审核包超过声明大小。")
                        digest.update(block)
                        stream.write(block)
                if count != size or digest.hexdigest() != sha256:
                    raise ValueError("审核包摘要与批准记录不一致。")
                return None
            raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError("桥接响应超过大小限制。")
            if response.status == 204:
                return None
            if not response.getheader("Content-Type", "").split(";")[0].endswith("json"):
                raise ValueError("桥接响应必须是 JSON。")
            try:
                document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_bad_number)
                if not isinstance(document, (dict, list)):
                    raise ValueError
                _json_shape(document)
                return document
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError("桥接响应格式不正确。") from None
        except (OSError, http.client.HTTPException):
            raise RuntimeError("桥接网络连接失败，未输出凭证或远端异常。") from None
        finally:
            connection.close()


class ServiceApi:
    def __init__(self, secret, *, origin_ip="", review_tunnel_port=""):
        self.http = TrustedHttp(SERVICE_URL, secret, origin_ip=origin_ip, review_tunnel_port=review_tunnel_port)

    def pending(self):
        for page in range(101):
            path = "/v1/admin/submissions/pending" + (f"/page/{page}" if page else "")
            result = self.http.request("GET", path)
            if not isinstance(result, dict) or not isinstance(result.get("submissions"), list) or len(result["submissions"]) > 100:
                raise ValueError("隔离投稿列表响应不正确。")
            yield from result["submissions"]
            next_page = result.get("next_page")
            if next_page is None:
                return
            if type(next_page) is not int or next_page != page + 1:
                raise ValueError("隔离投稿列表页码不正确。")
        raise ValueError("隔离投稿页数超过限制。")

    def detail(self, identifier):
        return self.http.request("GET", "/v1/admin/submissions/" + identifier)

    def bind_issue(self, identifier, number):
        return self.http.request("POST", f"/v1/admin/submissions/{identifier}/github-issue", {"issue_number": number})

    def scan(self, identifier):
        return self.http.request("POST", f"/v1/admin/submissions/{identifier}/scan")

    def review(self, identifier, payload):
        return self.http.request("POST", f"/v1/admin/submissions/{identifier}/review", payload)

    def download(self, row, path):
        return self.http.request("GET", f"/v1/admin/submissions/{row['submission_id']}/package", output=path,
                                 size=row["size"], sha256=row["sha256"], binary=True)


class GithubApi:
    def __init__(self, token):
        self.http = TrustedHttp("https://api.github.com", token)
        self.uploads = TrustedHttp("https://uploads.github.com", token)

    def request(self, method, path, value=None):
        return self.http.request(method, API_ROOT + path, value)

    def find_issue(self, identifier):
        marker = f"<!-- jixing-workshop:{identifier} -->"
        for page in range(1, 21):
            issues = self.request("GET", f"/issues?state=all&per_page=100&page={page}")
            if not isinstance(issues, list) or len(issues) > 100:
                raise ValueError("GitHub 审核单列表响应不正确。")
            matches = [issue for issue in issues if issue.get("user", {}).get("login") == "github-actions[bot]"
                       and not issue.get("pull_request") and marker in (issue.get("body") or "")]
            if len(matches) > 1:
                raise ValueError("同一投稿出现多份可信审核单，停止自动关联。")
            if matches:
                return matches[0]
            if len(issues) < 100:
                return None
        raise ValueError("审核单搜索超过限制，请维护者处理。")

    def catalog(self):
        value = self.request("GET", f"/contents/{CATALOG_PATH}?ref={CATALOG_BRANCH}")
        if (not isinstance(value, dict) or value.get("encoding") != "base64"
                or not isinstance(value.get("sha"), str) or not re.fullmatch(r"[a-f0-9]{40}", value["sha"])):
            raise ValueError("GitHub 工坊目录响应不正确。")
        try:
            raw = base64.b64decode(value["content"], validate=False)
        except (ValueError, TypeError, KeyError):
            raise ValueError("GitHub 工坊目录编码不正确。") from None
        return validate_catalog(raw), value["sha"]

    def write_catalog(self, catalog, sha, message):
        checked = validate_catalog(catalog)
        content = json.dumps(checked, ensure_ascii=False, indent=2).encode("utf-8")
        if len(content) > MAX_CATALOG_BYTES:
            raise ValueError("工坊目录超过客户端可读取大小，停止写入。")
        return self.request("PUT", f"/contents/{CATALOG_PATH}", {"message": message, "branch": CATALOG_BRANCH,
                            "sha": sha, "content": base64.b64encode(content).decode("ascii")})

    def release(self, tag):
        try:
            return self.request("GET", "/releases/tags/" + tag)
        except ApiFailure as exc:
            if exc.status == 404:
                # 按 tag 的接口可能只返回已公开版本；带 push 权限的列表含草稿。
                matches = []
                for page in range(1, 21):
                    releases = self.request("GET", f"/releases?per_page=100&page={page}")
                    if not isinstance(releases, list) or len(releases) > 100:
                        raise ValueError("GitHub Release 列表响应不正确。")
                    matches.extend(release for release in releases if release.get("tag_name") == tag)
                    if len(matches) > 1:
                        raise ValueError("同一工坊标签存在多个 Release，停止创建或发布。")
                    if len(releases) < 100:
                        return matches[0] if matches else None
                raise ValueError("GitHub Release 搜索超过限制，停止创建或发布。")
            raise

    def upload_asset(self, release_id, name, path, row):
        return self.uploads.request("POST", f"{API_ROOT}/releases/{release_id}/assets?name={quote(name, safe='')}",
                                    file=path, size=row["size"], sha256=row["sha256"])


def checked_submission(row, identifier=None):
    if (not isinstance(row, dict) or not isinstance(row.get("submission_id"), str)
            or not IDENTIFIER.fullmatch(row["submission_id"]) or identifier is not None and row["submission_id"] != identifier
            or row.get("status") not in ("pending", "approved", "rejected")
            or not isinstance(row.get("sha256"), str) or not SHA256.fullmatch(row["sha256"])
            or type(row.get("size")) is not int or not 0 < row["size"] <= MAX_PACKAGE_BYTES):
        raise ValueError("隔离投稿记录不正确。")
    validate_metadata(row.get("metadata"))
    return row


def approved_submission(row, identifier):
    checked_submission(row, identifier)
    review, scan = row.get("review", {}), row.get("scan", {})
    proof = review.get("game_test", {})
    if (row["status"] != "approved" or review.get("decision") != "approve" or scan.get("clean") is not True
            or scan.get("sha256") != row["sha256"] or review.get("scan", {}).get("scan_id") != scan.get("scan_id")
            or type(scan.get("scanned_at")) is not int or scan["scanned_at"] < time.time() - 48 * 3600
            or proof.get("passed") is not True or not isinstance(proof.get("version"), str) or not proof["version"].strip()
            or not isinstance(proof.get("evidence"), str) or not proof["evidence"].strip()
            or not isinstance(proof.get("notes"), str) or len(proof["notes"].strip()) < 20):
        raise ValueError("发布需要同摘要的服务器扫描、人工游戏测试和批准记录。")
    return row


def unchanged_approval(service, original):
    latest = approved_submission(service.detail(original["submission_id"]), original["submission_id"])
    if any(latest[key] != original[key] for key in ("sha256", "size", "metadata", "review")):
        raise ValueError("发布期间批准记录变化，停止发布。")
    return latest


def issue_body(row):
    marker = f"<!-- jixing-workshop:{row['submission_id']} -->"
    details = validate_metadata(row["metadata"])
    # HTML 转义再放入 pre；阻止用户内容成为链接、提及或可信 Markdown 指令。
    text = "\n".join(f"{key}: {details[key]}" for key in ("name", "author", "version", "game_version", "category", "description"))
    summary = row.get("inspection", {})
    count = summary.get("bundle_count", "未提供")
    return (marker + "\n\n待人工审核的资源替换作品。未审核的 ZIP 保留在私有隔离区。\n\n"
            + "<pre>" + html.escape(text, quote=True).replace("@", "&#64;") + "</pre>\n\n"
            + f"投稿编号：`{row['submission_id']}`\nSHA-256：`{row['sha256']}`\n大小：{row['size']} 字节\n"
            + "资源包数量：" + (str(count) if type(count) is int else "未提供") + "\n\n"
            + "维护者请在 Workshop Review 工作流中运行 approve/reject；批准需要服务器扫描和游戏实测证据。")


class WorkshopBridge:
    def __init__(self, service, github, *, actor, default_branch):
        self.service, self.github, self.actor, self.default_branch = service, github, actor, default_branch

    def sync(self):
        count = 0
        for candidate in self.service.pending():
            row = checked_submission(candidate)
            if row["status"] != "pending":
                continue
            identifier = row["submission_id"]
            bound = row.get("github_issue_number")
            if bound is not None:
                if type(bound) is not int or bound < 1:
                    raise ValueError("已关联审核单编号不正确。")
                continue
            issue = self.github.find_issue(identifier)
            if issue is None:
                issue = self.github.request("POST", "/issues", {"title": "[工坊审核] " + identifier,
                                                                  "body": issue_body(row)})
            if (type(issue.get("number")) is not int or issue["number"] < 1
                    or issue.get("user", {}).get("login") != "github-actions[bot]"):
                raise ValueError("只能关联可信工作流创建的审核单。")
            self.service.bind_issue(identifier, issue["number"])
            count += 1
        return {"synced": count}

    def _update_catalog(self, entry=None, *, revoke_id=None, revoke_asset=None):
        for _attempt in range(5):
            catalog, sha = self.github.catalog()
            existing = next((item for item in catalog["mods"] if item["id"] == (entry["id"] if entry else revoke_id)), None)
            if revoke_id is not None:
                if existing is not None and existing["asset"] != revoke_asset:
                    raise ValueError("撤回投稿与目录中的作品资产不一致，禁止撤回其它作品。")
                if existing is None or existing["revoked"]:
                    return
                existing["revoked"] = True
            elif existing is not None:
                if existing != entry:
                    raise ValueError("工坊作品编号已存在，禁止覆盖已有作品或撤回记录。")
                return
            else:
                catalog["mods"].append(entry)
            try:
                self.github.write_catalog(catalog, sha, "Revoke workshop resource" if revoke_id else "Publish reviewed workshop resource")
                return
            except ApiFailure as exc:
                if exc.status not in (409, 422):
                    raise
        raise RuntimeError("工坊目录持续被其它维护者修改，请稍后重试。")

    def approve(self, identifier, proof, message):
        row = checked_submission(self.service.detail(identifier), identifier)
        if row["status"] == "rejected":
            raise ValueError("被拒绝或已撤回的投稿不能发布。")
        if row["status"] == "pending":
            scan = self.service.scan(identifier)
            if not isinstance(scan, dict) or scan.get("clean") is not True or scan.get("sha256") != row["sha256"]:
                raise ValueError("服务器扫描未通过，停止批准。")
            self.service.review(identifier, {"decision": "approve", "reviewer": self.actor, "message": message,
                                             "scan_id": scan.get("scan_id"), "game_test": proof})
        row = approved_submission(self.service.detail(identifier), identifier)
        tag, asset_name = "workshop-" + identifier, "workshop-" + identifier + ".zip"
        entry = {**validate_metadata(row["metadata"]), "game_version": row["review"]["game_test"]["version"],
                 "asset": {"tag": tag, "name": asset_name, "size": row["size"], "sha256": row["sha256"]}, "revoked": False}
        # 先检查 id，避免为编号冲突的作品创建公开 Release。
        catalog, _sha = self.github.catalog()
        existing = next((item for item in catalog["mods"] if item["id"] == entry["id"]), None)
        if existing is not None and existing != entry:
            raise ValueError("作品编号与现有目录冲突，停止发布。")
        with tempfile.TemporaryDirectory(prefix="reviewed-workshop-") as temporary:
            package = Path(temporary) / asset_name
            self.service.download(row, package)
            inspection = inspect_package(package)
            if inspection["sha256"] != row["sha256"] or inspection["size"] != row["size"]:
                raise ValueError("下载的批准资源与记录不一致。")
            unchanged_approval(self.service, row)
            release = self.github.release(tag)
            marker = f"<!-- jixing-workshop:{identifier} -->"
            if release is None:
                release = self.github.request("POST", "/releases", {"tag_name": tag, "target_commitish": self.default_branch,
                    "name": "Reviewed workshop resource " + identifier, "body": marker + "\nSHA-256: " + row["sha256"],
                    "draft": True, "prerelease": False, "make_latest": "false"})
            if (release.get("tag_name") != tag or marker not in (release.get("body") or "")
                    or release.get("author", {}).get("login") != "github-actions[bot]"
                    or type(release.get("id")) is not int or release["id"] < 1):
                raise ValueError("已有 Release 不能证明来源于可信审查流程。")
            assets = self.github.request("GET", f"/releases/{release['id']}/assets?per_page=100")
            if not isinstance(assets, list) or len(assets) > 1:
                raise ValueError("独立工坊 Release 包含意外资产，停止发布。")
            asset = assets[0] if assets else self.github.upload_asset(release["id"], asset_name, package, row)
            if (asset.get("name") != asset_name or asset.get("size") != row["size"]
                    or asset.get("digest") != "sha256:" + row["sha256"] or asset.get("state") != "uploaded"):
                raise ValueError("GitHub 资产未通过服务端摘要校验，保留草稿并停止发布。")
            unchanged_approval(self.service, row)
            if release.get("draft") is not False:
                self.github.request("PATCH", f"/releases/{release['id']}", {"draft": False, "make_latest": "false"})
            try:
                unchanged_approval(self.service, row)
            except (ValueError, RuntimeError):
                try:
                    self.github.request("PATCH", f"/releases/{release['id']}", {"draft": True})
                except (RuntimeError, ValueError):
                    raise RuntimeError("发布后的批准复核未通过，目录未更新；Release 隐藏失败，需要维护者立即处理。") from None
                raise
            self._update_catalog(entry)
        return {"submission_id": identifier, "published": True, "tag": tag}

    def reject(self, identifier, message):
        checked_submission(self.service.detail(identifier), identifier)
        self.service.review(identifier, {"decision": "reject", "reviewer": self.actor, "message": message,
                                         "scan_id": "", "game_test": None})
        return {"submission_id": identifier, "rejected": True}

    def revoke(self, identifier, message):
        row = checked_submission(self.service.detail(identifier), identifier)
        if row["status"] != "approved" and not (row["status"] == "rejected" and row.get("review", {}).get("decision") == "revoke"):
            raise ValueError("只能撤回已批准的这份投稿。")
        asset = {"tag": "workshop-" + identifier, "name": "workshop-" + identifier + ".zip",
                 "sha256": row["sha256"], "size": row["size"]}
        self._update_catalog(revoke_id=row["metadata"]["id"], revoke_asset=asset)
        self.service.review(identifier, {"decision": "revoke", "reviewer": self.actor, "message": message,
                                         "scan_id": "", "game_test": None})
        release = self.github.release("workshop-" + identifier)
        if release is not None:
            marker = f"<!-- jixing-workshop:{identifier} -->"
            if marker not in (release.get("body") or "") or release.get("author", {}).get("login") != "github-actions[bot]":
                raise ValueError("撤回目录已生效，但 Release 来源异常，保留供维护者检查。")
            self.github.request("PATCH", f"/releases/{release['id']}", {"draft": True})
        return {"submission_id": identifier, "revoked": True}


def authorize_environment(environment, github):
    if environment.get("GITHUB_REPOSITORY") != REPOSITORY or environment.get("GITHUB_EVENT_NAME") not in ("schedule", "workflow_dispatch"):
        raise ValueError("审查桥接只允许主仓库的定时或维护者手动工作流。")
    repository = github.request("GET", "")
    default_branch = repository.get("default_branch")
    actor = environment.get("GITHUB_ACTOR", "")
    if (not isinstance(default_branch, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", default_branch)
            or environment.get("GITHUB_REF") != "refs/heads/" + default_branch
            or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", actor)):
        raise ValueError("审查桥接必须使用主仓库的可信默认分支。")
    if actor != repository.get("owner", {}).get("login"):
        permission = github.request("GET", "/collaborators/" + actor + "/permission")
        if permission.get("permission") not in ("write", "maintain", "admin"):
            raise ValueError("只有仓库维护者可以运行审查桥接。")
    return actor, default_branch


def main(environment=None):
    environment = os.environ if environment is None else environment
    github = GithubApi(environment.get("GITHUB_TOKEN", ""))
    actor, default_branch = authorize_environment(environment, github)
    service = ServiceApi(environment.get("WORKSHOP_ADMIN_SECRET", ""),
                         origin_ip=environment.get("WORKSHOP_ORIGIN_IP", ""),
                         review_tunnel_port=environment.get("WORKSHOP_REVIEW_TUNNEL_PORT", ""))
    bridge = WorkshopBridge(service, github, actor=actor, default_branch=default_branch)
    action = environment.get("BRIDGE_ACTION", "sync")
    identifier = environment.get("SUBMISSION_ID", "")
    if action == "sync":
        result = bridge.sync()
    else:
        if not IDENTIFIER.fullmatch(identifier):
            raise ValueError("投稿编号必须是 32 位小写十六进制值。")
        message = environment.get("REVIEW_MESSAGE", "").strip()
        if not 1 <= len(message) <= 2000:
            raise ValueError("维护者必须填写本次审查说明。")
        if action == "approve":
            proof = {"passed": True, "version": environment.get("GAME_VERSION", "").strip(),
                     "evidence": environment.get("GAME_EVIDENCE", "").strip(), "notes": environment.get("GAME_NOTES", "").strip()}
            result = bridge.approve(identifier, proof, message)
        elif action == "reject":
            result = bridge.reject(identifier, message)
        elif action == "revoke":
            result = bridge.revoke(identifier, message)
        else:
            raise ValueError("未知审查操作。")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
