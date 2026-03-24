#!/usr/bin/env bash
# NOTE: This script is written for macOS (Apple Silicon / Homebrew).
# It hardcodes Homebrew paths (/opt/homebrew). Linux users should use
# docker-compose.yml for Redis and adjust paths accordingly.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[LP-Sonar]${NC} $1"; }
warn() { echo -e "${YELLOW}[LP-Sonar]${NC} $1"; }
fail() { echo -e "${RED}[LP-Sonar]${NC} $1"; exit 1; }

# ── 前置检查 ──────────────────────────────────────────────
REDIS_SERVER=/opt/homebrew/bin/redis-server
REDIS_CLI=/opt/homebrew/bin/redis-cli
command -v node >/dev/null 2>&1 || fail "未找到 node，请先安装"
[ -x "$REDIS_SERVER" ] || fail "未找到 redis-server，请先执行: brew install redis"

[ -f "$PROJECT_DIR/backend/.env" ] || fail "缺少 backend/.env，请先复制 .env.example 并填写 OKX_ACCESS_KEY"

# ── 1. Redis ──────────────────────────────────────────────
log "启动 Redis..."
if $REDIS_CLI ping 2>/dev/null | grep -q PONG; then
  warn "Redis 已在运行，跳过启动"
else
  nohup $REDIS_SERVER /opt/homebrew/etc/redis.conf \
    > "$LOG_DIR/redis.log" 2>&1 &
  echo $! > "$LOG_DIR/redis.pid"

  for i in $(seq 1 20); do
    $REDIS_CLI ping 2>/dev/null | grep -q PONG && break
    [ $i -eq 20 ] && fail "Redis 启动超时，查看日志: logs/redis.log"
    sleep 1
  done
fi
log "Redis 就绪 ✓"

# ── 2. Backend ────────────────────────────────────────────
log "启动 Backend (port 8000)..."
# 清理旧进程
old_pid=$(lsof -ti tcp:8000 2>/dev/null || true)
[ -n "$old_pid" ] && kill $old_pid 2>/dev/null && sleep 1

cd "$PROJECT_DIR/backend"
[ -d ".venv" ] || fail "缺少 .venv，请先在 backend/ 下执行: uv sync 或 pip install -e ."

nohup .venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 \
  --log-level info \
  > "$LOG_DIR/backend.log" 2>&1 &
echo $! > "$LOG_DIR/backend.pid"

# 等待 Backend 健康
for i in $(seq 1 30); do
  curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
  [ $i -eq 30 ] && fail "Backend 启动超时，查看日志: logs/backend.log"
  sleep 1
done
log "Backend 就绪 ✓  (PID $(cat "$LOG_DIR/backend.pid"))"

# ── 3. Frontend ───────────────────────────────────────────
log "启动 Frontend (port 3000)..."
# 清理旧进程和 lock 文件
old_pid=$(lsof -ti tcp:3000 2>/dev/null || true)
[ -n "$old_pid" ] && kill $old_pid 2>/dev/null && sleep 1
rm -f "$PROJECT_DIR/frontend/.next/dev/lock"

cd "$PROJECT_DIR/frontend"
[ -d "node_modules" ] || fail "缺少 node_modules，请先在 frontend/ 下执行: npm install"

nohup npm run dev \
  > "$LOG_DIR/frontend.log" 2>&1 &
echo $! > "$LOG_DIR/frontend.pid"

# 等待 Frontend 就绪
for i in $(seq 1 60); do
  curl -sf http://localhost:3000 >/dev/null 2>&1 && break
  [ $i -eq 60 ] && fail "Frontend 启动超时，查看日志: logs/frontend.log"
  sleep 1
done
log "Frontend 就绪 ✓  (PID $(cat "$LOG_DIR/frontend.pid"))"

# ── 完成 ──────────────────────────────────────────────────
echo ""
log "========================================="
log "  LP-Sonar 启动完成"
log "  Dashboard : http://localhost:3000"
log "  API       : http://localhost:8000"
log "  API Docs  : http://localhost:8000/docs"
log "  日志目录  : logs/"
log "========================================="
