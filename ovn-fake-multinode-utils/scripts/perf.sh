#!/bin/bash

host=$1
node_name=$2

function collect_flamegraph_data() {
    c=$1
    mkdir ${host}-perf/$c
    pid=$(podman exec $c /bin/sh -c "pidof -s perf")
    podman exec $c /bin/sh -c "kill $pid && tail --pid=$pid -f /dev/null"
    podman exec $c /bin/sh -c "perf script report flamegraph -o /tmp/ovn-flamegraph.html"
    podman cp $c:/tmp/ovn-flamegraph.html ${host}-perf/$c/
}

mkdir /tmp/${host}-perf

pushd /tmp
for c in $(podman ps --format "{{.Names}}" --filter "name=${node_name}"); do
    collect_flamegraph_data $c
done

for c in $(podman ps --format "{{.Names}}" --filter "name=ovn-central"); do
    collect_flamegraph_data $c
done

tar cvfz ${host}-perf.tgz ${host}-perf
rm -rf ${host}-perf
popd
