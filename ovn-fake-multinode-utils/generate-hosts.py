#!/usr/bin/python3

from __future__ import print_function

import helpers
import yaml
import sys
from pathlib import Path


def usage(name):
    print(
        f"""
{name} DEPLOYMENT ovn-fake-multinode-target github-repo branch
where DEPLOYMENT is the YAML file defining the deployment.
""",
        file=sys.stderr,
    )


def generate_node_string(host, **kwargs):
    args = ' '.join(f"{key}={value}" for key, value in kwargs.items())
    print(f"{host} {args}")


def generate_node(config, internal_iface, **kwargs):
    host = config['name']
    internal_iface = config.get('internal-iface', internal_iface)
    generate_node_string(
        host,
        internal_iface=internal_iface,
        **kwargs,
    )


def generate_tester(config, internal_iface):
    ssh_key = config["ssh_key"]
    ssh_key = Path(ssh_key).resolve()
    generate_node(
        config,
        internal_iface,
        ovn_tester="true",
        ssh_key=str(ssh_key),
    )


def generate_nodes(nodes_config, internal_iface, **kwargs):
    for node_config in nodes_config:
        host, node_config = helpers.get_node_config(node_config)
        iface = node_config.get('internal-iface', internal_iface)
        generate_node_string(
            host,
            internal_iface=iface,
            **kwargs,
        )


def generate(input_file, target, repo, branch):
    with open(input_file, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)
        user = config.get('user', 'root')
        prefix = config.get('prefix', 'ovn-scale')
        tester_config = config['tester-node']
        internal_iface = config['internal-iface']

        print('[tester_hosts]')
        generate_tester(tester_config, internal_iface)
        print('\n[central_hosts]')
        generate_nodes(
            config['central-nodes'], internal_iface, ovn_central="true"
        )
        print('\n[worker_hosts]')
        generate_nodes(config['worker-nodes'], internal_iface)
        print()

        print('[all:vars]')
        print('ansible_user=' + user)
        print('become=true')
        print('node_name=' + prefix)
        print('ovn_fake_multinode_target_path=' + target)
        print('ovn_fake_multinode_path=' + target + '/ovn-fake-multinode')
        print('ovn_fake_multinode_repo=' + repo)
        print('ovn_fake_multinode_branch=' + branch)
        print('rundir=' + target)


def main():
    if len(sys.argv) != 5:
        usage(sys.argv[0])
        sys.exit(1)

    generate(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])


if __name__ == "__main__":
    main()
