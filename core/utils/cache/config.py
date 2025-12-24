"""
缓存配置管理
"""

from enum import Enum
from typing import Optional
from dataclasses import dataclass
from .strategies import CacheStrategy


class CacheType(Enum):
    """缓存类型枚举"""
    CONFIG = "config"


@dataclass
class CacheConfig:
    """缓存配置类"""

    strategy: CacheStrategy = CacheStrategy.TTL
    ttl: Optional[float] = 300  # 默认5分钟
    max_size: Optional[int] = 1000  # 默认最大1000条
    cleanup_interval: float = 60  # 清理间隔（秒）

    @classmethod
    def for_type(cls, cache_type: CacheType) -> "CacheConfig":
        """根据缓存类型返回预设配置"""
        configs = {
            CacheType.CONFIG: cls(
                strategy=CacheStrategy.FIXED_SIZE, ttl=None, max_size=20  # 手动失效
            ),
        }
        return configs.get(cache_type, cls())
