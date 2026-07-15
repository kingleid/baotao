# baota-ssh-deploy — 宝塔面板 SSH 部署技能

> 一个 [WorkBuddy](https://www.codebuddy.cn) 技能：通过 SSH 把 Node.js（或任意 Web）
> 应用部署到 **宝塔面板（Baota）** 管理的服务器，并使其公网可访问。**不依赖宝塔面板
> HTTP 接口（API）**，所有操作都走 SSH（端口 22）。

---

## 它能做什么

| 能力 | 说明 |
|------|------|
| 🔍 环境探测 | 只读探测远程服务器：系统、磁盘、宝塔自带 Node 路径、nginx 二进制、真实 vhost 目录、监听端口、已有 pm2/node 进程 |
| 🚀 完整部署 | 上传源码 → `npm install --production` → 写 `ecosystem.config.js`（随机 `SESSION_SECRET`）→ pm2 守护 + 开机自启 → 健康检查 |
| 🌐 公网暴露 | 用 Nginx 反代把 `80 → 127.0.0.1:<port>`，绕过云安全组对 3000 等自定义端口的封锁，直接 `http://<服务器IP>` 访问 |
| 🔁 增量更新 | 改完代码只重传源码 + `pm2 restart`，跳过慢速 `npm install` |
| 🗂️ 面板原生项目 | 一键把应用注册为宝塔**托管的 Node 项目**（在面板「网站/项目 → Node项目」可见，可启停/重启/看日志，支持「域名管理」），与同机其它项目隔离 |

支持 **SSH 密钥** 或 **密码** 两种认证方式。

---

## 为什么用 SSH，而不是宝塔面板 API

**这是本技能最重要的设计决策。** 宝塔面板 API 在应用层有 **IP 白名单** 保护：当调用方
IP 不在白名单时，**所有**路径（包括安全入口 `/xxx` 和 `/api/*`）都会返回 **nginx 404**
（而不是真的 404）。这看起来像路径写错，其实是 IP 被拦了。

**SSH（端口 22）通常不受该白名单限制**，是最可靠的通路。即使要操作「面板托管的 Node 项目」，
也仍然走 SSH，只不过是在服务器上直接用宝塔自带的 Python 解释器调用面板内部模型类
`nodejsModel.main()`——这等价于「localhost 调用面板」，既可靠又 100% 面板原生，且绕开了
HTTP 白名单。

---

## 两种部署模式

### 模式 A：裸 SSH 部署（pm2 守护 + Nginx 反代）

适合「先把站跑起来联调」。进程由 pm2 管理，Nginx 仅做 80→本地端口的反代。

```
SSH → 上传源码 → npm install → pm2 start → nginx -s reload(80→127.0.0.1:3000)
```

### 模式 B：注册为宝塔面板托管的 Node 项目

适合「希望应用由宝塔面板管理」。项目出现在面板 UI 的 Node项目 列表，有官方启停/重启按钮、
日志，以及官方「域名管理」。同机多个项目彼此隔离（例如一台机器上同时跑基金站和 Markdown 编辑器）。

```
SSH → 上传源码 → npm install → create_project(安全名) → 改名成域名 → project_add_domain + bind_extranet
```

> 两种模式都支持密钥/密码认证。**不要同时用两种模式管同一个端口**，否则会抢端口。

---

## 文件结构

```
baota-ssh-deploy/
├── SKILL.md                      # 技能主文档（触发条件 + 完整工作流）
├── README.md                     # 本说明
├── 功能说明.html                  # 可视化功能说明
├── references/
│   ├── gotchas.md                # 裸部署踩坑清单与修复（9 项真实故障）
│   └── node_project.md           # 面板原生 Node 项目：注册/改名/绑域名 完整规范
└── scripts/
    ├── ssh_probe.py              # 环境探测（只读）
    ├── deploy.py                 # 完整部署（上传 + install + pm2 + 健康检查）
    ├── deploy_nginx.py           # Nginx 反代（IP 直连 / 域名+SSL 两种模式）
    ├── deploy_update.py          # 增量更新（上传 + pm2 restart）
    └── register_node_project.py  # 一键注册为面板托管 Node 项目
```

---

## 快速开始

### 0. 准备连接信息（从用户处安全获取）

- SSH 主机（通常是服务器 IP）、端口（默认 22）、用户名（常是 `root`）
- 认证：优先 **密钥**。本地生成 ed25519 密钥对，把**公钥**交给用户追加到
  `~/.ssh/authorized_keys`（不交换任何秘密）。兜底：密码（仅在本次会话内使用，绝不写入文件/记忆）
- 服务器网站根目录，如 `/www/wwwroot/your.domain`
- 应用监听端口（默认 `3000`）与健康检查端点（默认 `/healthz`）

> ⚠️ 绝不硬编码秘密。API key / SSH 密码只在部署时经环境变量传递，永不写入项目或记忆。

### 1. 探测环境（只读）

```bash
SSH_HOST=1.2.3.4 SSH_PORT=22 SSH_USER=root \
SSH_KEYFILE=/path/to/id_ed25519 \
SSH_PUBKEY="ssh-ed25519 AAAA... workbuddy-deploy" \
python scripts/ssh_probe.py
```

### 2. 完整部署（模式 A）

```bash
SSH_HOST=1.2.3.4 SSH_USER=root SSH_KEYFILE=/path/to/id_ed25519 \
LOCAL_DIR=/local/project REMOTE_DIR=/www/wwwroot/your.domain \
APP_NAME=myapp REMOTE_PORT=3000 \
python scripts/deploy.py
```

### 3. 用 Nginx 暴露到公网（模式 A）

```bash
# IP 直连（域名还在备案时最常见）：
SSH_HOST=1.2.3.4 SSH_USER=root SSH_KEYFILE=/path/to/id_ed25519 \
SERVER_IP=1.2.3.4 APP_PORT=3000 CONF_NAME=myapp_ip.conf \
python scripts/deploy_nginx.py

# 域名 + 已有 SSL 证书：
SSH_HOST=1.2.3.4 SSH_USER=root SSH_KEYFILE=/path/to/id_ed25519 \
DOMAIN=your.domain SSL_CERT=/path/to/fullchain.pem SSL_KEY=/path/to/privkey.pem \
python scripts/deploy_nginx.py
```

### 4. 增量更新（改完代码后）

```bash
SSH_HOST=1.2.3.4 SSH_USER=root SSH_KEYFILE=/path/to/id_ed25519 \
LOCAL_DIR=/local/project REMOTE_DIR=/www/wwwroot/your.domain \
APP_NAME=myapp \
python scripts/deploy_update.py
```

### 5. 注册为面板托管 Node 项目（模式 B）

```bash
SSH_HOST=110.42.209.211 SSH_USER=root SSH_PWD='<pwd>' \
LOCAL_DIR=/local/markdown-editor REMOTE_DIR=/www/wwwroot/markdown.kingsnake.asia \
DOMAIN=markdown.kingsnake.asia APP_NAME=md_editor REMOTE_PORT=3100 \
python scripts/register_node_project.py
```

### 6. 验证

```bash
# 公网应返回 200
curl -s -o /dev/null -w "%{http_code}" http://<server-ip>/

# 带 cookie 跑登录 + 一个鉴权接口，确认会话能穿过反代
```

---

## 部署踩坑清单（实战血泪史）

完整细节见 `references/gotchas.md`（模式 A）与 `references/node_project.md`（模式 B）。
摘要：

1. **面板 API 返回 404 = IP 白名单拦截** → 改用 SSH。托管项目则 SSH 上调 `nodejsModel.main()`。
2. **真实 vhost 目录是 `/www/server/panel/vhost/nginx`**，不是 `/www/server/nginx/conf/vhost/`。
3. **云安全组封锁 3000** → Nginx 反代 `80 → 127.0.0.1:3000`，零用户操作即可公网访问。
4. **pm2 孤儿进程**：`pm2 startup` 后务必确认 `pm2 jlist` 列出该应用；为空则重新 `pm2 start ecosystem.config.js && pm2 save`。
5. **别又在宝塔面板里启动同一应用** → 会抢端口。二选一。
6. **前端 `401 → location.reload()` 死循环**：401 应跳登录页，而非刷新。会话 cookie 设 `HttpOnly; SameSite=Lax`。
7. **宝塔自带 Node 在 `/www/server/nodejs/<ver>/bin`**（含 node/npm/pm2），部署脚本自动探测，别用 `dnf` 另装。
8. **面板 `start_project` 不注入 `PORT`**：`server.js` 必须 `process.env.PORT || 3100` 兜底，别依赖环境变量 PORT。
9. **项目名 `^\w+$` 限制**：带点域名创建即报错 → 先用安全名（如 `md_editor`）创建，再改名成域名。
10. **绑域名 = `project_add_domain` + `bind_extranet`**（顺序不能反）；绑之前先删手工写的 nginx conf，否则 `nginx -t` 因重复 `server_name` 失败。
11. **幂等重跑保护**：`register_node_project.py` 会检测已存在项目并跳过，避免二次重跑留下孤儿项目。

---

## 安全说明

- 所有操作都在 **你自己的远程服务器** 上通过 SSH 执行，技能本身不向任何第三方发送数据。
- 上传源码时**明确排除** `node_modules`、`data`、`.git`、日志、`.env` 等敏感/冗余文件。
- `SESSION_SECRET` 用 `os.urandom(24)` 在**服务器端随机生成**，无硬编码密钥。
- 脚本中仅有的安装命令（`npm install --production`、`dnf -y install gcc-c++ make python3`，
  用于编译原生模块）均在**远程服务器**执行，不存在本地供应链投毒风险。
- 公钥追加带幂等去重；`register_node_project.py` 中的 `os.remove` 只删它自己识别的重复 conf。
- 信息性提醒：脚本使用 `AutoAddPolicy()` 关闭了 SSH 主机密钥校验（连接自有服务器可接受；
  高安全环境可改为 `RejectPolicy` 并在首次连接时手动确认指纹）。

---

## 适用范围

- ✅ Linux 服务器 + 已装宝塔 + SSH 可达 + Node.js 应用（Express / Next / Nuxt / Koa …），入口监听单一端口。
- ⚠️ 非 Node 技术栈（PHP / Java）仅 Nginx 反代部分可复用。
- ⚠️ 技能文档中的 IP / 域名（如 `110.42.209.211`、`markdown.kingsnake.asia`）是**原作者
  的部署上下文示例**，请替换为你自己的服务器信息。

---

## License

按原技能分发许可使用。部署脚本与文档仅供你在自有服务器上自动化部署使用。
