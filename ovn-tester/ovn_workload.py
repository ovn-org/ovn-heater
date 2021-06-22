import ovn_context
import ovn_stats
import ovn_utils
import time
import netaddr
import random
import string
import copy
from collections import namedtuple
from randmac import RandMac
from datetime import datetime


ClusterConfig = namedtuple('ClusterConfig',
                           ['cluster_cmd_path',
                            'monitor_all',
                            'logical_dp_groups',
                            'clustered_db',
                            'node_net',
                            'node_remote',
                            'node_timeout_s',
                            'internal_net',
                            'external_net',
                            'gw_net',
                            'cluster_net',
                            'n_workers'])


BrExConfig = namedtuple('BrExConfig', ['physical_net'])


class Node(ovn_utils.Sandbox):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip):
        super(Node, self).__init__(phys_node, container)
        self.container = container
        self.mgmt_net = mgmt_net
        self.mgmt_ip = mgmt_ip

    def build_cmd(self, cluster_cfg, cmd, *args):
        monitor_cmd = 'OVN_MONITOR_ALL={}'.format(
            'yes' if cluster_cfg.monitor_all else 'no'
        )
        cluster_db_cmd = 'OVN_DB_CLUSTER={}'.format(
            'yes' if cluster_cfg.clustered_db else 'no'
        )
        cmd = "cd {} && " \
              "CHASSIS_COUNT=0 GW_COUNT=0 IP_HOST={} IP_CIDR={} IP_START={} " \
              "{} {} CREATE_FAKE_VMS=no ./ovn_cluster.sh {}".format(
                  cluster_cfg.cluster_cmd_path, self.mgmt_net.ip,
                  self.mgmt_net.prefixlen, self.mgmt_ip, monitor_cmd,
                  cluster_db_cmd, cmd
              )
        for i in args:
            cmd += ' {}'.format(i)
        return cmd


class CentralNode(Node):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip):
        super(CentralNode, self).__init__(phys_node, container,
                                          mgmt_net, mgmt_ip)

    def start(self, cluster_cfg):
        print('***** starting central node *****')
        self.phys_node.run(self.build_cmd(cluster_cfg, 'start'))
        time.sleep(5)


