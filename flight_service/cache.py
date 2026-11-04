import os
import logging
from google.protobuf.message import DecodeError
from redis.exceptions import RedisError
from redis.sentinel import Sentinel

log = logging.getLogger(__name__)


def redis_from_env():
    addresses = [item.rsplit(":", 1) for item in os.environ["REDIS_SENTINELS"].split(",")]
    return Sentinel([(host, int(port)) for host, port in addresses],
        socket_timeout=0.3, socket_connect_timeout=0.3).master_for(
            os.getenv("REDIS_MASTER_NAME", "flight-cache"), socket_timeout=0.3)


class FlightCache:
    def __init__(self, redis, ttl):
        if not 300 <= ttl <= 600:
            raise ValueError("CACHE_TTL_SECONDS must be between 300 and 600")
        self.redis, self.ttl = redis, ttl

    def get(self, key, revision, message_type):
        key = f"{key}:v{revision}"
        try:
            value = self.redis.get(key)
            if value is not None:
                message = message_type.FromString(value)
                log.info("cache_hit key=%s", key)
                return message
        except (RedisError, DecodeError):
            log.warning("cache_unavailable_or_invalid key=%s", key)
        log.info("cache_miss key=%s", key)
        return None

    def put(self, key, revision, message):
        try:
            self.redis.set(f"{key}:v{revision}", message.SerializeToString(), ex=self.ttl)
        except RedisError:
            log.warning("cache_write_failed key=%s", key)

