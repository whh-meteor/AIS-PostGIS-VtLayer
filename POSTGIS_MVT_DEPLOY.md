# 北海渔船 PostGIS 与矢量切片服务部署文档

## 1. 部署范围

本说明只部署以下链路：

```text
已有 MySQL 8.0 数据库
  ├─ bh_location.boat_location_new       实时船位
  ├─ bh_business.dual_boat_base_mes      双控船档案
  └─ bh_business.town_boat_base_mes      乡镇船档案
                 │
                 ▼ 每 2 分钟读取一次，10 分钟重叠水位
          sync_boats.py
                 │
                 ▼
       PostgreSQL + PostGIS
                 │
                 ▼ 按请求动态生成 MVT
          tile_server.py :7800
                 │
                 ▼ 反向代理
            Nginx :6183
```

MySQL 库表和前端均视为已部署，本流程不创建或修改 MySQL 表，不构建前端，也不发布测试 HTML。源端账号只需拥有上述三张表的 `SELECT` 权限。

最终对外地址为：

```text
http://服务器IP:6183/vessel-tiles/tiles/boats/{z}/{x}/{y}.pbf
```

说明：

- `5432` 是 PostgreSQL 本机数据库端口，无远程管理需求时不应对外开放。
- `7800` 是 Uvicorn 内部服务端口，只监听 `127.0.0.1`。
- `6183` 是 Nginx 对外发布端口，只需向前端所在网络开放该端口。
- MVT 根据请求实时从 PostGIS 生成，不存在固定的瓦片文件目录；只有人工执行 `curl --output` 时才会产生测试文件。

## 2. 版本和目录要求

数据库函数使用了 `ST_TileEnvelope`、`ST_AsMVTGeom` 和 `ST_AsMVT`，要求 PostGIS 3.0 及以上；生产环境建议 PostgreSQL 14 及以上、PostGIS 3.3 及以上。PostgreSQL 与 PostGIS 必须来自兼容的软件包系列。

服务器既有 Conda 环境：

```text
/home/ubuntu/miniconda3/envs/py312
```

将 `deploy/vessel-tiles` 目录中的内容上传到服务器，最终目录结构如下：

```text
/data/ltgk/beihai/
├── .env.example
├── requirements.txt
├── sync_boats.py
├── tile_server.py
├── config/
│   └── vessel-tiles.env
├── nginx/
│   └── beihai6183.conf
├── sql/
│   └── 001_init_postgis.sql
├── systemd/
│   ├── vessel-sync.service
│   ├── vessel-sync.timer
│   ├── vessel-reconcile.service
│   ├── vessel-reconcile.timer
│   └── vessel-tiles.service
└── tmp/
```

所有后续命令均在服务器执行：

```bash
cd /data/ltgk/beihai
sudo install -d -o ubuntu -g ubuntu -m 0755 \
  /data/ltgk/beihai/config /data/ltgk/beihai/tmp
```

## 3. 安装并检查 PostgreSQL/PostGIS

如果 PostgreSQL 已安装，只需补装对应的 PostGIS 软件包。Ubuntu 可先检查版本及候选包：

```bash
psql --version
apt-cache search 'postgresql-[0-9]+-postgis-3$'
```

例如系统安装的是 PostgreSQL 16：

```bash
sudo apt update
sudo apt install postgresql-16-postgis-3 postgresql-16-postgis-3-scripts
sudo systemctl enable --now postgresql
```

如果实际主版本不是 16，将命令中的 `16` 替换为本机主版本。验证服务：

```bash
sudo -u postgres psql -tAc "select version();"
```

## 4. 创建数据库和最小权限账号

首次部署时创建写入账号 `vessel_sync`、只读函数调用账号 `vessel_tile` 和数据库 `vessel_gis`。不要将密码直接写在 shell 命令中：

```bash
sudo -u postgres psql
```

进入 `psql` 后执行：

```sql
-- 同步程序使用该账号写入船舶档案、最新船位和同步水位。
CREATE ROLE vessel_sync LOGIN;
\password vessel_sync

-- 切片程序仅使用该账号调用受控数据库函数。
CREATE ROLE vessel_tile LOGIN;
\password vessel_tile

-- 业务数据库由同步账号拥有。
CREATE DATABASE vessel_gis OWNER vessel_sync ENCODING 'UTF8';

-- PostGIS 扩展必须安装到目标数据库中。
\connect vessel_gis
CREATE EXTENSION IF NOT EXISTS postgis;
\quit
```

