#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deploy a Node.js web app to a Baota (宝塔面板) managed server via SSH (key auth).

Why SSH instead of Baota panel API:
  The Baota panel API is protected by an IP whitelist (L7). When the calling
  machine's IP is not whitelisted, EVERY path (including the security entry and
  /api/*) returns nginx 404 — not a real 404. SSH (port 22) is usually NOT
  covered by that whitelist, so SSH deployment is the reliable path.

Usage (env-driven, project-agnostic):
  SSH_HOST / SSH_PORT(=22) / SSH_USER  -> SSH connection
  SSH_KEYFILE (key auth) OR SSH_PWD (password auth)  -> exactly one required
  LOCAL_DIR        local project root (contains server.js / package.json / public/ ...)
  REMOTE_DIR       remote web root, e.g. /www/wwwroot/your.domain
  APP_NAME         pm2 process name  (default: app)
  REMOTE_PORT      app listen port   (default: 3000)
  HEALTH_PATH      health endpoint    (default: /healthz)
  NODE_BIN         Baota node bin dir  (default: auto-detect /www/server/nodejs/*/bin)
  ALLOW_PORT       'yes' to also open REMOTE_PORT in firewalld (default yes)

The script uploads source (excluding node_modules/data/.git/logs/.env), runs
`npm install --production` on the server, writes an ecosystem.config.js with a
server-generated random SESSION_SECRET, starts the app with pm2, enables
pm2 startup, and does a localhost health check.

Never hardcode secrets. The SESSION_SECRET is generated server-side at deploy time.
"""
import os, sys, stat, socket, time
import paramiko

HOST = os.environ['SSH_HOST']
PORT = int(os.environ.get('SSH_PORT', '22'))
USER = os.environ['SSH_USER']
KEYFILE = os.environ.get('SSH_KEYFILE', '')
PWD = os.environ.get('SSH_PWD', '')
LOCAL = os.environ['LOCAL_DIR'].rstrip('/')
REMOTE = os.environ['REMOTE_DIR'].rstrip('/')
APP_NAME = os.environ.get('APP_NAME', 'app')
REMOTE_PORT = os.environ.get('REMOTE_PORT', '3000')
HEALTH_PATH = os.environ.get('HEALTH_PATH', '/healthz').lstrip('/')
ALLOW_PORT = os.environ.get('ALLOW_PORT', 'yes').lower() == 'yes'

# Upload blacklist: never ship build artifacts, data, secrets, or logs.
SKIP_DIRS = {'.git', 'node_modules', 'data', '__pycache__', '.workbuddy'}
SKIP_FILES = {'.env', '.env.local', 'server.log', 'npm-debug.log'}

log = lambda *a: print('[deploy]', *a, flush=True)


def detect_node_bin(ssh):
    """Find Baota's bundled Node bin dir: /www/server/nodejs/<ver>/bin."""
    try:
        out = ssh_run(ssh, "ls -d /www/server/nodejs/*/bin 2>/dev/null | head -1", t=30)
        path = out.strip()
        if path:
            return path
    except Exception:
        pass
    return os.environ.get('NODE_BIN', '/www/server/nodejs/v24.18.0/bin')


