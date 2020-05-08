# ovn-heater

Mega script to install/configure/run
[ovn-scale-test](https://github.com/ovn-org/ovn-scale-test) using
[browbeat](https://github.com/cloud-bulldozer/browbeat) on
a simulated cluster deployed with
[ovn-fake-multinode](https://github.com/ovn-org/ovn-fake-multinode).

**NOTE**: This script is designed to be used on test machines only. It
performs disruptive changes to the machines it is run on (e.g., create
insecure docker registries, cleanup existing docker containers).

# Prerequisites

## Physical topology

* TESTER: One machine to run the tests which needs to be able to SSH
  paswordless (preferably as `root`) to all other machines in the topology.
  Performs the following:
  - prepares the test enviroment: clone the specified versions of `OVS` and
    `OVN` and build the `ovn-fake-multinode` image to be used by the `OVN`
    nodes.
  - provisions all other `OVN` nodes with the required software packages
    and with the correct version of `ovn-fake-multinode` to run simulated/fake
    `OVN` chassis.
  - runs a docker registry where the `ovn-fake-multinode` (i.e.,
    `ovn/ovn-multi-node`) image is pushed and from which all other `OVN`
    nodes will pull the image.
  - runs `browbeat` which in turns runs `ovn-scale-test` (`rally-ovs`)
    scenarios.

* OVN-CENTRAL: One machine to run the `ovn-central` container(s) which
  run `ovn-northd` and the `Northbound` and `Southbound` databases.
* OVN-WORKER-NODE(s): Machines to run `ovn-netlab` container(s), each of
  which will simulate an `OVN` chassis.

The initial provisioning for all the nodes is performed by the `do.sh install`
command. The simulated `OVN` chassis containers and central container are
spawned by the `rally-ovs` scenarios themselves.

**NOTE**: `ovn-fake-multinode` assumes that all nodes (OVN-CENTRAL and
OVN-WORKER-NODEs) have an additional Ethernet interface connected to a
single L2 switch. This interface will be used for traffic to/from the
`Northbound` and `Southbound` databases and for tunneled traffic.

**NOTE**: there's no restriction regarding physical machine roles so for
debugging issues the TESTER, OVN-CENTRAL and OVN-WORKER-NODEs can all
be the same physical machine in which case there's no need for the secondary
Ethernet interface to exist.

## Sample physical topology:
* TESTER: `host01.mydomain.com`
* OVN-CENTRAL: `host02.mydomain.com`
* OVN-WORKER-NODEs:
  - `host03.mydomain.com`
  - `host04.mydomain.com`

OVN-CENTRAL and OVN-WORKER-NODEs all have Ethernet interface `eno1`
connected to a physical switch in a separate VLAN, as untagged interfaces.

## Minimal requirements on the TESTER node (tested on Fedora 32)

### Install required packages:
```
dnf install -y git ansible
```

### Make docker work with Fedora 32 (disable cgroup hierarchy):

```
dnf install -y grubby
grubby --update-kernel=ALL --args="systemd.unified_cgroup_hierarchy=0"
reboot
```

## Minimal requirements on the OVN-CENTRAL and OVN-WORKER-NODEs

### Make docker work with Fedora 32 (disable cgroup hierarchy):

```
dnf install -y grubby
grubby --update-kernel=ALL --args="systemd.unified_cgroup_hierarchy=0"
reboot
```

# Installation

## Get the code:

```
cd
git clone https://github.com/dceara/ovn-heater.git
```

## Write the physical deployment description yaml file:

A sample file written for the deployment described above is available at
`physical-deployments/physical-deployment.yml`.

The file should contain the following mandatory sections and fields:
- `registry-node`: the hostname (or IP) of the node that will store the
  docker private registry. In usual cases this is should be the TESTER
  machine.
- `internal-iface`: the name of the Ethernet interface used by the underlay
  (DB and tunnel traffic). This can be overridden per node if needed.
- `central-node`:
  - `name`: the hostname (or IP) of the node that will run `ovn-central`
    (`ovn-northd` and databases).
- `worker-nodes`:
  - the list of worker node hostnames (or IPs). If needed, worker nodes can
    be further customized using the per-node optional fields described below.

Global optional fields:
- `user`: the username to be used when connecting from the tester node.
  Default: `root`.
- `prefix`: a string (no constraints) that will be used to prefix container
  names for all containers that run `OVN` fake chassis. For example,
  `prefix: ovn-test` will generate container names of the form
  `ovn-test-X-Y` where `X` is the unique part of the worker hostname and `Y`
  is the worker node local index. Default: `ovn-scale`.
- `max-containers`: the maximum number of containers allowed to run on one
  host. Default: 100.

In case some of the physical machines in the setup have different
capabilities (e.g, could host more containers, or use a different ethernet
interface), the following per-node fields can be used to customize the
deployment. Except for `fake-nodes` which is valid only in the context of
worker nodes, all others are valid both for the `central-node` and also for
`worker-nodes`:
- `user`: the username to be used when connecting from the tester node.
- `internal-iface`: the name of the Ethernet interface used for DB and
    tunnel traffic. This overrides the `internal-iface` global configuration.
- `fake-nodes`: the maximum number of containers allowed to run on this
  host. If not specified, the value of `max-containers` from the global
  section is used instead.

## Perform the script installation step:

This generates a `runtime` directory and a `runtime/hosts` ansible inventory
and installs all test components on all required nodes. Then it registers
the deployment with `rally-ovs`.

```
cd ~/ovn-heater
./do.sh install && ./do.sh rally-deploy
```

By default OVS/OVN binaries are built with
`CFLAGS="-g -O2 -fno-omit-frame-pointer"`.

For more aggressive optimizations we can use the `EXTRA_OPTIMIZE` env variable
when installing the runtime. E.g.:

```
cd ~/ovn-heater
EXTRA_OPTIMIZE=yes ./do.sh install && ./do.sh rally-deploy
```

## Perform a reinstallation (e.g., new OVS/OVN versions are needed):

```
cd ~/ovn-heater
rm -rf runtime
```

Assuming we need to run a private OVS branch from a fork and a different
OVN branch from another fork we can:

```
cd ~/ovn-heater
OVS_REPO=https://github.com/dceara/ovs OVS_BRANCH=tmp-branch OVN_REPO=https://github.com/dceara/ovn OVN_BRANCH=tmp-branch-2 ./do.sh install && ./do.sh rally-deploy
```

## Regenerate the ansible inventory and re-register the deployment:

If the physical topology has changed then update
`physical-deployment/physical-deployment.yml` to reflect the new physical
deployment.

Then deregister the old rally-ovs deployment:

```
cd ~/ovn-heater
./do.sh rally-undeploy
```

And generate the new ansible inventory and register the deployment:

```
cd ~/ovn-heater
./do.sh generate && ./do.sh rally-deploy
```

# Running tests:

## Scenario definitions

Browbeat and rally-ovs scenarios are defined in `browbeat-scenarios/`. YAML
files are browbeat scenarios which in turn will run the JSON rally-ovs
tests.

## Scenario execution

```
cd ~/ovn-heater
./do.sh browbeat-run <browbeat-scenario> <results-dir>
```

This executes `<browbeat-scenario>` on the registered deployment. Current
scenarios also cleanup the environment, i.e., remove all docker containers
from all physical nodes. **NOTE**: If the environment needs to be explictly
cleaned up, we can also execute before running the scenario:

```
cd ~/ovn-heater
./do.sh init
```

The results will be stored in `test_results/<results-dir>`. The results
consist of:
- a `config` file where remote urls and SHA/branch-name of all test components
  (browbeat, rally, ovn-scale-test, ovs, ovn) are stored.
- an `installer-log` where the ouptut of the `./do.sh install` command is
  stored.
- browbeat logs
- rally-ovs logs
- html reports
- a copy of the `hosts` ansible inventory used for the test.
- OVN docker container logs (i.e., ovn-northd, ovn-controller, ovs-vswitchd,
  ovsdb-server logs).
- physical nodes journal files.

## Example (run "scenario #2 - new node provisioning" for 2 nodes):

```
cd ~/ovn-heater
./do.sh browbeat-run browbeat-scenarios/switch-per-node-low-scale.yml test-low-scale
```

This will run `browbeat-scenarios/osh_workload_incremental.json` which will
perform two iterations, each of which:
- brings up a fake `OVN` node.
- configures a logical switch for the node and connects it to the
  `cluster-router`.
- configures a logical switch port ("mgmt port") and binds it to an OVS
  internal interface on the fake `OVN` node.
- configures a logical gateway router on the node and sets up NAT to allow
  communication to the outside.
- moves the OVS internal interface to a separate network namespace and
  configures its networking.
- waits until ping from the new network namespace to the "outside" works
  through the local gateway router.

Results will be stored in `~ovn-heater/test_results/test-low-scale/`:
- `config`: remote urls and SHA/branch-names of components used by the test.
- `hosts`: the autogenerated ansible host inventory.
- `logs`: the OVN container logs and journal files from each physical node.
- `20200212-083337.report` (timestamp will change): browbeat report overview.
- `20200212-083337/rally/plugin-workloads/all-rally-run-0.html`: HTML report
  of the test run.
- `20200212-083337/rally/plugin-workloads/switch-per-node-workload/20200212-083337-browbeat-switch-per-node-workload-1-1-iteration-0.log`:
  rally-ovs logs of the test run.

## Scenario execution with DBs in standalone mode

By default tests configure NB/SB ovsdb-servers to run in clustered mode
(RAFT). If instead tests should be run in standalone mode then the browbeat
scenarios must be adapted by adding the `ovn_cluster_db = False` configuration
and the deployment should be regenerated as follows:

```
cd ~/ovn-heater
./do.sh rally-undeploy
CLUSTERED_DB=False ./do.sh generate
./do.sh rally-deploy

echo '        ovn_cluster_db: "False"' >> browbeat-scenarios/switch-per-node-low-scale.yml
./do.sh browbeat-run <browbeat-scenario> <results-dir>
```
