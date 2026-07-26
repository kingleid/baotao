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
  is isolated from other projects). (C) deploying a pure static HTML site and registering
  it in the panel 网站 list via the internal `panelSite.AddSite` model (shows in the UI,
  avoids the site.db data-source pitfall). Both support SSH key OR password auth. It also
  documents the hard-won pitfalls: Baota HTTP panel API 404 from IP whitelist (use SSH
  + internal Python instead), the real vhost path, pm2 orphan processes, project-name
  ^\w+$ restriction, PORT not injected by Baota, and session-cookie infinite-reload.
  For static sites it also covers: acme.sh's ZeroSSL default CA, install-cert won't
  mkdir, AddSite overwriting files, the site.db data-source, the Git-Bash path-mangling
  trap, and the db.Sql-vs-public.M DB-layer mismatch that makes a successful AddSite look
  like it wrote nothing.
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
non-Node stacks (PHP/Java), only the Nginx reverse-proxy part is reusable. It also covers
pure static HTML sites (no backend) — see Section 7.

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

## 7. Static HTML site (纯静态，无后端) — deploy + register in 网站列表

For a pure static site (single/multi-file HTML/CSS/JS, no backend). No pm2, no reverse
proxy. Flow: upload → issue SSL (acme.sh) → register in the panel `网站` list (so it shows
in the UI) → write a static Nginx vhost + reload.

Key facts (verified on the Tencent-Cloud-customized Baota · 腾讯云专享版, common on
Tencent Cloud images — other Baota versions may differ slightly):

- **The panel 网站 list reads `sites`/`domain` from `/www/server/panel/data/db/site.db`**,
  NOT `/www/server/panel/data/default.db`. `db.Sql` routes by table name via
  `config/databases.json` (sites/domain → site.db). So **just writing a Nginx vhost will
  NOT make the site appear in the panel UI** — you must register it in `site.db`.
- Register cleanly (panel-native, the 规范 way) by running the panel's own model on the
  server instead of hand-editing the DB:
  `/www/server/panel/pyenv/bin/python` → `sys.path.insert(0,'/www/server/panel/class')` →
  `import public, panelSite` → `panelSite.panelSite().AddSite(get)`.
- `get` is a `public.to_dict_obj({})` with:
  `webname='{"domain":"<d>","domainlist":["<d>:80"],"count":1}'`,
  `path='/www/wwwroot/<d>'`, `port='80'`, `version='00'` (pure static; `'00'` is present
  in `GetPHPVersion`), `ps='备注'`, `ftp='false'`, `sql='false'`, `type_id=0`,
  `project_type='PHP'`, `deploy_type=''`.
- **AddSite OVERWRITES root `index.html` and the vhost conf** (`init_site_files` +
  `nginxAdd`). **Back up both before calling, restore after, then `nginx -s reload`.**
- AddSite returns a dict containing `siteId` on success. It pings `www.bt.cn`
  (Tencent-custom reporting) but that does not affect the result; apache/openlitespeed are
  skipped if not installed.
- SSL: `acme.sh --set-default-ca --server letsencrypt` first (default CA is ZeroSSL and
  needs EAB credentials), and `mkdir -p` the cert dir before `--install-cert` (it will not
  create it). See Section 8 pitfall list.

Verify: query `/www/server/panel/data/db/site.db` `sites` table (NOT `data/default.db`);
`curl -sI https://<d>` returns `200`.

## 8. Hard-won pitfalls (read references/gotchas.md and references/node_project.md)

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

### Static-site / panel-registration pitfalls (hard-won during the fanqie deploy)

7. **Local Git-Bash path mangling (running deploy scripts on Windows)**: when you invoke
   `python.exe script.py` from Git Bash using a `/c/Users/...` path, Git Bash auto-converts
   it to `c:\c\Users\...` (note the doubled `c:\`), so Python raises "can't open file".
   **Use Windows-style paths `C:/Users/...`** (forward slashes are fine) for the script path
   and for any `SSH_*` env vars. This bit every script invocation until switched.

8. **Two DB access layers point to DIFFERENT files** — the #1 cause of "AddSite succeeded
   but the site isn't in the UI / the DB looks empty". `public.M(...)` reads
   `data/default.db` (sqlite), but the panel UI's `db.Sql(...)` routes `sites`/`domain`
   tables to `data/db/site.db` (per `config/databases.json`). After `AddSite` returns a
   `siteId`, do NOT query `data/default.db.sites` (it stays empty + mtime stale). Query
   `data/db/site.db.sites`, or better, verify through the panel's own ORM (`db.Sql`) the
   way the UI does.

9. **`db.Sql` may mirror small DBs to `/dev/shm` and open them read-only**. A bare external
   `sqlite3 data/db/site.db` connection can show stale or empty rows right after a write
   (WAL + `/dev/shm` copy). If a query looks empty but `AddSite` clearly returned a `siteId`,
   re-query via `db.Sql` (the panel loader flushes correctly) instead of a raw external
   connection — don't conclude the write failed.

10. **`curl size_download=0` is a false alarm** when `-o` is omitted: `curl -s URL -w
    "size=%{size_download}"` can print 0 even though the full body is served (the metric is
    unreliable without a write sink). Confirm real size with `curl -s URL -o /tmp/f -w
    "size=%{size_download}"` (got 29743 for the pomodoro page), or grep the response
    (`grep -o '<title>[^<]*</title>'`) — a correct title proves the body arrived.

11. **acme.sh default CA is ZeroSSL (needs EAB)** — `--issue` against the default CA silently
    fails because ZeroSSL requires an email + EAB key id/secret that you don't have. Switch
    first: `acme.sh --set-default-ca --server letsencrypt`, then `--register-account
    --email you@example.com` (one-time), THEN `--issue -d <d> --webroot <dir>`. This applies
    to the bundled acme.sh on Tencent-Cloud-customized Baota.

12. **`--install-cert` does NOT create the target cert dir**. `acme.sh --install-cert -d <d>
    --cert-file ... --key-file ... --fullchain-file ...` errors if
    `/www/server/panel/vhost/cert/<d>/` does not exist. `mkdir -p` it before `install-cert`.
    Note the split: `--issue` creates `~/ .acme.sh/<d>_ecc/` (the source certs), but NOT the
    panel cert dir where nginx expects to read them.

13. **`AddSite` writes via `init_site_files` + `nginxAdd`**, which OVERWRITE root `index.html`
    (with a Baota placeholder) and the vhost conf (with a default HTTP config). Always
    **back up both before calling `AddSite`, then restore after** and `nginx -s reload` —
    otherwise your real page + SSL vhost get clobbered (caught and fixed this way on fanqie).

14. **Run panel models via the panel's own pyenv**, not system python: `/www/server/panel/pyenv/bin/python`
    with `sys.path.insert(0,'/www/server/panel/class'); import public, panelSite`. System
    python lacks the `bt` modules and `public.M`/`db.Sql` bindings. Pass the SSH password only
    via an env var (`SSH_PWD`) to a local wrapper; never embed it in files.

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
