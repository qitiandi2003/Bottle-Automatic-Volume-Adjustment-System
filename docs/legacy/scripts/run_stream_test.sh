#!/usr/bin/env bash
# =============================================================================
# run_stream_test.sh —— debug_stream 的 e2e 測試編排 (x5 側)
# -----------------------------------------------------------------------------
# 後台啟 run_stream_test.py, 睡幾秒等 HTTP server 起來, curl 各 stream 的 /snapshot,
# 最後 tail detector 日誌. 超時 15s 自動 SIGTERM, 觸發 Detector 乾淨停機.
#
# 用法 (在 x5 上, cwd 無所謂):
#   bash docs/scripts/run_stream_test.sh
#
# 環境變量:
#   DEBUG_STREAM_ENABLED  默認 1 (本測試就是要跑推流)
#   TX_ENABLED            默認 0 (避免連不到上位機時循環 warning)
#   STREAM_TEST_DURATION  默認 15 (秒, 整體超時)
#   STREAM_TEST_WAIT      默認 6  (秒, bg 啟動後等多久再 curl)
# =============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
DURATION="${STREAM_TEST_DURATION:-20}"
WAIT_S="${STREAM_TEST_WAIT:-10}"
DS_ENABLED="${DEBUG_STREAM_ENABLED:-1}"
TX_ENA="${TX_ENABLED:-0}"

LOG=/tmp/stream_test.log
rm -f /tmp/snap_*.jpg "$LOG"

cd "$PROJ"

echo "[orchestrator] proj=$PROJ duration=${DURATION}s wait=${WAIT_S}s"

DEBUG_STREAM_ENABLED="$DS_ENABLED" TX_ENABLED="$TX_ENA" \
    timeout --preserve-status "$DURATION" \
    python3 "$HERE/run_stream_test.py" > "$LOG" 2>&1 &
BG_PID=$!

sleep "$WAIT_S"

echo "=== log head (first 20 lines) ==="
head -20 "$LOG"
echo

echo "=== ps snapshot ==="
ps -ef | grep -E "python3 .*run_stream_test|timeout" | grep -v grep
echo

echo "=== ss -tlnp on 8080 ==="
ss -tlnp 2>/dev/null | grep -E "8080|State" | head -5
echo

echo "=== index page body ==="
curl -s http://127.0.0.1:8080/ | head -c 800
echo; echo

echo "=== streams timeline (每 2s 一次, 看 slot 增長) ==="
for i in 1 2 3; do
    sleep 2
    n=$(curl -s http://127.0.0.1:8080/ | grep -oE "streams \([0-9]+ active\)" || echo "?")
    echo "t+$((WAIT_S + i*2))s : $n"
done
echo

for name in tube_viz center_scale liq_viz liq_mask scale_refine_overlay; do
    echo "=== snapshot $name ==="
    curl -sS -o "/tmp/snap_$name.jpg" \
        -w "HTTP %{http_code} bytes=%{size_download} ctype=%{content_type}\n" \
        "http://127.0.0.1:8080/snapshot/$name"
done

echo "=== file types ==="
file /tmp/snap_*.jpg 2>&1

wait "$BG_PID"
echo "=== bg exit status: $? ==="

echo "=== stream_test.log tail ==="
tail -60 "$LOG"
