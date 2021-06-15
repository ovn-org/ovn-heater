#!/usr/bin/env python

import os
import sys
import ovn_utils
import netaddr
import time
import yaml
import ovn_workload

sandboxes = [] # ovn sanbox list
farm_list = []

run_args = {
}
controller_args = {
}
fake_multinode_args = {
}
lnetwork_create_args = {
}
lswitch_create_args = {
}
lport_bind_args = {
}
lport_create_args = {
}
nbctld_config = {
}

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
            farm = { 'ip' : worker }
            farm_list.append(farm)

        central_config = config['central-node']
        controller_args['ip'] = central_config['name']
        controller_args['user'] = central_config.get('user', 'root')
        controller_args['password'] = central_config.get('password', '')
        controller_args['name'] = central_config.get('prefix', 'ovn-central')

def read_test_conf(test_conf):
    with open(test_conf, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)

        run_config = config['run_args']
        run_args['n_sandboxes'] = run_config['n_sandboxes'] # Total number of fake hvs
        run_args['n_lports'] = run_config['n_lports'] # Total number of pods
        run_args['log'] = run_config['log']

        fake_multinode_config = config['fake_multinode_args']
        fake_multinode_args['node_net'] = fake_multinode_config['node_net']
        fake_multinode_args['node_net_len'] = fake_multinode_config['node_net_len']
        fake_multinode_args['node_ip'] = fake_multinode_config['node_ip']
        fake_multinode_args['ovn_cluster_db'] = fake_multinode_config['ovn_cluster_db']
        fake_multinode_args['ovn_monitor_all'] = fake_multinode_config.get('ovn_monitor_all')
        fake_multinode_args['central_ip'] = fake_multinode_config['central_ip']
        fake_multinode_args['sb_proto'] = fake_multinode_config['sb_proto']
        fake_multinode_args['max_timeout_s'] = fake_multinode_config['max_timeout_s']
        fake_multinode_args['cluster_cmd_path'] = fake_multinode_config['cluster_cmd_path']

        lnetwork_config = config['lnetwork_create_args']
        lnetwork_create_args['start_ext_cidr'] = lnetwork_config['start_ext_cidr']
        lnetwork_create_args['gw_router_per_network'] = lnetwork_config['gw_router_per_network']
        lnetwork_create_args['start_gw_cidr'] = lnetwork_config['start_gw_cidr']
        lnetwork_create_args['start_ext_cidr'] = lnetwork_config['start_ext_cidr']
        lnetwork_create_args['cluster_cidr'] = lnetwork_config['cluster_cidr']

        lswitch_config = config['lswitch_create_args']
        lswitch_create_args['start_cidr'] = lswitch_config['start_cidr']
        lswitch_create_args['nlswitch'] = run_args['n_sandboxes']

        lport_bind_config = config['lport_bind_args']
        lport_bind_args['internal'] = lport_bind_config['internal']
        lport_bind_args['wait_up'] = lport_bind_config['wait_up']
        lport_bind_args['wait_sync'] = lport_bind_config['wait_sync']

        lport_create_config = config['lport_create_args']
        lport_create_args['network_policy_size'] = lport_create_config['network_policy_size']
        lport_create_args['name_space_size'] = lport_create_config['name_space_size']
        lport_create_args['create_acls'] = lport_create_config['create_acls']

        nbctld_configuration = config['nbctld_config']
        nbctld_config['daemon'] = nbctld_configuration['daemon']

def create_sandbox(sandbox_create_args = {}, iteration = 0):
    amount = sandbox_create_args.get("amount", 1)

    bcidr = sandbox_create_args.get("cidr", "1.0.0.0/8")
    base_cidr = netaddr.IPNetwork(bcidr)
    cidr = "{}/{}".format(str(base_cidr.ip + iteration * amount + 1),
                          base_cidr.prefixlen)
    start_cidr = netaddr.IPNetwork(cidr)
    sandbox_cidr = netaddr.IPNetwork(start_cidr)
    if not sandbox_cidr.ip + amount in sandbox_cidr:
        message = _("Network %s's size is not big enough for %d sandboxes.")
        raise exceptions.InvalidConfigException(
                message  % (start_cidr, amount))

    for i in range(amount):
        farm = farm_list[ (i + iteration) % len(farm_list) ]
        sandbox = {
                "farm" : farm['ip'],
                "ssh" : farm['ssh'],
                "name" : "ovn-scale-%s" % iteration
        }
        sandboxes.append(sandbox)

def run_test():
    # create ssh connections
    for i in range(len(farm_list)):
        farm = farm_list[i]
        farm['ssh'] = ovn_utils.SSH(farm)

    # create sandox list
    print("***** creating following sanboxes *****")
    for i in range(run_args['n_sandboxes']):
        create_sandbox(iteration = i)
        sandbox = sandboxes[i]
        print("name: " + sandbox['name'] + " farm: " + sandbox['farm'])

    # start ovn-northd on ovn central
    ovn = ovn_workload.OvnWorkload(controller_args, sandboxes,
            fake_multinode_args.get("ovn_cluster_db", False),
            log = run_args['log'])
    ovn.add_central(fake_multinode_args, nbctld_config = nbctld_config)

    # creat swith-per-node topology
    for i in range(run_args['n_sandboxes']):
        ovn.add_chassis_node(fake_multinode_args, iteration = i)
        if lnetwork_create_args.get('gw_router_per_network', False):
            ovn.add_chassis_node_localnet(fake_multinode_args, iteration = i)
            ovn.add_chassis_external_host(lnetwork_create_args, iteration = i)

    # create ovn topology
    ovn.create_routed_network(fake_multinode_args = fake_multinode_args,
                              lswitch_create_args = lswitch_create_args,
                              lnetwork_create_args = lnetwork_create_args,
                              lport_bind_args = lport_bind_args)
    # create ovn logical ports
    for i in range(run_args['n_lports']):
        ovn.create_routed_lport(lport_create_args = lport_create_args,
                                lport_bind_args = lport_bind_args,
                                iteration = i)

if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])
        sys.exit(1)

    # parse configuration
    read_physical_deployment(sys.argv[1])
    read_test_conf(sys.argv[2])
    # execute the test
    sys.exit(run_test())
