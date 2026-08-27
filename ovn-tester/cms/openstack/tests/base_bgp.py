import logging
import time
import ovn_stats
import ipaddress

from dataclasses import dataclass
from typing import List

from ovn_ext_cmd import ExtCmd
from ovn_context import Context
from ovn_workload import ChassisNode
from io import StringIO

from cms.openstack import OpenStackCloud, ExternalNetworkSpec

log = logging.getLogger(__name__)


@dataclass
class BaseBgpConfig:
    n_projects: int = 1
    n_chassis_per_gw_lrp: int = 3
    n_vms_per_project: int = 10
    n_routes_per_vrf: int = 100
    n_advertised_routes: int = 100
    batch_size: int = 0
    route_timeout_s: int = 30


class BaseBgp(ExtCmd):
    def __init__(self, config, cluster, global_cfg):
        super().__init__(config, cluster)
        test_config = config.get("base_bgp")
        self.config = BaseBgpConfig(**test_config)

    def inject_vrf_routes(self, hosting, nsenter, vrf_dev, nexthop, vrf_id):
        hosting.run(
            f"{nsenter} python3 /tmp/ovn_inject_routes.py "
            f"--vrf-dev {vrf_dev} --nexthop {nexthop} "
            f"--table {vrf_id} --n-routes {self.config.n_routes_per_vrf} "
            f"--vrf-id {vrf_id}"
        )

    def wait_for_learned_routes(self, ovn, expected, timeout=30):
        deadline = time.time() + timeout
        polls = 0
        while True:
            learned = ovn.sbctl.learned_routes()
            polls += 1
            if polls % 10 == 0 or learned >= expected:
                log.info(
                    f"Total learned routes {learned} vs expected {expected}"
                )
            if learned >= expected:
                return learned
            if time.time() > deadline:
                log.warning(
                    f"Timed out waiting for learned routes: "
                    f"{learned}/{expected} after {timeout}s"
                )
                return learned
            time.sleep(0.1)

    def wait_for_vrf_routes(
        self, chassis_list, vrf_name, expected, timeout=30
    ):
        deadline = time.time() + timeout
        while True:
            max_count = 0
            for chassis in chassis_list:
                stdout = StringIO()
                chassis.run(
                    f"ip route show vrf {vrf_name} | cat",
                    stdout=stdout,
                )
                count = len(stdout.getvalue().strip().splitlines())
                max_count = max(max_count, count)

            log.info(
                f"VRF {vrf_name} routes: {max_count} vs expected {expected}"
            )
            if max_count >= expected:
                return max_count
            if time.time() > deadline:
                log.warning(
                    f"Timed out waiting for VRF routes: "
                    f"{max_count}/{expected} after {timeout}s"
                )
                return max_count
            time.sleep(1)

    def find_vrf_host(self, ovn, nsenter, vrf_dev, timeout=30):
        """Find which chassis is hosting the given VRF device.

        Returns:
            The hosting chassis, or None if not found within timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for chassis in ovn.worker_nodes:
                try:
                    chassis.run(
                        f"{nsenter} test -d /sys/class/net/{vrf_dev}",
                        raise_on_error=True,
                    )
                    return chassis
                except Exception:
                    pass
            time.sleep(1)
        return None

    @ovn_stats.timeit
    def run_batch_advertise(self, ovn, start_idx, end_idx):
        # Find VRF hosts and snapshot baseline route counts BEFORE adding.
        vrf_info = {}
        for vrf_id, project in enumerate(
            ovn.projects[start_idx:end_idx], start=start_idx + 1
        ):
            if not project.gw_port:
                continue

            vrf_dev = f"ovnvrf{vrf_id}"
            vrf_hosts = []
            for chassis in ovn.worker_nodes:
                try:
                    chassis.run(
                        f"test -d /sys/class/net/{vrf_dev}",
                        raise_on_error=True,
                    )
                    vrf_hosts.append(chassis)
                except Exception:
                    pass

            if not vrf_hosts:
                log.warning(f"{vrf_dev} not found on any chassis")
                continue

            stdout = StringIO()
            vrf_hosts[0].run(
                f"ip route show vrf {vrf_dev} | cat",
                stdout=stdout,
            )
            baseline = len(stdout.getvalue().strip().splitlines())
            vrf_info[vrf_id] = (vrf_dev, vrf_hosts, baseline)
            log.info(f"{vrf_dev} baseline: {baseline} routes")

        # Add static routes to NB.
        for vrf_id, project in enumerate(
            ovn.projects[start_idx:end_idx], start=start_idx + 1
        ):
            if not project.gw_port:
                continue

            base_ip = ipaddress.IPv4Address((40 << 24) | (vrf_id << 8))

            for r in range(self.config.n_advertised_routes):
                dst = f"{base_ip + r}/32"
                gw = str(project.int_net.gateway)
                ovn.nbctl.idl.lr_route_add(
                    project.router.uuid, dst, gw
                ).execute(check_error=True)

            log.info(
                f"Added {self.config.n_advertised_routes} NB routes "
                f"to router {project.router.name}"
            )

        # Wait for advertised routes to appear.
        for vrf_id, (vrf_dev, vrf_hosts, baseline) in vrf_info.items():
            self.wait_for_vrf_routes(
                vrf_hosts,
                vrf_dev,
                baseline + self.config.n_advertised_routes,
                timeout=self.config.route_timeout_s,
            )

    @ovn_stats.timeit
    def run_batch_learn(self, ovn, nsenter, start_idx, end_idx, ext_net):
        # Create projects and VMs for this batch.
        for _ in range(end_idx - start_idx):
            ovn.new_project(ext_net=ext_net)

        for project in ovn.projects[start_idx:end_idx]:
            for index in range(self.config.n_vms_per_project):
                ovn.add_vm_to_project(project, f"{project.uuid[:6]}-{index}")

        # Enable dynamic routing for this batch.
        for vrf_id, project in enumerate(
            ovn.projects[start_idx:end_idx], start=start_idx + 1
        ):
            ovn.nbctl.lr_set_options(
                project.router,
                {
                    "dynamic-routing": "true",
                    "dynamic-routing-vrf-id": str(vrf_id),
                    "dynamic-routing-redistribute": "static,connected",
                },
            )
            if project.gw_port:
                ovn.nbctl.lr_port_set_options(
                    project.gw_port,
                    {
                        "dynamic-routing-maintain-vrf": "true",
                    },
                )
            log.info(
                f"Enabled dynamic routing on router "
                f"{project.router.name} vrf-id={vrf_id}"
            )

        # Inject routes into VRFs for this batch.
        for vrf_id, project in enumerate(
            ovn.projects[start_idx:end_idx], start=start_idx + 1
        ):
            if not project.gw_port:
                continue

            vrf_dev = f"ovnvrf{vrf_id}"
            nexthop = str(
                ipaddress.IPv4Address((172 << 24) | (vrf_id << 8) | 1)
            )

            hosting = self.find_vrf_host(ovn, nsenter, vrf_dev, timeout=30)
            if not hosting:
                log.warning(f"{vrf_dev} not found on any chassis")
                continue

            self.inject_vrf_routes(hosting, nsenter, vrf_dev, nexthop, vrf_id)
            log.info(
                f"Injected {self.config.n_routes_per_vrf} routes "
                f"into {vrf_dev} on {hosting.container}"
            )

        # Wait for cumulative learned routes.
        # Count only projects that actually had routes injected (have gw_port).
        injected_count = sum(
            1 for project in ovn.projects[start_idx:end_idx] if project.gw_port
        )
        expected = injected_count * self.config.n_routes_per_vrf
        self.wait_for_learned_routes(
            ovn, expected, timeout=self.config.route_timeout_s
        )

    def run(self, clouds: List[OpenStackCloud], global_cfg):
        # Phase 1: Standard OpenStack bringup.
        with Context(clouds, "bgp_bringup", len(clouds)) as ctx:
            for i in ctx:
                ovn = clouds[i]
                worker_count = len(ovn.worker_nodes)
                for j in range(worker_count):
                    worker_node: ChassisNode = ovn.worker_nodes[j]
                    log.info(
                        f"Provisioning {worker_node.__class__.__name__} "
                        f"({j + 1}/{worker_count})"
                    )
                    worker_node.provision(ovn)

        # Test BGP on all clouds.
        for cloud_idx, ovn in enumerate(clouds):
            batch_size = self.config.batch_size or self.config.n_projects
            n_batches = (self.config.n_projects + batch_size - 1) // batch_size

            ext_net = ExternalNetworkSpec(
                neutron_net=ovn.new_external_network(),
                num_gw_nodes=self.config.n_chassis_per_gw_lrp,
            )

            with Context(
                [ovn], f"bgp_batch_cloud_{cloud_idx}", n_batches
            ) as ctx:
                for batch_idx in ctx:
                    start_idx = batch_idx * batch_size
                    end_idx = min(
                        start_idx + batch_size, self.config.n_projects
                    )
                    nsenter = (
                        "nsenter -t $(cat /run/ovn/ovn-controller.pid) -n"
                    )

                    self.run_batch_learn(
                        ovn, nsenter, start_idx, end_idx, ext_net
                    )

                    log.info(
                        f"Cloud {cloud_idx} batch "
                        f"{batch_idx + 1}/{n_batches}: "
                        f"projects {start_idx + 1}-{end_idx} complete"
                    )

            with Context(
                [ovn], f"bgp_advertise_cloud_{cloud_idx}", n_batches
            ) as ctx:
                for batch_idx in ctx:
                    start_idx = batch_idx * batch_size
                    end_idx = min(
                        start_idx + batch_size, self.config.n_projects
                    )
                    nsenter = (
                        "nsenter -t $(cat /run/ovn/ovn-controller.pid) -n"
                    )

                    self.run_batch_advertise(ovn, start_idx, end_idx)

                    log.info(
                        f"Cloud {cloud_idx} advertise batch "
                        f"{batch_idx + 1}/{n_batches}: "
                        f"projects {start_idx + 1}-{end_idx} complete"
                    )
