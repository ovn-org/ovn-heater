#!/bin/bash

host=$1
node_name=$2

mkdir /tmp/${host}-perf

pushd /tmp
for c in $(docker ps --format "{{.Names}}" --filter "name=${node_name}"); do
    mkdir ${host}-perf/$c
    docker exec $c /bin/sh -c "perf report -i /ovn-perf.data --stdio > /tmp/ovn-perf.log"
    docker cp $c:/tmp/ovn-perf.log ${host}-perf/$c/
done

for c in $(docker ps --format "{{.Names}}" --filter "name=ovn-central"); do
    mkdir ${host}-perf/$c
    docker exec $c /bin/sh -c "perf report -i /ovn-perf.data --stdio > /tmp/ovn-perf.log"
    docker cp $c:/tmp/ovn-perf.log ${host}-perf/$c/
done

tar cvfz ${host}-perf.tgz ${host}-perf
rm -rf ${host}-perf
popd
