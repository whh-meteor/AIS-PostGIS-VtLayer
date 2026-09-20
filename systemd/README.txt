北海渔船同步与矢量切片 systemd 配置说明
==========================================

一、文件关系
------------

vessel-sync.timer
  每 2 分钟触发一次 vessel-sync.service。
  MySQL 约每 5 分钟产生一次新船位，因此部分触发可能没有新数据，这是正常现象。

vessel-sync.service
  执行普通增量同步，运行程序：
  /data/ltgk/beihai/sync_boats.py
  服务类型为 Type=oneshot，执行成功后显示 inactive (dead) 属正常结果。
  是否成功应以日志中的 sync complete、Finished 和退出码 0 为准。

vessel-reconcile.timer
  每天北京时间 03:15 触发 vessel-reconcile.service。
  RandomizedDelaySec=10min 表示最多随机延迟 10 分钟，避免多个定时任务同时启动。
  Persistent=true 表示服务器错过执行时间后，会在下次开机时补执行。

vessel-reconcile.service
  使用 sync_boats.py --full 执行每日全量校准。
  作用是补偿源表时间字段未变化而可能被增量同步漏掉的修订或软删除。
  本服务没有添加 --prune，不会删除 MySQL 中已经不存在的 PostGIS 记录。

vessel-tiles.service
  常驻运行 FastAPI/Uvicorn 矢量切片服务，内部监听：
  http://127.0.0.1:7800
  7800 只供本机 Nginx 反向代理使用，不直接开放给外部客户端。
  外部统一访问：
  http://服务器IP:6183/vessel-tiles/...
  服务异常退出时由 Restart=on-failure 在 3 秒后自动重启。

二、公共配置项
--------------

User=ubuntu / Group=ubuntu
  所有 Python 服务均使用 ubuntu 用户运行，不使用 root。

WorkingDirectory=/data/ltgk/beihai
  Python 主程序、requirements.txt、sql、nginx、html、systemd 等文件的部署根目录。

EnvironmentFile=/data/ltgk/beihai/config/vessel-tiles.env
  保存 MySQL、PostgreSQL、同步批量大小和缓存时间等运行参数。
  该文件含数据库密码，建议权限设置为 600，不要提交到代码仓库。

ExecStartPre
  正式启动前检查 Conda py312 解释器及对应 Python 主程序是否存在且可读。

PYTHONUNBUFFERED=1
  关闭 Python 标准输出缓冲，使 journalctl 能及时看到日志。

PYTHONDONTWRITEBYTECODE=1
  禁止在程序目录生成 __pycache__ 和 .pyc 文件。

NoNewPrivileges=true
  阻止服务进程通过 setuid 等方式获得额外权限。

PrivateTmp=true
  为服务提供隔离的 /tmp 和 /var/tmp；不影响 /data/ltgk/beihai/tmp。

ProtectSystem=strict
  将大部分系统目录设为只读。程序正常处理数据依赖数据库，不需要写系统目录。

ProtectHome=read-only
  将用户主目录设为只读，同时仍可执行 Conda 环境中的 Python。

三、服务之间的数据流
--------------------

MySQL
  -> vessel-sync.service / vessel-reconcile.service
  -> PostgreSQL/PostGIS 的 vessel schema
  -> vessel-tiles.service（127.0.0.1:7800）
  -> Nginx（服务器IP:6183）
  -> 浏览器或前端 Maptalks 图层

四、常用检查命令
----------------

查看定时器：
  systemctl list-timers vessel-sync.timer vessel-reconcile.timer

查看增量同步状态：
  systemctl status vessel-sync.service --no-pager
  journalctl -u vessel-sync.service -n 100 -l --no-pager

查看全量校准日志：
  journalctl -u vessel-reconcile.service -n 100 -l --no-pager

查看瓦片服务：
  systemctl status vessel-tiles.service --no-pager
  journalctl -u vessel-tiles.service -n 100 -l --no-pager
  curl --fail http://127.0.0.1:7800/health

修改任何 .service 或 .timer 文件后必须执行：
  sudo systemctl daemon-reload

修改环境文件后，应重启对应服务使新配置生效；timer 本身通常无需重启。
