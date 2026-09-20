# MySQL 船位同步与 PostGIS 矢量切片

> 当前生产部署仅包含 PostgreSQL/PostGIS、MySQL 只读同步、动态 MVT 服务和 Nginx 发布。MySQL 库表及前端已部署时，请以 [POSTGIS_MVT_DEPLOY.md](./POSTGIS_MVT_DEPLOY.md) 为准；该文档不使用 `systemctl link`，也不依赖 `html` 或 `dist` 目录。

本目录对应以下实际数据源：

- `bh_location.boat_location_new`：当前船舶定位，`terminal_phone` 为主键。
- `bh_business.dual_boat_base_mes`：双控船档案，主键为 `id`，船名为 `boat_name`，船号为 `boat_code`。
- `bh_business.town_boat_base_mes`：乡镇船舶档案，主键为 `id`，船牌号为 `plan_boat_code`。
- MySQL `DATETIME` 按 `Asia/Shanghai` 解释，经纬度按 WGS84 的“经度、纬度”顺序写入 EPSG:4326。

最终链路：

```text
MySQL 8.0 -> sync_boats.py -> PostgreSQL/PostGIS
          -> boat_points_mvt(z,x,y,offlineHours) -> tile_server.py -> Nginx -> 前端
```

瓦片只开放 `z=0～14`。所有返回要素都是真实船位，不生成聚合中心点。`z=0～10` 只对船位严重重叠的热点网格抽稀，热点稳定保留约70%，低密度网格全部显示；`z=11～14` 完全不抽稀。beihai-front在1～10级使用抽稀切片，11～13级使用全量切片，MVT仅渲染分类圆点；13～20级显示WebSocket船型图标。13级允许两个图层重叠作为 `seamlessZoom` 的连续衔接层。定位时间在当前北京时间前2小时以内（含边界）为在线，超过2小时为离线；定位时间为空或超过24小时的记录不参与档案关联、热点计数、抽稀和前端展示。

同步脚本每次保留 10 分钟重叠窗口，通过 `terminal_phone` 幂等 UPSERT。首次运行自动全量读取；后续从 PostgreSQL `vessel.sync_state` 保存的水位继续。定时器每 2 分钟执行，因此即使 MySQL 每 5 分钟更新一次，也不依赖两个定时任务精确对齐。

由于源表 `boat_location_new.update_time` 没有 `ON UPDATE CURRENT_TIMESTAMP`，无法保证每类人工修改都会推进增量水位。部署包同时安排每天 03:15 做一次只读全量校准，捕获时间字段未更新的软删除或修订；这张表只保存每个终端的当前点，不是轨迹明细，因此日校准通常可控。

## 1. 检查并安装 PostgreSQL/PostGIS

先检查 `sudo apt install postgresql` 实际安装的版本：

```bash
psql --version
sudo -u postgres psql -tAc "select version();"
```

新环境建议 PostgreSQL 16 或 17、PostGIS 3.5 或 3.6。若当前是 PostgreSQL 16：

```bash
sudo apt update
sudo apt install postgresql-16-postgis-3 postgresql-16-postgis-3-scripts
```

## 2. 创建最小权限数据库角色

不要在命令行参数或脚本中直接写密码。进入 `psql` 后用 `\password` 交互设置：

```bash
sudo -u postgres psql
```

```sql
CREATE ROLE vessel_sync LOGIN;
CREATE ROLE vessel_tile LOGIN;
\password vessel_sync
\password vessel_tile
CREATE DATABASE vessel_gis OWNER vessel_sync ENCODING 'UTF8';
\connect vessel_gis
CREATE EXTENSION postgis;
\quit
```

PostGIS 扩展按数据库启用。已执行过该命令后，仍要确认它是在目标库 `vessel_gis` 中执行的：

```bash
sudo -u postgres psql -d vessel_gis -tAc \
  "select extversion from pg_extension where extname='postgis';"
```

如果没有返回版本号，需要在 `vessel_gis` 中再次创建；在其他数据库创建过不会自动对本库生效。

