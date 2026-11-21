#!/usr/bin/env python3
"""VPS-local read-only HTTP API probe for a verified private owner runner.

This script must already be deployed on the same VPS at an approved, pinned SHA.
It reads its local token directly and never prints or transmits it to GitHub.
"""
import argparse
import json
import os
import stat
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--token-file', type=Path, default=Path('/etc/khabarchin/control.token'))
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args()
    path = args.token_file
    if (path.is_symlink() or not path.is_file() or
            (path.stat().st_mode & 0o077) or not 1024 <= args.port <= 65535):
        raise SystemExit('KHABARCHIN_GATE=TOKEN_OR_PORT_INVALID')
    token = path.read_bytes().strip().decode('ascii')
    if not 32 <= len(token) <= 256:
        raise SystemExit('KHABARCHIN_GATE=TOKEN_INVALID')
    try:
        request = Request('http://127.0.0.1:%d/v1/status' % args.port,
                          headers={'Authorization': 'Bearer ' + token,
                                   'Host': '127.0.0.1:%d' % args.port})
        # Force direct loopback; HTTP_PROXY/HTTPS_PROXY must never see the bearer token.
        with build_opener(ProxyHandler({})).open(request, timeout=5) as res:
            data = json.loads(res.read(4096))
        if (data.get('db') != 'readable' or
                not all(isinstance(data.get(k), int)
                        for k in ('feeds', 'subscriptions', 'active_subscriptions'))):
            raise ValueError('unexpected response')
        for name in ('feeds', 'subscriptions', 'active_subscriptions'):
            print('KHABARCHIN_' + name.upper() + '=' + str(data[name]))
        print('KHABARCHIN_API_STATUS=PASS_READ_ONLY')
    except Exception:
        raise SystemExit('KHABARCHIN_API_STATUS=NOT_VERIFIED') from None


if __name__ == '__main__':
    main()
