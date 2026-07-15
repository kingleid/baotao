# Baota 面板原生 Node 项目：注册 / 改名 / 绑域名 操作规范

> 适用场景：用户希望应用**作为宝塔面板托管的 Node 项目**存在（在面板「网站/项目 → Node项目」中可见、可启停/重启/看日志、支持「域名管理」），而不是裸 pm2 进程或手工 nginx 反代。
>
> 关键原则：**不走 HTTP 面板 API**（被 IP 白名单拦截，返回 nginx 404）。而是 SSH 连上服务器后，用宝塔自带的 Python 解释器直接跑面板内部模型类 `projectModel.nodejsModel.main()`。SSH 不在白名单范围内，等价"localhost 调用面板"，既可靠又完全面板原生。

---

## 0. 环境事实（已在 v24.18.0 环境验证）

| 项 | 路径 / 值 |
|----|-----------|
| 面板 Python（3.7） | `/www/server/panel/pyenv/bin/python` |
| 模型类文件 | `/www/server/panel/class/projectModel/nodejsModel.py` |
| 模型类名 | `main`（注意：不是文件名 `nodejsModel`） |
| `public` 模块位置 | `/www/server/panel/class/public.py` → 需 `sys.path.insert(0,'/www/server/panel/class')` |
| Baota Node 版本目录 | `/www/server/nodejs/<ver>/bin`（`<ver>` 如 `v24.18.0`） |
| pm2 二进制 | `/www/server/nodejs/<ver>/bin/pm2` |
| 面板项目表 | `/www/server/panel/data/default.db` 的 `sites` 表，`project_type='Node'` |
| 项目 pid/启动脚本/日志 | `/www/server/nodejs/vhost/{pids,scripts,logs}/<name>.{pid,sh,log}` |
| nginx vhost 目录 | `/www/server/panel/vhost/nginx/`（反代配置 `node_<name>.conf` 在此） |
| nginx 二进制 | `/www/server/nginx/sbin/nginx` |

**调用面板 Python 的固定头**（任何一段面板内逻辑都先加这两行）：
```python
import sys
sys.path.insert(0, '/www/server/panel/class')   # 让 'public' 与 'projectModel' 可导入
sys.path.insert(0, '/www/server/panel')
import public
from projectModel import nodejsModel
m = nodejsModel.main()
```

> WAL 陷阱：`sqlite3` CLI 直接读 `default.db` 可能读不到刚写入的行（WAL 未落盘）。**权威读取请用面板自己的 `public.M`**：
> ```bash
> /www/server/panel/pyenv/bin/python -c "
> import sys; sys.path.insert(0,'/www/server/panel/class'); sys.path.insert(0,'/www/server/panel')
> import public
> for r in public.M('sites').where('project_type=?','Node').select():
>     print(r['name'], r['project_config'])
> "
> ```

---

## 1. 创建项目 `create_project(get)`

签名：`def create_project(self, get)`，`get` 必须是 `public.dict_obj`（不是普通 dict）。

### 必填字段（缺一个会 AttributeError 或 return_error）
```python
data = {
    'project_name':    'md_editor',          # 仅 ^\w+$（字母/数字/下划线）。带点域名会被拒
    'project_cwd':     '/www/wwwroot/your.domain',  # 目录必须已存在
    'project_script':  'node server.js',     # 启动命令；'node server.js' 走 raw command 分支
    'project_ps':      'Markdown 编辑器',     # 备注
    'bind_extranet':   0,                     # 0=先不绑外网（域名单独绑）；1=创建时即绑
    'domains':         [],                    # bind_extranet=1 时需给域名列表
    'is_power_on':     1,                     # 1=开机自启
    'run_user':        'root',                # 目录属主一致，通常 root
    'max_memory_limit': 0,                     # 必须给（0=不限）
    'nodejs_version':  'v24.18.0',            # 已安装的版本，否则 is_install_nodejs 报错
    'pkg_manager':     'npm',                 # 必须 ∈ {npm,pnpm,yarn}
    'port':            3100,                  # 整数；注意端口占用检测用 get.get('port/port')，自填 dict 时该键为 None 会跳过检测——自己先确认端口空闲
}
print(m.create_project(public.dict_obj(data)))
```
返回含 `添加项目成功` 即成功。该方法内部会：写 `sites` 表 → `install_packages`(npm install) → `set_config` → `start_project` → 防火墙 → 返回。