然后以 `postgres` 执行初始化文件。`CREATE EXTENSION postgis` 已执行也没关系，脚本可重复检查。业务对象归属为 `vessel_sync`，`vessel_tile` 只能执行瓦片/健康函数：

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 -d vessel_gis -f /data/ltgk/beihai/sql/001_init_postgis.sql
```

如果旧版脚本曾报 `permission denied for schema pg_catalog`，不要给 `vessel_sync` 增加 `pg_catalog` 权限。该问题是旧版脚本把 `pg_catalog` 放在会话 `search_path` 第一位造成的；更新 `001_init_postgis.sql` 后直接重新执行上述命令即可，已经创建的 `vessel` schema 会安全复用。

如果此前只执行过 `CREATE EXTENSION postgis`，不需要做迁移清理。如果曾执行过包含个人编队表的旧版初始化脚本，先备份数据库，再运行下列脚本；只要旧编队表中存在数据，脚本就会中止且不删除任何数据：

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 -d vessel_gis -f /data/ltgk/beihai/sql/003_remove_unused_fleet.sql
```

清理脚本不会自动删除可能被其他数据库使用的 `vessel_api` 角色；确认集群内没有其他依赖后，才由管理员单独处理该角色。

## 3. 安装程序和私密配置

程序目录固定为 `/data/ltgk/beihai`，Python 固定使用现有 Conda 环境 `py312`。先确认解释器并安装依赖，不再创建 venv：

```bash
cd /data/ltgk/beihai
test -r /data/ltgk/beihai/sync_boats.py
test -r /data/ltgk/beihai/tile_server.py
test -r /data/ltgk/beihai/requirements.txt
/home/ubuntu/miniconda3/envs/py312/bin/python --version
/home/ubuntu/miniconda3/envs/py312/bin/python -m pip install  -r /data/ltgk/beihai/requirements.txt
/home/ubuntu/miniconda3/envs/py312/bin/python -c "import pymysql, psycopg, fastapi, uvicorn; print('Python dependencies OK')"
```

配置、程序和测试输出也统一放在该目录下：

```bash
mkdir -p /data/ltgk/beihai/config /data/ltgk/beihai/tmp
test -f /data/ltgk/beihai/config/vessel-tiles.env || install -m 600 /data/ltgk/beihai/.env.example /data/ltgk/beihai/config/vessel-tiles.env
nano /data/ltgk/beihai/config/vessel-tiles.env
```

环境文件中的 MySQL 配置使用 `119.167.138.11:3306` 和已有 `ltgk` 账号。真实密码含 `!`，应在文件中使用单引号包裹，例如 `MYSQL_PASSWORD='实际密码'`，不要把密码直接放进命令参数、README 或提交到代码库。同时填写第 2 步创建的 `vessel_sync`、`vessel_tile` PostgreSQL 密码。

先交互输入 MySQL 密码测试网络和权限，避免密码进入 shell 历史：

```bash
mysql --host=119.167.138.11 --port=3306 --user=ltgk --password --execute="SELECT VERSION(); SELECT COUNT(*) FROM bh_location.boat_location_new;"
```

现有账号能使用，但权限可能过大。稳定运行后建议为同步创建 MySQL 专用只读账号，只授予：

```sql
GRANT SELECT ON bh_location.boat_location_new TO 'vessel_reader'@'<sync-host>';
GRANT SELECT ON bh_business.dual_boat_base_mes TO 'vessel_reader'@'<sync-host>';
GRANT SELECT ON bh_business.town_boat_base_mes TO 'vessel_reader'@'<sync-host>';
```

用户提供的现有数据库凭据已经出现在会话中，部署完成后建议轮换，生产脚本不要继续使用可写业务账号。

当前目录中的 `getInfo.txt` 是登录接口响应样例，不属于服务运行输入，并包含用户资料字段。应限制其文件权限，不要放入镜像或代码仓库；确认不再需要后安全删除。本目录的 `.gitignore` 已将它及真实环境文件排除。

## 4. 为增量查询增加源端索引

