import logging
import random
import uuid

from dataclasses import dataclass
from itertools import cycle
from typing import List, Optional, Dict

import netaddr

from randmac import RandMac

from ovn_sandbox import PhysicalNode
from ovn_utils import (
    DualStackSubnet,
    LSwitch,
    LSPort,
    PortGroup,
    LRouter,
    LRPort,
    DualStackIP,
)
from ovn_workload import ChassisNode, Cluster

log = logging.getLogger(__name__)

OVN_HEATER_CMS_PLUGIN = 'OpenStackCloud'


@dataclass
class NeutronNetwork:
    """Group of OVN objects that logically belong to same Neutron network."""

    network: LSwitch
    ports: Dict[str, LSPort]
    name: str
    security_group: Optional[PortGroup] = None

    def __post_init__(self):
        self._ip_pool = self.network.cidr.iter_hosts()
        self.gateway = self.next_host_ip()

    def next_host_ip(self) -> netaddr.IPAddress:
        """Return next available host IP in this network's range."""
        return next(self._ip_pool)


@dataclass
class ExternalNetworkSpec:
    """Information required to connect external network to Project's router.
    :param neutron_net: Object of already provisioned NeutronNetwork
    :param num_gw_nodes: Number of Chassis to use as gateways for this
                         network. Note that the number can't be greater than 5
                         and can't be greater than total number of Chassis
                         available in the system.
    """

    neutron_net: NeutronNetwork
    num_gw_nodes: int


class Project:
    """Represent network components of an OpenStack Project aka. Tenant."""

    def __init__(
        self,
        int_net: NeutronNetwork = None,
        ext_net: NeutronNetwork = None,
        router: Optional[LRouter] = None,
    ):
        self.int_net = int_net
        self.ext_net = ext_net
        self.router = router
        self.vm_ports: List[LSPort] = []
        self._id = str(uuid.uuid4())

    @property
    def uuid(self) -> str:
        """Return arbitrary UUID assigned to this Openstack Project."""
        return self._id


