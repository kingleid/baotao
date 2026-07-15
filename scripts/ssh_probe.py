#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read-only probe of a Baota server before deploying. Reports the environment so the
deploy scripts can be tuned. Performs NO writes except appending the local deploy
public key to authorized_keys when SSH_PUBKEY is provided (keeps key-based login).

Env-driven:
  SSH_HOST / SSH_PORT / SSH_USER
  SSH_KEYFILE (key auth) OR SSH_PWD (password auth)  -- choose one
  SSH_PUBKEY   (optional) public key string to append to ~/.ssh/authorized_keys
"""
import os, paramiko, socket

HOST = os.environ['SSH_HOST']
PORT = int(os.environ.get('SSH_PORT', '22'))
USER = os.environ['SSH_USER']
KEYFILE = os.environ.get('SSH_KEYFILE', '')
PWD = os.environ.get('SSH_PWD', '')
PUBKEY = os.environ.get('SSH_PUBKEY', '').strip()


def run(ssh, cmd, t=60):
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
    ec = ch.recv_exit_status()
    return ec, ''.join(out)


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    conn = dict(hostname=HOST, port=PORT, username=USER, timeout=30)
    if PWD:
        conn['password'] = PWD
    else:
        conn['key_filename'] = KEYFILE
    ssh.connect(**conn)
    P = lambda *c: print(*c, flush=True)

    def show(title, cmd, t=60):
        ec, out = run(ssh, cmd, t)
        P(f"=== {title} ===")
        P(out.strip() or "(empty)")

    P(f"connected {USER}@{HOST}")
    show("OS / arch", "cat /etc/os-release 2>/dev/null | head -2; uname -m")
    show("disk & mem", "df -h / 2>/dev/null | tail -1; free -h 2>/dev/null | head -2")
    show("Baota Node bin", "ls -d /www/server/nodejs/*/bin 2>/dev/null || echo 'no baota node'")
    show("node/npm/pm2 (baota)", "for p in /www/server/nodejs/*/bin/node; do $p -v 2>/dev/null; done; which pm2 2>/dev/null")
    show("nginx", "nginx -v 2>&1; ls /www/server/nginx/sbin/nginx 2>/dev/null")
    show("vhost dir (real)", "ls -la /www/server/panel/vhost/nginx/ 2>/dev/null | head")
    show("listening ports", "ss -ltnp 2>/dev/null | grep -E ':80 |:443 |:300[0-9]' || echo none")
    show("existing node procs", "ps aux | grep -E 'server.js|PM2' | grep -v grep | head")
    show("pm2 list", "/www/server/nodejs/*/bin/pm2 list 2>/dev/null | tail -8")

    if PUBKEY:
        ec, out = run(ssh, "mkdir -p ~/.ssh && chmod 700 ~/.ssh; grep -qxF '%s' ~/.ssh/authorized_keys 2>/dev/null || echo '%s' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; grep -c '%s' ~/.ssh/authorized_keys" % (PUBKEY, PUBKEY, PUBKEY), t=30)
        P("=== appended pubkey (count) ===")
        P(out.strip())

    ssh.close()
    P("DONE")


if __name__ == '__main__':
    main()