三个源表的 `update_time` 都需要适合增量水位查询的联合索引。先在测试环境检查重复索引及建索引耗时，再执行：

```bash
mysql --host=119.167.138.11 --port=3306 --user=ltgk --password < /data/ltgk/beihai/sql/002_optional_mysql_indexes.sql
```

如果不能修改 MySQL 表，脚本仍可运行，但数据库可能每两分钟全表扫描。应使用 `EXPLAIN` 和慢查询日志确认影响。

## 5. 首次同步与验证

五个 systemd 配置文件的用途、触发关系、安全选项和常用检查命令，见 `systemd/README.txt`。

通过 systemd 做首次测试，避免在命令行展开密码，并能正确读取包含特殊字符的 EnvironmentFile：

```bash
sudo systemctl link --force /data/ltgk/beihai/systemd/vessel-sync.service
sudo systemctl link --force /data/ltgk/beihai/systemd/vessel-sync.timer
sudo systemctl link --force /data/ltgk/beihai/systemd/vessel-reconcile.service
sudo systemctl link --force /data/ltgk/beihai/systemd/vessel-reconcile.timer
sudo systemctl link --force /data/ltgk/beihai/systemd/vessel-tiles.service
sudo systemctl daemon-reload
sudo systemctl start vessel-sync.service
sudo systemctl status vessel-sync.service --no-pager
sudo journalctl -u vessel-sync.service -n 100 -l --no-pager
```

`vessel-sync.service` 是 `Type=oneshot`。同步成功后进程会退出，状态显示 `inactive (dead)` 属于正常现象；判断成功与否应查看本次日志是否包含 `sync complete`、`Deactivated successfully` 和 `Finished vessel-sync.service`，并确认没有新的 `status=1/FAILURE`。本次首次成功同步的基线为：双控船档案 9,514 条、乡镇船档案 16,582 条、定位记录 28,259 条；后续数量会随源库变化。

如果日志提示 `can't open file '/data/ltgk/beihai/sync_boats.py'`，说明只部署了 `sql/systemd` 等子目录，没有把 Python 主程序放到部署根目录。先定位文件：

```bash
ls -l /data/ltgk/beihai/sync_boats.py  /data/ltgk/beihai/tile_server.py /data/ltgk/beihai/requirements.txt
find /data/ltgk/beihai -maxdepth 4 -type f \( -name 'sync_boats.py' -o -name 'tile_server.py' -o -name 'requirements.txt' \)
```

推荐目录结构是三个文件直接位于 `/data/ltgk/beihai/`。如果它们实际位于子目录，可以复制到部署根目录，或者同时修改三个 service 的 `WorkingDirectory`、`ExecStart/ExecStartPre`，两种方式只能选择一种并保持一致。更新 service 文件后重新执行 `sudo systemctl daemon-reload`。

检查环境文件中的非敏感连接项（命令不会输出密码）：

```bash
grep -E '^(MYSQL_HOST|MYSQL_PORT|MYSQL_USER|MYSQL_LOCATION_DB|MYSQL_BUSINESS_DB)=' /data/ltgk/beihai/config/vessel-tiles.env
```

当前实际 MySQL 地址应为 `119.167.138.11`；如果仍是旧地址，编辑环境文件后再启动同步。

如果日志提示 `password authentication failed for user "vessel_sync"`，说明 `PG_SYNC_DSN` 中仍是占位密码或与 PostgreSQL 角色密码不一致。使用交互方式重置两个角色密码，避免密码进入 shell 历史：

```bash
sudo -u postgres psql
```

```sql
\password vessel_sync
\password vessel_tile
\quit
```

随后编辑 `/data/ltgk/beihai/config/vessel-tiles.env`，把相同密码分别写入 `PG_SYNC_DSN` 和 `PG_TILE_DSN`。建议 PostgreSQL 密码不要包含空格、单引号或反斜杠，以免 libpq conninfo 需要额外转义。可在不输出密码的情况下直接用同步程序验证；密码修正后重新启动服务即可。

