#!/usr/bin/env python3

import argparse
import json
import os
import psutil
import time


process_names = ['ovn-', 'ovs-', 'ovsdb-', 'etcd']


def monitor(suffix, out_file, exit_file):
    data = {}
    while True:
        try:
            if os.path.exists(exit_file):
                raise KeyboardInterrupt

            processes = set()
            for p in psutil.process_iter():
                if any(name in p.name() for name in process_names):
                    processes.add(p)
                elif any(
                    name in part
                    for part in p.cmdline()
                    for name in process_names
                ):
                    processes.add(p)

            if len(processes) == 0:
                time.sleep(0.5)
                continue

            tme = time.time()
            for p in processes:
                try:
                    name = p.name() + "-" + suffix + "-" + str(p.pid)
                    # cpu_percent(seconds) call will block
                    # for the amount of seconds specified.
                    cpu = p.cpu_percent(0.5)
                    mem = p.memory_info().rss
                except psutil.NoSuchProcess:
                    # Process went away.  Skipping.
                    continue

                if not data.get(tme):
                    data[tme] = {}

                data[tme][name] = {'cpu': cpu, 'rss': mem}

        except KeyboardInterrupt:
            with open(out_file, "w") as f:
                json.dump(data, f, indent=4, sort_keys=True)
            break

        except Exception:
            # Ignoring all unexpected exceptions to avoid loosing data.
            continue


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OVS/OVN process monitor')
    parser.add_argument(
        '-s', '--suffix', help='Process name suffix to add', default=''
    )
    parser.add_argument(
        '-o', '--output', help='Output file name', default='process-stats.json'
    )
    parser.add_argument(
        '-x',
        '--exit-file',
        help='File that signals to exit',
        default='process-monitor.exit',
    )

    args = parser.parse_args()
    monitor(args.suffix, args.output, args.exit_file)
