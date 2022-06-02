from datetime import datetime
from datetime import timedelta
import numpy as np
import sys

if len(sys.argv) < 4:
    print(
        f'Usage {sys.argv[0]} "$(date +%z)" '
        f'ovn-binding.log ovn-installed.log'
    )

tz = -int(sys.argv[1])
delta = timedelta(hours=int(tz / 100), minutes=int(tz % 100))

with open(sys.argv[3], 'r') as installed_file:
    ovn_installed = installed_file.read().strip().splitlines()

with open(sys.argv[2], 'r') as binding_file:
    ovn_binding = binding_file.read().strip().splitlines()

latency_per_port = {}

for record in ovn_binding:
    record = record.split(' ')

    date = record[0]
    time = record[1]
    port = record[2]

    date = datetime.strptime(f'{date} {time}', '%Y-%m-%d %H:%M:%S,%f')
    latency_per_port[port] = date + delta

for record in ovn_installed:
    record = record.split(' ')

    date = record[0]
    time = record[1]
    port = record[2]

    if isinstance(latency_per_port[port], timedelta):
        # ovn-installed more than once, ignoring.
        continue

    date = datetime.strptime(f'{date} {time}', '%Y-%m-%d %H:%M:%S.%fZ')
    latency_per_port[port] = date - latency_per_port[port]

failures = 0
latencies = []

for port, latency in latency_per_port.items():
    if isinstance(latency, datetime):
        failures = failures + 1
        continue
    ms = int(latency.total_seconds() * 1000)
    latencies.append(ms)

print(
    '''
Latency between logical port binding, i.e. creation of the corresponding
OVS port on the worker node, and ovn-controller setting up ovn-installed
flag for that port according to test-log and ovn-controller logs.

Note:
This data can not be directly interpreted as OVN end-to-end latency,
because port bindings are always happening after logical ports were already
created in Northbound database.  And for bulk creations, bindings performed
also in bulk after all creations are done.

Look for 'Not installed' below to find for which ports ovn-installed was
never set (at least, there is no evidence in logs).
'''
)

print('min     :', min(latencies), 'ms')
print('max     :', max(latencies), 'ms')
print('avg     :', int(sum(latencies) / len(latencies)), 'ms')
print('95%     :', int(np.percentile(latencies, 95)), 'ms')
print('total   :', len(latency_per_port))
print('failures:', failures)
print()

for port, latency in latency_per_port.items():
    if isinstance(latency, datetime):
        print(f'{port:<10}: Not installed')
        continue
    ms = int(latency.total_seconds() * 1000)
    print(f'{port:<10}: {ms}')
