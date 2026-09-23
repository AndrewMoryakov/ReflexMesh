"""Optional integration check against a built, pinned JevRouter checkout (no API key)."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from reflexmesh.contracts.task import Task, Route
from reflexmesh.routing.jev_router import route_task

PIN = 'f944acb6530621bced023352e2358a63218bf4d9'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upstream-dir', required=True, type=Path)
    args = parser.parse_args()
    upstream = args.upstream_dir.resolve()
    sha = subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], text=True).strip()
    if sha != PIN:
        raise SystemExit('Unexpected upstream revision; inspect compatibility before changing the pin')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    endpoint = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory() as directory:
        process = subprocess.Popen(['node', str(upstream/'dist/cli.js'), 'serve', '--provider', 'demo',
                                    '--port', str(port)], cwd=directory,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            opener = build_opener(ProxyHandler({}))
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('Upstream server failed to start')
                try:
                    with opener.open(endpoint + '/health', timeout=.2) as response:
                        if json.load(response)['provider'] == 'jevrouter-demo':
                            break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError('Upstream server did not become ready')
            one = Task('0.1', 'one', 'Generate text', (Route.LLM,), (Route.LLM,))
            selected = route_task(one, endpoint=endpoint, allow_demo=True)
            if selected['status'] != 'selected' or not selected['is_stub'] or selected['route'] != 'LLM':
                raise RuntimeError(f'Selection failed: {selected}')
            many = Task('0.1', 'many', 'Привет', (Route.CUA, Route.LLM), (Route.CUA, Route.LLM))
            abstained = route_task(many, endpoint=endpoint, allow_demo=True)
            if abstained['status'] != 'abstained':
                raise RuntimeError('Expected low-confidence refusal')
            refused = route_task(one, endpoint=endpoint)
            if refused['reason_code'] != 'demo_not_allowed':
                raise RuntimeError('Demo was not blocked')
            print(json.dumps({'upstream_commit': sha, 'provider': 'jevrouter-demo',
                              'selected': selected['status'], 'low_confidence': abstained['status'],
                              'demo_guard': refused['reason_code'], 'live_jev_tested': False}))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()


if __name__ == '__main__':
    main()
