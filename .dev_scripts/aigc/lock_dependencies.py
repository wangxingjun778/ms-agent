"""输出 Python 3.12 服务锁；调用方将 stdout 保存为 aigc-lock.txt。"""

from __future__ import annotations

import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from packaging.utils import InvalidWheelFilename, parse_wheel_filename


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.urls.append(href)


def hashes_for(entry: str, index: str) -> str:
    name, version = entry.split('==')
    url = f'{index.rstrip("/")}/{name}/'
    with urlopen(url, timeout=30) as response:
        parser = Links()
        parser.feed(response.read().decode('utf-8'))
    hashes = set()
    for url in parser.urls:
        parts = urlsplit(url)
        filename = unquote(parts.path.rsplit('/', 1)[-1])
        if not filename.endswith('.whl') or not parts.fragment.startswith('sha256='):
            continue
        try:
            _, wheel_version, _, tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            continue
        if str(wheel_version) != version:
            continue
        for tag in tags:
            interpreter = tag.interpreter in {'py3', 'cp312'} or (
                tag.abi == 'abi3' and tag.interpreter.startswith('cp') and 37 <= int(tag.interpreter[2:]) <= 312
            )
            platform = (
                tag.platform == 'any'
                or (tag.platform.startswith('manylinux') and tag.platform.endswith(('x86_64', 'aarch64')))
                or (tag.platform.startswith('macosx') and tag.platform.endswith(('arm64', 'universal2')))
            )
            if interpreter and platform:
                hashes.add(parts.fragment.split('=', 1)[1])
    if not hashes:
        raise RuntimeError(f'{entry} 没有支持平台的 wheel')
    return entry + ' \\\n' + ' \\\n'.join('    --hash=sha256:' + value for value in sorted(hashes))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--index', default='https://pypi.org/simple')
    args = parser.parse_args()
    result = subprocess.run(
        [
            'uv',
            'pip',
            'compile',
            'requirements/aigc.txt',
            '--python-version',
            '3.12',
            '--index-url',
            args.index,
            '--no-header',
            '--no-annotate',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    entries = [line for line in result.stdout.splitlines() if '==' in line and not line.startswith('#')]
    with ThreadPoolExecutor(max_workers=6) as pool:
        output = list(pool.map(lambda entry: hashes_for(entry, args.index), entries))
    print('# Python 3.12；Linux amd64/arm64 与 macOS arm64 的 wheel 哈希。')
    print('\n'.join(output))


if __name__ == '__main__':
    main()
