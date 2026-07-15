---
name: baota-ssh-deploy
description: |-
  Deploy a Node.js (or any web) app to a server managed by Baota (宝塔面板) via SSH
  and make it publicly reachable. This skill should be used when the user provides
  Baota panel credentials, wants a website deployed onto a Baota-managed server for
  real-environment testing, or asks to "put the site on the server / 宝塔 / deploy to
  宝塔 / 部署进宝塔联调 / 在宝塔里建 Node 项目 / 把域名绑到宝塔项目". It covers two
  deployment styles: (A) bare SSH deploy with pm2 guarding + Nginx reverse proxy
  (port 80 → app, bypassing cloud security-group port blocks); and (B) registering the
  app as a MANAGED Baota Node project via the panel's own internal Python model (so it
  appears in the Baota UI under Node项目, supports start/stop/restart + 域名管理, and
  is isolated from other projects). Both support SSH key OR password auth. It also
  documents the hard-won pitfalls: Baota HTTP panel API 404 from IP whitelist (use SSH
  + internal Python instead), the real vhost path, pm2 orphan processes, project-name
  ^\w+$ restriction, PORT not injected by Baota, and session-cookie infinite-reload.
agent_created: true
---

# Baota SSH Deploy

Deploy a web app onto a Baota (宝塔面板) managed server and make it publicly
reachable, without relying on the Baota panel API.

## When to use

- The user says they have a Baota (宝塔) panel and wants a developed site deployed
  there for real-environment debugging.
- The user provides Baota backend credentials (panel URL / API key / SSH / FTP).
- Any task of the form "部署到宝塔", "put it on my server", "联调一下线上".

Scope: the skill assumes a **Linux server with Baota installed, SSH reachable, and a
Node.js app** (Express/Next/Nuxt/Koa…) whose entry listens on a single port. For
non-Node stacks (PHP/Java), only the Nginx reverse-proxy part is reusable.

## Critical decision: use SSH, NOT the panel API

**Do not start from the Baota panel API.** The panel API is protected by an IP
whitelist at the application layer. When the calling machine's IP is not whitelisted,
*every* path — including the security entry `/xxx` and `/api/*` — returns **nginx 404**
(not a real 404). This looks like a wrong path but is really an IP block. Adding the
caller IP to the whitelist is possible but fiddly (panel + any front WAF/security group).

**SSH (port 22) is normally NOT covered by that whitelist**, so it is the reliable path.
Use the bundled scripts over SSH. See `references/gotchas.md` for the full diagnosis.

## Workflow

### 0. Gather connection info (from the user, secure channel)
- SSH host (default the server IP), port (default 22), username (often `root`).
- Auth: prefer **key-based**. Generate a local ed25519 keypair, hand the *public* key
  to the user to append to `~/.ssh/authorized_keys` (no secret exchange). Fallback:
  password (use paramiko; keep it in-session only, never write to files/memory).
- Web root on server, e.g. `/www/wwwroot/your.domain`.
- App listen port (default `3000`) and a health endpoint (default `/healthz`).
- Whether to use the server IP directly (most common while a domain is still ICP-filing)
  or a domain + SSL cert they will provide.

> ⚠️ Never hardcode secrets. API keys / SSH passwords travel only via env vars at
> deploy time and are never written into the project or memory.

### 1. Probe the environment (read-only)
Run `scripts/ssh_probe.py`. It reports OS, disk, Baota's bundled Node path
(`/www/server/nodejs/<ver>/bin`), nginx binary, the **real** vhost dir, listening
ports, and existing pm2/node processes. It can also append a local deploy pubkey.

```bash
SSH_HOST=1.2.3.4 SSH_PORT=22 SSH_USER=root \
SSH_KEYFILE=/path/to/id_ed25519 \
SSH_PUBKEY="ssh-ed25519 AAAA... workbuddy-deploy" \
python scripts/ssh_probe.py
```

### 2. Full deploy
Run `scripts/deploy.py`. It:
1. Uploads the project source (excluding `node_modules`, `data`, `.git`, logs, `.env`).
2. Runs `npm install --production` using Baota's Node; if it fails (native module like
   `better-sqlite3`), installs `gcc-c++ make python3` and retries.
3. Writes `ecosystem.config.js` with a **server-generated random SESSION_SECRET**.
4. Starts the app with `pm2`, runs `pm2 save` + `pm2 startup` (boot auto-start).
5. Verifies the app is actually in `pm2 jlist` (guards against the orphan-process gotcha).
6. Opens the app port in firewalld (optional) and does a localhost health check.

```bash
SSH_HOST=1.2.3.4 SSH_USER=root SSH_KEYFILE=/path/to/id_ed25519 \
LOCAL_DIR=/local/project REMOTE_DIR=/www/wwwroot/your.domain \
APP_NAME=myapp REMOTE_PORT=3000 \
python scripts/deploy.py
```

### 3. Make it public via Nginx reverse proxy
Cloud security groups usually block custom ports (3000). Port 80/443 are already open
for the panel's other sites, so proxy **80 -> 127.0.0.1:3000** and reach the app at
`http://<server-ip>` without touching the security group. Run `scripts/deploy_nginx.py`:

