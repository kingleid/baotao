#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Write a Baota Nginx reverse-proxy vhost and reload Nginx.

Context / gotchas:
  - Real vhost dir is /www/server/panel/vhost/nginx  (NOT /www/server/nginx/conf/vhost/).
    The main nginx.conf `include`s the panel vhost dir.
  - Cloud security groups usually BLOCK custom ports (e.g. 3000). Port 80 / 443 are
    already open for the panel's other sites, so proxy 80 -> 127.0.0.1:APP_PORT and
    access via http://<server-ip> without touching the security group.
  - When a domain is provided AND an SSL cert already exists, emit a 443 server with
    proxy + a 80->443 redirect. Otherwise emit a plain 80 server keyed by server IP.

Usage (env-driven):
  SSH_HOST / SSH_PORT / SSH_USER
  SSH_KEYFILE (key auth) OR SSH_PWD (password auth)  -- choose one
  SERVER_IP        public IP (used as server_name for the no-domain mode)
  APP_PORT         upstream port the Node app listens on (default 3000)
  CONF_NAME        vhost filename (default: <APP_NAME>_ip.conf or <DOMAIN>.conf)
  DOMAIN           (optional) domain; if set AND SSL_CERT/SSL_KEY given, emit HTTPS
  SSL_CERT         (optional) full path to cert (e.g. /www/server/panel/vhost/cert/...)
  SSL_KEY          (optional) full path to key
  UPSTREAM         (optional) override proxy_pass target (default http://127.0.0.1:APP_PORT)
"""
import os, paramiko, socket

HOST = os.environ['SSH_HOST']
PORT = int(os.environ.get('SSH_PORT', '22'))
USER = os.environ['SSH_USER']
KEYFILE = os.environ.get('SSH_KEYFILE', '')
PWD = os.environ.get('SSH_PWD', '')
SERVER_IP = os.environ.get('SERVER_IP', '')
APP_PORT = os.environ.get('APP_PORT', '3000')
DOMAIN = os.environ.get('DOMAIN', '').strip()
SSL_CERT = os.environ.get('SSL_CERT', '').strip()
SSL_KEY = os.environ.get('SSL_KEY', '').strip()
UPSTREAM = os.environ.get('UPSTREAM', f'http://127.0.0.1:{APP_PORT}')

VHOST_DIR = '/www/server/panel/vhost/nginx'
NGINX_BIN = '/www/server/nginx/sbin/nginx'

log = lambda *a: print('[nginx]', *a, flush=True)


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
    ec = ch.recv_exit_status()
    return ec, ''.join(out)


def build_conf():
    proxy_block = f"""        proxy_pass {UPSTREAM};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;"""

    if DOMAIN and SSL_CERT and SSL_KEY:
        name = (DOMAIN + '.conf')
        conf = f"""server {{
    listen 80;
    server_name {DOMAIN};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {DOMAIN};
    ssl_certificate {SSL_CERT};
    ssl_certificate_key {SSL_KEY};
    client_max_body_size 10m;

    location / {{
{proxy_block}
    }}
}}
"""
    else:
        name = os.environ.get('CONF_NAME', 'app_ip.conf')
        sn = DOMAIN if DOMAIN else SERVER_IP
        conf = f"""server {{
    listen 80;
    server_name {sn};
    client_max_body_size 10m;

    location / {{
{proxy_block}
    }}
}}
"""
    return name, conf


def main():
    name, conf = build_conf()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    conn = dict(hostname=HOST, port=PORT, username=USER, timeout=30)
    if PWD:
        conn['password'] = PWD
    else:
        conn['key_filename'] = KEYFILE
    ssh.connect(**conn)
    sftp = ssh.open_sftp()
    with sftp.open(VHOST_DIR + '/' + name, 'w') as f:
        f.write(conf)
    log('wrote', VHOST_DIR + '/' + name)

    ec, out = ssh_run(ssh, f'{NGINX_BIN} -t 2>&1', t=60)
    log('nginx -t:', out.strip(), '| exit', ec)
    if ec == 0:
        ec2, out2 = ssh_run(ssh, f'{NGINX_BIN} -s reload 2>&1', t=60)
        log('nginx reload exit', ec2, out2.strip())
    else:
        log('!! nginx config test FAILED — not reloading')
    sftp.close()
    ssh.close()
    log('DONE')


if __name__ == '__main__':
    main()
