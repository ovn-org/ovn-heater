#!/bin/bash

host=$1
node_name=$2

mkdir /tmp/${host}
pushd /tmp
for c in $(docker ps --format "{{.Names}}" --filter "name=${node_name}"); do
    mkdir ${host}/$c
    docker cp $c:/var/log/ovn/ovn-controller.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovs-vswitchd.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovsdb-server.log ${host}/$c/
done

for c in $(docker ps --format "{{.Names}}" --filter "name=ovn-central"); do
    mkdir ${host}/$c
    docker cp $c:/var/log/ovn/ovn-controller.log ${host}/$c/
    docker cp $c:/var/log/ovn/ovn-northd.log ${host}/$c/
    docker cp $c:/var/log/ovn/ovsdb-server-nb.log ${host}/$c/
    docker cp $c:/var/log/ovn/ovsdb-server-sb.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovs-vswitchd.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovsdb-server.log ${host}/$c/
    docker cp $c:/var/log/openvswitch/ovn-nbctl.log ${host}/$c/
done

journalctl --since "8 hours ago" -a > ${host}/messages

tar cvfz ${host}.tgz ${host}
rm -rf ${host}
popd