```bash
# IP-direct mode (no domain yet):
SSH_HOST=1.2.3.4 SSH_USER=root SSH_KEYFILE=/path/to/id_ed25519 \
SERVER_IP=1.2.3.4 APP_PORT=3000 CONF_NAME=myapp_ip.conf \
python scripts/deploy_nginx.py

# Domain + existing SSL cert mode:
SSH_HOST=1.2.3.4 SSH_USER=root SSH_KEYFILE=/path/to/id_ed25519 \
DOMAIN=your.domain SSL_CERT=/path/to/fullchain.pem SSL_KEY=/path/to/privkey.pem \
python scripts/deploy_nginx.py
```

The script writes the vhost into `/www/server/panel/vhost/nginx/`, runs `nginx -t`,
and reloads only if the test passes.

### 4. Incremental updates after code changes
After editing source (no new deps), run `scripts/deploy_update.py` — uploads source and
`pm2 restart`s, skipping the slow `npm install`.

### 5. Verify
From a machine with internet access:
- `curl -s -o /dev/null -w "%{http_code}" http://<server-ip>/` should be 200.
- Exercise login + an authenticated endpoint with a cookie jar to confirm the session
  survives through the proxy.

## 6. Register as a MANAGED Baota Node project (panel-native, shows in UI + 域名管理)

Use this when the user wants the app managed inside the Baota panel itself — visible
under **网站/项目 → Node项目**, with panel start/stop/restart buttons, logs, and
official **域名管理** (domain binding), fully isolated from other projects (e.g. a
fund-site also running on the same server). This is the规范 way to "在宝塔里建 Node 项目"
and "把域名加进项目的域名管理".

Principle: still SSH (port 22), but instead of the Baota **HTTP panel API** (which is
IP-whitelist-gated and returns nginx 404), run the panel's own model class directly on
the server: `/www/server/panel/pyenv/bin/python` → `from projectModel import
nodejsModel; m = nodejsModel.main()`. This is localhost-internal and bypasses the
whitelist while staying 100% panel-native.

Run `scripts/register_node_project.py` (handles upload + npm install + create + optional
rename-to-domain + optional domain bind in one shot):

```bash
SSH_HOST=110.42.209.211 SSH_USER=root SSH_PWD='<pwd>' \
LOCAL_DIR=/local/markdown-editor REMOTE_DIR=/www/wwwroot/markdown.kingsnake.asia \
DOMAIN=markdown.kingsnake.asia APP_NAME=md_editor REMOTE_PORT=3100 \
python scripts/register_node_project.py
```

What the script does and the gotchas baked in (full detail in
`references/node_project.md`):
- **Name `^\w+$` restriction**: `create_project` rejects dotted names, so it creates
  with a safe name (`md_editor`) then renames the DB row + the 3 pid/sh/log files to
  the domain (`markdown.kingsnake.asia`). Rename is safe because Baota's start/stop
  logic locates files by `name` and does not re-validate the format.
- **PORT not injected**: Baota's `start_project` does NOT set `PORT`. `server.js` MUST
  default to a fixed port (`process.env.PORT || 3100`). Never rely on env PORT.
- **Domain binding = `project_add_domain` + `bind_extranet`** (in that order). This is
  the official path that registers the domain in 域名管理 and generates
  `node_<name>.conf` (`server_name <domain>; proxy_pass http://127.0.0.1:<port>;`).
  `set_config` is a no-op unless BOTH `bind_extranet=1` AND `domains` is non-empty.
- **Remove any manually-written nginx conf** before binding, or duplicate `server_name`
  makes `nginx -t` fail.
- **Port clash**: pick a port other than 3000 (often taken by another project on the
  same box). Check `ss -ltnp | grep :<port>` first.

## 7. Hard-won pitfalls (read references/gotchas.md and references/node_project.md)

1. **Baota HTTP panel API 404 = IP whitelist block** (external caller IP not
   whitelisted → nginx 404 on every path). Use SSH. For a *managed* Node project,
   still SSH but call the panel's internal Python (`nodejsModel.main()`) — that is
   localhost-internal and NOT subject to the HTTP whitelist. See Section 6.
2. **Real vhost dir is `/www/server/panel/vhost/nginx`**, not `/www/server/nginx/conf/vhost/`.
3. **Security group blocks 3000** → proxy 80 → 127.0.0.1:3000.
4. **pm2 orphan process**: after `pm2 startup`, verify `pm2 jlist` lists the app; if
   empty, re-pull via `ecosystem.config.js` + `pm2 save`.
5. **Don't also start the same app from the Baota panel** — it will fight for the port.
6. **Frontend session bug**: a `401 → location.reload()` handler causes an infinite
   refresh loop. On 401, route to the login screen instead of reloading. Set session
   cookies `HttpOnly; SameParty=Lax` (or `SameSite=Lax`).

## References

- `references/gotchas.md` — detailed diagnosis and fixes for the bare-deploy pitfalls.
- `references/node_project.md` — **full spec for the managed-Node-project path**:
  panel Python path, `nodejsModel` method signatures (`create_project`,
  `project_add_domain`, `bind_extranet`, `set_config`), the `^\w+$` name restriction,
  PORT-not-injected caveat, rename procedure, verification queries, and a prioritized
  pitfall list.
- `scripts/ssh_probe.py`, `scripts/deploy.py`, `scripts/deploy_nginx.py`,
  `scripts/deploy_update.py` — the bare-deploy (pm2) workflow. All support key OR
  password auth (`SSH_KEYFILE` or `SSH_PWD`).
- `scripts/register_node_project.py` — **one-shot managed-Node-project registration**
  (upload + npm install + create + optional rename-to-domain + optional domain bind),
  with password/key auth. See Section 6.