数据库验证：

```bash
sudo -u postgres psql -d vessel_gis -c "
SELECT
  (SELECT count(*) FROM vessel.dual_boat_archive) AS dual_count,
  (SELECT count(*) FROM vessel.town_boat_archive) AS town_count,
  (SELECT count(*) FROM vessel.boat_location_latest) AS location_count,
  (SELECT count(*) FROM vessel.boat_location_latest
    WHERE geom_3857 IS NOT NULL AND del_flag = false) AS valid_geometry_count;
"
sudo -u postgres psql -d vessel_gis -c "SELECT vessel.tile_health();"
sudo -u postgres psql -d vessel_gis -c "SELECT postgis_full_version();"
```

只有明确要使 PostGIS 成为 MySQL 当前表的精确镜像时，才执行硬删除对账：

```bash
sudo systemctl stop vessel-sync.timer
sudo -u ubuntu bash -c   'set -a; source /data/ltgk/beihai/config/vessel-tiles.env; exec /home/ubuntu/miniconda3/envs/py312/bin/python /data/ltgk/beihai/sync_boats.py --full --prune'
sudo systemctl start vessel-sync.timer
```

`--prune` 会删除 MySQL 中已不存在的目标记录；日常软删除由三个源表的 `del_flag` 自动处理。

## 6. 启动定时同步和瓦片服务

```bash
sudo systemctl enable --now vessel-sync.timer
sudo systemctl enable --now vessel-reconcile.timer
sudo systemctl enable --now vessel-tiles.service
systemctl list-timers vessel-sync.timer vessel-reconcile.timer
sudo systemctl status vessel-tiles.service --no-pager
curl --fail http://127.0.0.1:7800/health
```

两个 timer 应显示下一次触发时间；`vessel-tiles.service` 应为 `active (running)`。同步 timer 每两分钟触发一次 oneshot，同步 service 在每次成功运行后仍会回到 `inactive (dead)`。

测试一个北海附近的 XYZ 瓦片时，应根据实际地图层级替换坐标：

```bash
curl --fail --output /data/ltgk/beihai/tmp/boats.pbf  http://127.0.0.1:7800/tiles/boats/10/822/449.pbf
file /data/ltgk/beihai/tmp/boats.pbf
```

限制只显示最近 2 小时报位的船：

```bash
curl --fail --output /data/ltgk/beihai/tmp/boats-2h.pbf  'http://127.0.0.1:7800/tiles/boats/10/822/449.pbf?offlineHours=2'
```

也可以使用类似船讯网的查询参数形式：

```bash
curl --fail --output /data/ltgk/beihai/tmp/boats-2h.pbf \
  'http://127.0.0.1:7800/tileserver/mvt/cache?z=10&x=822&y=449&offlineHours=2'
```

命令中必须直接填写原始 URL，不能复制成 Markdown 的 `[URL](URL)` 形式。`.pbf` 是二进制 Protobuf，`file` 显示 `data` 属正常结果。检查响应头和字节数可使用：

```bash
curl --fail --silent --show-error \
  --dump-header /data/ltgk/beihai/tmp/boats-2h.headers \
  --output /data/ltgk/beihai/tmp/boats-2h.pbf \
  'http://127.0.0.1:7800/tileserver/mvt/cache?z=10&x=822&y=449&offlineHours=2'
file /data/ltgk/beihai/tmp/boats-2h.pbf
wc -c /data/ltgk/beihai/tmp/boats-2h.pbf
```

本服务的合法空 `boats` 图层正好是 14 字节。因此 14 字节表示该 XYZ 瓦片在指定 `offlineHours` 下没有匹配船位，不表示请求失败。可以去掉 `offlineHours` 对比；也可直接检查数据库中该瓦片的船数：

```bash
sudo -u postgres psql -d vessel_gis -c "
WITH bounds AS (
  SELECT ST_TileEnvelope(10, 822, 449) AS geom
)
SELECT
  count(*) AS all_boats,
  count(*) FILTER (
    WHERE location_time >= timezone('Asia/Shanghai', statement_timestamp())
      - interval '2 hours'
  ) AS boats_in_2h
FROM vessel.boat_location_latest, bounds
WHERE del_flag = false
  AND geom_3857 IS NOT NULL
  AND geom_3857 && bounds.geom;
"
```

