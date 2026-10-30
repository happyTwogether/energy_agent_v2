#!/usr/bin/env bash
# 将本文件与发布分包放在同一目录，然后 bash 部署更新.sh。
# 只替换源码；保留原容器、镜像、网络、.env、数据库和导出文件。
set -Eeuo pipefail

release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
container=energy
exec > >(tee "$release_dir/部署结果.txt") 2>&1
trap 'printf "部署未完成，停止位置：第%s行。请保留本目录的部署结果.txt。\n" "$LINENO"' ERR

for command_name in docker unzip sha256sum mktemp; do
    command -v "$command_name" >/dev/null || { printf '缺少部署工具：%s\n' "$command_name"; exit 1; }
done
(cd "$release_dir" && sha256sum -c SHA256SUMS.txt)

project_root="$(docker inspect "$container" --format '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}')"
mount_type="$(docker inspect "$container" --format '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Type}}{{end}}{{end}}')"
image_id="$(docker inspect "$container" --format '{{.Image}}')"
case "$project_root" in
    /) printf '拒绝以根目录作为项目目录\n'; exit 1 ;;
    /*) ;;
    *) printf '未找到原容器 /app 的有效宿主机挂载\n'; exit 1 ;;
esac
if [ "$mount_type" != bind ] || [ ! -d "$project_root/app" ] || [ ! -f "$project_root/main.py" ]; then
    printf '原容器挂载或项目结构不符合预期，尚未修改生产文件\n'
    exit 1
fi

stage_dir="$(mktemp -d "$project_root/.energy-stage.XXXXXX")"
for archive in app完整.zip scripts完整.zip 根目录文件.zip; do
    unzip -q -o "$release_dir/$archive" -d "$stage_dir"
done
for required in app scripts main.py chat.html patch-manifest.json; do
    test -e "$stage_dir/$required"
done

printf '在隔离网络中校验待部署代码，原服务尚未停止…\n'
docker run --rm --network none --volumes-from "$container" \
    -v "$stage_dir/app:/app/app:ro" -v "$stage_dir/scripts:/app/scripts:ro" \
    -v "$stage_dir/main.py:/app/main.py:ro" -v "$stage_dir/chat.html:/app/chat.html:ro" \
    -v "$stage_dir/patch-manifest.json:/app/patch-manifest.json:ro" \
    --workdir /app --entrypoint python "$image_id" scripts/verify_production_patch.py --root /app

backup_dir="$(mktemp -d "$project_root/../energy-before-update.XXXXXX")"
printf '校验通过，开始替换源码；备份目录：%s\n' "$backup_dir"
docker stop "$container"
for item in app scripts main.py chat.html patch-manifest.json; do
    if [ -e "$project_root/$item" ]; then mv "$project_root/$item" "$backup_dir/"; fi
    mv "$stage_dir/$item" "$project_root/$item"
done
rmdir "$stage_dir"
docker start "$container"

printf '新版代码已替换，等待数据库就绪（最多60秒）…\n'
if docker exec -i -w /app "$container" python - <<'PY'
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from app.core.config import get_settings

settings = get_settings()
print('数据库目标=%s:%s；DB_HOST来源=%s；DB_PORT来源=%s' % (
    settings.db_host, settings.db_port,
    'process_env' if 'DB_HOST' in os.environ else '.env/default',
    'process_env' if 'DB_PORT' in os.environ else '.env/default',
), flush=True)
deadline = time.monotonic() + 60
last_state = None
while time.monotonic() < deadline:
    try:
        try:
            response = urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)
        except HTTPError as error:
            response = error
        with response:
            payload = json.load(response)
            if response.code == 200 and payload.get('status') == 'ok':
                print('PASS 新版 HTTP 与数据库会话表检查已就绪；请在页面验证实际业务查询。', flush=True)
                sys.exit(0)
            state = payload.get('database', {})
            summary = (state.get('code'), state.get('message'))
            if summary != last_state:
                print('等待恢复：%s %s' % summary, flush=True)
                last_state = summary
    except (URLError, TimeoutError, OSError, ValueError):
        if last_state != 'http_starting':
            print('等待 HTTP 服务启动…', flush=True)
            last_state = 'http_starting'
    time.sleep(2)
print('NOT_READY 就绪检查未通过；进程将继续自动重连，请从页面查看状态。', flush=True)
sys.exit(3)
PY
then
    printf '部署流程完成。结果已保存：%s/部署结果.txt\n' "$release_dir"
else
    result=$?
    printf '代码已部署，就绪尚未通过；未声明业务恢复。结果已保存：%s/部署结果.txt\n' "$release_dir"
    exit "$result"
fi
