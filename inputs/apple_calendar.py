"""macOS Calendar input through Apple's EventKit, with no account credentials."""

from datetime import datetime, time as day_time, timedelta
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from memory.calendar import CalendarMemory


class CalendarReadError(ValueError):
    pass


PERMISSION_HELP = ('请在 Mac 的日历授权弹窗中允许“个人助理日历读取器”访问；'
                   '如已拒绝，请在系统设置 → 隐私与安全性 → 日历中开启。'
                   '系统要求日历完整访问，助理只读取。')


class AppleCalendarReader:
    def __init__(self, data_dir):
        self.directory = Path(data_dir) / 'runtime' / 'apple-calendar'
        self.bundle = self.directory / 'Calendar Reader.app'
        self.executable = self.bundle / 'Contents' / 'MacOS' / 'calendar-reader'

    def setup(self):
        if sys.platform != 'darwin':
            raise CalendarReadError('Apple 日历接入需要 macOS 14 或更新版本')
        self.executable.parent.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).with_suffix('.swift')
        plist = source.with_suffix('.plist')
        (self.bundle / 'Contents' / 'Info.plist').write_bytes(plist.read_bytes())
        temporary = self.executable.with_suffix('.new')
        try:
            result = subprocess.run(['/usr/bin/swiftc', str(source), '-o', str(temporary),
                '-framework', 'EventKit'],
                capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise CalendarReadError('无法构建日历读取器，请检查 Apple Command Line Tools：' + result.stderr[-2000:])
            temporary.replace(self.executable)
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(self.bundle)],
                           capture_output=True, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CalendarReadError('无法构建日历读取器，请检查 Apple Command Line Tools 和代码签名') from exc
        finally:
            temporary.unlink(missing_ok=True)

    def run(self, *args, timeout=30):
        if not self.executable.is_file():
            raise CalendarReadError('Apple 日历读取器尚未安装，请先在 Mac 完成日历接入设置')
        try:
            # LaunchServices gives the helper its own stable macOS permission identity,
            # including when the caller is the background assistant service.
            with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                output = Path(temporary) / 'result.json'
                result = subprocess.run(['/usr/bin/open', '-W', '-n', str(self.bundle.resolve()),
                    '--args', str(output.resolve()), *map(str, args)],
                    capture_output=True, text=True, timeout=timeout)
                value = json.loads(output.read_text())
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise CalendarReadError('Apple 日历读取失败，保留上次日程；请检查本机日历和权限') from exc
        if not isinstance(value, dict):
            raise CalendarReadError('Apple 日历读取结果无效，保留上次日程')
        if args != ('status',) and value.get('access') != 'full_access':
            raise CalendarReadError(PERMISSION_HELP)
        if result.returncode or value.get('error'):
            raise CalendarReadError('Apple 日历读取失败，保留上次日程')
        return value

    def authorize(self):
        if not self.executable.is_file():
            self.setup()
        return self.run('authorize', timeout=180)

    def read(self, start, end):
        return self.run(start, end)


class AppleCalendar:
    """Poll in the existing follow-up loop; failures never stop other reminders."""

    def __init__(self, store, reader=None):
        self.store = store
        self.memory = CalendarMemory(store)
        self.reader = reader or AppleCalendarReader(store.path.parent)

    def configure(self, *, enabled, calendar_ids=None):
        with self.store.worker_lock():
            config = self.memory.status()
            config.update(enabled=enabled, last_attempt_at=None)
            if calendar_ids is not None:
                config['calendar_ids'] = list(dict.fromkeys(calendar_ids))
            self.memory.save_status(config)
        return config

    def sync(self, *, now=None, force=False):
        now = time.time() if now is None else now
        # Caller holds worker_lock, shared by the service and the manual CLI sync.
        config = self.memory.status()
        if not config['enabled'] or (not force and config.get('last_attempt_at') is not None
                and now - config['last_attempt_at'] < 300):
            return config
        config['last_attempt_at'] = now
        try:
            tz = ZoneInfo(self.store.settings()['timezone'])
            today = datetime.fromtimestamp(now, tz).date()
            start = datetime.combine(today - timedelta(days=7), day_time(), tz).timestamp()
            end = datetime.combine(today + timedelta(days=90), day_time(), tz).timestamp()
            snapshot = self.reader.read(start, end)
            result = self.memory.apply(snapshot, start=start, end=end, now=now,
                                       calendar_ids=config.get('calendar_ids', []))
            config.update(last_success_at=now, error=None, calendars=snapshot['calendars'], **result)
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            # Do not include event data or subprocess output in persistent logs.
            config['error'] = str(exc) if isinstance(exc, CalendarReadError) else '日历数据不完整，保留上次日程'
        self.memory.save_status(config)
        return config
