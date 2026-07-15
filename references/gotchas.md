# Baota SSH Deploy — Gotchas & Fixes

Real failures hit during a Node.js fund-nav platform deployment to a Baota server
(`110.42.209.211`, OpenCloudOS 9.6, panel on `:8888/tencentcloud`). Each item below is
something that actually broke and the fix that worked.

---

## 1. Baota panel API returns 404 for everything → IP whitelist (L7)

**Symptom.** With a valid panel URL + security entry + API key, every request —
`/api/GetSystemTotal`, even the bare security-entry GET — returns `HTTP 404` with an
`nginx` (not Baota) server header. Path guesses (`/api/*`, HTTPS, alternate ports) all
fail the same way.

**Root cause.** The panel has an **API/IP whitelist at the application layer**. Unlisted
source IPs are "hidden" by replying 404 to every path. If it were a network/firewall
block you'd get a connection timeout (`000`), not a clean 404. A `curl` to a public IP
service confirmed the caller's egress IP (e.g. `116.233.235.206`).

**Fix.** Don't fight the whitelist. Use **SSH (port 22)** — it is normally outside that
L7 whitelist and connects directly. All deploy scripts in this skill run over SSH.

**If you must use the API:** ask the user to add the caller's egress IP to
面板设置 → API接口 → IP白名单 (and any front WAF / cloud security group), then retry.

---

## 2. The real Nginx vhost directory

**Symptom.** A hand-written vhost in `/www/server/nginx/conf/vhost/` has no effect.

**Root cause.** Baota's `nginx.conf` `include`s `/www/server/panel/vhost/nginx/`
(line ~96), NOT the generic `conf/vhost/`. Panel-managed sites live there.

**Fix.** Write vhost files to `/www/server/panel/vhost/nginx/<name>.conf`, then
`nginx -t && nginx -s reload`. The bundled `deploy_nginx.py` already targets this path.

---

## 3. Cloud security group blocks the app port (3000)

**Symptom.** Locally `curl 127.0.0.1:3000/healthz` → `{"ok":true}`, but from the public
internet `http://<ip>:3000/` times out (`000`).

**Root cause.** The cloud security group only allows 80/443/22 (and 8888 for the panel),
not 3000. The server's own `firewall-cmd` was opened, but the *cloud* group still blocks.

**Fix (zero user action).** Proxy **port 80 → 127.0.0.1:3000** via Nginx. Port 80 is
already open for the panel's other sites, so `http://<server-ip>` works immediately
without touching the security group. Use `deploy_nginx.py` in IP-direct mode.

**Alternative.** Ask the user to add an inbound rule for TCP 3000 in the cloud security
group (only the caller's IP, or 0.0.0.0/0 if acceptable), then access `:3000` directly.

---

## 4. pm2 orphan process after `pm2 startup`

**Symptom.** After `pm2 kill` + `pm2 start ecosystem.config.js` + `pm2 save` + `pm2
startup`, the app is still reachable (an old node process lingers on 3000) but `pm2
jlist` / `pm2 list` is **empty** — the process is no longer managed by pm2. On reboot,
pm2 may fail to resurrect it.

**Root cause.** `pm2 startup` re-spawns the pm2 daemon but the app wasn't loaded into the
new daemon's memory; only a stale node process kept serving.

**Fix.**
1. `pm2 kill` to clear everything.
2. `pm2 start ecosystem.config.js` (fresh), then `pm2 save`.
3. Re-check `pm2 jlist` — confirm the app name is present.
4. If empty, re-run `pm2 start ecosystem.config.js && pm2 save`.
5. Enable boot auto-start: `pm2 startup` (creates `pm2-root.service`, `systemctl enable`).
   Verify with `systemctl list-unit-files | grep pm2` and that `dump.pm2` contains the app.

The bundled `deploy.py` already performs the `jlist` guard (step 3/4) automatically.

---

## 5. Don't also start the app from the Baota panel

**Symptom.** User adds the site via the Baota "网站" UI; later the panel's Node manager
tries to launch its own instance on 3000 → port bind conflict.

**Root cause.** Two managers (SSH+pm2 vs panel Node manager) both want 3000.

**Fix / rule of thumb.** Pick ONE manager. Since our pm2 instance already holds 3000, an
attempt by the panel to bind 3000 just fails for the panel (our instance survives), but
it's confusing. Cleanest: have the user **delete the panel site entry** (check "不删除文件/
保留根目录" so our code isn't removed), or leave it but never click "启动". If the user
insists on panel management, stop our pm2 app + remove our vhost conf, then let the panel
run it (the dir's `www` ownership from panel creation is already compatible).

---

## 6. Frontend `401 → reload` infinite loop

**Symptom.** After login the page flickers / reloads forever, never reaching the app.

**Root cause.** A frontend `api()` helper did `if (res.status === 401) { location.reload();
throw ... }`. Any single 401 (e.g. a request that momentarily didn't carry the cookie)
triggers reload → still unauthenticated → 401 → reload → … death spiral. The server,
Nginx, and pm2 were all healthy; the bug was purely client-side.

**Fix.**
- On 401, redirect to the login view (`showLogin()`), do NOT `location.reload()`.
- After a successful login, don't immediately re-call `/api/me`; trust the login response.
- Set the session cookie explicitly: `cookie: { httpOnly: true, sameSite: 'lax', maxAge:
  ... }` so the browser reliably stores/returns it through the proxy.
- The user must **hard-refresh** (Ctrl+Shift+R / Cmd+Shift+R, or incognito) to clear the
  stale cookie/JS after a fix.

---

## 7. Baota's Node toolchain location

**Fact.** Baota bundles Node under `/www/server/nodejs/<ver>/bin` (e.g. `v24.18.0`), which
contains `node`, `npm`, AND `pm2`. Use it instead of installing Node via `dnf`. The deploy
scripts auto-detect this path (`ls -d /www/server/nodejs/*/bin`). Don't assume `node` is on
`PATH` in a fresh SSH session — always `export PATH=/www/server/nodejs/<ver>/bin:$PATH`.

---

## 8. `better-sqlite3` / native modules on the server

**Fact.** `npm install --production` on the server must compile native addons
(`better-sqlite3` uses prebuilt binaries when available, else node-gyp). If it fails,
ensure build tools: `dnf -y install gcc-c++ make python3`, then reinstall. The deploy
script does this fallback automatically.

---

## 9. Verifying the deployed static asset really arrived

**Gotcha.** `curl -s -o /dev/null -w "%{size_download}"` through a Git-Bash shell reported
`0` even though the file was fully present (205 KB) — a shell/curl quirk, not a missing
file. Confirm real delivery with `curl -sI` (look at `Content-Length`) or download to a
temp file and `wc -c`. When diagnosing Nginx 404s, make sure the test request uses the
**same `Host` header** the user's browser sends (e.g. `Host: <server-ip>`), otherwise it
hits the wrong `server_name` block (e.g. the `127.0.0.1` status block) and misleads you.