当前查询入口只定义 `z`、`x`、`y`、`offlineHours`。船讯网示例里的 `shipType`、`shipLength`、`shipNaviStat`、`shipSog`、`preMmsi` 是其自身数据字典，不能在没有字段映射规则时直接照搬。MVT 使用 Web Mercator 瓦片边界并在编码时转为瓦片局部坐标，不需要额外传 `srid=3395`。

`offlineHours` 支持小数，范围为 0～24。省略或传 `0` 表示采用系统的 24 小时显示上限，不表示无限制；传 1、2、3 等值可进一步缩短范围。修改下拉值后，前端必须更新瓦片 URL 并清理当前图层的瓦片缓存。

MVT 图层名固定为 `boats`，所有层级都返回真实单船点，不返回 `cluster` 或 `pointCount`。热点抽稀规则如下：

| 层级 | 每瓦片网格 | 网格热点阈值 | 热点保留规则 |
|---|---:|---:|---:|
| 0～10（前端使用1～10） | 32 × 32 | 超过 4 艘 | `ceil(网格船数 × 70%)` |
| 11～14（前端使用11～13） | 不划分 | 不判断 | 全部 |

在512像素瓦片上，`32×32` 网格约等于每格 `16×16` 屏幕像素，与12像素船位图标的重叠范围接近。0～10级网格内1～4艘全部返回；从第5艘开始才视为热点，并按定位时间从新到旧稳定保留约70%。使用向上取整，例如5艘保留4艘、10艘保留7艘。11～14级不计算抽稀名额，直接返回该瓦片内全部符合时间条件的船位。

阈值位于 `001_init_postgis.sql` 的 `bounds.density_threshold`，保留比例位于 `bounds.retention_ratio`。上线后可根据图标尺寸、热点瓦片体积和前端帧率调整。修改 SQL 后在现有数据库重新执行初始化脚本即可替换函数，不需要重建表：

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 -d vessel_gis \
  -f /data/ltgk/beihai/sql/001_init_postgis.sql
```

## 7. Nginx 发布

本机 Nginx 位于 `/usr/sbin/nginx`，加载目录为 `/etc/nginx/conf.d`。6183 发布独立的 Maptalks 查看页和渔船矢量切片，不依赖前端工程的 `dist`。完整配置为 `/data/ltgk/beihai/nginx/beihai6183.conf`：根地址的 `root` 必须是 `/data/ltgk/beihai/html`，从而读取 `/data/ltgk/beihai/html/index.html`；`/vessel-tiles/` 转发到 `127.0.0.1:7800`。

`nginx/vessel-tiles.conf` 只是可嵌入其他 `server` 的 `location` 片段，不能作为独立文件放在 `/etc/nginx/conf.d` 顶层。当前服务器已经存在该文件，先将它移动到 `/data` 下备份，避免 `location directive is not allowed here` 或重复配置：

```bash
mkdir -p /data/ltgk/beihai/backup/nginx
sudo mv /etc/nginx/conf.d/vessel-tiles.conf /data/ltgk/beihai/backup/nginx/vessel-tiles.conf.disabled
```

不使用软连接。先确认查看页存在，再把完整配置直接复制到 Nginx 配置目录。`install` 会将目标配置设为 `root:root`、权限 `0644`：

```bash
test -r /data/ltgk/beihai/html/index.html
sudo install -o root -g root -m 0644 /data/ltgk/beihai/nginx/beihai6183.conf /etc/nginx/conf.d/beihai6183.conf
sudo /usr/sbin/nginx -t
sudo systemctl reload nginx
```

浏览器访问 `http://192.168.2.60:6183/` 即可打开查看页。每次修改 `/data/ltgk/beihai/nginx/beihai6183.conf` 后，都要重新执行 `install`、`nginx -t` 和 `reload`，因为 `/etc/nginx/conf.d/beihai6183.conf` 是独立副本，不会自动更新；只修改 HTML 不需要重载 Nginx。

