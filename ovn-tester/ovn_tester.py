#!/usr/bin/env python

import os
import sys
import ovn_context
import ovn_utils
import ovn_stats
import ovn_workload
import netaddr
import time
import yaml

from ovn_context import OvnContext

sandboxes = []
farm_list = []

run_args = {}
controller_args = {}
fake_multinode_args = {}
lnetwork_create_args = {}
lswitch_create_args = {}
lport_bind_args = {}
lport_create_args = {}
nbctld_config = {}


def usage(name):
    print("""
{} PHYSICAL_DEPLOYMENT TEST_CONF
where PHYSICAL_DEPLOYMENT is the YAML file defining the deployment.
where TEST_CONF is the YAML file defining the test parameters.
""".format(name), file=sys.stderr)


def read_physical_deployment(deployment):
    with open(deployment, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)

        for worker in config['worker-nodes']:
            farm = {'ip': worker}
            farm_list.append(farm)

        central_config = config['central-node']
        controller_args['ip'] = central_config['name']
        controller_args['user'] = central_config.get('user', 'root')
        controller_args['password'] = central_config.get('password', '')
        controller_args['name'] = central_config.get('prefix', 'ovn-central')


def read_test_conf(test_conf):
    global run_args
    global fake_multinode_args
    global lnetwork_create_args
    global lswitch_create_args
    global lport_bind_args
    global lport_create_args
    global nbctld_config

    with open(test_conf, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)
        run_args = config['run_args']
        fake_multinode_args = config['fake_multinode_args']
        lnetwork_create_args = config['lnetwork_create_args']
        lswitch_create_args = config['lswitch_create_args']
        lport_bind_args = config['lport_bind_args']
        lport_create_args = config['lport_create_args']
        nbctld_config = config['nbctld_config']


def create_sandbox():
    iteration = ovn_context.active_context.iteration
    farm = farm_list[iteration % len(farm_list)]
    sandbox = {
            "farm": farm['ip'],
            "ssh": farm['ssh'],
            "name": "ovn-scale-%s" % iteration
    }
    sandboxes.append(sandbox)
    return sandbox


def prepare_test():
    # create ssh connections
    for i in range(len(farm_list)):
        farm = farm_list[i]
        farm['ssh'] = ovn_utils.SSH(farm)

    # create sandox list
    with OvnContext("create_sandboxes", run_args['n_sandboxes']) as ctx:
        for i in ctx:
            sandbox = create_sandbox()
            print("name: " + sandbox['name'] + " farm: " + sandbox['farm'])

    # start ovn-northd on ovn central
    with OvnContext("add_central", 1) as ctx:
        clustered_db = fake_multinode_args.get("ovn_cluster_db", False)
        ovn = ovn_workload.OvnWorkload(controller_args, sandboxes,
                                       clustered_db, run_args['log'])
        ovn.add_central(fake_multinode_args, nbctld_config)
        use_dp_groups = run_args.get('use_dp_groups', True)
        ovn.set_global_option('use_logical_dp_groups', use_dp_groups)

    # create swith-per-node topology
    with OvnContext("prepare_chassis", run_args['n_sandboxes']) as ctx:
        for i in ctx:
            ovn.add_chassis_node(fake_multinode_args)
            if lnetwork_create_args.get('gw_router_per_network', False):
                ovn.add_chassis_node_localnet(fake_multinode_args)
                ovn.add_chassis_external_host(lnetwork_create_args)

    return ovn


def run_test_base_cluster(ovn):
    # create cluster router
    with OvnContext("create_cluster_router", 1) as ctx:
        for _ in ctx:
            ovn.create_cluster_router("lr-cluster")

    # create ovn topology
    with OvnContext("create_routed_network", run_args['n_sandboxes']) as ctx:
        for _ in ctx:
            ovn.create_routed_network(fake_multinode_args, lswitch_create_args,
                                      lnetwork_create_args, lport_bind_args)


def run_test_network_policy(ovn):
    with OvnContext("create_routed_lport", run_args['n_lports']) as ctx:
        for _ in ctx:
            ovn.create_routed_lport(lport_create_args, lport_bind_args)

if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])
        sys.exit(1)

    # parse configuration
    read_physical_deployment(sys.argv[1])
    read_test_conf(sys.argv[2])

    ovn = prepare_test()
    run_test_base_cluster(ovn)
    run_test_network_policy(ovn)
    sys.exit(0)
