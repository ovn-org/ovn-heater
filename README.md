# ovn-heater

Mega script to install/configure/run
a simulated OVN cluster deployed with
[ovn-fake-multinode](https://github.com/ovn-org/ovn-fake-multinode).

**NOTE**: This script is designed to be used on test machines only. It
performs disruptive changes to the machines it is run on (e.g., create
insecure docker registries, cleanup existing docker containers).

# Prerequisites

## Physical topology

* ORCHESTRATOR: One machine that needs to be able to SSH paswordless
  (preferably as `root`) to all other machines in the topology. Performs the
  following:
  - prepares the test enviroment: clone the specified versions of `OVS` and
    `OVN` and build the `ovn-fake-multinode` image to be used by the `OVN`
    nodes.
  - provisions all other `OVN` nodes with the required software packages
    and with the correct version of `ovn-fake-multinode` to run simulated/fake
    `OVN` chassis.
  - runs a docker registry where the `ovn-fake-multinode` (i.e.,
    `ovn/ovn-multi-node`) and `ovn-tester` images are pushed and from which all
    other `OVN` nodes will pull the image.

* TESTER: One machine to run the `ovn-tester` container which runs the python
  ovn-tester code. Like the ORCHESTRATOR, the TESTER also needs to be able to
  SSH passwordless to all other machines in the topology.
* OVN-CENTRAL: One machine to run the `ovn-central` container(s) which
  run `ovn-northd` and the `Northbound` and `Southbound` databases.
* OVN-WORKER-NODE(s): Machines to run `ovn-netlab` container(s), each of
  which will simulate an `OVN` chassis.

The initial provisioning for all the nodes is performed by the `do.sh install`
command. The simulated `OVN` chassis containers and central container are
spawned by the test scripts in `ovn-tester/`.

**NOTE**: `ovn-fake-multinode` assumes that all nodes (OVN-CENTRAL and
OVN-WORKER-NODEs) have an additional Ethernet interface connected to a
single L2 switch. This interface will be used for traffic to/from the
`Northbound` and `Southbound` databases and for tunneled traffic.

**NOTE**: there's no restriction regarding physical machine roles so for
debugging issues the ORCHESTRATOR, TESTER, OVN-CENTRAL and OVN-WORKER-NODEs can
all be the same physical machine in which case there's no need for the
secondary Ethernet interface to exist.

## Sample physical topology:
* ORCHESTRATOR: `host01.mydomain.com`
* OVN-CENTRAL: `host02.mydomain.com`
* OVN-WORKER-NODEs:
  - `host03.mydomain.com`
  - `host04.mydomain.com`

OVN-CENTRAL and OVN-WORKER-NODEs all have Ethernet interface `eno1`
connected to a physical switch in a separate VLAN, as untagged interfaces.

**NOTE**: The hostnames specified in the physical topology are used by both
the ORCHESTRATOR and by the `ovn-tester` container running in the TESTER.
Therefore, the values need to be resolvable by both of these entities and
need to resolve to the same host. `localhost` will not work since this does
not resolve to a unique host.

## Minimal requirements on the ORCHESTRATOR node (tested on Fedora 32)

### Install required packages:

#### RPM-based
```
dnf install -y git ansible \
    ansible-collection-ansible-posix ansible-collection-ansible-utils
```

#### DEB-based
```
sudo apt -y install ansible
```

## Minimal requirements on the TESTER node (tested on Fedora 36)

### Make docker work with nested containers (disable cgroup hierarchy):

#### RPM-based Fedora 32+
```
dnf install -y grubby
grubby --update-kernel=ALL --args="systemd.unified_cgroup_hierarchy=0"
reboot
```

#### DEB-based
Edit /etc/default/grub and add `systemd.unified_cgroup_hierarchy=0` at the
end of the `GRUB_CMDLINE_LINUX_DEFAULT` variable.

```
sudo update-grub
sudo reboot
````

## Minimal requirements on the OVN-CENTRAL and OVN-WORKER-NODEs

### Make docker work with nested containers (disable cgroup hierarchy):

#### RPM-based Fedora 32+
```
dnf install -y grubby
grubby --update-kernel=ALL --args="systemd.unified_cgroup_hierarchy=0"
reboot
```

#### DEB-based
Edit /etc/default/grub and add `systemd.unified_cgroup_hierarchy=0` at the
end of the `GRUB_CMDLINE_LINUX_DEFAULT` variable.

```
sudo update-grub
sudo reboot
````

# Installation

## Ensure all nodes can be accessed passwordless via SSH

On Fedora 33 RSA keys are not considered secure enough, an alternative is:

```
ssh-keygen -t ed25519 -a 64 -N '' -f ~/.ssh/id_ed25519
```

Then append `~/.ssh/id_ed25519.pub` to `~/.ssh/authorized_keys` on all physical
nodes.

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
  docker private registry. In usual cases this is should be the ORCHESTRATOR
  machine.
- `internal-iface`: the name of the Ethernet interface used by the underlay
  (DB and tunnel traffic). This can be overridden per node if needed.
- `tester-node`:
  - `name`: the hostname (or IP) of the node that will run `ovn-tester` (the
    python code that performs the actual test)
  - `ssh_key`: An ssh private key to install in the TESTER that can be used
    to communicate with the other machines in the cluster.
    Default: `~/.ssh/id_rsa`
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

## Perform the installation step:

This must be run on the TESTER node and generates a `runtime` directory, a
`runtime/hosts` ansible inventory and installs all test components on
all other nodes.

```
cd ~/ovn-heater
./do.sh install
```

This step will:
- clone OVS, OVN and ovn-fake-multinode upstream master branches in the
  `runtime` directory.
- build the `ovn/ovn-multi-node` container image which will be used by the
  fake nodes spawned during the tests.  OVS/OVN binaries are built with
  `CFLAGS="-g -O2 -fno-omit-frame-pointer"`.  More aggressive optimizations
  can be enabled by setting the `EXTRA_OPTIMIZE=yes` environment variable
  (`EXTRA_OPTIMIZE=yes ./do.sh install`).
- push the container image to all other nodes and prepare the test environment.
- build the `ovn/ovn-tester` container image which will be used by the TESTER
  node to run the ovn-tester application.
- push the `ovn/ovn-tester` container image to the TESTER node.

To override the OVS, OVN or ovn-fake-multinode repos/branches use the
following environment variables:
- OVS_REPO, OVS_BRANCH
- OVN_REPO, OVN_BRANCH
- OVN_FAKE_MULTINODE_REPO, OVN_FAKE_MULTINODE_BRANCH

For example, installing components with custom OVS/OVN code:

```
cd ~/ovn-heater
OVS_REPO=https://github.com/dceara/ovs OVS_BRANCH=tmp-branch OVN_REPO=https://github.com/dceara/ovn OVN_BRANCH=tmp-branch-2 ./do.sh install
```

NOTE: Because the installation step is responsible for deploying the ovn-tester
container to the TESTER, this means that if any changes are made to the
ovn-tester application, the installation step must be re-run.

## Perform a reinstallation (e.g., new OVS/OVN versions are needed):

For OVS, OVN or ovn-fake-multinode code changes to be reflected the
`ovn/ovn-multi-node` container image must be rebuilt.  The simplest
way to achieve that is to remove the current `runtime` directory and
reinstall:

```
cd ~/ovn-heater
rm -rf runtime
OVS_REPO=... OVS_BRANCH=... OVN_REPO=... OVN_BRANCH=... ./do.sh install
```

## Perform a reinstallation (e.g., install OVS/OVN from rpm packages):

```
cd ~/ovn-heater
rm -rf runtime
```

Run the installation with rpm packages parameters specified:

```
cd ~/ovn-heater
RPM_SELINUX=$rpm_url_openvswitch-selinux-extra-policy RPM_OVS=$rpm_url_openvswitch RPM_OVN_COMMON=$rpm_url_ovn RPM_OVN_HOST=$rpm_url_ovn-host RPM_OVN_CENTRAL=$rpm_url_ovn-central ./do.sh install
```

## Regenerate the ansible inventory:

If the physical topology has changed then update
`physical-deployment/physical-deployment.yml` to reflect the new physical
deployment.

Then generate the new ansible inventory:

```
cd ~/ovn-heater
./do.sh generate
```

# Running tests:

## Scenario definitions

Scenarios are defined in `ovn-tester/ovn_tester.py` and are configurable
through YAML files.  Sample scenario configurations are available in
`test-scenarios/*.yml`.

## Scenario execution

```
cd ~/ovn-heater
./do.sh run <scenario> <results-dir>
```

This executes `<scenario>` on the physical deployment (specifically on the
`ovn-tester` container on the TESTER). Current scenarios also cleanup the
environment, i.e., remove all docker containers from all physical nodes.
**NOTE**: If the environment needs to be explictly cleaned up, we can also
execute before running the scenario:

```
cd ~/ovn-heater
./do.sh init
```

The results will be stored in `test_results/<results-dir>`. The results
consist of:
- a `config` file where remote urls and SHA/branch-name of all test components
  (ovn-fake-multinode, ovs, ovn) are stored.
- an `installer-log` where the ouptut of the `./do.sh install` command is
  stored.
- html reports
- a copy of the `hosts` ansible inventory used for the test.
- OVN docker container logs (i.e., ovn-northd, ovn-controller, ovs-vswitchd,
  ovsdb-server logs).
- physical nodes journal files.
- perf sampling results if enabled

## Example: run 20 nodes "density light"

```
cd ~/ovn-heater
./do.sh run test-scenarios/ocp-20-density-light.yml test-20-density-light
```

This test consists of two stages:
- bring up a base cluster having 20 worker nodes (`n_workers`) and 10 simulated
  pods/vms (`n_pods_per_node`) on each of the nodes.
- provision 4000 initial pods (`n_startup`) distributed across the 20 workers.
- provision the remaining 1000 pods (up to `n_pods`) and measure the time it
  takes for each of them to become reachable.

Results will be stored in `~ovn-heater/test_results/test-20-density-light*/`:
- `config`: remote urls and SHA/branch-names of components used by the test.
- `hosts`: the autogenerated ansible host inventory.
- `logs`: the OVN container logs and journal files from each physical node.
- `*html`: the html reports for each of the scenarios run.

## Example: run 20 nodes "density heavy"

```
cd ~/ovn-heater
./do.sh run test-scenarios/ocp-20-density-heavy.yml test-20-density-heavy
```

This test consists of two stages:
- bring up a base cluster having 20 worker nodes (`n_workers`) and 10 simulated
  pods/vms (`n_pods_per_node`) on each of the nodes.
- provision 4000 initial pods (`n_startup`) distributed across the 20 workers.
- for every other pod (`pods_vip_ratio`), provision a load balancer VIP using
  the pod as backend.
- provision the remaining 1000 pods (up to `n_pods`) and 500 VIPs and measure
   the time it takes for each of them to become reachable.

Results will be stored in `~ovn-heater/test_results/test-20-density-heavy*/`:
- `config`: remote urls and SHA/branch-names of components used by the test.
- `hosts`: the autogenerated ansible host inventory.
- `logs`: the OVN container logs and journal files from each physical node.
- `*html`: the html reports for each of the scenarios run.

## Example: run 20 nodes "cluster density"

```
cd ~/ovn-heater
./do.sh run test-scenarios/ocp-20-cluster-density.yml test-20-cluster-density
```

This test consists of two stages:
- bring up a base cluster having 20 worker nodes (`n_workers`) and 10 simulated
  pods/vms (`n_pods_per_node`) on each of the nodes.
- run 500 iterations (`n_runs`) each of which:
  - provisions 6 short-lived pods (removed at the end of the iteration)
  - provisions 4 long-lived pods (survive the end of the iteration)
  - creates a VIP with 2 backends (2 of the long-lived pods)
  - creates two VIPs with one backend each (the remaining 2 long-lived pods)
  - for the last 100 iterations (`n_runs` - `n_startup`) measure the time it
    takes for the pods to become reachable.

Results will be stored in `~ovn-heater/test_results/test-20-cluster-density*/`:
- `config`: remote urls and SHA/branch-names of components used by the test.
- `hosts`: the autogenerated ansible host inventory.
- `logs`: the OVN container logs and journal files from each physical node.
- `*html`: the html reports for each of the scenarios run.

## Scenario execution with DBs in standalone mode

By default tests configure NB/SB ovsdb-servers to run in clustered mode
(RAFT). If instead tests should be run in standalone mode then the test
scenarios must be adapted by setting `clustered_db: false` in the `cluster`
section of the test scenario YAML file.

## Scenario execution with ovsdb-etcd in standalone node

 This test requires ovn-fake-multinode, etcd and ovsdb-etcd

to build and run with ETCD

```
USE_OVSDB_ETCD=yes ./do.sh install

cd ~/ovn-heater
./do.sh run test-scenarios/ovn-etcd-low-scale.yml etcd-test-low-scale
```

The following fields are important for ovn-fake-node to detect and run ovsdb-etcd
```
  enable_ssl: False
  use_ovsdb_etcd: true
```

# Contributing to ovn-heater

Please check out our [contributing guidelines](./CONTRIBUTING.md) for
instructions about contributing patches to ovn-heater.  Please open
[GitHub issues](https://github.com/dceara/ovn-heater/issues) for
reporting any potential bugs or for requesting new ovn-heater
features.
