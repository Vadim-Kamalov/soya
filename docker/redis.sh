#!/bin/sh
set -eu
# Writable configs persist Sentinel's elected topology across restarts.
if [ ! -f /data/redis.conf ]; then
  {
    echo 'bind 0.0.0.0'
    echo 'protected-mode no'
    echo 'appendonly yes'
    echo 'dir /data'
    if [ "${REDIS_ROLE}" = replica ]; then
      echo "replicaof ${REDIS_MASTER_IP} 6379"
    fi
  } > /data/redis.conf
fi
exec redis-server /data/redis.conf