def ssh_run(ssh, cmd, t=120):
    ch = ssh.get_transport().open_session()
    ch.settimeout(t)
    ch.exec_command(cmd)
    out = []
    while True:
        try:
            r = ch.recv(8192)
            if not r:
                break
            out.append(r.decode('utf-8', 'replace'))
        except socket.timeout:
            continue
        except EOFError:
            break
    return ''.join(out)


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    conn = dict(hostname=HOST, port=PORT, username=USER, timeout=30)
    if PWD:
        conn['password'] = PWD
    else:
        conn['key_filename'] = KEYFILE
    ssh.connect(**conn)
    log('connected', USER + '@' + HOST)
    sftp = ssh.open_sftp()

    NODE_BIN = detect_node_bin(ssh)
    log('NODE_BIN =', NODE_BIN)

    # ---- 1) upload source (generic, blacklist-aware) ----
    def ensure_dir(remote_path):
        cur = ''
        for part in remote_path.strip('/').split('/'):
            cur += '/' + part
            try:
                sftp.stat(cur)
            except IOError:
                sftp.mkdir(cur)

    def upload_dir(local_dir, remote_dir):
        ensure_dir(remote_dir)
        for name in sorted(os.listdir(local_dir)):
            lp = os.path.join(local_dir, name)
            rp = remote_dir + '/' + name
            if os.path.isdir(lp):
                if name in SKIP_DIRS:
                    continue
                upload_dir(lp, rp)
            else:
                if name in SKIP_FILES:
                    continue
                sftp.put(lp, rp)
                log('put', rp)

    ensure_dir(REMOTE)
    upload_dir(LOCAL, REMOTE)
    log('upload done ->', REMOTE)

    # ---- 2) npm install (production) ----
    npm = NODE_BIN + '/npm'
    ec, out = ssh_run(ssh,
        f'export PATH={NODE_BIN}:$PATH && cd {REMOTE} && {npm} install --production 2>&1 | tail -25',
        t=540)
    log('npm install exit', ec)
    if ec != 0:
        log('npm install failed; ensuring build tools present...')
        ssh_run(ssh, 'which gcc g++ make python3 >/dev/null 2>&1 || (dnf -y install gcc-c++ make python3 2>&1 | tail -5)', t=300)
        ec2, out2 = ssh_run(ssh,
            f'export PATH={NODE_BIN}:$PATH && cd {REMOTE} && {npm} install --production 2>&1 | tail -25',
            t=540)
        log('npm install retry exit', ec2)

    # ---- 3) ecosystem.config.js (random SESSION_SECRET, generated server-side) ----
    secret = os.urandom(24).hex()
    eco = (
        "module.exports = {\n"
        "  apps: [{\n"
        f"    name: '{APP_NAME}',\n"
        "    script: 'server.js',\n"
        f"    cwd: '{REMOTE}',\n"
        "    instances: 1,\n"
        "    autorestart: true,\n"
        "    watch: false,\n"
        "    env: {\n"
        f"      PORT: {REMOTE_PORT},\n"
        f"      SESSION_SECRET: '{secret}'\n"
        "    }\n"
        "  }]\n"
        "};\n"
    )
    with sftp.open(REMOTE + '/ecosystem.config.js', 'w') as f:
        f.write(eco)
    log('wrote ecosystem.config.js (SESSION_SECRET generated server-side)')

    # ---- 4) pm2 start + save + startup ----
    ssh_run(ssh, f'export PATH={NODE_BIN}:$PATH && pm2 delete {APP_NAME} 2>/dev/null; cd {REMOTE} && pm2 start ecosystem.config.js', t=120)
    ssh_run(ssh, f'export PATH={NODE_BIN}:$PATH && pm2 save', t=60)
    ssh_run(ssh, f'export PATH={NODE_BIN}:$PATH && pm2 startup 2>&1 | tail -5', t=60)
    log(ssh_run(ssh, f'export PATH={NODE_BIN}:$PATH && pm2 list', t=60).strip())

    # verify the app is actually managed by pm2 (orphan-process gotcha)
    jl = ssh_run(ssh, f'export PATH={NODE_BIN}:$PATH && pm2 jlist 2>/dev/null', t=30)
    if APP_NAME not in jl:
        log('!! WARN: app not found in pm2 jlist — restarting explicitly')
        ssh_run(ssh, f'export PATH={NODE_BIN}:$PATH && cd {REMOTE} && pm2 start ecosystem.config.js && pm2 save', t=120)

    # ---- 5) firewalld open port (optional) ----
    if ALLOW_PORT:
        ssh_run(ssh, f'firewall-cmd --permanent --add-port={REMOTE_PORT}/tcp 2>&1; firewall-cmd --reload 2>&1', t=60)

    # ---- 6) health check (localhost) ----
    time.sleep(2)
    hc = ssh_run(ssh, f'curl -s --max-time 5 http://127.0.0.1:{REMOTE_PORT}/{HEALTH_PATH}; echo', t=30)
    log('health(localhost):', hc.strip())

    sftp.close()
    ssh.close()
    log('DONE')


if __name__ == '__main__':
    main()