验证监听端口、网关根地址和瓦片代理：

```bash
sudo ss -lntp | grep ':6183'
curl --fail --silent --show-error http://127.0.0.1:6183/
curl --fail --silent --show-error http://127.0.0.1:6183/vessel-tiles/health
curl --fail --silent --show-error --output /data/ltgk/beihai/tmp/boats-via-nginx.pbf 'http://127.0.0.1:6183/vessel-tiles/tileserver/mvt/cache?z=10&x=822&y=449&offlineHours=2'
wc -c /data/ltgk/beihai/tmp/boats-via-nginx.pbf
```

如果服务器启用了 UFW 且需要从其他机器访问，再按实际网络安全策略开放 `6183/tcp`；不要在未确认访问范围时直接对公网放行。

前端模板地址为：

```text
/vessel-tiles/tiles/boats/{z}/{x}/{y}.pbf
```

查询参数形式为：

```text
/vessel-tiles/tileserver/mvt/cache?z={z}&x={x}&y={y}&offlineHours={hours}
```

服务返回 `application/vnd.mapbox-vector-tile`，默认浏览器/代理缓存 60 秒。同步周期是 2 分钟、源数据周期是 5 分钟，因此不建议设置长时间瓦片缓存。
无要素瓦片仍返回一个合法的空 `boats` 图层，而不是零字节响应，以避免 `@maptalks/vt` 将空响应当成网络错误。

## 8. 与 message.proto 的字段关系

MVT 本身就是 Protobuf 编码，但它遵循 Mapbox Vector Tile schema，不能使用 `BoatPointList.decode()` 解码。前端需要用 `@maptalks/vt` 的 `VectorTileLayer` 加载上述 URL，或保留现有 WebSocket 图层并将 MVT 作为另一图层。

如需点击船位读取属性，`VectorTileLayer` 必须同时设置 `features: true` 和 `picking: true`。`picking` 只负责命中渲染对象；未开启 `features` 时，拾取结果通常只有 `point`、`coordinate`、`plugin`、`type`，不会携带船舶属性。当前 `html/index.html` 已按 `picked.data.feature.properties` 读取属性，并兼容其他版本的返回结构。

单船瓦片中已经提供以下同名属性：

| MVT 属性 | 来源 |
|---|---|
| `terminalType` | 定位表 |
| `terminalPhone` | 定位表主键 |
| `sourceName` | `boat_location_new.source_name` |
| `boatName` | 定位表优先，其次双控船 `boat_name` 或乡镇船 `plan_boat_code` |
| `longitude`, `latitude` | 定位表 |
| `speed`, `direction`, `locationTime` | 定位表 |
| `boatManageType` | 定位表 |
| `filterValue` | 按原前端规则归类为捕捞大/中/小船、养殖船或乡镇船舶，供切片样式与筛选复用 |
| `boatType` | 双控船 `boat_type` 或乡镇船 `boat_usage` |
| `totalPower` | 双控船 `total_power` 或乡镇船 `boat_total_power` |
| `boatLength` | 双控船 `boat_length` 或乡镇船 `boat_length/boat_total_length` |
| `areaCode` | 对应双控船或乡镇船档案的 `area_code` |
| `hasArchiveMes` | 是否关联到任一未删除的船舶档案 |
| `boatCode` | 双控船 `boat_code` 或乡镇船 `plan_boat_code` |
| `lineType` | 当前北京时间前 2 小时以内为 `在线`；超过 2 小时或时间为空为 `离线` |

两张档案表都没有 `terminal_phone`，且各自的 `id` 可能重复，因此不能只用 `source_name=id` 跨表关联。当前规则先根据 `boat_location_new.boat_manage_type` 优先选择双控船或乡镇船档案，再匹配船名/船牌号；只有船型明确时才把数字形式的 `source_name` 作为对应表主键的后备匹配。双控船使用 `boat_name = dual_boat_base_mes.boat_name`，乡镇船使用 `boat_name = town_boat_base_mes.plan_boat_code`。上线前必须用实际数据核对船名、船号和 `source_name` 语义。