如果角色或数据库已经存在，跳过相应 `CREATE`，仅按需使用 `\password` 重设密码。确认扩展版本：

```bash
sudo -u postgres psql -d vessel_gis -tAc \
  "select extversion from pg_extension where extname='postgis';"
```

本方案中同步程序和切片程序都在数据库服务器本机运行，因此保持 PostgreSQL 仅监听本机即可，不需要把 `listen_addresses` 改成 `*`，也不需要为前端开放 5432。

## 5. 初始化 PostGIS 业务对象

以 `postgres` 执行初始化 SQL：

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -d vessel_gis \
  -f /data/ltgk/beihai/sql/001_init_postgis.sql
```

该脚本负责：

- 创建 `vessel` schema；
- 创建双控船、乡镇船、最新船位和同步状态表；
- 创建空间字段及查询索引；
- 创建 `vessel.boat_points_mvt(...)` 动态切片函数；
- 创建健康检查函数；
- 只向 `vessel_tile` 授予 schema 使用权和指定函数执行权。

SQL 使用 `SET search_path = vessel, pg_catalog, public`，避免表被误建到 `pg_catalog`。脚本可以重复执行以更新函数和注释，不会清空已有业务数据。

验证对象：

```bash
sudo -u postgres psql -d vessel_gis -c "\dt vessel.*"
sudo -u postgres psql -d vessel_gis -c "\df vessel.boat_points_mvt"
```

## 6. 安装 Python 依赖并配置连接

只使用既有 `py312` Conda 环境：

```bash
cd /data/ltgk/beihai
/home/ubuntu/miniconda3/envs/py312/bin/python -m pip install \
  -r /data/ltgk/beihai/requirements.txt
```

首次部署时复制配置模板；若正式配置已存在，不要覆盖：

```bash
test -f /data/ltgk/beihai/config/vessel-tiles.env || \
  sudo install -o ubuntu -g ubuntu -m 0600 \
  /data/ltgk/beihai/.env.example \
  /data/ltgk/beihai/config/vessel-tiles.env

sudo -u ubuntu vim /data/ltgk/beihai/config/vessel-tiles.env
```

至少修改以下字段。`CHANGE_ME` 不能保留在正式配置中：

```dotenv
# 已有 MySQL，只读访问三张源表。
MYSQL_HOST=实际MySQL地址
MYSQL_PORT=3306
MYSQL_USER=实际MySQL账号
MYSQL_PASSWORD='实际MySQL密码'
MYSQL_LOCATION_DB=bh_location
MYSQL_BUSINESS_DB=bh_business
SOURCE_TIMEZONE=Asia/Shanghai

# 密码分别对应第4步创建的两个 PostgreSQL 账号。
PG_SYNC_DSN="host=127.0.0.1 port=5432 dbname=vessel_gis user=vessel_sync password=同步账号密码 application_name=vessel_sync"
PG_TILE_DSN="host=127.0.0.1 port=5432 dbname=vessel_gis user=vessel_tile password=切片账号密码 application_name=vessel_tiles"

SYNC_OVERLAP_SECONDS=600
SYNC_FETCH_SIZE=2000
TILE_DB_POOL_SIZE=10
TILE_CACHE_SECONDS=60
TILE_HOST=127.0.0.1
TILE_PORT=7800
```

密码包含空格、`#`、`!` 等字符时应保留引号。保护正式配置：

```bash
sudo chown ubuntu:ubuntu /data/ltgk/beihai/config/vessel-tiles.env
sudo chmod 0600 /data/ltgk/beihai/config/vessel-tiles.env
```

修改 MySQL 地址、端口、账号或密码时，只需修改这个文件中的 `MYSQL_*` 项，然后重启/触发相关服务：

```bash
sudo systemctl restart vessel-tiles.service
sudo systemctl start vessel-sync.service
```

## 7. 安装 systemd 服务（不使用外部软连接）

将单元文件直接复制到 `/etc/systemd/system`，不要使用 `systemctl link`：

