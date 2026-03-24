#!/usr/bin/env bash

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[LP-Sonar]${NC} $1"; }
warn() { echo -e "${YELLOW}[LP-Sonar]${NC} $1"; }

kill_pid_file() {
  local name="$1"
  local pidfile="$LOG_DIR/$2.pid"
  if [ -f "$pidfile" ]; then
    local pid
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && log "$name 已停止 (PID $pid)"
    else
      warn "$name 进程不存在 (PID $pid)"
    fi
    rm -f "$pidfile"
  else
    warn "$name 未找到 pid 文件，尝试按端口查找..."
    # 兜底：按端口杀
    local port="$3"
    if [ -n "$port" ]; then
      local found_pid
      found_pid=$(lsof -ti tcp:"$port" 2>/dev/null || true)
      if [ -n "$found_pid" ]; then
        kill $found_pid 2>/dev/null && log "$name (port $port) 已停止"
      fi
    fi
  fi
}

# ── 1. Frontend ───────────────────────────────────────────
kill_pid_file "Frontend" "frontend" "3000"

# ── 2. Backend ────────────────────────────────────────────
kill_pid_file "Backend" "backend" "8000"

# ── 3. Redis ──────────────────────────────────────────────
kill_pid_file "Redis" "redis" ""
# 兜底：用 redis-cli shutdown 优雅关闭
if /opt/homebrew/bin/redis-cli ping 2>/dev/null | grep -q PONG; then
  /opt/homebrew/bin/redis-cli shutdown nosave 2>/dev/null || true
  log "Redis 已关闭"
fi

echo ""
log "LP-Sonar 全部服务已关闭"
