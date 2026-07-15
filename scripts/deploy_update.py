#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Incremental update of an already-deployed app: re-upload source + pm2 restart.
Use after code changes (no dependency change). Faster than full deploy.py.

Env-driven (same as deploy.py minus install):
  SSH_HOST / SSH_PORT / SSH_USER
  SSH_KEYFILE (key auth) OR SSH_PWD (password auth)  -- choose one
  LOCAL_DIR / REMOTE_DIR
  APP_NAME   (default: app)
  REMOTE_PORT (default 3000, used for health check)
  NODE_BIN   (default: auto-detect)
"""
import os, paramiko, socket, time

HOST = os.environ['SSH_HOST']
PORT = int(os.environ.get('SSH_PORT', '22'))
USER = os.environ['SSH_USER']
KEY = os.environ.get('SSH_KEYFILE', '')
PWD = os.environ.get('SSH_PWD', '')
LOCAL = os.environ['LOCAL_DIR'].rstrip('/')
REMOTE = os.environ['REMOTE_DIR'].rstrip('/')
APP_NAME = os.environ.get('APP_NAME', 'app')
REMOTE_PORT = os.environ.get('REMOTE_PORT', '3000')

SKIP_DIRS = {'.git', 'node_modules', 'data', '__pycache__', '.workbuddy'}
SKIP_FILES = {'.env', '.env.local', 'server.log', 'npm-debug.log'}


def detect_node_bin(ssh):
    try:
        out = ssh_run(ssh, "ls -d /www/server/nodejs/*/bin 2>/dev/null | head -1", t=30).strip()
        if out:
            return out
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
        conn['key_filename'] = KEY
    ssh.connect(**conn)
    sftp = ssh.open_sftp()
    NODE_BIN = detect_node_bin(ssh)
    log = lambda *a: print('[update]', *a, flush=True)

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

    log('uploading source ->', REMOTE)
    upload_dir(LOCAL, REMOTE)

    log(ssh_run(ssh, f'export PATH={NODE_BIN}:$PATH && cd {REMOTE} && pm2 restart {APP_NAME} 2>&1 | tail -8', t=60).strip())
    time.sleep(3)
    log('health:', ssh_run(ssh, f'curl -s http://127.0.0.1:{REMOTE_PORT}/healthz; echo', t=30).strip())
    sftp.close()
    ssh.close()
    log('DONE')


if __name__ == '__main__':
    main()