class WorkerNode(Node):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip,
                 int_net, ext_net, gw_net, unique_id):
        super(WorkerNode, self).__init__(phys_node, container,
                                         mgmt_net, mgmt_ip)
        self.int_net = int_net
        self.ext_net = ext_net
        self.gw_net = gw_net
        self.id = unique_id
        self.switch = None
        self.join_switch = None
        self.gw_router = None
        self.ext_switch = None
        self.lports = []

    def start(self, cluster_cfg):
        print('***** starting worker {} *****'.format(self.container))
        self.phys_node.run(self.build_cmd(cluster_cfg, 'add-chassis',
                                          self.container, 'tcp:0.0.0.1:6642'))

    @ovn_stats.timeit
    def connect(self, cluster_cfg):
        print('***** connecting worker {} *****'.format(self.container))
        self.phys_node.run(self.build_cmd(cluster_cfg,
                                          'set-chassis-ovn-remote',
                                          self.container,
                                          cluster_cfg.node_remote))

    def configure_localnet(self, physical_net):
        print('***** creating localnet on {} *****'.format(self.container))
        cmd = \
            'ovs-vsctl -- set open_vswitch . external-ids:{}={}:br-ex'.format(
                'ovn-bridge-mappings',
                physical_net
            )
        self.run(cmd=cmd)

    def configure_external_host(self):
        print('***** add external host on {} *****'.format(self.container))
        gw_ip = netaddr.IPAddress(self.ext_net.last - 1)
        host_ip = netaddr.IPAddress(self.ext_net.last - 2)

        self.run(cmd='ip link add veth0 type veth peer name veth1')
        self.run(cmd='ip link add veth0 type veth peer name veth1')
        self.run(cmd='ip netns add ext-ns')
        self.run(cmd='ip link set netns ext-ns dev veth0')
        self.run(cmd='ip netns exec ext-ns ip link set dev veth0 up')
        self.run(
            cmd='ip netns exec ext-ns ip addr add {}/{} dev veth0'.format(
                host_ip, self.ext_net.prefixlen))
        self.run(
            cmd='ip netns exec ext-ns ip route add default via {}'.format(
                gw_ip))
        self.run(cmd='ip link set dev veth1 up')
        self.run(cmd='ovs-vsctl add-port br-ex veth1')

    def configure(self, physical_net):
        self.configure_localnet(physical_net)
        self.configure_external_host()

    @ovn_stats.timeit
    def wait(self, sbctl, timeout_s):
        for _ in range(timeout_s * 10):
            if sbctl.chassis_bound(self.container):
                return
            time.sleep(0.1)
        raise ovn_utils.OvnChassisTimeoutException()

    @ovn_stats.timeit
    def provision(self, cluster):
        self.connect(cluster.cluster_cfg)
        self.wait(cluster.sbctl, cluster.cluster_cfg.node_timeout_s)

        # Create a node switch and connect it to the cluster router.
        self.switch = cluster.nbctl.ls_add('lswitch-{}'.format(self.container),
                                           cidr=self.int_net)
        lrp_name = 'rtr-to-node-{}'.format(self.container)
        ls_rp_name = 'node-to-rtr-{}'.format(self.container)
        lrp_ip = netaddr.IPAddress(self.int_net.last - 1)
        self.rp = cluster.nbctl.lr_port_add(
            cluster.router, lrp_name, RandMac(), lrp_ip,
            self.int_net.prefixlen
        )
        self.ls_rp = cluster.nbctl.ls_port_add(
            self.switch, ls_rp_name, self.rp
        )

        # Create a join switch and connect it to the cluster router.
        self.join_switch = cluster.nbctl.ls_add(
            'join-{}'.format(self.container), cidr=self.gw_net
        )
        join_lrp_name = 'rtr-to-join-{}'.format(self.container)
        join_ls_rp_name = 'join-to-rtr-{}'.format(self.container)
        lrp_ip = netaddr.IPAddress(self.gw_net.last - 1)
        self.join_rp = cluster.nbctl.lr_port_add(
            cluster.router, join_lrp_name, RandMac(), lrp_ip,
            self.gw_net.prefixlen
        )
        self.join_ls_rp = cluster.nbctl.ls_port_add(
            self.join_switch, join_ls_rp_name, self.join_rp
        )

        # Create a gw router and connect it to the join switch.
        self.gw_router = cluster.nbctl.lr_add(
            'gwrouter-{}'.format(self.container)
        )
        cluster.nbctl.run('set Logical_Router {} options:chassis={}'.format(
            self.gw_router, self.container))
        join_grp_name = 'gw-to-join-{}'.format(self.container)
        join_ls_grp_name = 'join-to-gw-{}'.format(self.container)
        gr_gw = netaddr.IPAddress(self.gw_net.last - 2)
        self.gw_rp = cluster.nbctl.lr_port_add(
            self.gw_router, join_grp_name, RandMac(), gr_gw,
            self.gw_net.prefixlen
        )
        self.join_gw_rp = cluster.nbctl.ls_port_add(
            self.join_switch, join_ls_grp_name, self.gw_rp
        )

        # Create an external switch connecting the gateway router to the
        # physnet.
        self.ext_switch = cluster.nbctl.ls_add(
            'ext-{}'.format(self.container), cidr=self.ext_net
        )
        ext_lrp_name = 'gw-to-ext-{}'.format(self.container)
        ext_ls_rp_name = 'ext-to-gw-{}'.format(self.container)
        lrp_ip = netaddr.IPAddress(self.ext_net.last - 1)
        self.ext_rp = cluster.nbctl.lr_port_add(
            self.gw_router, ext_lrp_name, RandMac(), lrp_ip,
            self.ext_net.prefixlen
        )
        self.ext_gw_rp = cluster.nbctl.ls_port_add(
            self.ext_switch, ext_ls_rp_name, self.ext_rp
        )

        # Configure physnet.
        self.physnet_port = cluster.nbctl.ls_port_add(
            self.ext_switch, 'provnet-{}'.format(self.container),
            ip="unknown"
        )
        cluster.nbctl.ls_port_set_set_type(self.physnet_port, 'localnet')
        cluster.nbctl.ls_port_set_set_options(
            self.physnet_port,
            'network_name={}'.format(cluster.brex_cfg.physical_net)
        )

        # Route for traffic entering the cluster.
        rp_gw = netaddr.IPAddress(self.gw_net.last - 1)
        cluster.nbctl.route_add(self.gw_router, cluster.net, str(rp_gw))

        # Default route to get out of cluster via physnet.
        gr_def_gw = netaddr.IPAddress(self.ext_net.last - 2)
        cluster.nbctl.route_add(self.gw_router, gw=str(gr_def_gw))

        # Force return traffic to return on the same node.
        cluster.nbctl.run(
            'set Logical_Router {} options:lb_force_snat_ip={}'.format(
                self.gw_router, str(gr_gw)
            )
        )

        # Route for traffic that needs to exit the cluster
        # (via gw router).
        cluster.nbctl.route_add(cluster.router, str(self.int_net),
                                str(gr_gw), policy="src-ip")

        # SNAT traffic leaving the cluster.
        cluster.nbctl.nat_add(self.gw_router, external_ip=str(gr_gw),
                              logical_ip=cluster.net)

    @ovn_stats.timeit
    def provision_port(self, cluster):
        name = 'lp-{}-{}'.format(self.id, len(self.lports))
        ip = netaddr.IPAddress(self.int_net.first + len(self.lports) + 1)
        mask = self.int_net.prefixlen
        ip_mask = '{}/{}'.format(ip, mask)
        gw = netaddr.IPAddress(self.int_net.last - 1)
        ext_gw = netaddr.IPAddress(self.ext_net.last - 2)

        print("***** creating lport {} *****".format(name))
        lport = cluster.nbctl.ls_port_add(self.switch, name,
                                          mac=str(RandMac()), ip=ip_mask,
                                          gw=gw, ext_gw=ext_gw)
        self.lports.append(lport)
        return lport

    @ovn_stats.timeit
    def bind_port(self, port):
        vsctl = ovn_utils.OvsVsctl(self)
        vsctl.add_port(port, 'br-int', internal=True, ifaceid=port['name'])
        vsctl.bind_vm_port(port)

    def provision_ports(self, cluster, n_ports):
        ports = [self.provision_port(cluster) for i in range(n_ports)]
        for port in ports:
            self.bind_port(port)
        return ports

    @ovn_stats.timeit
    def ping_port(self, cluster, port):
        start_time = datetime.now()
        dest = port['ext-gw']
        cmd = 'ip netns exec {} ping -q -c 1 -W 0.1 {}'.format(
            port['name'], dest
        )
        while True:
            try:
                self.run(cmd=cmd, raise_on_error=True)
                break
            except ovn_utils.SSHError:
                pass

            duration = (datetime.now() - start_time).seconds
            if (duration > cluster.cluster_cfg.node_timeout_s):
                print('***** Error: Timeout waiting for port {} to be able '
                      'to ping gateway {} *****'.format(port['name'], dest))
                raise ovn_utils.OvnPingTimeoutException()

    def ping_ports(self, cluster, ports):
        for port in ports:
            self.ping_port(cluster, port)


