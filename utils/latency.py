from datetime import datetime
import numpy as np
import sys

if len(sys.argv) < 3:
    print(f'Usage {sys.argv[0]} ' f'ovn-binding.log ovn-installed.log')

with open(sys.argv[2], 'r') as installed_file:
    ovn_installed = installed_file.read().strip().splitlines()

with open(sys.argv[1], 'r') as binding_file:
    ovn_binding = binding_file.read().strip().splitlines()

binding_times = dict()
for record in ovn_binding:
    date, time, port = record.split(' ')

    date = datetime.strptime(f'{date} {time}', '%Y-%m-%d %H:%M:%S,%f')
    binding_times[port] = date

latency_per_port = dict()
for record in ovn_installed:
    date, time, port = record.split(' ')

    if port in latency_per_port:
        # ovn-installed more than once, ignoring.
        continue

    binding_time = binding_times.pop(port)

    date = datetime.strptime(f'{date} {time}', '%Y-%m-%d %H:%M:%S.%fZ')
    latency_per_port[port] = date - binding_time

failures = len(binding_times)
latencies = []

for latency in latency_per_port.values():
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

print('min     :', min(latencies, default=0), 'ms')
print('max     :', max(latencies, default=0), 'ms')
print('avg     :', sum(latencies) // len(latencies), 'ms')
print('95%     :', int(np.percentile(latencies, 95)), 'ms')
print('total   :', len(latency_per_port))
print('failures:', failures)
print()

for port, latency in latency_per_port.items():
    if port in binding_times:
        print(f'{port:<10}: Not installed')
        continue
    ms = int(latency.total_seconds() * 1000)
    print(f'{port:<10}: {ms}')
