import ovn_context
import ovn_stats
import ovn_utils
import time
import netaddr
import random
import string
import copy
from randmac import RandMac
from datetime import datetime


class OvnWorkload:
    def __init__(self, controller, sandboxes, cluster_db, log):
        self.controller = controller
        if cluster_db:
            self.controller["name"] = controller["name"] + "-1"
        self.sandboxes = sandboxes
        self.nbctl = ovn_utils.OvnNbctl(controller,
                                        container=controller["name"],
                                        log=log)
        self.sbctl = ovn_utils.OvnSbctl(controller,
                                        container=self.controller["name"],
                                        log=log)
        self.router = None
        self.lswitches = []
        self.lports = []
        self.log = log

    def set_global_option(self, option, value):
        self.nbctl.run("set NB_Global . options:{}={}".format(
            option, value
        ))

    def add_central(self, fake_multinode_args, nbctld_config):
        print("***** creating central node *****")

        node_net = fake_multinode_args.get("node_net")
        node_net_len = fake_multinode_args.get("node_net_len")
        node_ip = fake_multinode_args.get("node_ip")
        ovn_fake_path = fake_multinode_args.get("cluster_cmd_path")

        if fake_multinode_args.get("ovn_monitor_all"):
            monitor_cmd = "OVN_MONITOR_ALL=yes"
        else:
            monitor_cmd = "OVN_MONITOR_ALL=no"

        if fake_multinode_args.get("ovn_cluster_db"):
            cluster_db_cmd = "OVN_DB_CLUSTER=yes"
        else:
            cluster_db_cmd = "OVN_DB_CLUSTER=no"

        cmd = "cd {} && " \
              "CHASSIS_COUNT=0 GW_COUNT=0 IP_HOST={} IP_CIDR={} IP_START={} " \
              "{} {} CREATE_FAKE_VMS=no ./ovn_cluster.sh start".format(
                  ovn_fake_path, node_net, node_net_len, node_ip, monitor_cmd,
                  cluster_db_cmd
                  )
        client = ovn_utils.RemoteConn(node=self.controller, log=self.log)
        client.run(cmd=cmd)

        if nbctld_config.get("daemon", False):
            self.nbctl.start_daemon(nbctld_config=nbctld_config)

        time.sleep(5)

    def add_chassis_node_localnet(self, fake_multinode_args):
        iteration = ovn_context.active_context.iteration
        sandbox = self.sandboxes[iteration % len(self.sandboxes)]

        print("***** creating localnet on %s *****" % sandbox["name"])

        cmd = \
            "ovs-vsctl -- set open_vswitch . external-ids:{}={}:br-ex".format(
                "ovn-bridge-mappings",
                fake_multinode_args.get("physnet", "providernet")
            )
        client = ovn_utils.RemoteConn(ssh=sandbox["ssh"],
                                      container=sandbox["name"],
                                      log=self.log)
        client.run(cmd=cmd)

    def add_chassis_external_host(self, lnetwork_create_args):
        iteration = ovn_context.active_context.iteration
        sandbox = self.sandboxes[iteration % len(self.sandboxes)]
        cidr = netaddr.IPNetwork(lnetwork_create_args.get('start_ext_cidr'))
        ext_cidr = cidr.next(iteration)

        gw_ip = netaddr.IPAddress(ext_cidr.last - 1)
        host_ip = netaddr.IPAddress(ext_cidr.last - 2)

        client = ovn_utils.RemoteConn(ssh=sandbox["ssh"],
                                      container=sandbox["name"],
                                      log=self.log)
        client.run(cmd="ip link add veth0 type veth peer name veth1")
        client.run(cmd="ip link add veth0 type veth peer name veth1")
        client.run(cmd="ip netns add ext-ns")
        client.run(cmd="ip link set netns ext-ns dev veth0")
        client.run(cmd="ip netns exec ext-ns ip link set dev veth0 up")
        client.run(
            cmd="ip netns exec ext-ns ip addr add {}/{} dev veth0".format(
                host_ip, ext_cidr.prefixlen))
        client.run(
            cmd="ip netns exec ext-ns ip route add default via {}".format(
                gw_ip))
        client.run(cmd="ip link set dev veth1 up")
        client.run(cmd="ovs-vsctl add-port br-ex veth1")

    def add_chassis_node(self, fake_multinode_args):
        iteration = ovn_context.active_context.iteration
        node_net = fake_multinode_args.get("node_net")
        node_net_len = fake_multinode_args.get("node_net_len")
        node_cidr = netaddr.IPNetwork("{}/{}".format(node_net, node_net_len))
        node_ip = str(node_cidr.ip + iteration + 1)

        ovn_fake_path = fake_multinode_args.get("cluster_cmd_path")

        sandbox = self.sandboxes[iteration % len(self.sandboxes)]

        print("***** adding %s controller *****" % sandbox["name"])

        if fake_multinode_args.get("ovn_monitor_all"):
            monitor_cmd = "OVN_MONITOR_ALL=yes"
        else:
            monitor_cmd = "OVN_MONITOR_ALL=no"

        if fake_multinode_args.get("ovn_cluster_db"):
            cluster_db_cmd = "OVN_DB_CLUSTER=yes"
        else:
            cluster_db_cmd = "OVN_DB_CLUSTER=no"

        cmd = "cd {} && IP_HOST={} IP_CIDR={} IP_START={} {} {} " \
              "./ovn_cluster.sh add-chassis {} {}".format(
                  ovn_fake_path, node_net, node_net_len, node_ip, monitor_cmd,
                  cluster_db_cmd, sandbox["name"], "tcp:0.0.0.1:6642"
                  )
        client = ovn_utils.RemoteConn(ssh=sandbox["ssh"], log=self.log)
        client.run(cmd)

    @ovn_stats.timeit
    def connect_chassis_node(self, fake_multinode_args):
        iteration = ovn_context.active_context.iteration
        sandbox = self.sandboxes[iteration % len(self.sandboxes)]

        print("***** connecting %s controller *****" % sandbox["name"])

        central_ip = fake_multinode_args.get("central_ip")
        sb_proto = fake_multinode_args.get("sb_proto", "ssl")
        ovn_fake_path = fake_multinode_args.get("cluster_cmd_path")

        central_ips = [ip.strip() for ip in central_ip.split('-')]
        remote = ",".join(
            ["{}:{}:6642".format(sb_proto, r) for r in central_ips])

        cmd = "cd {} && ./ovn_cluster.sh set-chassis-ovn-remote {} {}".format(
            ovn_fake_path, sandbox["name"], remote
        )
        client = ovn_utils.RemoteConn(ssh=sandbox["ssh"], log=self.log)
        client.run(cmd=cmd)

    @ovn_stats.timeit
    def wait_chassis_node(self, fake_multinode_args):
        iteration = ovn_context.active_context.iteration
        sandbox = self.sandboxes[iteration % len(self.sandboxes)]
        max_timeout_s = fake_multinode_args.get("max_timeout_s")
        for i in range(0, max_timeout_s * 10):
            if self.sbctl.chassis_bound(chassis=sandbox["name"]):
                break
            time.sleep(0.1)

    @ovn_stats.timeit
    def ping_port(self, lport, sandbox, wait_timeout_s):
        start_time = datetime.now()
        client = ovn_utils.RemoteConn(ssh=sandbox["ssh"],
                                      container=sandbox["name"],
                                      log=self.log)

        if lport.get("ext-gw"):
            dest = lport["ext-gw"]
        else:
            dest = lport["gw"]
        while True:
            try:
                cmd = "ip netns exec {} ping -q -c 1 -W 0.1 {}".format(
                        lport["name"], dest)
                client.run(cmd=cmd, raise_on_error=True)
                break
            except:
                pass

            if (datetime.now() - start_time).seconds > wait_timeout_s:
                print("***** Error: Timeout waiting for port {} to be able "
                      "to ping gateway {} *****".format(lport["name"], dest))
                raise ovn_utils.OvnPingTimeoutException()
                break

    @ovn_stats.timeit
    def wait_up_port(self, lport, sandbox, lport_bind_args):
        wait_timeout_s = lport_bind_args.get("wait_timeout_s", 20)
        wait_sync = lport_bind_args.get("wait_sync", "hv")
        if wait_sync.lower() not in ['hv', 'sb', 'ping', 'none']:
            raise ovn_utils.OvnInvalidConfigException(
                "Unknown value for wait_sync: {}. "
                "Only 'hv', 'sb' and 'none' are allowed.".format(wait_sync))

        print("***** wait port up: sync: {} *****".format(wait_sync))

        if wait_sync == 'ping':
            self.ping_port(lport, sandbox, wait_timeout_s)
        else:
            cmd = "Logical_Switch_Port {} up=true".format(lport["name"])
            self.nbctl.wait_until(cmd)
            if wait_sync != 'none':
                self.nbctl.sync(wait_sync)

    @ovn_stats.timeit
    def bind_and_wait_port(self, lport, lport_bind_args, sandbox):
        internal = lport_bind_args.get("internal", False)
        internal_vm = lport_bind_args.get("internal_vm", True)
        vsctl = ovn_utils.OvsVsctl(ssh=sandbox["ssh"],
                                   container=sandbox["name"],
                                   log=self.log)
        # add ovs port
        vsctl.add_port(lport["name"], "br-int", internal=internal,
                       ifaceid=lport["name"])
        if internal and internal_vm:
            vsctl.bind_vm_port(lport)
        if lport_bind_args.get("wait_up", False):
            self.wait_up_port(lport, sandbox, lport_bind_args)

    @ovn_stats.timeit
    def create_lswitch_port(self, lswitch, iteration, ext_cidr=None):
        cidr = lswitch.get("cidr", None)
        if cidr:
            ip = str(next(netaddr.iter_iprange(cidr.ip + iteration + 1,
                                               cidr.last)))
            ip_mask = '{}/{}'.format(ip, cidr.prefixlen)
            gw = str(netaddr.IPAddress(cidr.last - 1))
            name = "lp_{}".format(ip)
        else:
            name = "lp_".join(
                random.choice(string.ascii_letters) for i in range(10))
            ip_mask = ""
            ip = ""
            gw = ""
        if ext_cidr:
            ext_gw = str(netaddr.IPAddress(ext_cidr.last - 2))
        else:
            ext_gw = ""

        print("***** creating lport {} *****".format(name))
        lswitch_port = self.nbctl.ls_port_add(lswitch["name"], name,
                                              mac=str(RandMac()),
                                              ip=ip_mask, gw=gw,
                                              ext_gw=ext_gw)
        return lswitch_port

    @ovn_stats.timeit
    def create_lswitch(self, lswitch_create_args, iteration, prefix):
        start_cidr = lswitch_create_args.get("start_cidr", "")
        if start_cidr:
            start_cidr = netaddr.IPNetwork(start_cidr)
            cidr = start_cidr.next(iteration)
            name = prefix + str(cidr)
        else:
            name = prefix.join(
                random.choice(string.ascii_letters) for i in range(10))

        print("***** creating lswitch {} *****".format(name))
        lswitch = self.nbctl.ls_add(name)
        if start_cidr:
            lswitch["cidr"] = cidr

        return lswitch

    @ovn_stats.timeit
    def connect_lswitch_to_router(self, lrouter, lswitch):
        gw = netaddr.IPAddress(lswitch["cidr"].last - 1)
        lrouter_port_ip = '{}/{}'.format(gw, lswitch["cidr"].prefixlen)
        mac = RandMac()
        self.nbctl.lr_port_add(lrouter["name"], lswitch["name"], mac,
                               lrouter_port_ip)
        self.nbctl.ls_port_add(lswitch["name"], "rp-" + lswitch["name"],
                               lswitch["name"])

    @ovn_stats.timeit
    def create_phynet(self, lswitch, physnet):
        port = "provnet-{}".format(lswitch["name"])
        print("***** creating phynet {} *****".format(port))

        self.nbctl.ls_port_add(lswitch["name"], port, ip="unknown")
        self.nbctl.ls_port_set_set_type(port, "localnet")
        self.nbctl.ls_port_set_set_options(port, "network_name=%s" % physnet)

    @ovn_stats.timeit
    def connect_gateway_router(self, lrouter, lswitch, lswitch_create_args,
                               lnetwork_create_args, gw_cidr, ext_cidr,
                               sandbox):

        # Create a join switch to connect the GW router to the cluster router.
        lswitch_args = copy.copy(lswitch_create_args)
        lswitch_args["start_cidr"] = gw_cidr if str(gw_cidr) else ""
        join_switch = self.create_lswitch(lswitch_args, 0, "join_")

        # Create ports between the join switch and the cluster router.
        self.connect_lswitch_to_router(lrouter, join_switch)

        # Create a gateway router and bind it to the local chassis.
        gw_router = self.nbctl.lr_add("grouter_" + str(gw_cidr))
        self.nbctl.run("set Logical_Router {} options:chassis={}".format(
            gw_router["name"], sandbox["name"]))

        # Create ports between the join switch and the gateway router.
        gr_gw = netaddr.IPAddress(gw_cidr.last - 2) if gw_cidr else None
        grouter_port_join_switch = \
            "grpj-" + str(gw_cidr) if gw_cidr else "grpj"
        grouter_port_join_switch_ip = '{}/{}'.format(gr_gw, gw_cidr.prefixlen)
        self.nbctl.lr_port_add(gw_router["name"], grouter_port_join_switch,
                               RandMac(), grouter_port_join_switch_ip)
        self.nbctl.ls_port_add(join_switch["name"],
                               "jrpg-" + join_switch["name"],
                               grouter_port_join_switch)

        # Create an external switch connecting the gateway router to the
        # physnet.
        lswitch_args["start_cidr"] = ext_cidr if str(ext_cidr) else ""
        ext_switch = self.create_lswitch(lswitch_args, 0, "ext_")
        self.connect_lswitch_to_router(gw_router, ext_switch)
        self.create_phynet(ext_switch,
                           lnetwork_create_args.get("physnet", "providernet"))

        cluster_cidr = lnetwork_create_args.get("cluster_cidr", "")
        if cluster_cidr and gw_cidr:
            # Route for traffic entering the cluster.
            rp_gw = netaddr.IPAddress(gw_cidr.last - 1)
            self.nbctl.route_add(gw_router["name"], cluster_cidr, str(rp_gw))

        if ext_cidr:
            # Default route to get out of cluster via physnet.
            gr_def_gw = netaddr.IPAddress(ext_cidr.last - 2)
            self.nbctl.route_add(gw_router["name"], gw=str(gr_def_gw))

        # Force return traffic to return on the same node.
        self.nbctl.run(
            "set Logical_Router {} options:lb_force_snat_ip={}".format(
                gw_router["name"], str(gr_gw)))

        # Route for traffic that needs to exit the cluster
        # (via gw router).
        self.nbctl.route_add(lrouter["name"], str(lswitch["cidr"]),
                             str(gr_gw), policy="src-ip")

        # SNAT traffic leaving the cluster.
        self.nbctl.nat_add(gw_router["name"], external_ip=str(gr_gw),
                           logical_ip=cluster_cidr)

    def create_cluster_router(self, rtr_name):
        self.router = self.nbctl.lr_add(rtr_name)

    @ovn_stats.timeit
    def create_routed_network(self, fake_multinode_args, lswitch_create_args,
                              lnetwork_create_args, lport_bind_args):
        iteration = ovn_context.active_context.iteration
        self.connect_chassis_node(fake_multinode_args)
        self.wait_chassis_node(fake_multinode_args)

        lswitch = self.create_lswitch(lswitch_create_args, iteration,
                                      "lswitch_")
        self.lswitches.append(lswitch)
        self.connect_lswitch_to_router(self.router, lswitch)

        if lnetwork_create_args.get('gw_router_per_network', False):
            start_ext_cidr = lnetwork_create_args.get('start_ext_cidr', '')
            ext_cidr = None
            start_gw_cidr = lnetwork_create_args.get('start_gw_cidr', '')
            gw_cidr = None

            if start_gw_cidr:
                gw_cidr = netaddr.IPNetwork(start_gw_cidr).next(iteration)
            if start_ext_cidr:
                ext_cidr = netaddr.IPNetwork(start_ext_cidr).next(iteration)
            self.connect_gateway_router(self.router, lswitch,
                                        lswitch_create_args,
                                        lnetwork_create_args,
                                        gw_cidr, ext_cidr,
                                        self.sandboxes[iteration])

        lport = self.create_lswitch_port(lswitch, 0, ext_cidr=ext_cidr)
        self.lports.append(lport)
        sandbox = self.sandboxes[iteration % len(self.sandboxes)]
        self.bind_and_wait_port(lport, lport_bind_args, sandbox)

    def create_acl(self, target, lport, acl_create_args):
        print("***** creating acl on {} *****".format(lport["name"]))

        direction = acl_create_args.get("direction", "to-lport")
        priority = acl_create_args.get("priority", 1000)
        verdict = acl_create_args.get("action", "allow")
        address_set = acl_create_args.get("address_set", "")
        acl_type = acl_create_args.get("type", "switch")

        '''
        match template: {
            "direction": "<inport/outport>",
            "lport": "<switch port or port-group>",
            "address_set": "<address_set id>"
            "l4_port": "<l4 port number>",
        }
        '''
        match_template = acl_create_args.get("match",
                                             "%(direction)s == %(lport)s && \
                                             ip4 && udp && \
                                             udp.src == %(l4_port)s")
        p = "inport" if direction == "from-lport" else "outport"
        match = match_template % {
            "direction": p,
            "lport": lport["name"],
            "address_set": address_set,
            "l4_port": 100
        }
        self.nbctl.acl_add(target["name"], direction, priority, acl_type,
                           match, verdict)

    @ovn_stats.timeit
    def create_port_group_acls(self, name):
        port_group_acl = {"name": "@%s" % name}
        port_group = {"name": name}
        """
        create two acl for each ingress/egress of the Network Policy (NP)
        to allow ingress and egress traffic selected by the NP
        """
        # ingress
        match = "%(direction)s == %(lport)s && ip4.src == $%(address_set)s"
        acl_create_args = {
            "match": match,
            "address_set": "%s_ingress_as" % name,
            "priority": 1010, "direction": "from-lport",
            "type": "port-group"
        }
        self.create_acl(port_group, port_group_acl, acl_create_args)
        acl_create_args = {
            "priority": 1009,
            "match": "%(direction)s == %(lport)s && ip4",
            "type": "port-group", "direction": "from-lport",
            "action": "allow-related"
        }
        self.create_acl(port_group, port_group_acl, acl_create_args)
        # egress
        match = "%(direction)s == %(lport)s && ip4.dst == $%(address_set)s"
        acl_create_args = {
            "match": match,
            "address_set": "%s_egress_as" % name,
            "priority": 1010, "type": "port-group"
        }
        self.create_acl(port_group, port_group_acl, acl_create_args)
        acl_create_args = {
            "priority": 1009,
            "match": "%(direction)s == %(lport)s && ip4",
            "type": "port-group", " action": "allow-related"
        }
        self.create_acl(port_group, port_group_acl, acl_create_args)

    @ovn_stats.timeit
    def create_update_deny_port_group(self, lport, create):
        self.nbctl.port_group_add("portGroupDefDeny", lport, create)
        if create:
            # Create default acl for ingress and egress traffic:
            # only allow ARP traffic.
            port_group_acl = {
                "name": "@portGroupDefDeny"
            }
            port_group = {
                "name": "portGroupDefDeny"
            }
            # ingress
            acl_create_args = {
                "match": "%(direction)s == %(lport)s && arp",
                "priority": 1001, "direction": "from-lport",
                "type": "port-group"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)
            acl_create_args = {
                "match": "%(direction)s == %(lport)s",
                "direction": "from-lport", "action": "drop",
                "type": "port-group"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)
            # egress
            acl_create_args = {
                "match": "%(direction)s == %(lport)s && arp",
                "priority": 1001,
                "type": "port-group"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)
            acl_create_args = {
                "match": "%(direction)s == %(lport)s",
                "action": "drop",
                "type": "port-group"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)

    @ovn_stats.timeit
    def create_update_deny_multicast_port_group(self, lport, create):
        self.nbctl.port_group_add("portGroupMultiDefDeny", lport, create)
        if create:
            # Create default acl for ingress and egress multicast traffic:
            # drop all multicast.
            port_group_acl = {
                "name": "@portGroupMultiDefDeny"
            }
            port_group = {
                "name": "portGroupMultiDefDeny"
            }
            # ingress
            acl_create_args = {
                "match": "%(direction)s == %(lport)s && ip4.mcast",
                "priority": 1011, "direction": "from-lport",
                "type": "port-group", "action": "drop"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)
            # egress
            acl_create_args = {
                "match": "%(direction)s == %(lport)s && ip4.mcast",
                "priority": 1011, "type": "port-group",
                "action": "drop"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)

    @ovn_stats.timeit
    def create_update_network_policy(self, lport, ip, lport_create_args):
        iteration = ovn_context.active_context.iteration
        network_policy_size = lport_create_args.get("network_policy_size", 1)
        network_policy_index = iteration / network_policy_size
        create = (iteration % network_policy_size) == 0
        name = "networkPolicy%d" % network_policy_index

        self.nbctl.port_group_add(name, lport, create)
        self.nbctl.address_set_add("%s_ingress_as" % name, ip, create)
        self.nbctl.address_set_add("%s_egress_as" % name, ip, create)
        if (create):
            self.create_port_group_acls(name)

        self.create_update_deny_port_group(lport, iteration == 0)
        self.create_update_deny_multicast_port_group(lport, iteration == 0)

    @ovn_stats.timeit
    def create_update_name_space(self, lport, ip, lport_create_args):
        iteration = ovn_context.active_context.iteration
        name_space_size = lport_create_args.get("name_space_size", 1)
        name_space_index = iteration / name_space_size
        create = (iteration % name_space_size) == 0
        name = "nameSpace%d" % name_space_index
        port_group_name = "mcastPortGroup_%s" % name
        port_group_acl = {
            "name": "@" + port_group_name
        }
        port_group = {
            "name": port_group_name
        }

        self.nbctl.port_group_add(port_group_name, lport, create)
        self.nbctl.address_set_add(name, ip, create)

        if (create):
            # create multicast ACL
            match = "%(direction)s == %(lport)s && ip4.mcast"
            acl_create_args = {
                "match": match, "priority": 1012,
                "direction": "from-lport",
                "type": "port-group"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)
            acl_create_args = {
                "match": match, "priority": 1012,
                "type": "port-group"
            }
            self.create_acl(port_group, port_group_acl, acl_create_args)

    def configure_routed_lport(self, sandbox, lswitch, lport_create_args,
                               lport_bind_args):
        iteration = ovn_context.active_context.iteration
        lport = self.create_lswitch_port(lswitch, iteration + 2)
        self.bind_and_wait_port(lport, lport_bind_args, sandbox)
        if lport_create_args.get("create_acls", False):
            cidr = lswitch.get("cidr", None)
            if cidr:
                ip = str(next(netaddr.IPNetwork(cidr.ip + 2).iter_hosts()))
            else:
                ip = ""

            # create or update network policy
            self.create_update_network_policy(lport, ip, lport_create_args)

            # create/update namespace
            self.create_update_name_space(lport, ip, lport_create_args)

    @ovn_stats.timeit
    def create_routed_lport(self, lport_create_args, lport_bind_args):
        iteration = ovn_context.active_context.iteration
        lswitch = self.lswitches[iteration % len(self.lswitches)]
        sandbox = self.sandboxes[iteration % len(self.sandboxes)]
        self.configure_routed_lport(sandbox, lswitch, lport_create_args,
                                    lport_bind_args)
