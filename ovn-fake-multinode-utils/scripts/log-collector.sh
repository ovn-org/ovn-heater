#!/bin/bash

host=$1
node_name=$2

mkdir /tmp/${host}
pushd /tmp
for c in $(docker ps --format "{{.Names}}" --filter "name=${node_name}"); do
    mkdir ${host}/$c
    docker exec $c ps -aux > ${host}/$c/ps
    docker exec $c bash -c 'touch /tmp/process-monitor.exit && sleep 5'
    docker cp $c:/var/log/ovn/ovn-controller.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovs-vswitchd.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovsdb-server.log ${host}/$c/
    docker cp $c:/etc/openvswitch/conf.db ${host}/$c/
    docker cp $c:/var/log/process-stats.json ${host}/$c/
done

for c in $(docker ps --format "{{.Names}}" --filter "name=ovn-central"); do
    mkdir ${host}/$c
    docker exec $c ps -aux > ${host}/$c/ps-before-compaction
    docker exec $c ovs-appctl --timeout=30 -t /var/run/ovn/ovnsb_db.ctl ovsdb-server/compact
    docker exec $c ovs-appctl --timeout=30 -t /var/run/ovn/ovnnb_db.ctl ovsdb-server/compact
    docker exec $c ps -aux > ${host}/$c/ps-after-compaction
    docker exec $c bash -c 'touch /tmp/process-monitor.exit && sleep 5'
    docker cp $c:/var/log/ovn/ovn-controller.log ${host}/$c/
    docker cp $c:/var/log/ovn/ovn-northd.log ${host}/$c/
    docker cp $c:/var/log/ovn/ovsdb-server-nb.log ${host}/$c/
    docker cp $c:/var/log/ovn/ovsdb-server-sb.log ${host}/$c/
    docker cp $c:/etc/ovn/ovnnb_db.db ${host}/$c/
    docker cp $c:/etc/ovn/ovnsb_db.db ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovs-vswitchd.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovsdb-server.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovn-nbctl.log ${host}/$c/
    docker cp $c:/var/log/process-stats.json ${host}/$c/
done

for c in $(docker ps --format "{{.Names}}" --filter "name=ovn-relay"); do
    mkdir ${host}/$c
    docker exec $c ps -aux > ${host}/$c/ps-before-compaction
    docker exec $c ovs-appctl --timeout=30 -t /var/run/ovn/ovnsb_db.ctl ovsdb-server/compact
    docker exec $c ps -aux > ${host}/$c/ps-after-compaction
    docker exec $c bash -c 'touch /tmp/process-monitor.exit && sleep 5'
    docker cp $c:/var/log/ovn/ovsdb-server-sb.log ${host}/$c/
    docker cp $c:/var/log/process-stats.json ${host}/$c/
done

journalctl --since "8 hours ago" -a > ${host}/messages

tar cvfz ${host}.tgz ${host}
rm -rf ${host}
popd