class OpenStackCloud(Cluster):
    """Representation of Openstack cloud/deployment."""

    MAX_GW_PER_ROUTER = 5

    def __init__(self, cluster_cfg, central, brex_cfg, az):
        super().__init__(cluster_cfg, central, brex_cfg, az)
        self.router = None
        self._projects: List[Project] = []

        self._int_net_base = DualStackSubnet(cluster_cfg.node_net)
        self._int_net_offset = 0

        self._ext_net_pool_index = 0

    def add_workers(self, workers: List[PhysicalNode]):
        """Expand parent method to update cycled list."""
        super().add_workers(workers)
        self._compute_nodes = cycle(
            [
                node
                for node in self.worker_nodes
                if isinstance(node, ComputeNode)
            ]
        )

    def add_cluster_worker_nodes(self, workers: List[PhysicalNode]):
        """Add OpenStack flavored worker nodes as per configuration."""
        # Allocate worker IPs after central and relay IPs.
        mgmt_ip = (
            self.cluster_cfg.node_net.ip
            + 2
            + len(self.central_nodes)
            + len(self.relay_nodes)
        )

        protocol = "ssl" if self.cluster_cfg.enable_ssl else "tcp"
        # XXX: To facilitate network nodes we'd have to make bigger change
        #      to introduce `n_compute_nodes` and `n_network_nodes` into the
        #      ClusterConfig. I tested it and sems like additional changes
        #      would be required in `ovn-fake-multinode` repo to handle worker
        #      creation in similar way as it's done for `n_workers` and
        #      `n_relays` right now.
        network_nodes = []
        compute_nodes = [
            ComputeNode(
                workers[i % len(workers)],
                f"ovn-scale-{i}",
                mgmt_ip + i,
                protocol,
                True,
            )
            for i in range(self.cluster_cfg.n_workers)
        ]
        self.add_workers(network_nodes + compute_nodes)

    @property
    def projects(self) -> List[Project]:
        """Return list of Projects that exist in Openstack deployment."""
        return self._projects

    def next_external_ip(self) -> DualStackIP:
        """Return next available IP address from configured 'external_net'."""
        self._ext_net_pool_index += 1
        return self.cluster_cfg.external_net.forward(self._ext_net_pool_index)

    def next_int_net(self):
        """Return next subnet for project/tenant internal network.

        This method takes subnet configured as 'node_net' in cluster config as
        base and each call returns next subnet in line.
        """
        network = DualStackSubnet.next(
            self._int_net_base, self._int_net_offset
        )
        self._int_net_offset += 1

        return network

    def select_worker_for_port(self) -> 'ComputeNode':
        """Cyclically return compute nodes available in cluster."""
        return next(self._compute_nodes)

    def new_project(self, ext_net: Optional[ExternalNetworkSpec]) -> Project:
        """Create new Project/Tenant in the Openstack cloud.

        Following things are provisioned for the Project:
          * Project router
          * Internal network

        If 'ext_net' is provided, this external network will be connected
        to the project's router and default gateway route will be configured
        through this network.

        :param ext_net: External network to be connected to the Project.
        :return: New Project object that is automatically also added
                 to 'self.projects' list.
        """
        project = Project()
        gateway_port = None

        project.router = self._create_project_router(
            f"provider-router-{project.uuid}"
        )
        if ext_net is not None:
            gateway_port = self.connect_external_network_to_project(
                project, ext_net
            )

        self.add_internal_network_to_project(project, gateway_port)
        self._projects.append(project)
        return project

    def new_external_network(
        self, provider_network: str = "physnet1"
    ) -> NeutronNetwork:
        """Provision external network that can be added as gw to Projects.

        :param provider_network: Name of the provider network used when
                                 creating provider port.
        :return: NeutronNetwork object representing external network.
        """
        ext_net_uuid = uuid.uuid4()
        ext_net_name = f"ext_net_{ext_net_uuid}"
        ext_net = self._create_project_net(f"ext_net_{ext_net_uuid}", 1500)
        ext_net_port = self._add_metadata_port(ext_net, str(ext_net_uuid))
        provider_port = self._add_provider_network_port(
            ext_net, provider_network
        )
        return NeutronNetwork(
            network=ext_net,
            ports={
                ext_net_port.uuid: ext_net_port,
                provider_port.uuid: provider_port,
            },
            name=ext_net_name,
        )

    def connect_external_network_to_project(
        self,
        project: Project,
        external_network: ExternalNetworkSpec,
    ) -> LRPort:
        """Connect external net to Project that adds external connectivity.
        This method takes existing Neutron network, connects it to the
        Project's router and configures default route on the router to go
        through this network.

        Gateway Chassis will be picked at random from available Chassis
        nodes based on the number of gateways specified in 'external_network'
        specification.

        :param project: Project to which external network will be added
        :param external_network: External network that will be connected.
        :return: Logical Router Port that connects to the external network.
        """

        ls_port, lr_port = self._add_router_port_external_gw(
            external_network.neutron_net, project.router
        )
        self.external_port = lr_port
        gw_net = DualStackSubnet(netaddr.IPNetwork("0.0.0.0/0"))

        # XXX: ovsdbapp does not allow setting external IDs to static route
        # XXX: Setting 'policy' to "" throws "constraint violation" error in
        #      logs because ovsdbapp does not allow not specifying policy.
        #      However, the route itself is created successfully with no
        #      policy, the same way Neutron does it.
        self.nbctl.route_add(project.router, gw_net, lr_port.ip, "")

        gw_nodes = self._get_gateway_chassis(external_network.num_gw_nodes)
        for index, chassis in enumerate(gw_nodes):
            self.nbctl.lr_port_set_gw_chassis(
                lr_port, chassis.container, index + 1
            )

        project.ext_net = external_network

        return lr_port

    def add_internal_network_to_project(
        self, project: Project, snat_port: Optional[LRPort] = None
    ) -> None:
        """Provision internal net to the Project for guest communication.

        If 'snat_port' is provided. A SNAT rule will be configured for traffic
        exiting this network.

        :param project: Project to which internal network will be added.
        :param snat_port: Gateway port that should SNAT outgoing traffic.
        :return: None
        """
        int_net_name = f"int_net_{project.uuid}"
        int_net = self._create_project_net(int_net_name, 1442)

        int_net_port = self._add_metadata_port(int_net, project.uuid)
        security_group = self._create_default_security_group()
        neutron_int_network = NeutronNetwork(
            network=int_net,
            ports={int_net_port.uuid: int_net_port},
            name=int_net_name,
            security_group=security_group,
        )

        self._add_network_subnet(network=neutron_int_network)
        self._assign_port_ips(neutron_int_network)
        project.int_net = neutron_int_network

        self._add_router_port_internal(
            neutron_int_network, project.router, project
        )

        snated_network = DualStackSubnet(int_net.cidr)
        if snat_port is not None:
            self.nbctl.nat_add(project.router, snat_port.ip, snated_network)

    def add_vm_to_project(self, project: Project, vm_name: str):
        compute = self.select_worker_for_port()
        vm_port = self._add_vm_port(
            project.int_net, project.uuid, compute, vm_name
        )
        compute.bind_port(vm_port)

        project.vm_ports.append(vm_port)

    def _get_gateway_chassis(self, count: int = 1) -> List[ChassisNode]:
        """Return list of Gateway Chassis with size defined by 'count'.

        Chassis are picked at random from all available 'worker_nodes' to
        attempt to evenly distribute gateway loads.

        Parameter 'count' can't be larger than number of all available gateways
        or larger than 5 which is hardcoded maximum of gateways per router in
        Neutron.
        """
        warn = ""
        worker_count = len(self.worker_nodes)
        if count > worker_count:
            count = worker_count
            warn += (
                f"{count} Gateway chassis requested but only "
                f"{worker_count} available.\n"
            )

        if count > self.MAX_GW_PER_ROUTER:
            count = self.MAX_GW_PER_ROUTER
            warn += (
                f"Maximum number of gateways per router "
                f"is {self.MAX_GW_PER_ROUTER}\n"
            )

        if warn:
            warn += f"Using only {count} Gateways per router."
            log.warning(warn)

        return random.sample(self.worker_nodes, count)

    def _create_project_net(self, net_name: str, mtu: int = 1500) -> LSwitch:
        """Create Logical Switch that represents neutron network.

        This method creates Logical Switch with same parameters as Neutron
        would.
        """
        switch_ext_ids = {
            "neutron:availability_zone_hints": "",
            "neutron:mtu": str(mtu),
            "neutron:network_name": net_name,
            "neutron:revision_number": "1",
        }
        switch_config = {
            "mcast_flood_unregistered": "false",
            "mcast_snoop": "false",
            "vlan-passthru": "false",
        }

        neutron_net_name = f"neutron-{uuid.uuid4()}"
        neutron_network = self.nbctl.ls_add(
            neutron_net_name,
            self.next_int_net(),
            ext_ids=switch_ext_ids,
            other_config=switch_config,
        )

        return neutron_network

    def _add_network_subnet(self, network: NeutronNetwork) -> None:
        """Create DHCP subnet for a network.

        Adding subnet to a network in Neutron results in DHCP_Options being
        created in OVN.
        """
        external_ids = {
            "neutron:revision_number": "0",
            "subnet_id": str(uuid.uuid4()),
        }
        static_routes = (
            f"{{169.254.169.254/32,{network.gateway}, "
            f"0.0.0.0/0,{network.gateway}}}"
        )
        options = {
            "classless_static_route": static_routes,
            "dns_server": "{1.1.1.1}",
            "lease_time": "43200",
            "mtu": "1442",
            "router": str(network.gateway),
            "server_id": str(network.gateway),
            "server_mac": str(RandMac()),
        }
        dhcp_options = self.nbctl.create_dhcp_options(
            str(network.network.cidr), ext_ids=external_ids
        )

        self.nbctl.dhcp_options_set_options(dhcp_options.uuid, options)

    def _assign_port_ips(self, network: NeutronNetwork) -> None:
        """Assign IPs to each LS port associated with the Network."""
        for uuid_, port in network.ports.items():
            if port.ip is None or port.ip == "unknown":
                updated_port = self.nbctl.ls_port_set_ipv4_address(
                    port, str(network.next_host_ip())
                )
                network.ports[uuid_] = updated_port
                # XXX: Unable to update external_ids

    def _add_vm_port(
        self,
        neutron_net: NeutronNetwork,
        project_id: str,
        chassis: 'ComputeNode',
        vm_name: str,
    ) -> LSPort:
        """Create port that will represent interface of a VM.

        :param neutron_net: Network in which the port will be created.
        :param project_id: ID of a project to which the VM belongs.
        :param chassis: Chassis on which the port will be provisioned.
        :param vm_name: VM name that's used to derive port name. Beware that
                        due to the port naming convention and limits on
                        interface name length, this name can't be longer than
                        12 characters.
        :return: LSPort representing VM's network interface
        """
        port_name = f"lp-{vm_name}"
        if len(port_name) > 15:
            raise RuntimeError(
                f"Maximum port name length is 15. Port {port_name} is too "
                f"long. Consider using shorter VM name."
            )
        port_addr = neutron_net.next_host_ip()
        net_addr: netaddr.IPNetwork = neutron_net.network.cidr
        port_ext_ids = {
            "neutron:cidrs": f"{port_addr}/{net_addr.prefixlen}",
            "neutron:device_id": str(uuid.uuid4()),
            "neutron:device_owner": "compute:nova",
            "neutron:network_name": neutron_net.name,
            "neutron:port_name": "",
            "neutron:project_id": project_id,
            "neutron:revision_number": "1",
            "neutron:security_group_ids": neutron_net.security_group.name,
            "neutron:subnet_pool_addr_scope4": "",
            "neutron:subnet_pool_addr_scope6": "",
        }
        port_options = (
            f"requested-chassis={chassis.container}"
        )
        ls_port = self.nbctl.ls_port_add(
            lswitch=neutron_net.network,
            name=port_name,
            mac=str(RandMac()),
            ip=DualStackIP(port_addr, net_addr.prefixlen, None, None),
            ext_ids=port_ext_ids,
            gw=DualStackIP(neutron_net.gateway, None, None, None),
            metadata=chassis,
        )
        self.nbctl.ls_port_set_set_options(ls_port, port_options)
        self.nbctl.ls_port_enable(ls_port)
        self.nbctl.port_group_add_ports(
            pg=neutron_net.security_group, lports=[ls_port]
        )

        return ls_port

    def _add_metadata_port(self, network: LSwitch, project_id: str) -> LSPort:
        """Create metadata port in LSwitch with Neutron's external IDs."""
        port_ext_ids = {
            "neutron:cidrs": "",
            "neutron:device_id": f"ovnmeta-{network.uuid}",
            "neutron:device_owner": "network:distributed",
            "neutron:network_name": network.name,
            "neutron:port_name": "",
            "neutron:project_id": project_id,
            "neutron:revision_number": "1",
            "neutron:security_group_ids": "",
            "neutron:subnet_pool_addr_scope4": "",
            "neutron:subnet_pool_addr_scope6": "",
        }

        port = self.nbctl.ls_port_add(
            lswitch=network,
            name=str(uuid.uuid4()),
            mac=str(RandMac()),
            ext_ids=port_ext_ids,
        )
        self.nbctl.ls_port_set_set_type(port, "localport")
        self.nbctl.ls_port_enable(port)

        return port

    def _add_provider_network_port(
        self, network: LSwitch, network_name: str
    ) -> LSPort:
        """Add port to Logical Switch that represents connection to ext. net
        :param network: Network (LSwitch) in which the port will be created.
        :param network_name: Name of the provider network (used when setting
                              provider port options)
        :return: LSPort representing provider port
        """
        options = (
            "mcast_flood=false mcast_flood_reports=true "
            f"network_name={network_name}"
        )
        port = self.nbctl.ls_port_add(
            lswitch=network,
            name=f"provnet-{uuid.uuid4()}",
            localnet=True,
        )
        self.nbctl.ls_port_set_set_type(port, "localnet")
        self.nbctl.ls_port_set_set_options(port, options)

        return port

    def _add_router_port_internal(
        self, neutron_net: NeutronNetwork, router: LRouter, project: Project
    ) -> (LSPort, LRPort):
        """Add a pair of ports that connect router to the internal network.

        The Router Port is automatically assigned network's gateway IP.

        :param neutron_net: Neutron network that represents internal network
        :param router: Router to which the network is connected
        :param project: Project to which 'neutron_net' belongs
        :return: The pair of Logical Switch Port and Logical Router Port
        """
        port_ip = DualStackIP(
            neutron_net.gateway, neutron_net.network.cidr.prefixlen, None, None
        )
        return self._add_router_port(
            neutron_net.network, router, port_ip, project.uuid, False
        )

    def _add_router_port_external_gw(
        self, neutron_net: NeutronNetwork, router: LRouter
    ) -> (LSPort, LRPort):
        """Add a pair of ports that connect router to the external network.

        The Router Port is assigned IP form external network's range.

        :param neutron_net: Neutron network that represents external network
        :param router: Router to which the network is connected
        :return: The pair of Logical Switch Port and Logical Router Port
        """
        port_ip = self.next_external_ip()
        return self._add_router_port(
            neutron_net.network, router, port_ip, "", True
        )

    def _add_router_port(
        self,
        network: LSwitch,
        router: LRouter,
        port_ip: DualStackIP,
        project_id: str = "",
        is_gw: bool = False,
    ) -> (LSPort, LRPort):
        """Add a pair of ports that connect Logical Router and Logical Switch.

        :param network: Logical Switch to which a Logical Switch Port is added
        :param router: Logical Router to which a Logical Router Port is added
        :param port_ip: IP assigned to the router port
        :param project_id: Optional Openstack Project ID that's associated with
                           the network.
        :param is_gw: Set to True if Router port is connected to external
                      network and should act as gateway.
        :return: The pair of Logical Switch Port and Logical Router Port
        """
        subnet_id = str(uuid.uuid4())
        port_name = uuid.uuid4()
        router_port_name = f"lrp-{port_name}"
        device_owner = "network:router_gateway" if is_gw else ""
        # XXX: neutron adds "neutron-" prefix to router name (which is UUID),
        #      but for the ports' external IDs we want to extract just the
        #      UUID.
        router_id = router.name.lstrip("neutron-")

        lsp_external_ids = {
            "neutron:cidrs": str(self.cluster_cfg.external_net.n4),
            "neutron:device_id": router_id,
            "neutron:device_owner": device_owner,
            "neutron:network_name": network.name,
            "neutron:port_name": "",
            "neutron:project_id": project_id,
            "neutron:revision_number": "1",
            "neutron:security_group_ids": "",
            "neutron:subnet_pool_addr_scope4": "",
            "neutron:subnet_pool_addr_scope6": "",
        }
        lrp_external_ids = {
            "neutron:network_name": network.name,
            "neutron:revision_number": "1",
            "neutron:router_name": router_id,
            "neutron:subnet_ids": subnet_id,
        }

        lr_port = self.nbctl.lr_port_add(
            router, router_port_name, str(RandMac()), port_ip, lrp_external_ids
        )

        ls_port = self.nbctl.ls_port_add(
            lswitch=network,
            name=str(port_name),
            router_port=lr_port,
            ext_ids=lsp_external_ids,
        )
        self.nbctl.ls_port_enable(ls_port)

        if is_gw:
            lsp_options = (
                "exclude-lb-vips-from-garp=true, "
                "nat-addresses=router, "
                f"router-port={router_port_name}"
            )
            self.nbctl.ls_port_set_set_options(ls_port, lsp_options)

        return ls_port, lr_port

    def _create_default_security_group(self) -> PortGroup:
        """Create default security group for the project.

        This method provisions Port Group and default ACL rules.
        """
        neutron_security_group = str(uuid.uuid4())
        pg_name = f"pg_{neutron_security_group}".replace("-", "_")
        pg_ext_ids = {"neutron:security_group_id": neutron_security_group}
        port_group = self.nbctl.port_group_create(pg_name, ext_ids=pg_ext_ids)

        in_rules = [
            f"inport == @{pg_name} && ip4",
            f"inport == @{pg_name} && ip6",
        ]
        out_rules = [
            f"outport == @{pg_name} && ip4 && ip4.src == $pg_{pg_name}_ip4",
            f"outport == @{pg_name} && ip6 && ip6.src == $pg_{pg_name}_ip6",
        ]

        for rule in in_rules:
            ext_ids = {"neutron:security_group_rule_id": str(uuid.uuid4())}
            self.nbctl.acl_add(
                name=port_group.name,
                direction="from-lport",
                priority=1002,
                entity="port-group",
                match=rule,
                verdict="allow-related",
                ext_ids=ext_ids,
            )

        for rule in out_rules:
            ext_ids = {"neutron:security_group_rule_id": str(uuid.uuid4())}
            self.nbctl.acl_add(
                name=port_group.name,
                direction="to-lport",
                priority=1002,
                entity="port-group",
                match=rule,
                verdict="allow-related",
                ext_ids=ext_ids,
            )

        return port_group

    def _create_project_router(self, name: str) -> LRouter:
        """Create Logical Router in the same way as Neutron would."""
        options = {
            "always_learn_from_arp_request": "false",
            "dynamic_neigh_routers": "true",
        }
        external_ids = {
            "neutron:availability_zone_hints": "",
            "neutron:gw_port_id": "",
            "neutron:revision_number": "1",
            "neutron:router_name": name,
        }
        router = self.nbctl.lr_add(f"neutron-{uuid.uuid4()}", external_ids)
        self.nbctl.lr_set_options(router, options)

        return router


class NetworkNode(ChassisNode):
    """Represent a network node, a node with OVS/OVN but no VMs."""

    def __init__(
        self,
        phys_node,
        container,
        mgmt_ip,
        protocol,
        is_gateway: bool = True,
    ):
        super().__init__(phys_node, container, mgmt_ip, protocol)
        self.is_gateway = is_gateway

    def configure(self, physical_net):
        pass

    def provision(self, cluster: OpenStackCloud) -> None:
        """Connect Chassis to OVN Central."""
        self.connect(cluster.get_relay_connection_string())


class ComputeNode(ChassisNode):
    """Represent a compute node, a node with OVS/OVN as well as VMs."""

    def __init__(
        self,
        phys_node,
        container,
        mgmt_ip,
        protocol,
        is_gateway: bool = True,
    ):
        super().__init__(phys_node, container, mgmt_ip, protocol)
        self.is_gateway = is_gateway

    def configure(self, physical_net):
        pass

    def provision(self, cluster: OpenStackCloud) -> None:
        """Connect Chassis to OVN Central."""
        self.connect(cluster.get_relay_connection_string())