## 9. 离线时长过滤

瓦片 SQL 的规则是：

```sql
location_time IS NOT NULL
AND location_time >= 当前北京时间 - interval '24 hours'
AND (
    offlineHours IS NULL
    OR location_time >= 当前北京时间 - offlineHours * interval '1 hour'
)
```

24小时是不可突破的服务端硬上限。`location_time IS NULL` 或超过24小时的记录在候选数据阶段即被排除，不参与档案关联、在线状态计算、热点网格计数、抽稀和前端展示。选择1、2、3等小时数时会在24小时基础上进一步缩短范围；页面中的“全部”以及参数省略或传 `0` 均表示“最近24小时内全部”。该过滤在PostGIS查询阶段执行，不只是前端隐藏。

`offlineHours` 是“是否返回该船”的附加查询过滤条件，`lineType` 是固定2小时阈值计算出的在线状态，两者互不替代。例如传 `offlineHours=3` 时，2～3小时内报位的船仍会返回，但其 `lineType` 为“离线”；2～24小时的离线船仅在所选范围允许时显示为灰色，超过24小时的船始终不返回。

`@maptalks/vt 0.118.1` 的 `setURLModifier()` 不会修改 MVT 瓦片的 `urlTemplate`。切换下拉选项时，应使用函数形式的 `urlTemplate` 直接生成带查询参数的瓦片地址，并强制刷新：

```js
let hours = selectedHours || 0
let filterVersion = 1

const boatVectorTileLayer = new maptalks.VectorTileLayer('boats-mvt', {
  urlTemplate: (x, y, z) =>
    `/vessel-tiles/tiles/boats/${z}/${x}/${y}.pbf`
    + `?offlineHours=${encodeURIComponent(hours)}`
    + `&filterVersion=${filterVersion}`
})

hours = newSelectedHours || 0
filterVersion += 1
boatVectorTileLayer.forceReload()
```

切换时间后必须重新请求瓦片，不能只隐藏已经加载到浏览器中的旧要素。服务端先执行24小时硬过滤和用户选择的时间过滤，再执行热点抽稀，因此过期船不会占用热点网格的显示名额。

## 10. 根据 getInfo 判定本地船并渲染颜色

### 独立验证页

HTML 在配置 `window.VESSEL_GET_INFO_URL` 后优先调用真实 getInfo 接口，递归读取响应中 `areaCode/areaCodes` 的编码数组或区域树。船舶 `areaCode` 与该集合精确匹配时判定为本地船；编码为空或不在集合内时判定为非本地船。

颜色判断按下列顺序执行，前一项优先级更高：

| 条件 | 分类 | 颜色 |
|---|---|---|
| `lineType = 离线` | 离线渔船 | 灰色 `#9baab5` |
| 在线且 `areaCode` 不在 getInfo 集合内 | 非本地渔船 | 绿色 `#22c55e` |
| 在线、本地且 `boatManageType` 包含“双控” | 双控船 | 红色 `#ef4444` |
| 在线、本地且 `boatManageType` 包含“乡镇” | 乡镇船 | 黄色 `#facc15` |
| 在线、本地但档案类型无法识别 | 其他本地船 | 蓝色 `#20d9ff` |

`offlineHours` 仍然是服务端附加过滤条件：传值后，超过指定报位时长的船不会进入 MVT，前端也就不会渲染；省略或传0时也只返回最近24小时。返回到前端的船再按上述规则着色。`lineType` 的在线/离线阈值固定为2小时，与 `offlineHours` 的显隐过滤相互独立。

### beihai-front 正式页面

