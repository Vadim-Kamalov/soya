#!/bin/sh
set -eu
if [ ! -f /data/sentinel.conf ]; then
  cat > /data/sentinel.conf <<EOF
port 26379
bind 0.0.0.0
protected-mode no
dir /data
sentinel monitor flight-cache ${REDIS_MASTER_IP} 6379 2
sentinel down-after-milliseconds flight-cache 3000
sentinel failover-timeout flight-cache 10000
sentinel parallel-syncs flight-cache 1
EOF
fi
exec redis-server /data/sentinel.conf --sentinel

