#!/bin/bash

host=$1
node_name=$2

mkdir /tmp/${host}
pushd /tmp
for c in $(podman ps --format "{{.Names}}" --filter "name=${node_name}"); do
    mkdir ${host}/$c
    podman exec $c ps -aux > ${host}/$c/ps
    podman exec $c bash -c 'touch /tmp/process-monitor.exit && sleep 5'
    podman cp $c:/var/log/ovn/ovn-controller.log ${host}/$c/
    podman cp $c:/var/log/openvswitch/ovs-vswitchd.log ${host}/$c/
    podman cp $c:/var/log/openvswitch/ovsdb-server.log ${host}/$c/
    podman cp $c:/etc/openvswitch/conf.db ${host}/$c/
    podman cp $c:/var/log/process-stats.json ${host}/$c/
done

for c in $(podman ps --format "{{.Names}}" --filter "name=ovn-central"); do
    mkdir ${host}/$c
    podman exec $c ps -aux > ${host}/$c/ps-before-compaction
    podman exec $c ovs-appctl --timeout=30 -t /var/run/ovn/ovnsb_db.ctl ovsdb-server/compact
    podman exec $c ovs-appctl --timeout=30 -t /var/run/ovn/ovnnb_db.ctl ovsdb-server/compact
    podman exec $c ps -aux > ${host}/$c/ps-after-compaction
    podman exec $c bash -c 'touch /tmp/process-monitor.exit && sleep 5'
    podman cp $c:/var/log/ovn/ovn-controller.log ${host}/$c/
    podman cp $c:/var/log/ovn/ovn-northd.log ${host}/$c/
    podman cp $c:/var/log/ovn/ovsdb-server-nb.log ${host}/$c/
    podman cp $c:/var/log/ovn/ovsdb-server-sb.log ${host}/$c/
    podman cp $c:/etc/ovn/ovnnb_db.db ${host}/$c/
    podman cp $c:/etc/ovn/ovnsb_db.db ${host}/$c/
    podman cp $c:/var/log/openvswitch/ovs-vswitchd.log ${host}/$c/
    podman cp $c:/var/log/openvswitch/ovsdb-server.log ${host}/$c/
    podman cp $c:/var/log/openvswitch/ovn-nbctl.log ${host}/$c/
    podman cp $c:/var/log/process-stats.json ${host}/$c/
done

for c in $(podman ps --format "{{.Names}}" --filter "name=ovn-relay"); do
    mkdir ${host}/$c
    podman exec $c ps -aux > ${host}/$c/ps-before-compaction
    podman exec $c ovs-appctl --timeout=30 -t /var/run/ovn/ovnsb_db.ctl ovsdb-server/compact
    podman exec $c ps -aux > ${host}/$c/ps-after-compaction
    podman exec $c bash -c 'touch /tmp/process-monitor.exit && sleep 5'
    podman cp $c:/var/log/ovn/ovsdb-server-sb.log ${host}/$c/
    podman cp $c:/var/log/process-stats.json ${host}/$c/
done

for c in $(podman ps --format "{{.Names}}" --filter "name=ovn-tester"); do
    mkdir ${host}/$c
    podman exec $c bash -c 'touch /tmp/process-monitor.exit && sleep 5'
    podman exec $c bash -c "mkdir -p /htmls; cp -f /*.html /htmls"
    podman cp $c:/htmls/. ${host}/$c/
    podman cp $c:/var/log/process-stats.json ${host}/$c/
done

journalctl --since "8 hours ago" -a > ${host}/messages

tar cvfz ${host}.tgz ${host}
rm -rf ${host}
popd
