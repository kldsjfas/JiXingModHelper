# 软件内投稿服务

上传者在助手里选择 ZIP、填写作品介绍后直接上传，不需要 GitHub 账号。服务把包存入隔离投稿箱，重新检查 ZIP 和 UnityFS 内部内容，接受后只返回 `pending`。维护者仍在 GitHub 审核，正式工坊目录和下载继续使用仓库的审核流程；这个服务不会自动上架，也没有公共包下载接口。

作品里的作者名是投稿者填写的显示名，不代表已验证身份。自动结构检查和杀毒扫描都不能保证资源没有风险，维护者仍需检查替换内容并在对应游戏版本测试。

当前服务入口是 `https://xn--killer-gx2m.top/jixing-workshop`，HTTPS 代理只转发到服务器 `127.0.0.1:19092`，初始单包上限为 80 MiB。2026-10-13 已验证公网收件、幂等重试、私人状态查询、程序集拒绝和缺少游戏证据时不能批准；真实 ClamAV 检查通过干净合成资源，并检出 EICAR 测试签名。这些结果不替代作品的实际游戏验证。

## 运行

需要 Python 3.11 或以上，从仓库根运行：

```sh
python -m venv .venv-service
.venv-service/bin/pip install -r workshop_service/requirements.txt
export WORKSHOP_DATA_DIR=/var/lib/jixing-workshop
export WORKSHOP_ADMIN_SECRET='<至少32个字符的独立随机密钥>'
export WORKSHOP_MAX_UPLOAD_BYTES=83886080
export WORKSHOP_QUOTA_BYTES=2147483648
export WORKSHOP_MAX_SUBMISSIONS=100
export WORKSHOP_MAX_WORKERS=2
export WORKSHOP_FORBIDDEN_BUNDLES='["程序集对应的资源包名称.bundle"]'
export WORKSHOP_SCANNER_COMMAND='["/opt/jixing-workshop/current/.venv-service/bin/python","-B","-m","workshop_service.scan_package","{path}"]'
.venv-service/bin/python -B -m workshop_service.server --host 127.0.0.1 --port 19092
```

示例密钥和黑名单必须换成真实配置。程序集黑名单由维护者从当前游戏目录提取纯文件名；不要上传游戏原包。黑名单未配置时内部有效 PE 和程序集 TextAsset 仍会拒绝，但失去当前版本目标包的额外保护。

源码导入现有 `modkit` 包，因此服务的 requirements 包含项目基础依赖。实际不可信包只交给有界结构解析器，没有执行、注入或调用 UnityPy 加载上传内容。

服务只允许一个进程使用同一存储目录。启动时会清除之前中断的 `uploading` 行和对应随机 `.part` 文件，保留已经接受的投稿。不要让两个部署共用目录，也不要将存储目录放在网络共享、符号链接、Web 根目录或游戏目录里。

扫描 wrapper 使用固定 `/usr/bin/clamscan` 和 `/var/lib/clamav`，不接受投稿者指定命令或签名库。运营者需要安装 ClamAV 并通过 `freshclam` 定期更新数据库。扫描前必须存在有效的 `daily.cvd` 或 `daily.cld`；读取其 512 字节头内的构建时间，要求不超过 7 天，且不能比当前服务器时间超前 15 分钟，不以文件修改时间判断新鲜度。

实际扫描参数固定为展开总量 512 MiB、单文件 128 MiB、递归深度 8、最多 1024 个文件，并启用 `alert-exceeds-max=yes`。命中病毒返回 1，缺库、过期、超限、异常和未知退出码均不能产生通过记录。wrapper 内部超时为 90 秒，早于服务默认 120 秒扫描上限；命令没有 shell，上传内容不会被执行。

存储包含：

- `quarantine/<随机投稿ID>.zip`：已接受的原始 ZIP，文件没有执行权限；不会解包成可运行目录。
- `inbox.sqlite3`：元数据、状态、摘要、审核和扫描记录。
- `status-key`：私人查询凭证派生密钥。和数据库一起备份，丢失后旧凭证失效。
- `reviewed/<ID>.json`：通过审核的记录，仍标记 `not_published`，供 GitHub 发布流程使用。

Linux 应使用专用非特权账号、目录 `0700`、文件 `0600`，存储挂载尽可能加 `noexec,nodev,nosuid`。Windows 上 `chmod` 不等于 NTFS 私有 ACL，运营者必须给专用服务账户设置目录权限。私有数据、密钥和上传包不得提交到 Git。

