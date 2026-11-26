#!/usr/bin/env python3

import argparse
import ipaddress
import subprocess

# RTPROT_BGP.  ovn-controller only learns routes whose protocol is above
# RTPROT_STATIC and is not its own RTPROT_OVN, so injected routes have to
# look like they came from a routing daemon.
ROUTE_PROTO = 186


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vrf-dev', required=True)
    parser.add_argument('--nexthop', required=True)
    parser.add_argument('--table', type=int, required=True)
    parser.add_argument('--n-routes', type=int, required=True)
    parser.add_argument('--base', required=True)
    args = parser.parse_args()

    # 'replace' rather than 'add' so that a second injection into the same
    # VRF does not fail on an address that is already configured.
    subprocess.run(
        ['ip', 'addr', 'replace', f'{args.nexthop}/32', 'dev', args.vrf_dev],
        check=True,
    )

    # ovn-controller drops routes with no nexthop, so every route needs a
    # 'via'.
    base_ip = ipaddress.IPv4Address(args.base)
    batch = ''.join(
        f'route add {base_ip + r}/32 via {args.nexthop} '
        f'dev {args.vrf_dev} table {args.table} proto {ROUTE_PROTO}\n'
        for r in range(args.n_routes)
    )

    # One invocation for the whole batch.  It stops at the first failing
    # line and exits non-zero, which the caller checks.
    subprocess.run(['ip', '-batch', '-'], input=batch, text=True, check=True)


if __name__ == '__main__':
    main()
