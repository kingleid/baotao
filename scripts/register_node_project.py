#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Register a Node app as a MANAGED Baota (宝塔) Node project via the panel's own
internal Python model -- so it shows up in the Baota panel UI
(网站/项目 -> Node项目), can be started/stopped/restarted from the panel, and
supports panel domain management (域名管理).

This does NOT use the Baota HTTP panel API (which is IP-whitelist-gated and returns
nginx 404 when the caller IP is not whitelisted). Instead it runs the panel's own
model class `projectModel.nodejsModel.main()` directly on the server over SSH --
bypassing the HTTP whitelist while staying fully panel-native.

What it does (all over SSH, no manual nginx edits):
  1. (optional) upload LOCAL_DIR -> REMOTE_DIR
  2. (optional) npm install on the server (Baota's Node)
  3. create_project() with a SAFE project_name (^\\w+$ only -- dots are rejected)
  4. (optional) rename the project to DOMAIN. Baota's create_project rejects dotted
     names, so we create with a safe name then rename: sites.name +
     project_config.project_name + the 3 pid/sh/log files.
  5. (optional) bind DOMAIN via project_add_domain() + bind_extranet() -- this is the
     OFFICIAL way to register the domain in the panel and generate the
     node_<name>.conf reverse proxy (server_name DOMAIN -> 127.0.0.1:PORT).

Env:
  SSH_HOST / SSH_PORT(=22) / SSH_USER
  SSH_KEYFILE (key auth) OR SSH_PWD (password auth)  -- exactly one required
  LOCAL_DIR     (optional) local project root to upload
  REMOTE_DIR    remote web root, e.g. /www/wwwroot/your.domain  (REQUIRED)
  DOMAIN        the site domain, e.g. your.domain  (used for rename + bind)
  APP_NAME      safe project name (^\\w+$, no dots). Default: DOMAIN with [^0-9A-Za-z_] -> '_'
  NODE_VER      Baota node version dir name, e.g. v24.18.0  (default: auto-detect)
  REMOTE_PORT   app listen port (default 3100 -- NOT 3000, to avoid clashing with others)
  RUN_USER      (default root)
  DO_NPM        'yes' to run npm install (default yes)
  BIND_DOMAIN   'yes' to bind DOMAIN in the panel (default yes, but forced off if DOMAIN empty)
  SKIP_UPLOAD   'yes' to skip upload (app already on server)

IMPORTANT - port handling:
  Baota's start_project does NOT inject a PORT env var. Your server.js MUST default
  to a fixed port, e.g. `const PORT = process.env.PORT || 3100;`. Do NOT rely on
  env PORT from the panel.

Secrets: never hardcode. SSH_PWD travels only via env at deploy time.
"""
import os, sys, stat, time, paramiko, socket

HOST = os.environ['SSH_HOST']
PORT = int(os.environ.get('SSH_PORT', '22'))
USER = os.environ['SSH_USER']
KEYFILE = os.environ.get('SSH_KEYFILE', '')
PWD = os.environ.get('SSH_PWD', '')

LOCAL = os.environ.get('LOCAL_DIR', '').rstrip('/')
REMOTE = os.environ['REMOTE_DIR'].rstrip('/')
DOMAIN = os.environ.get('DOMAIN', '').strip()
APP_NAME = os.environ.get('APP_NAME', '').strip()
NODE_VER = os.environ.get('NODE_VER', '').strip()
REMOTE_PORT = os.environ.get('REMOTE_PORT', '3100')
RUN_USER = os.environ.get('RUN_USER', 'root')
DO_NPM = os.environ.get('DO_NPM', 'yes').lower() == 'yes'
BIND = os.environ.get('BIND_DOMAIN', 'yes').lower() == 'yes' and bool(DOMAIN)
SKIP_UPLOAD = os.environ.get('SKIP_UPLOAD', 'no').lower() == 'yes'

# safe project name: Baota create_project rejects anything not matching ^\w+$
if not APP_NAME:
    APP_NAME = ''.join(c if c.isalnum() or c == '_' else '_' for c in DOMAIN) if DOMAIN else 'app'
APP_NAME = ''.join(c if c.isalnum() or c == '_' else '_' for c in APP_NAME)

log = lambda *a: print('[register]', *a, flush=True)


def connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    conn = dict(hostname=HOST, port=PORT, username=USER, timeout=30)
    if PWD:
        conn['password'] = PWD
    else:
        conn['key_filename'] = KEYFILE
    ssh.connect(**conn)
    return ssh


def ssh_run(ssh, cmd, t=300):
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


def detect_node_ver(ssh):
    if NODE_VER:
        return NODE_VER
    out = ssh_run(ssh, "ls -d /www/server/nodejs/*/bin 2>/dev/null | head -1", t=30).strip()
    if out:
        # /www/server/nodejs/v24.18.0/bin -> v24.18.0
        return out.split('/nodejs/')[1].split('/bin')[0]
    return 'v24.18.0'


# ---- panel-python payload (runs on the server via /www/server/panel/pyenv/bin/python) ----
PAYLOAD = r'''
import sys, json, os
sys.path.insert(0, '/www/server/panel/class')
sys.path.insert(0, '/www/server/panel')
import public
from projectModel import nodejsModel

REMOTE_DIR = "__REMOTE_DIR__"
PORT = __PORT__
RUN_USER = "__RUN_USER__"
SAFE_NAME = "__SAFE_NAME__"
DOMAIN = "__DOMAIN__"
BIND = __BIND__
PS = "__PS__"
NODE_VER = "__NODE_VER__"

m = nodejsModel.main()

# target final name (DOMAIN if given, else SAFE_NAME)
TARGET = DOMAIN if DOMAIN else SAFE_NAME
final_name = TARGET

# 1) create (skip if SAFE_NAME OR TARGET already exists -- idempotent re-run safe)
if public.M('sites').where('name=?', SAFE_NAME).count() or public.M('sites').where('name=?', TARGET).count():
    print('CREATE: project already exists (safe=%s or target=%s), skip' % (SAFE_NAME, TARGET))
else:
    data = {
        'project_name': SAFE_NAME,
        'project_cwd': REMOTE_DIR,
        'project_script': 'node server.js',
        'project_ps': PS,
        'bind_extranet': 0,
        'domains': [],
        'is_power_on': 1,
        'run_user': RUN_USER,
        'max_memory_limit': 0,
        'nodejs_version': NODE_VER,
        'pkg_manager': 'npm',
        'port': PORT,
    }
    print('CREATE:', m.create_project(public.dict_obj(data)))

# 2) rename SAFE_NAME -> TARGET (only if SAFE_NAME exists AND TARGET does not yet).
#    Panel rejects dotted names at create time, so we create with a safe name then
#    rename; but on a re-run the safe name is gone and the target already exists, so
#    we must NOT recreate/rename (that would duplicate the row).
if SAFE_NAME != TARGET and public.M('sites').where('name=?', SAFE_NAME).count() and not public.M('sites').where('name=?', TARGET).count():
    r = public.M('sites').where('name=?', SAFE_NAME).find()
    cfg = json.loads(r['project_config'])
    cfg['project_name'] = DOMAIN
    public.M('sites').where('name=?', SAFE_NAME).save({'name': DOMAIN, 'project_config': json.dumps(cfg)})
    base = '/www/server/nodejs/vhost'
    for sub, ext in (('pids', '.pid'), ('scripts', '.sh'), ('logs', '.log')):
        old = '%s/%s/%s%s' % (base, sub, SAFE_NAME, ext)
        new = '%s/%s/%s%s' % (base, sub, DOMAIN, ext)
        if os.path.exists(old):
            os.rename(old, new)
            print('RENAME file', old, '->', new)
    print('RENAMED project to', DOMAIN)
elif SAFE_NAME != TARGET:
    if not public.M('sites').where('name=?', SAFE_NAME).count():
        print('RENAME: safe name %s already gone, target %s exists, skip' % (SAFE_NAME, TARGET))
    else:
        print('RENAME: target %s already exists, skip rename' % TARGET)

# 3) bind domain via OFFICIAL panel API (registers in 域名管理 + generates node_<name>.conf)
#    NOTE: public.dict_obj() takes NO args in this Baota version; build it then set
#    attributes (get.project_name / get.domains) -- the model reads them as attributes.
if BIND and DOMAIN:
    manual = '/www/server/panel/vhost/nginx/%s.conf' % DOMAIN
    if os.path.exists(manual):
        os.remove(manual)
        print('REMOVED manual conf', manual)
    try:
        g = public.dict_obj()
        g.project_name = final_name
        g.domains = [DOMAIN]
        print('ADD_DOMAIN:', m.project_add_domain(g))
    except Exception as e:
        print('ADD_DOMAIN err', e)
    try:
        g2 = public.dict_obj()
        g2.project_name = final_name
        print('BIND_EXTRANET:', m.bind_extranet(g2))
    except Exception as e:
        print('BIND_EXTRANET err', e)
    print('BOUND', DOMAIN, '-> project', final_name)

print('DONE')
'''


def main():
    ssh = connect()
    log('connected', USER + '@' + HOST)
    sftp = ssh.open_sftp()

    node_ver = detect_node_ver(ssh)
    log('NODE_VER =', node_ver)

    # ---- 1) upload source (optional) ----
    if LOCAL and not SKIP_UPLOAD:
        SKIP_DIRS = {'.git', 'node_modules', 'data', '__pycache__', '.workbuddy'}
        SKIP_FILES = {'.env', '.env.local', 'server.log', 'npm-debug.log'}

        def ensure_dir(rp):
            cur = ''
            for part in rp.strip('/').split('/'):
                cur += '/' + part
                try:
                    sftp.stat(cur)
                except IOError:
                    sftp.mkdir(cur)

        def upload_dir(ld, rd):
            ensure_dir(rd)
            for name in sorted(os.listdir(ld)):
                lp = os.path.join(ld, name)
                rp = rd + '/' + name
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
    else:
        log('skip upload (SKIP_UPLOAD=yes or no LOCAL_DIR)')

    # ---- 2) npm install (optional) ----
    if DO_NPM:
        npm = '/www/server/nodejs/%s/bin/npm' % node_ver
        ec, out = ssh_run(ssh,
            f'export PATH=/www/server/nodejs/{node_ver}/bin:$PATH && cd {REMOTE} && {npm} install --production 2>&1 | tail -25',
            t=540), None
        # ssh_run returns string; emulate ec via 'npm install' presence
        log('npm install done; tail:')
        log(ec[:1500] if ec else '(no output)')
    else:
        log('skip npm install (DO_NPM=no)')

    # ---- 3) run panel-python payload ----
    payload = (PAYLOAD
               .replace('__REMOTE_DIR__', REMOTE)
               .replace('__PORT__', str(REMOTE_PORT))
               .replace('__RUN_USER__', RUN_USER)
               .replace('__SAFE_NAME__', APP_NAME)
               .replace('__DOMAIN__', DOMAIN)
               .replace('__BIND__', 'True' if BIND else 'False')
               .replace('__PS__', DOMAIN or APP_NAME)
               .replace('__NODE_VER__', node_ver))
    with sftp.open('/tmp/register_payload.py', 'w') as f:
        f.write(payload)
    log('payload written to /tmp/register_payload.py')

    out = ssh_run(ssh, '/www/server/panel/pyenv/bin/python /tmp/register_payload.py 2>&1', t=320)
    log('--- panel output ---')
    log(out.strip())
    ssh_run(ssh, 'rm -f /tmp/register_payload.py', t=30)

    sftp.close()
    ssh.close()
    log('DONE')


if __name__ == '__main__':
    main()