```bash
sudo install -o root -g root -m 0644 \
  /data/ltgk/beihai/systemd/vessel-sync.service \
  /etc/systemd/system/vessel-sync.service
sudo install -o root -g root -m 0644 \
  /data/ltgk/beihai/systemd/vessel-sync.timer \
  /etc/systemd/system/vessel-sync.timer
sudo install -o root -g root -m 0644 \
  /data/ltgk/beihai/systemd/vessel-reconcile.service \
  /etc/systemd/system/vessel-reconcile.service
sudo install -o root -g root -m 0644 \
  /data/ltgk/beihai/systemd/vessel-reconcile.timer \
  /etc/systemd/system/vessel-reconcile.timer
sudo install -o root -g root -m 0644 \
  /data/ltgk/beihai/systemd/vessel-tiles.service \
  /etc/systemd/system/vessel-tiles.service
sudo systemctl daemon-reload
```

服务作用：

| 单元 | 作用 |
| --- | --- |
| `vessel-sync.service` | 执行一次增量同步，成功退出后显示 `inactive (dead)` 属于正常现象 |
| `vessel-sync.timer` | 每 2 分钟触发一次增量同步 |
| `vessel-reconcile.service` | 执行一次只读源端的全量校准 |
| `vessel-reconcile.timer` | 每天 03:15 触发全量校准 |
| `vessel-tiles.service` | 常驻运行 MVT HTTP 服务，只监听 `127.0.0.1:7800` |

先执行首次同步并查看完整日志：

```bash
sudo systemctl start vessel-sync.service
sudo systemctl status vessel-sync.service --no-pager -l
sudo journalctl -u vessel-sync.service -n 100 --no-pager
```

日志出现 `sync complete` 即成功。随后启动切片服务和两个定时器：

```bash
sudo systemctl enable --now vessel-tiles.service
sudo systemctl enable --now vessel-sync.timer
sudo systemctl enable --now vessel-reconcile.timer
```

这里的 `enable` 只让 systemd 正常管理开机启动；部署文件本身已复制进 `/etc/systemd/system`，没有使用指向 `/data` 的 `systemctl link`。

检查运行状态：

```bash
systemctl status vessel-tiles.service --no-pager -l
systemctl list-timers 'vessel-*' --all
curl --fail http://127.0.0.1:7800/health
```

检查入库数量：

```bash
sudo -u postgres psql -d vessel_gis -c \
  "select count(*) as locations, max(location_time) as latest_time from vessel.boat_location_latest;"
```

## 8. 安装 Nginx 配置

完整配置文件是：

```text
/data/ltgk/beihai/nginx/beihai6183.conf
```

它是完整的 `server {}` 配置，负责把：

```text
http://服务器IP:6183/vessel-tiles/...
```

反向代理到：

```text
http://127.0.0.1:7800/...
```

直接复制配置，不创建软连接：

```bash
sudo install -o root -g root -m 0644 \
  /data/ltgk/beihai/nginx/beihai6183.conf \
  /etc/nginx/conf.d/beihai6183.conf

sudo /usr/sbin/nginx -t
sudo systemctl reload nginx
```

不要再把只有 `location {}` 的 `vessel-tiles.conf` 单独放到 `/etc/nginx/conf.d/`，否则会出现：

```text
"location" directive is not allowed here
```

如果服务器仍残留该旧文件，先确认后移出 Nginx 加载目录，再重新测试：

```bash
sudo install -d -o root -g root -m 0755 /data/ltgk/beihai/backup/nginx
sudo mv /etc/nginx/conf.d/vessel-tiles.conf \
  /data/ltgk/beihai/backup/nginx/vessel-tiles.conf.disabled
sudo /usr/sbin/nginx -t
sudo systemctl reload nginx
```

若启用了 UFW，只允许可信网段访问 6183。例如：

```bash
sudo ufw allow from 192.168.0.0/16 to any port 6183 proto tcp
```

按实际前端来源网段替换示例网段，不需要开放 7800 和 5432。

## 9. 服务地址与验证

健康检查：

```text
http://服务器IP:6183/vessel-tiles/health
```

标准路径瓦片：

```text
http://服务器IP:6183/vessel-tiles/tiles/boats/{z}/{x}/{y}.pbf
```

带离线时长过滤：

```text
http://服务器IP:6183/vessel-tiles/tiles/boats/{z}/{x}/{y}.pbf?offlineHours=2
```

兼容船讯网形式的地址：

```text
http://服务器IP:6183/vessel-tiles/tileserver/mvt/cache?z={z}&x={x}&y={y}&offlineHours=2
```

服务器本机验证：

