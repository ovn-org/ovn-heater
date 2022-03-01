#!/usr/bin/python3

from __future__ import print_function

import helpers
import yaml
import sys

def usage(name):
    print("""
{} DEPLOYMENT ovn-fake-multinode-target github-repo branch
where DEPLOYMENT is the YAML file defining the deployment.
""".format(name), file=sys.stderr)

def generate_nodes(nodes_config, user, prefix, internal_iface):
    for node_config in nodes_config:
        host, node_config = helpers.get_node_config(node_config)
        iface = node_config.get('internal-iface', internal_iface)
        generate_worker(host, user, prefix, iface)

def generate_controller(config, user, prefix, internal_iface):
    host = config['name']
    internal_iface = config.get('internal-iface', internal_iface)
    print('{} ansible_user=root become=true internal_iface={} node_name={} ovn_central=true'.format(
        host, internal_iface, prefix
    ))

def generate_worker(host, user, prefix, internal_iface):
    print('{} ansible_user=root become=true internal_iface={} node_name={}'.format(
        host, internal_iface, prefix
    ))

def generate(input_file, target, repo, branch):
    with open(input_file, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)
        user = config.get('user', 'root')
        prefix = config.get('prefix', 'ovn-scale')
        registry_node = config['registry-node']
        central_config = config['central-node']

        print('[ovn_hosts]')
        internal_iface = config['internal-iface']
        generate_controller(central_config, user, prefix, internal_iface)
        generate_nodes(config['worker-nodes'], user, prefix, internal_iface)
        print()

        print('[ovn_hosts:vars]')
        print('ovn_fake_multinode_target_path=' + target)
        print('ovn_fake_multinode_path=' + target + '/ovn-fake-multinode')
        print('ovn_fake_multinode_repo=' + repo)
        print('ovn_fake_multinode_branch=' + branch)
        print('registry_node=' + registry_node)
        print('rundir=' + target)

def main():
    if len(sys.argv) != 5:
        usage(sys.argv[0])
        sys.exit(1)

    generate(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])

if __name__ == "__main__":
    main()
