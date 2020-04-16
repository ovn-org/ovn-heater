#!/usr/bin/python3

from __future__ import print_function

import helpers
import yaml
import sys

def usage(name):
    print("""
{} DEPLOYMENT CLUSTERED_DB
where DEPLOYMENT is the YAML file defining the deployment.
where CLUSTERED_DB (boolean) enables clustered db. True if not specified.
""".format(name), file=sys.stderr)

def generate_nodes(nodes_config, user, prefix, max_containers):
    host_configs = [helpers.get_node_config(c) for c in nodes_config]
    hosts = [h for h, _ in host_configs]
    p, s = helpers.get_prefix_suffix(hosts)

    nodes = 0
    print('    "nodes": [')
    for container in range(1, max_containers + 1):
        for host, node_config in host_configs:
            host_shortname = helpers.get_shortname(host, p, s)
            fake_nodes = node_config.get('fake-nodes', max_containers)
            if container > fake_nodes:
                continue
            generate_worker(host, user, prefix, host_shortname, container,
                            nodes)
            nodes += 1
    print('    ],')

def generate_controller(config, user, clustered_db):
    host = config['name']
    host_container = "ovn-central-1" if clustered_db else "ovn-central"

    print('''
    "controller": {
        "controller_cidr": "%(host)s",
        "deployment_name": "ovn-controller-node",
        "install_method": "physical",
        "host_container": "%(host_container)s",
        "ovs_user": "%(user)s",
        "provider": {
            "credentials": [
                {
                    "host": "%(host)s",
                    "user": "%(user)s"
                }
            ],
            "type": "OvsSandboxProvider"
        },
        "type": "OvnSandboxControllerEngine"
    },''' % {'host': host, 'host_container': host_container, 'user': user})

def generate_worker(host, user, prefix, shortname, container_index, i):
    container = '{}-{}-{}'.format(prefix, shortname, container_index)
    print('''
        {
            "deployment_name": "ovn-farm-node-%(i)d",
            "install_method": "physical",
            "host_container": "%(container)s",
            "ovs_user": "%(user)s",
            "provider": {
                "credentials": [
                    {
                        "host": "%(host)s",
                        "user": "%(user)s"
                    }
                ],
                "type": "OvsSandboxProvider"
            },
            "type": "OvnSandboxFarmEngine"
        },''' % {'i': i, 'container': container, 'host': host, 'user': user})

def generate(input_file, clustered_db=True):
    with open(input_file, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)
        user = config.get('user', 'root')
        prefix = config.get('prefix', 'ovn-scale')
        max_containers = config.get('max-containers', 100)
        central_config = config['central-node']

        print('{')
        generate_controller(central_config, user,
                            clustered_db)
        generate_nodes(config['worker-nodes'], user, prefix, max_containers)
        print('    "type": "OvnMultihostEngine"')
        print('}')

def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        usage(sys.argv[0])
        sys.exit(1)

    clustered_db = False if (len(sys.argv) == 3 and
                             sys.argv[2].lower() == 'false') else True
    generate(sys.argv[1], clustered_db=clustered_db)

if __name__ == "__main__":
    main()