```bash
curl --fail http://127.0.0.1:6183/vessel-tiles/health
curl --fail \
  --output /data/ltgk/beihai/tmp/boats.pbf \
  'http://127.0.0.1:6183/vessel-tiles/tiles/boats/10/822/449.pbf?offlineHours=2'
file /data/ltgk/beihai/tmp/boats.pbf
stat -c '%n %s bytes' /data/ltgk/beihai/tmp/boats.pbf
```

`file` 显示 `data` 是正常的，因为 PBF 是二进制 Protobuf 数据。瓦片很小甚至只有十余字节时，可能只是该瓦片范围内没有符合时间条件的船位，不代表服务失败。

## 10. 数据规则

- 服务端支持 `z=0～14`；当前前端可按实际方案使用其中层级。
- `z=0～10`：只对热点网格抽稀，稳定保留约 70% 点位，低密度网格全部保留。
- `z=11～14`：返回全量有效点位，不抽稀。
- 不生成聚合中心点，保留下来的要素仍是真实渔船点位。
- 定位时间在当前北京时间前 2 小时以内（含边界）标记为在线，超过 2 小时标记为离线。
- 定位时间为空或超过 24 小时的数据不参与关联、热点计数、抽稀和展示。
- `offlineHours` 可进一步缩短时间范围；省略或传 `0` 表示使用系统的 24 小时硬上限，不表示无限历史。
- 同步采用 10 分钟重叠水位和 PostgreSQL UPSERT，可重复执行。
- 每日全量校准用于捕获源表时间字段未推进时的修订或软删除变化。

## 11. 常见故障

### 11.1 `No module named 'dotenv'`

依赖装到了错误的 Python 环境。重新使用固定解释器安装：

```bash
/home/ubuntu/miniconda3/envs/py312/bin/python -m pip install \
  -r /data/ltgk/beihai/requirements.txt
```

### 11.2 PostgreSQL 密码认证失败

确认 `config/vessel-tiles.env` 中两个 DSN 的用户和密码分别匹配 `vessel_sync` 与 `vessel_tile`。需要重设时：

```bash
sudo -u postgres psql
\password vessel_sync
\password vessel_tile
\quit
sudo systemctl restart vessel-tiles.service
sudo systemctl start vessel-sync.service
```

### 11.3 `permission denied for schema pg_catalog`

应执行本部署包中的最新版 `001_init_postgis.sql`。不要把业务 schema 的 `search_path` 写成只有 `pg_catalog`；当前脚本已经显式设置为 `vessel, pg_catalog, public`。

### 11.4 同步服务显示 `inactive (dead)`

`vessel-sync.service` 是一次性任务。日志出现 `sync complete` 且退出码为 0 时，执行结束后显示 `inactive (dead)` 正常；周期调度状态应查看 `vessel-sync.timer`。

### 11.5 Nginx 根地址 403

本配置不发布静态页面，根地址会跳转到 `/vessel-tiles/health`，不依赖 `/data/ltgk/beihai/dist` 或 `/data/ltgk/beihai/html`。若仍返回旧的 403，通常是 Nginx 尚未加载新配置：

```bash
sudo /usr/sbin/nginx -T | grep -n -E 'listen 6183|vessel-tiles'
sudo /usr/sbin/nginx -t
sudo systemctl reload nginx
```

### 11.6 查看运行日志

```bash
sudo journalctl -u vessel-sync.service -n 100 --no-pager
sudo journalctl -u vessel-reconcile.service -n 100 --no-pager
sudo journalctl -u vessel-tiles.service -n 100 --no-pager
sudo tail -n 100 /var/log/nginx/error.log
```

## 12. 更新部署文件

更新 Python、SQL 或 systemd 文件后按类型执行：

```bash
# Python 服务代码更新
sudo systemctl restart vessel-tiles.service

# SQL 函数更新
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -d vessel_gis \
  -f /data/ltgk/beihai/sql/001_init_postgis.sql

# systemd 文件更新：重新复制对应文件后加载
sudo systemctl daemon-reload

# Nginx 配置更新
sudo install -o root -g root -m 0644 \
  /data/ltgk/beihai/nginx/beihai6183.conf \
  /etc/nginx/conf.d/beihai6183.conf
sudo /usr/sbin/nginx -t
sudo systemctl reload nginx
```

上线前最后检查：

```bash
systemctl is-active postgresql
systemctl is-active vessel-tiles.service
systemctl is-active vessel-sync.timer
systemctl is-active vessel-reconcile.timer
curl --fail http://127.0.0.1:6183/vessel-tiles/health
```
