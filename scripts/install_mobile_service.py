"""Stage or install a persistent assistant using an existing Cloudflare tunnel."""

import argparse
from datetime import datetime
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.personal-assistant.service'


def add_ingress(text, hostname):
    route = f'  - hostname: {hostname}\n    service: http://127.0.0.1:8775\n'
    existing = re.search(r'^  - hostname: ' + re.escape(hostname) + r'\s*\n(.*?)(?=^  - |\Z)',
                         text, re.MULTILINE | re.DOTALL)
    if existing:
        if not re.search(r'^    service: http://127\.0\.0\.1:8775\s*$', existing[1], re.MULTILINE):
            raise ValueError(f'{hostname} 已被其他服务使用，请选择另一个子域名')
        return text
    if '\ningress:\n' not in text:
        raise ValueError('现有隧道配置中没有 ingress，请先指定正确的 --config')
    return text.replace('\ningress:\n', '\ningress:\n' + route, 1)


def service_plist(hostname):
    runtime = ROOT / 'data/service'
    return dict(Label=LABEL,
                ProgramArguments=[sys.executable, '-u', '-m', 'personal_assistant',
                                  '--wechat', '--public-url', f'https://{hostname}',
                                  'serve', '--port', '8773', '--public-port', '8775'],
                WorkingDirectory=str(ROOT), RunAtLoad=True, KeepAlive=True,
                ThrottleInterval=10, ExitTimeOut=45,
                EnvironmentVariables=dict(
                    PATH=f'{Path.home()}/.local/node/bin:{Path.home()}/.local/bin:'
                         '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                    PYTHONUNBUFFERED='1'),
                StandardOutPath=str(runtime / 'assistant.log'),
                StandardErrorPath=str(runtime / 'assistant.log'))


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def main():
    parser = argparse.ArgumentParser(description='固定手机链接与登录自启；默认只生成待安装文件')
    parser.add_argument('--hostname', required=True, help='已有 Cloudflare 账号管理的子域名')
    parser.add_argument('--config', type=Path, default=Path.home() / '.cloudflared/config.yml')
    parser.add_argument('--tunnel-job', help='现有 cloudflared launchd Label；--apply 时必填')
    parser.add_argument('--cloudflared', type=Path, default=Path.home() / '.local/bin/cloudflared')
    parser.add_argument('--apply', action='store_true', help='配置 DNS、安装助理服务并重启现有隧道')
    args = parser.parse_args()
    if args.apply and not args.tunnel_job:
        parser.error('--apply 需要明确指定现有隧道的 --tunnel-job')
    text = args.config.read_text()
    tunnel = re.search(r'^tunnel:\s*([\w-]+)\s*$', text, re.MULTILINE)[1]
    runtime = ROOT / 'data/service'
    runtime.mkdir(parents=True, exist_ok=True)
    candidate = runtime / 'cloudflared.yml'
    candidate.write_text(add_ingress(text, args.hostname))
    staged_plist = runtime / f'{LABEL}.plist'
    staged_plist.write_bytes(plistlib.dumps(service_plist(args.hostname)))
    print(f'手机固定入口：https://{args.hostname}/task/<原任务令牌>', flush=True)
    print(f'待安装文件：{staged_plist} 和 {candidate}', flush=True)
    if not args.apply:
        return

    domain = f'gui/{os.getuid()}'
    target = f'{domain}/{LABEL}'
    run(args.cloudflared, 'tunnel', '--config', candidate, 'ingress', 'validate')
    run(args.cloudflared, 'tunnel', 'route', 'dns', tunnel, args.hostname)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = args.config.with_name(args.config.name + f'.bak.{stamp}')
    shutil.copy2(args.config, backup)
    shutil.copyfile(candidate, args.config)
    print(f'原隧道配置已备份：{backup}', flush=True)

    destination = Path.home() / 'Library/LaunchAgents' / staged_plist.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.copy2(destination, runtime / f'{LABEL}.{stamp}.bak')
    loaded = subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0
    if loaded:
        run('launchctl', 'bootout', target)
    shutil.copyfile(staged_plist, destination)
    run('launchctl', 'enable', target)
    run('launchctl', 'bootstrap', domain, destination)
    run('launchctl', 'kickstart', '-k', f'{domain}/{args.tunnel_job}')
    print(f'已安装 {target}；服务异常退出后自动重启，登录后自动启动。', flush=True)


if __name__ == '__main__':
    main()
