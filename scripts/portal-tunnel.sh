#!/usr/bin/env bash
# portal-tunnel.sh —— 给 File Portal 开一条 Cloudflare Quick Tunnel（免费、零注册、即刻可用）
#
#   ./portal-tunnel.sh start    启动后端(若未跑) + cloudflared，打印公网 https 地址
#   ./portal-tunnel.sh status   各进程状态
#   ./portal-tunnel.sh url      打印最近一次的公网地址
#   ./portal-tunnel.sh stop-tunnel  只停隧道（保留后端，适合本地继续用）
#   ./portal-tunnel.sh stop     停隧道 + 停后端
#
# 后端进程与 cloudflared 的参数都从环境读取，仓库内不含任何账号/密码/域名。
#   后端端口   FILE_PORTAL_PORT  (默认 8000，与 file_portal.py 默认一致)
#   后端目录   FILE_PORTAL_ROOT  (默认 $HOME)
#   鉴权       FILE_PORTAL_AUTH  ("user:pass"，必须设置！隧道会把服务暴露到公网)
#   绑定地址   FILE_PORTAL_HOST  (默认 127.0.0.1；交给隧道的就是这个本机回环)
#
# 背景：Quick Tunnel 是 Cloudflare 边缘转发，无需域名、无需登录、无需配置文件。
# 想固定域名/固定地址 → 官方 Named Tunnel（登录+自有域名），见 README「绑定自有域名」。
# 进程管理只用 pid 文件精确击杀，绝不 pkill -f（会误杀同端口的其它/本机旧隧道）。

PORT="${FILE_PORTAL_PORT:-8000}"
ROOT="${FILE_PORTAL_ROOT:-$HOME}"
AUTH="${FILE_PORTAL_AUTH:-}"
HOST="${FILE_PORTAL_HOST:-127.0.0.1}"
CF="$(command -v cloudflared || true)"
STATEDIR="${XDG_STATE_HOME:-$HOME/.local/state}/file-portal"
LOG="$STATEDIR/tunnel.log"
PIDD="$STATEDIR/pids"
mkdir -p "$STATEDIR" "$PIDD"
SERVER_PID="$PIDD/server.pid"
TUNNEL_PID="$PIDD/tunnel.pid"
URLFILE="$STATEDIR/url"

# 隧道日志里最后出现的 https://xxx.trycloudflare.com
url_of() { grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1; }

# 仅当 pid 文件记录与"命令行端口匹配"双重吻合才算活着，防止杀错/误判
alive_pid() { # $1=pid文件 $2=特征串(端口)
  local f="$1" pat="$2" p
  [ -f "$f" ] || return 1
  p="$(cat "$f" 2>/dev/null)" || return 1
  [ -n "$p" ] && [ "$p" -gt 0 ] 2>/dev/null || return 1
  kill -0 "$p" 2>/dev/null || return 1
  # 命令行必须含特征串，避免 pid 复用误判
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -qF "$pat" || return 1
}
kill_pidfile() { # $1=pid文件；仅精确 kill pid 文件内进程
  local f="$1" p
  [ -f "$f" ] || return 0
  p="$(cat "$f" 2>/dev/null)" || return 0
  if [ -n "$p" ] && [ "$p" -gt 0 ] 2>/dev/null; then
    kill "$p" 2>/dev/null
    for _ in $(seq 1 10); do kill -0 "$p" 2>/dev/null || break; sleep 0.3; done
    kill -9 "$p" 2>/dev/null
  fi
  echo 0 > "$f"
}

spawn_server() {
  # 后端若已在目标端口监听则直接复用，不重复起
  if ! (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null; then
    setsid -f nohup env FILE_PORTAL_PORT="$PORT" FILE_PORTAL_ROOT="$ROOT" \
      FILE_PORTAL_AUTH="$AUTH" python3 "$(dirname "$0")/../file_portal.py" \
      >> "$STATEDIR/server.log" 2>&1 &
    echo $! > "$SERVER_PID"
  else
    exec 3>&- 3<&-
    echo "$(date +%H:%M:%S) port $PORT 已有服务监听，复用后端" >> "$STATEDIR/server.log"
    echo 0 > "$SERVER_PID"
  fi
}

start() {
  if [ -z "$AUTH" ]; then
    echo "错误: 未设置 FILE_PORTAL_AUTH=\"user:pass\"。Quick Tunnel 会把服务暴露到公网，禁止无鉴权启动。" >&2
    return 1
  fi
  if [ -z "$CF" ]; then
    echo "错误: 未找到 cloudflared。安装: GitHub releases 或 ghproxy 镜像 (cloudflared-linux-amd64)，或包管理器。" >&2
    return 1
  fi
  # 1) 后端
  spawn_server
  # 2) cloudflared quick tunnel（未按本隧道特征活着才起）
  local TU="tunnel --url http://$HOST:$PORT"
  if ! alive_pid "$TUNNEL_PID" "$TU"; then
    : > "$LOG"
    setsid -f nohup "$CF" tunnel --url "http://$HOST:$PORT" --no-autoupdate \
      --edge-ip-version 4 --protocol http2 --logfile "$LOG" >> "$STATEDIR/cf.out" 2>&1 &
    echo $! > "$TUNNEL_PID"
  fi
  # 3) 轮询等地址（国内网络注册偶发超时 → 最多 8 轮 × 20s，每轮重开隧道清日志）
  local U i j
  U=""
  for i in $(seq 1 8); do
    for j in $(seq 1 20); do
      U="$(url_of)"; [ -n "$U" ] && break
      sleep 1
    done
    [ -n "$U" ] && break
    kill_pidfile "$TUNNEL_PID"
    : > "$LOG"; sleep 2
    setsid -f nohup "$CF" tunnel --url "http://$HOST:$PORT" --no-autoupdate \
      --edge-ip-version 4 --protocol http2 --logfile "$LOG" >> "$STATEDIR/cf.out" 2>&1 &
    echo $! > "$TUNNEL_PID"
  done
  if [ -z "$U" ]; then
    echo "WARN 8 轮内未拿到公网地址。稍后重跑: $(basename "$0") url" >&2
    return 1
  fi
  echo "$U" > "$URLFILE"
  echo "OK  File Portal 公网地址: $U"
  echo "    鉴权已开启 (FILE_PORTAL_AUTH)。此地址人人可访问，勿外泄、勿关鉴权。"
}

stop_tunnel() {
  kill_pidfile "$TUNNEL_PID"
  echo "已停止外网隧道，后端本地服务保持运行"
}

stop() {
  stop_tunnel
  kill_pidfile "$SERVER_PID"
  echo "已停止隧道与后端"
}

status() {
  local srv tun
  if alive_pid "$SERVER_PID" "file_portal.py"; then srv="running"; else srv="stopped"; fi
  if alive_pid "$TUNNEL_PID" "tunnel --url http://$HOST:$PORT"; then tun="running"; else tun="stopped"; fi
  echo "server  : $srv"
  echo "tunnel  : $tun"
  echo "url     : $(cat "$URLFILE" 2>/dev/null || echo '-')"
}

url() { cat "$URLFILE" 2>/dev/null || echo "暂无地址，先执行 start"; }

case "$1" in
  start)        start ;;
  stop)         stop ;;
  stop-tunnel)  stop_tunnel ;;
  status)       status ;;
  url)          url ;;
  *) echo "用法: $(basename "$0") {start|stop|stop-tunnel|status|url}"; exit 1 ;;
esac
