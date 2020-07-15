#!/usr/bin/python3

from __future__ import print_function

import yaml
import sys

def usage(name):
    print("""
{} DEPLOYMENT
where DEPLOYMENT is the YAML file defining the deployment.
""".format(name), file=sys.stderr)

def generate(input_file):
    with open(input_file, 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)

        print('''
[registries.search]
registries = ['registry.access.redhat.com', 'registry.redhat.io']
[registries.block]
registries = []
            ''')
        print('''
[registries.insecure]
registries = ['{}:5000','localhost:5000']
            '''.format(config['registry-node']))

def main():
    if len(sys.argv) != 2:
        usage(sys.argv[0])
        sys.exit(1)

    generate(sys.argv[1])

if __name__ == "__main__":
    main()