### 端口注意（极易踩坑）
- 面板 `start_project` **不会注入 `PORT` 环境变量**。应用 `server.js` 必须自己兜底默认端口：
  ```js
  const PORT = process.env.PORT || 3100;   // 不要用 3000，易与同机其它项目冲突
  ```
- 创建前先确认端口没被占用：`ss -ltnp | grep :<port>`。

---

## 2. 改名成域名（绕过 `^\w+$` 限制）

`create_project` 拒绝带点的名字，但**运行期启停逻辑只按 `name` 拼文件、不校验格式**。所以标准做法：先用安全名创建，再直接改库 + 重命名文件。

需同步改 3 处：
1. `sites.name`
2. `project_config` 里的 `project_name`
3. 三个文件：`pids/<old>.pid`、`scripts/<old>.sh`、`logs/<old>.log` → 重命名为 `<new>`

```python
import json, os
r = public.M('sites').where('name=?', 'md_editor').find()
cfg = json.loads(r['project_config'])
cfg['project_name'] = 'markdown.kingsnake.asia'
public.M('sites').where('name=?', 'md_editor').save({
    'name': 'markdown.kingsnake.asia',
    'project_config': json.dumps(cfg),
})
base = '/www/server/nodejs/vhost'
for sub, ext in (('pids','.pid'), ('scripts','.sh'), ('logs','.log')):
    old = f'{base}/{sub}/md_editor{ext}'
    new = f'{base}/{sub}/markdown.kingsnake.asia{ext}'
    if os.path.exists(old):
        os.rename(old, new)
```
改名后面板「启动/停止/重启」按钮仍可用（它们按 `name` 动态定位文件）。`auto_run`（开机自启）是遍历 `sites` 表读 `name` 启动的，改名后依然生效。

---

## 3. 绑域名（官方方式，出现在「域名管理」）

正确顺序：**先 `project_add_domain` 登记域名 → 再 `bind_extranet` 开启外网映射**。`bind_extranet` 内部会：
- 校验 `domains` 非空（否则报"请先到域名管理添加域名"）
- 置 `bind_extranet=1` 并保存
- 调 `set_config` → `set_nginx_config` 生成 `node_<name>.conf`（`server_name <domain>;` → `proxy_pass http://127.0.0.1:<port>;`）
- `public.serviceReload()` 做 `nginx -t` + reload

> `set_config(name)` 在 `bind_extranet=0` 或 `domains` 为空时**直接 return False**，不发配置。所以"只 add_domain 不 bind_extranet"不会生成反代。

```python
# 先登记域名
g = public.dict_obj()            # 注意：本版 public.dict_obj() 不接受参数，只能无参构造
g.project_name = 'markdown.kingsnake.asia'
g.domains = ['markdown.kingsnake.asia']
print(m.project_add_domain(g))
# 再开启外网映射（生成 node_<name>.conf 并 reload nginx）
g2 = public.dict_obj()
g2.project_name = 'markdown.kingsnake.asia'
print(m.bind_extranet(g2))
```

### 必须清理手工反代配置
若之前为"先能用"而手工塞过一个 `markdown.kingsnake.asia.conf`，它的 `server_name` 与生成的 `node_markdown.kingsnake.asia.conf` 重复，会让 `nginx -t` 失败。**在 `bind_extranet` 之前删掉手工配置**：
```bash
rm -f /www/server/panel/vhost/nginx/markdown.kingsnake.asia.conf
```

---

## 4. 完整流程编排（推荐）

1. 上传源码（排除 node_modules/data/.git/.env）→ `/www/wwwroot/<domain>/`
2. 服务器 `npm install --production`（用 `/www/server/nodejs/<ver>/bin/npm`）
3. `server.js` 用 `process.env.PORT || 3100` 兜底端口
4. `create_project`（安全名，如 `md_editor`，`bind_extranet=0`）
5. （可选）改名为域名 `markdown.kingsnake.asia`
6. `project_add_domain` + `bind_extranet` 绑域名（先删手工 conf）
7. 验证（见下）

