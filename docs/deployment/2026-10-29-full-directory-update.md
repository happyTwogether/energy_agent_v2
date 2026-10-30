# 2026-10-29 v4：受限数据库账号与完整目录替换

## 本次日志说明

应用已连接数据库；在 `jd_agent` 中创建新表 `agent_session_states` 被拒绝，随后结构检查发现新表不存在，启动失败。日志没有执行到 INSERT，不能据此断定应用账号不能写入。

原有表有写权限，不代表另一个账号新建的表自动给应用账号相同权限。应用需要 schema USAGE，以及两张会话表的 SELECT、INSERT、UPDATE、DELETE；不需要 schema CREATE。本版 `DB_AUTO_CREATE_TABLES=false` 为默认值，启动只检查必要表，不再尝试建表。

管理员使用实际 `.env` 的 DB_NAME 指定数据库：

1. 执行 `db/agent_session_state.sql`，仅创建新状态表及索引，保留原有 `agent_conversations`。
2. 将 `db/agent_session_grants.sql` 的 `YOUR_APP_DB_USER` 改成实际 DB_USER 后执行。这里填写账号，不填写密码。已具备的权限可以重复授予；不扩大到整个数据库或业务表。

本机未连接生产 PostgreSQL，管理员执行 SQL 和真实写入仍需在内网验收。[PostgreSQL 权限说明](https://www.postgresql.org/docs/current/ddl-priv.html)。

## 已确认的远端部署方式

用户提供的 `docker inspect` 输出确认：

- 宿主机 `/home/sunlu/energy_agent/docker_run/energy_agent_v1` 绑定到容器 `/app`。
- 工作目录 `/app`，启动 `uvicorn main:app --host 0.0.0.0 --port 8000`，没有 `--reload`。
- 本次输出只有项目目录这一条挂载，未显示单独的日志目录挂载。

这是复用依赖镜像、挂载外部源码、重启加载代码的方式。文件替换会立即反映在 `/app`，已导入的 Python 模块需要进程重启才能稳定加载新版本。本次不新增依赖，无需重建镜像。不要在自动 reload 运行时先删除完整目录再上传。

本仓库原 Compose 的 `energy_angent/docker_run` 路径与实际部署不一致，已改为 `.:/app`，显式指定工作目录和启动命令。必须把 Compose 放在上述 `energy_agent_v1` 项目根目录使用。镜像名称和标签大小写、端口及外部网络仍应与现场配置一致；首次本次更新可以直接 stop/start 已有容器，不依赖重新创建容器。原有 load-test profile 不属于本次交付验收，请勿启用。

业务配置统一保留在项目根目录 `.env`，由应用读取 `/app/.env`；Compose 只保留 TZ，没有重复注入数据库或模型参数。进程已有同名环境变量会覆盖 `.env`，现场若另有注入应先消除冲突。本方案修改 `.env` 后重启即可；若现场使用 Compose 的 `environment` / `env_file` 注入配置，则变更注入值需重新创建容器，仅 restart 不会更新注入值。[Docker restart 说明](https://docs.docker.com/reference/cli/docker/compose/restart/)。

## 压缩包内容

- `app完整.zip`：完整 `app/` 源码，包含全部业务工具和新增模块，可以替换整个 app；不带缓存。
- `scripts完整.zip`：完整可部署脚本 `scripts/`；排除本地调试文本和缓存。
- `根目录文件.zip`：main.py、chat.html、修正后的 docker-compose.yml、.env.example、patch-manifest.json。不会包含或覆盖实际 `.env`。
- `数据库脚本.zip`：项目数据库脚本，排除本地数据导出；本次只需执行两份 agent_session SQL，不需要执行历史性能索引脚本。

每个 ZIP 内含正确目录层级，例如 `app/agent/...`。解压目标是项目根目录，不是已经存在的 app 子目录。总包只包含上述分包、说明和 SHA256SUMS.txt，需先解出分包。

生产 `.env`、logs、static/exports、config 的业务目录及其他现场文件保留。本次 config 和 static 没有改动，不需要整目录删除或替换。不要删除整个项目根目录、数据库 schema 或原有问答表。此前 v1/v2/v3 的增量补丁不再用于此次替换。

## 操作步骤

先完成管理员建表与授权。将分包上传到独立的临时目录，例如项目根目录的 `update-v4/`，不要上传到 app 里面。下面在远端宿主机执行，容器名称按当前 `energy`：

```sh
cd /home/sunlu/energy_agent/docker_run/energy_agent_v1
docker stop energy
backup_dir="../energy_agent_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$backup_dir"
mv app "$backup_dir/"
if [ -d scripts ]; then mv scripts "$backup_dir/"; fi
cp -p main.py chat.html docker-compose.yml "$backup_dir/"
if [ -f patch-manifest.json ]; then cp -p patch-manifest.json "$backup_dir/"; fi

unzip -o update-v4/app完整.zip -d .
unzip -o update-v4/scripts完整.zip -d .
unzip -o update-v4/根目录文件.zip -d .
unzip -o update-v4/数据库脚本.zip -d .
```

如旧根目录某个文件不存在，跳过对应备份；如果现场脚本有额外定制，保留备份后单独比较，别把旧 app 又覆盖回新 app。检查根目录 `.env` 保持原有配置，并设 `DB_AUTO_CREATE_TABLES=false`；该项不写也默认 false。不要用 `.env.example` 替换实际 `.env`。

先使用当前容器的真实镜像执行离线校验；此时服务容器保持停止，不会导入上传了一半的业务代码：

```sh
energy_image="$(docker inspect energy --format '{{.Config.Image}}')"
docker run --rm --volumes-from energy --workdir /app --entrypoint python "$energy_image" scripts/verify_production_patch.py --root /app
```

这条检查不访问模型或生产数据库，不需要复用原服务网络。所有检查 PASS 后才启动。若失败，保持停止并修正文件或恢复本次备份。不要绕过文件校验继续启动。

```sh
docker start energy
docker logs --tail 100 energy
docker exec energy python scripts/verify_production_patch.py --root /app --check-database-schema --check-database-permissions
```

必须出现 Application startup complete。ACL 检查输出两表是否存在、USAGE 和 SELECT/INSERT/UPDATE/DELETE 是否具备；不输出密码、不读取消息、不创建表、不试写。ACL 全部通过不代表已经验证行级策略、触发器或真实保存。

如果启动仍失败，在停着的镜像单次检查中追加数据库参数前，需要另行匹配原服务网络；不要把默认 bridge 网络连不上数据库误判为账号权限问题。也可以先根据启动日志处理缺表/读取权限，再重试启动。

## 验收与回退

同一用户、同一 conversation_id 连续提问“湘潭市的节电空间”→“株洲市的呢？”→“中等扩展”，第三轮应仍为株洲、中等扩展。确认报告成功生成、消息存储成功，再重启服务继续追问，验证状态恢复；另一用户不能访问该会话。只读自检不替代这一真实流程。

新建状态表保留在数据库即可；回退代码时停止容器，恢复本次备份的 app、scripts、main.py、chat.html 和原清单后启动，不删除任何问答数据。若曾使用新 Compose 重新创建容器，另需恢复现场原 Compose 配置和镜像标签。
