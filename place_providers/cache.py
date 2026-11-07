"""Cache normalized external results, never credentials or raw responses."""

import hashlib
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
import time


log = logging.getLogger(__name__)


class CachedProvider:
    name = ''

    def __init__(self, cache_dir, *, fetch=None):
        self.cache_dir = Path(cache_dir)
        self.fetch = fetch or self._fetch

    def _query(self, endpoint, params, parse, *, ttl=21600, empty=None):
        key = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / (self.name + '-' + key + '.json')
        try:
            cached = json.loads(path.read_text())
            if time.time() - cached['at'] < (ttl if cached['value'] else 900):
                return cached['value']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            value = parse(self.fetch(endpoint, params))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.info('外部地点数据暂不可用（%s）：%s', self.name, type(exc).__name__)
            value = empty
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_dir,
                                    suffix='.tmp', delete=False) as handle:
                json.dump(dict(at=time.time(), value=value), handle, ensure_ascii=False)
                temporary = Path(handle.name)
            temporary.replace(path)
        except OSError:
            log.info('地点缓存暂不可写入')
        return value