脚本封装见 `scripts/register_node_project.py`（一键完成 1–6，支持密码/密钥认证）。

---

## 5. 验证清单

```bash
# 面板项目列表（权威，绕过 WAL）
/www/server/panel/pyenv/bin/python -c "
import sys; sys.path.insert(0,'/www/server/panel/class'); sys.path.insert(0,'/www/server/panel')
import public
for r in public.M('sites').where('project_type=?','Node').select():
    print(r['name'], '| status', r['status'])
    print('   config:', r['project_config'])
"

# 端口监听（应被宝塔托管进程监听，非 pm2）
ss -ltnp | grep ':3100'

# 域名访问
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: markdown.kingsnake.asia' http://127.0.0.1/
curl -s http://127.0.0.1:3100/ | grep -oE '<title>[^<]*</title>'

# 反代配置
cat /www/server/panel/vhost/nginx/node_markdown.kingsnake.asia.conf

# 同机其它项目不受影响（如基金站 3000）
ss -ltnp | grep ':3000'
```

---

## 6. 常见坑（按出现频率）

1. **命名 `^\w+$` 限制**：带点域名（如 `markdown.kingsnake.asia`）创建即报错 → 用安全名创建后改名（第 2 节）。
2. **面板 `create_project` 不注入 `PORT`**：应用必须 `|| 3100` 兜底，否则落到 3000 与别的项目抢端口。
3. **端口冲突**：创建前 `ss -ltnp | grep :<port>` 确认空闲；同机多项目务必错开（如 3000/3100）。
4. **手工 nginx conf 与面板生成 conf 重复 server_name**：`nginx -t` 失败 → 绑域名前删掉手工 conf。
5. **sqlite3 CLI 读不到新行（WAL）**：用 `public.M` 读取，不要信 sqlite3 CLI 的空结果。
6. **`from projectModel import nodejsModel` 报 No module named 'public'**：必须同时把 `/www/server/panel/class` 和 `/www/server/panel` 都加进 `sys.path`（`public` 在 class 下）。
7. **类名是 `main` 不是 `nodejsModel`**：用 `nodejsModel.main()`。
8. **删除项目用面板 `remove_project`**，它会停进程、清配置、删 pid/sh/log 与域名记录——不要手动 rm 散文件。
9. **`public.dict_obj()` 不接受参数（本版）**：`project_add_domain` / `bind_extranet` 的 `get` 必须 `public.dict_obj()` 无参构造后**用属性赋值**（`g.project_name=...`、`g.domains=[...]`）。传 `public.dict_obj({...})` 会抛 `dict_obj() takes no arguments`，导致域名绑不上。`create_project` 同理，但它的入参写成普通 dict 会被内部 `_check_args`/`get` 适配，故仍可用 `public.dict_obj(data)` 形式——唯独 add_domain/bind_extranet 必须走属性方式。
10. **幂等重跑保护**：`register_node_project.py` 的 create 会同时检查 `SAFE_NAME` 与最终 `TARGET`（域名）是否任一已存在，存在则跳过；rename 仅在「SAFE_NAME 存在且 TARGET 不存在」时执行。否则二次重跑会再建一个 SAFE_NAME 并试图改名到已存在的 TARGET，引发 `sites` 表主键/唯一冲突、留下孤儿项目。
11. **IP 被手工 conf 按字母序抢占（同机多项目共享 IP）**：nginx 按 `server_name` 精确匹配；多个 server 块声明同一 IP（如 `server_name 110.42.209.211;`）时，按**文件名字母序**取先加载者（手工 `facai_ip.conf` 的 `f` 排在面板 `node_markdown.*.conf` 的 `n` 前），IP 访问会命中更早的 conf。最小风险修复：**不删**手工文件，仅把它的 `server_name` 从 IP 改成对应域名（如 `facai.kingsnake.asia`），释放 IP 给后加载的面板 conf；改前先 `cp` 备份、`nginx -t` 通过、`nginx -s reload`。要点：① 一个 IP 只能命中一个站，另一项目需改用域名访问（前提 DNS 已解析到本机）；② 面板「域名管理」里登记了域名但若 vhost 无对应生效 conf，该域名实际落到默认页——此时让手工 conf 改认域名即可同时救活该域名入口。