## 限制与代理

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `WORKSHOP_MAX_UPLOAD_BYTES` | 268435456 | 单包上限，不能超过 256 MiB；健康接口会返回运营者设定值 |
| `WORKSHOP_QUOTA_BYTES` | 2147483648 | 全部已留存 ZIP 和正在上传的长度预留总额 |
| `WORKSHOP_MAX_SUBMISSIONS` | 1000 | 全部留存投稿数，包含待审核、已拒绝、已审核和上传预留 |
| `WORKSHOP_MAX_WORKERS` | 4 | HTTP 处理线程上限，范围 1–16 |
| `WORKSHOP_READ_TIMEOUT` | 10 | 单次 socket 读取超时秒数 |
| `WORKSHOP_UPLOAD_TIMEOUT` | 180 | 整个 ZIP 读取时限秒数 |
| `WORKSHOP_UPLOADS_PER_HOUR` | 10 | 每来源地址每小时投稿尝试数，失败也计数 |
| `WORKSHOP_STATUS_PER_MINUTE` | 60 | 每来源地址每分钟私人查询次数 |
| `WORKSHOP_SCAN_TIMEOUT` | 120 | 独立扫描进程超时秒数 |
| `WORKSHOP_TRUSTED_PROXY_IPS` | `[]` | 只向这些连接来源信任一个合法 `X-Real-IP` |

配额满时返回 `503 inbox_full`，不会覆盖旧作品、悄悄删除投稿或继续接收。拒绝投稿不会自动释放配额，需维护者先备份审核记录，再按保留策略维护存储。匿名投稿无法保证杜绝刷量；还应在公网代理限制来源请求、并发和总带宽。服务限速只做有界单进程防护，不替代代理层防护。

结构检查限制沿用桌面工坊：最多 512 个文件、ZIP 展开 512 MiB、单资源包 128 MiB、内层展开和对象数上限、压缩比限制、危险路径和大小写冲突拒绝、有效 PE/程序集 TextAsset 拒绝。检查用临时流，部署必须另外预留检查临时空间和内存；ZIP 存储配额不包括 SQLite、扫描器数据库和内层临时流。

反向代理使用 HTTPS，并将专用前缀转发到 loopback：

```nginx
location /jixing-workshop/ {
    client_max_body_size 80m;
    client_body_timeout 10s;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_connect_timeout 5s;
    proxy_read_timeout 200s;
    proxy_send_timeout 200s;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For "";
    proxy_pass http://127.0.0.1:19092/;
}
```

此配置需要 `WORKSHOP_TRUSTED_PROXY_IPS='["127.0.0.1"]'`。代理必须覆盖 `X-Real-IP`，不能透传上传者自己的头。如果在 Cloudflare 等上游代理后，还需先在 Nginx 配置可信上游的真实 IP 处理；不要直接相信公网请求的 `CF-Connecting-IP`。本进程不要直接绑定公网。管理员 API 也要由代理限速，可进一步限制维护者来源 IP。

## 上传协议

`POST /v1/submissions`，原始 ZIP body，必须有明确的正整数 `Content-Length`。不接收 chunked、multipart、重复关键头或 query 参数。

请求头：

- `Content-Type: application/zip`
- `X-Submission-Metadata`：7 个固定字段的 UTF-8 JSON，使用无 padding base64url 编码，编码后最多 24 KiB。字段为 `id,name,author,description,category,version,game_version`，限制与桌面 `validate_metadata` 一致。
- `Idempotency-Key`：32–128 位随机 `[A-Za-z0-9_-]` 字符，用于上传失败后安全重试；不要用作品名或序号。

新投稿返回 201，同键、同元数据、同长度、同摘要重试返回 200：

```json
{
  "submission_id": "32位随机十六进制ID",
  "status": "pending",
  "status_token": "私人查询凭证",
  "sha256": "ZIP SHA256",
  "size": 1234
}
```

同一键改包或改资料返回 409。同键上传还未结束时也返回 409。所有投稿以服务器随机 ID 存储，用户文件名不能选择服务器路径。私人查询为 `GET /v1/submissions/<ID>`，凭证放 `Authorization: Bearer <status_token>`，不得放 URL。状态接口不返回凭证、内部检查记录或 GitHub 审核单编号。