正式页面不读取本目录的 `getInfo.txt`，也不重复请求 `getInfo`。路由初始化已有的 `getInfo` 结果由用户仓库提取 `areaCode/areaCodes`，地图初始化时通过 `BoatUtil.setLocalAreaCodes()` 注入；终端显示控制中的管内地区树也按该集合裁剪。数据源范围直接由图层配置控制：MVT为1～13级，其中1～10级热点保留70%、11～13级返回全量点位且只画圆点；WebSocket `PointLayer` 为13～20级且只画船型图标。两个范围在13级重叠，因此 `seamlessZoom` 的12.x由MVT覆盖、13.x由WebSocket覆盖，不会出现空档。

正式页面的切片地址在 `.env.development` 与 `.env.production` 中配置：

```dotenv
VITE_VESSEL_TILE_URL = 'http://192.168.2.60:6183/vessel-tiles/tiles/boats/{z}/{x}/{y}.pbf'
```

修改地址后需要重新构建前端。报位时间下拉会把 `offlineHours` 拼入每个切片请求并调用 `forceReload()`；WebSocket 点位同步在浏览器端应用相同时长，时间为空或超过24小时的数据不显示。在线状态始终按2小时阈值重新计算，不依赖推送消息中的在线/离线分组。

为减轻密集区域遮挡，1～13级MVT圆点随层级从5像素逐步增加到12像素；13～20级WebSocket图标从24像素开始并随层级逐步放大。船名仅从16级开始抽样显示：16级最多100个、17级最多180个、18级以上最多300个，并使用32像素屏幕网格碰撞检测；名称过长时显示前10个字符和省略号，完整信息仍可通过点击船位查看。标签会在船标上、左、右、下四个候选位置中自动避让，并使用真实坐标连线，不再启用Maptalks几何拖动，避免标签刷新过程中出现拖动临时层被销毁的问题。尾迹请求使用500毫秒尾缘防抖，同一时刻最多保留一个请求，相同参数2秒内不重复提交；返回的非法经纬度会在创建尾迹前过滤。WebSocket格式化结果必须保留 `locationTime`，否则24小时显隐判断会把全部实时船位当作过期数据隐藏。

## 11. 上线前关联核验

首次同步后至少执行：

```sql
SELECT
    count(DISTINCT l.terminal_phone) AS location_count,
    count(DISTINCT l.terminal_phone) FILTER (
        WHERE l.boat_manage_type LIKE '%双控%' AND d.source_id IS NOT NULL
    ) AS matched_dual_count,
    count(DISTINCT l.terminal_phone) FILTER (
        WHERE l.boat_manage_type LIKE '%乡镇%' AND t.source_id IS NOT NULL
    ) AS matched_town_count,
    count(DISTINCT l.terminal_phone) FILTER (
        WHERE d.source_id IS NULL AND t.source_id IS NULL
    ) AS unmatched_count
FROM vessel.boat_location_latest AS l
LEFT JOIN LATERAL (
    SELECT source_id
    FROM vessel.dual_boat_archive
    WHERE del_flag = false
      AND boat_name = l.boat_name
    LIMIT 1
) AS d ON true
LEFT JOIN LATERAL (
    SELECT source_id
    FROM vessel.town_boat_archive
    WHERE del_flag = false
      AND plan_boat_code = l.boat_name
    LIMIT 1
) AS t ON true;
```

按双控船、乡镇船各抽查至少 20 条匹配结果，并单独检查同一船名同时命中两张表的记录。如果 `source_name` 实际不是船舶档案主键，应删除后备 ID 匹配，只保留经业务确认的稳定关联字段。

## 12. 运维检查

```bash
journalctl -u vessel-sync.service --since today
journalctl -u vessel-reconcile.service --since today
journalctl -u vessel-tiles.service --since today
sudo -u postgres psql -d vessel_gis -c "select vessel.tile_health();"
sudo -u postgres psql -d vessel_gis -c \
  "select stream_name, watermark, last_succeeded_at, rows_seen, rows_applied, last_error from vessel.sync_state;"
```

重点监控：同步最后成功时间、最新定位时间、无效坐标数量、单次同步耗时、MySQL 慢查询、PostgreSQL 连接数、热点瓦片大小与响应时间。
