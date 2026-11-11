#!/usr/bin/env bash
# 本地端到端演示（无需 Docker、无需外部邮箱）：
#   1. 启动纯标准库 IMAP 演示源（6 封邮件，含多附件/无 CSV/坏行/重复）
#   2. 启动两个 API 进程（8000 / 8010），共享同一个 SQLite 文件
#   3. 演示：采集在坏行 UID4 前停止 -> 状态查询 -> 两进程并发互斥
#      -> 结果查询 -> 续采 -> 修复坏邮件后续采完成
#
# 用法: bash demo/run_local_demo.sh
# 清理: bash demo/run_local_demo.sh --clean
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${RUN_DIR:-/tmp/lab-demo}"
MAIL_DIR="$RUN_DIR/mailbox"
DB="$RUN_DIR/collect.db"
IMAP_PORT="${IMAP_PORT:-3143}"
PORT_A="${PORT_A:-8000}"
PORT_B="${PORT_B:-8010}"

cleanup() {
  [[ -n "${SERVER_PID:-}" ]] && kill "${SERVER_PID}" 2>/dev/null || true
  [[ -n "${API_A_PID:-}" ]] && kill "${API_A_PID}" 2>/dev/null || true
  [[ -n "${API_B_PID:-}" ]] && kill "${API_B_PID}" 2>/dev/null || true
}
trap cleanup EXIT

if [[ "${1:-}" == "--clean" ]]; then
  cleanup
  rm -rf "$RUN_DIR"
  echo "已清理 $RUN_DIR"
  exit 0
fi

mkdir -p "$RUN_DIR" "$MAIL_DIR"
rm -f "$DB" "$DB"-wal "$DB"-shm

PYTHON="${PYTHON:-python3}"
cd "$ROOT"

echo "==> [1/6] 生成演示邮件到 $MAIL_DIR"
"$PYTHON" -m tools.make_fixtures --out "$MAIL_DIR"

echo "==> [2/6] 启动本地 IMAP 演示源 :$IMAP_PORT"
# IMAPD_DROP_UID 仅在非空时导出，空值会被服务器视为未设置
if [[ -n "${IMAPD_DROP_UID:-}" ]]; then
  export IMAPD_DROP_UID
fi
"$PYTHON" -m tools.imap_server \
  --host 127.0.0.1 --port "$IMAP_PORT" --mail-dir "$MAIL_DIR" \
  --user lab --password labpass >"$RUN_DIR/imap.log" 2>&1 &
SERVER_PID=$!

# 等 IMAP 端口就绪，服务器启动失败则立即中止而不是带着坏状态继续
for _ in $(seq 1 30); do
  if "$PYTHON" - "$IMAP_PORT" <<'PY'
import socket, sys
sys.exit(0 if socket.socket().connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "IMAP 演示源启动失败，日志:" >&2
    cat "$RUN_DIR/imap.log" >&2
    exit 1
  fi
  sleep 0.3
done

export IMAP_HOST=127.0.0.1 IMAP_PORT="$IMAP_PORT" IMAP_USERNAME=lab \
       IMAP_PASSWORD=labpass IMAP_MAILBOX=INBOX IMAP_USE_SSL=false \
       COLLECT_DB_PATH="$DB"

echo "==> [3/6] 启动两个 API 进程 :$PORT_A 与 :$PORT_B（共享 $DB）"
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT_A" \
  >"$RUN_DIR/api-a.log" 2>&1 &
API_A_PID=$!
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT_B" \
  >"$RUN_DIR/api-b.log" 2>&1 &
API_B_PID=$!

for port in "$PORT_A" "$PORT_B"; do
  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$port/health" >/dev/null && break
    sleep 0.3
  done
done

echo
echo "==> [4/6] 首次采集（预期停在坏行 UID4 之前）"
curl -s -X POST "http://127.0.0.1:$PORT_A/api/v1/collect/imap" | "$PYTHON" -m json.tool

echo "==> 采集进度"
curl -s "http://127.0.0.1:$PORT_A/api/v1/collect/status" | "$PYTHON" -m json.tool

echo
echo "==> [5/6] 两个进程同时采集：其中一个必须收到 409 锁冲突"
curl -s -o "$RUN_DIR/r1.json" -w "进程A HTTP %{http_code}\n" \
  -X POST "http://127.0.0.1:$PORT_A/api/v1/collect/imap" &
CURL1=$!
curl -s -o "$RUN_DIR/r2.json" -w "进程B HTTP %{http_code}\n" \
  -X POST "http://127.0.0.1:$PORT_B/api/v1/collect/imap" &
CURL2=$!
# 只等待这两个请求，不能用无参数 wait（否则会一直等待常驻的 IMAP/API 进程）
wait "$CURL1" "$CURL2"
echo "进程A 响应:"; "$PYTHON" -m json.tool <"$RUN_DIR/r1.json" | head -8
echo "进程B 响应:"; "$PYTHON" -m json.tool <"$RUN_DIR/r2.json" | head -8

echo
echo "==> 按结果 ID 查询（返回可直接用于核对的 result_rows）"
curl -s -X POST "http://127.0.0.1:$PORT_A/api/v1/results/query" \
  -H "Content-Type: application/json" \
  --data @"$ROOT/demo/result_query.json" | "$PYTHON" -m json.tool

echo
echo "==> [6/6] 修复坏邮件后续采（替换 UID4 为合法 CSV）"
"$PYTHON" -m tools.fix_fixture_message "$MAIL_DIR/04_bad_row.eml"
# 演示源支持目录热重载，下一次连接会自动重新加载，无需重启 IMAP 进程。

echo "-- 续采（UID4/5/6 完成，UID6 同内容去重）"
curl -s -X POST "http://127.0.0.1:$PORT_A/api/v1/collect/imap" | "$PYTHON" -m json.tool
echo "-- 最终进度"
curl -s "http://127.0.0.1:$PORT_A/api/v1/collect/status" | "$PYTHON" -m json.tool

echo
echo "演示完成。日志目录: $RUN_DIR"
echo "Swagger 文档: http://127.0.0.1:$PORT_A/docs"