class Cluster(object):
    def __init__(self, central_node, worker_nodes, cluster_cfg, brex_cfg):
        # In clustered mode use the first node for provisioning.
        self.central_node = central_node
        self.worker_nodes = worker_nodes
        self.cluster_cfg = cluster_cfg
        self.brex_cfg = brex_cfg
        self.nbctl = ovn_utils.OvnNbctl(self.central_node)
        self.sbctl = ovn_utils.OvnSbctl(self.central_node)
        self.net = cluster_cfg.cluster_net
        self.router = None

    def start(self):
        self.central_node.start(self.cluster_cfg)
        for w in self.worker_nodes:
            w.start(self.cluster_cfg)
            w.configure(self.brex_cfg.physical_net)
        self.nbctl.start_daemon()
        self.nbctl.set_global(
            'use_logical_dp_groups',
            self.cluster_cfg.logical_dp_groups
        )

    def create_cluster_router(self, rtr_name):
        self.router = self.nbctl.lr_add(rtr_name)

    # FIXME: This needs to be reworked.
    # def create_acl(self, target, lport, acl_create_args):
    #     print("***** creating acl on {} *****".format(lport["name"]))

    #     direction = acl_create_args.get("direction", "to-lport")
    #     priority = acl_create_args.get("priority", 1000)
    #     verdict = acl_create_args.get("action", "allow")
    #     address_set = acl_create_args.get("address_set", "")
    #     acl_type = acl_create_args.get("type", "switch")

    #     '''
    #     match template: {
    #         "direction": "<inport/outport>",
    #         "lport": "<switch port or port-group>",
    #         "address_set": "<address_set id>"
    #         "l4_port": "<l4 port number>",
    #     }
    #     '''
    #     match_template = acl_create_args.get("match",
    #                                          "%(direction)s == %(lport)s && \
    #                                          ip4 && udp && \
    #                                          udp.src == %(l4_port)s")
    #     p = "inport" if direction == "from-lport" else "outport"
    #     match = match_template % {
    #         "direction": p,
    #         "lport": lport["name"],
    #         "address_set": address_set,
    #         "l4_port": 100
    #     }
    #     self.nbctl.acl_add(target["name"], direction, priority, acl_type,
    #                        match, verdict)

    # @ovn_stats.timeit
    # def create_port_group_acls(self, name):
    #     port_group_acl = {"name": "@%s" % name}
    #     port_group = {"name": name}
    #     """
    #     create two acl for each ingress/egress of the Network Policy (NP)
    #     to allow ingress and egress traffic selected by the NP
    #     """
    #     # ingress
    #     match = "%(direction)s == %(lport)s && ip4.src == $%(address_set)s"
    #     acl_create_args = {
    #         "match": match,
    #         "address_set": "%s_ingress_as" % name,
    #         "priority": 1010, "direction": "from-lport",
    #         "type": "port-group"
    #     }
    #     self.create_acl(port_group, port_group_acl, acl_create_args)
    #     acl_create_args = {
    #         "priority": 1009,
    #         "match": "%(direction)s == %(lport)s && ip4",
    #         "type": "port-group", "direction": "from-lport",
    #         "action": "allow-related"
    #     }
    #     self.create_acl(port_group, port_group_acl, acl_create_args)
    #     # egress
    #     match = "%(direction)s == %(lport)s && ip4.dst == $%(address_set)s"
    #     acl_create_args = {
    #         "match": match,
    #         "address_set": "%s_egress_as" % name,
    #         "priority": 1010, "type": "port-group"
    #     }
    #     self.create_acl(port_group, port_group_acl, acl_create_args)
    #     acl_create_args = {
    #         "priority": 1009,
    #         "match": "%(direction)s == %(lport)s && ip4",
    #         "type": "port-group", " action": "allow-related"
    #     }
    #     self.create_acl(port_group, port_group_acl, acl_create_args)

    # @ovn_stats.timeit
    # def create_update_deny_port_group(self, lport, create):
    #     self.nbctl.port_group_add("portGroupDefDeny", lport, create)
    #     if create:
    #         # Create default acl for ingress and egress traffic:
    #         # only allow ARP traffic.
    #         port_group_acl = {
    #             "name": "@portGroupDefDeny"
    #         }
    #         port_group = {
    #             "name": "portGroupDefDeny"
    #         }
    #         # ingress
    #         acl_create_args = {
    #             "match": "%(direction)s == %(lport)s && arp",
    #             "priority": 1001, "direction": "from-lport",
    #             "type": "port-group"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)
    #         acl_create_args = {
    #             "match": "%(direction)s == %(lport)s",
    #             "direction": "from-lport", "action": "drop",
    #             "type": "port-group"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)
    #         # egress
    #         acl_create_args = {
    #             "match": "%(direction)s == %(lport)s && arp",
    #             "priority": 1001,
    #             "type": "port-group"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)
    #         acl_create_args = {
    #             "match": "%(direction)s == %(lport)s",
    #             "action": "drop",
    #             "type": "port-group"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)

    # @ovn_stats.timeit
    # def create_update_deny_multicast_port_group(self, lport, create):
    #     self.nbctl.port_group_add("portGroupMultiDefDeny", lport, create)
    #     if create:
    #         # Create default acl for ingress and egress multicast traffic:
    #         # drop all multicast.
    #         port_group_acl = {
    #             "name": "@portGroupMultiDefDeny"
    #         }
    #         port_group = {
    #             "name": "portGroupMultiDefDeny"
    #         }
    #         # ingress
    #         acl_create_args = {
    #             "match": "%(direction)s == %(lport)s && ip4.mcast",
    #             "priority": 1011, "direction": "from-lport",
    #             "type": "port-group", "action": "drop"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)
    #         # egress
    #         acl_create_args = {
    #             "match": "%(direction)s == %(lport)s && ip4.mcast",
    #             "priority": 1011, "type": "port-group",
    #             "action": "drop"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)

    # @ovn_stats.timeit
    # def create_update_network_policy(self, lport, ip, lport_create_args):
    #     iteration = ovn_context.active_context.iteration
    #     network_policy_size = lport_create_args.get("network_policy_size", 1)
    #     network_policy_index = iteration / network_policy_size
    #     create = (iteration % network_policy_size) == 0
    #     name = "networkPolicy%d" % network_policy_index

    #     self.nbctl.port_group_add(name, lport, create)
    #     self.nbctl.address_set_add("%s_ingress_as" % name, ip, create)
    #     self.nbctl.address_set_add("%s_egress_as" % name, ip, create)
    #     if (create):
    #         self.create_port_group_acls(name)

    #     self.create_update_deny_port_group(lport, iteration == 0)
    #     self.create_update_deny_multicast_port_group(lport, iteration == 0)

    # @ovn_stats.timeit
    # def create_update_name_space(self, lport, ip, lport_create_args):
    #     iteration = ovn_context.active_context.iteration
    #     name_space_size = lport_create_args.get("name_space_size", 1)
    #     name_space_index = iteration / name_space_size
    #     create = (iteration % name_space_size) == 0
    #     name = "nameSpace%d" % name_space_index
    #     port_group_name = "mcastPortGroup_%s" % name
    #     port_group_acl = {
    #         "name": "@" + port_group_name
    #     }
    #     port_group = {
    #         "name": port_group_name
    #     }

    #     self.nbctl.port_group_add(port_group_name, lport, create)
    #     self.nbctl.address_set_add(name, ip, create)

    #     if (create):
    #         # create multicast ACL
    #         match = "%(direction)s == %(lport)s && ip4.mcast"
    #         acl_create_args = {
    #             "match": match, "priority": 1012,
    #             "direction": "from-lport",
    #             "type": "port-group"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)
    #         acl_create_args = {
    #             "match": match, "priority": 1012,
    #             "type": "port-group"
    #         }
    #         self.create_acl(port_group, port_group_acl, acl_create_args)

    # def configure_routed_lport(self, sandbox, lswitch, lport_create_args,
    #                            lport_bind_args):
    #     iteration = ovn_context.active_context.iteration
    #     lport = self.create_lswitch_port(lswitch, iteration + 2)
    #     self.bind_and_wait_port(lport, lport_bind_args, sandbox)
    #     if lport_create_args.get("create_acls", False):
    #         cidr = lswitch.get("cidr", None)
    #         if cidr:
    #             ip = str(next(netaddr.IPNetwork(cidr.ip + 2).iter_hosts()))
    #         else:
    #             ip = ""

    #         # create or update network policy
    #         self.create_update_network_policy(lport, ip, lport_create_args)

    #         # create/update namespace
    #         self.create_update_name_space(lport, ip, lport_create_args)

    # @ovn_stats.timeit
    # def create_routed_lport(self, lport_create_args, lport_bind_args):
    #     iteration = ovn_context.active_context.iteration
    #     lswitch = self.lswitches[iteration % len(self.lswitches)]
    #     sandbox = self.worker_sbs[iteration % len(self.worker_sbs)]
    #     self.configure_routed_lport(sandbox, lswitch, lport_create_args,
    #                                 lport_bind_args)