`GET /v1/health` 返回协议版本、包大小上限和 `accepting_uploads`。错误为 `{"error":{"code":"...","message":"..."}}`；客户端应按固定错误码显示文字，不将任意服务消息当 HTML 渲染。

## 维护者与 GitHub 审核

所有以下接口需要 `Authorization: Bearer <WORKSHOP_ADMIN_SECRET>`。密钥只存服务器运行环境及 GitHub Actions Secret；软件内没有维护者密钥和 GitHub token。

- `GET /v1/admin/submissions`、`/v1/admin/submissions/page/<页码>`：每页 100 条，按创建顺序返回 `submissions,total,next_page`。
- `GET /v1/admin/submissions/pending`、`/v1/admin/submissions/pending/page/<页码>`：只列待审核投稿，返回格式同上。
- 两种列表仅提供资源包数、文件数和展开量等检查摘要，保留扫描和审核记录；完整文件列表和内部检查资料仅由下面的详情接口返回，避免 100 个多文件作品使队列响应膨胀。
- `GET /v1/admin/submissions/<ID>`：含结构检查、扫描和人工审核记录。
- `GET /v1/admin/submissions/<ID>/package`：下载隔离 ZIP 供受控检查。已拒绝投稿不能下载。
- `POST /v1/admin/submissions/<ID>/github-issue`：JSON `{"issue_number":123}`。同编号重复绑定成功，改绑不同编号返回 409；客户端无法填写这个字段。GitHub 自动化还应以投稿 ID 标记搜索现有 Issue，避免“已建单但绑定响应丢失”时重复创建。
- `POST /v1/admin/submissions/<ID>/scan`：运行服务器配置的独立杀毒命令，没有扫描器或超时不能产生通过记录。只接受扫描器退出码 0 为通过，1/2 等值均不通过。命令参数来自服务器环境，无 shell，不执行上传文件，也不返回扫描器原始输出。
- `POST /v1/admin/submissions/<ID>/review`：提交以下固定 JSON。批准需要与当前 ZIP 摘要一致、48 小时内的服务器扫描记录，还需要明确的游戏测试证据；批准前重新执行结构检查。拒绝时 `scan_id` 可为空、`game_test` 为 null。

相同审核结论和证据的重复请求幂等返回 200。`decision:"revoke"` 可将已批准投稿下架到 `rejected`，`scan_id` 必须为空、`game_test` 为 null；保留此前审核和新增撤销记录，不再提供隔离包下载。GitHub 自动化还必须移除或撤销正式目录中的对应作品，服务状态变化本身不能撤销已经发布到 GitHub 的文件。

```json
{
  "decision": "approve",
  "reviewer": "维护者",
  "message": "审核结论",
  "scan_id": "scan接口返回的ID",
  "game_test": {
    "passed": true,
    "version": "实际测试的游戏版本",
    "evidence": "可核对的测试记录位置或链接",
    "notes": "至少20字符的详细测试结论，说明替换范围和实际验证结果"
  }
}
```

人工测试证据是受信任维护者的审核声明，服务无法自动证明实际玩过游戏。没有通过门槛不能 `approved`，通过门槛也只生成待发布审核记录。GitHub 工作流负责审核身份、Issue 操作、正式 Release 和工坊目录更新；投稿者不参与 GitHub 操作。

GitHub 自动化使用独立、固定到已审核提交的桥接代码，不从投稿包或 Issue 提供的分支执行代码。待审核 ZIP 始终留在私有投稿箱，不上传公开的 GitHub Actions artifact；审核 Issue 只公开作品介绍、投稿编号和 SHA256 等必要资料。维护者完成独立扫描和本地游戏验证，填写可核对的证据后，工作流才可以生成公开 Release 和目录条目。Issue 内容和标签都不应被当成认证身份；工作流必须另外验证操作人员的仓库权限。

## 自检

```sh
python -B -m unittest tests.test_workshop_service -v
python -B -m unittest tests.test_workshop_scanner -v
```

服务测试使用真实 loopback HTTP 流，覆盖断流、超时、包内程序、畸形头、配额、重试、私人查询、代理来源、维护者鉴权和审核门槛。扫描 wrapper 测试验证固定命令、签名日期、限制和异常处理；它使用 mock，不执行真实杀毒。服务的扫描协议测试也使用单独子进程 QA stub，两者都不能当成真实杀毒或游戏播放验证。部署后需另外使用真实 ClamAV 验证干净 fixture 和 EICAR 测试签名，再确认 HTTPS 全流程。
