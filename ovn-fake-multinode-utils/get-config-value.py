#!/usr/bin/env python3
import argparse
import yaml


def parser_setup(parser):
    group = parser.add_argument_group()
    group.add_argument(
        "config",
        metavar="CONFIG_FILE",
        help="Configuration file",
    )
    group.add_argument(
        "section",
        metavar="SECTION",
        help="Configuration file section",
    )
    group.add_argument(
        "variable", metavar="VAR", help="Configuration variable to retrieve"
    )
    group.add_argument(
        "--default", help="Default value if VAR is not in CONFIG"
    )


def get_config_value(args):
    with open(args.config, 'r') as config_file:
        parsed = yaml.safe_load(config_file)

    try:
        return parsed[args.section][args.variable]
    except KeyError:
        if args.default:
            return args.default
        raise


def main():
    parser = argparse.ArgumentParser(description="Read YAML config value")
    parser_setup(parser)
    args = parser.parse_args()
    val = get_config_value(args)
    print(val, end='')


if __name__ == "__main__":
    main()
