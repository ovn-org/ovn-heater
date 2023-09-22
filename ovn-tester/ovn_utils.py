import logging
import netaddr
import select
import ovn_exceptions
import time
from collections import namedtuple
from functools import partial
from typing import Dict, List, Optional
import ovsdbapp.schema.open_vswitch.impl_idl as ovs_impl_idl
import ovsdbapp.schema.ovn_northbound.impl_idl as nb_impl_idl
import ovsdbapp.schema.ovn_southbound.impl_idl as sb_impl_idl
import ovsdbapp.schema.ovn_ic_northbound.impl_idl as nb_ic_impl_idl
from ovsdbapp.backend import ovs_idl
from ovsdbapp.backend.ovs_idl import connection
from ovsdbapp.backend.ovs_idl import idlutils
from ovsdbapp.backend.ovs_idl import transaction
from ovsdbapp.backend.ovs_idl import vlog
from ovsdbapp import exceptions as ovsdbapp_exceptions
from ovs import poller


log = logging.getLogger(__name__)

LRouter = namedtuple('LRouter', ['uuid', 'name'])
LRPort = namedtuple('LRPort', ['name', 'mac', 'ip'])
LSwitch = namedtuple('LSwitch', ['uuid', 'name', 'cidr', 'cidr6'])
LSPort = namedtuple(
    'LSPort',
    [
        'name',
        'mac',
        'ip',
        'plen',
        'gw',
        'ext_gw',
        'ip6',
        'plen6',
        'gw6',
        'ext_gw6',
        'metadata',
        'passive',
        'uuid',
    ],
)
PortGroup = namedtuple('PortGroup', ['name'])
AddressSet = namedtuple('AddressSet', ['name'])
LoadBalancer = namedtuple('LoadBalancer', ['name', 'uuid'])
LoadBalancerGroup = namedtuple('LoadBalancerGroup', ['name', 'uuid'])
DhcpOptions = namedtuple("DhcpOptions", ["uuid", "cidr"])

DEFAULT_CTL_TIMEOUT = 60


MAX_RETRY = 5


DualStackIP = namedtuple('DualStackIP', ['ip4', 'plen4', 'ip6', 'plen6'])


vlog.use_python_logger(max_level=vlog.INFO)

# Under the hood, ovsdbapp uses select.select, but it has a hard-coded limit
# on the number of file descriptors that can be selected from. In large-scale
# tests (500 nodes), we exceed this number and run into issues. By switching to
# select.poll, we do not have this limitation.
poller.SelectPoll = select.poll


class PhysCtl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", stdout=None, timeout=DEFAULT_CTL_TIMEOUT):
        self.sb.run(cmd=cmd, stdout=stdout, timeout=timeout)

    def external_host_provision(self, ip, gw, netns='ext-ns'):
        log.info(f'Adding external host on {self.sb.container}')
        cmd = (
            f'ip link add veth0 type veth peer name veth1; '
            f'ip netns add {netns}; '
            f'ip link set netns {netns} dev veth0; '
            f'ip netns exec {netns} ip link set dev veth0 up; '
        )

        if ip.ip4:
            cmd += (
                f'ip netns exec ext-ns ip addr add {ip.ip4}/{ip.plen4}'
                f' dev veth0; '
            )
        if gw.ip4:
            cmd += f'ip netns exec ext-ns ip route add default via {gw.ip4}; '

        if ip.ip6:
            cmd += (
                f'ip netns exec ext-ns ip addr add {ip.ip6}/{ip.plen6}'
                f' dev veth0; '
            )
        if gw.ip6:
            cmd += f'ip netns exec ext-ns ip route add default via {gw.ip6}; '

        cmd += 'ip link set dev veth1 up; ' 'ovs-vsctl add-port br-ex veth1'

        # Run as a single invocation:
        cmd = f'bash -c \'{cmd}\''
        self.run(cmd=cmd)


class DualStackSubnet:
    def __init__(self, n4=None, n6=None):
        self.n4 = n4
        self.n6 = n6

    @classmethod
    def next(cls, n, index=0):
        n4 = n.n4.next(index) if n.n4 else None
        n6 = n.n6.next(index) if n.n6 else None
        return cls(n4, n6)

    def forward(self, index=0):
        if self.n4 and self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.first + index),
                self.n4.prefixlen,
                netaddr.IPAddress(self.n6.first + index),
                self.n6.prefixlen,
            )
        if self.n4 and not self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.first + index),
                self.n4.prefixlen,
                None,
                None,
            )
        if not self.n4 and self.n6:
            return DualStackIP(
                None,
                None,
                netaddr.IPAddress(self.n6.first + index),
                self.n6.prefixlen,
            )
        raise ovn_exceptions.OvnInvalidConfigException("invalid configuration")

    def reverse(self, index=1):
        if self.n4 and self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.last - index),
                self.n4.prefixlen,
                netaddr.IPAddress(self.n6.last - index),
                self.n6.prefixlen,
            )
        if self.n4 and not self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.last - index),
                self.n4.prefixlen,
                None,
                None,
            )
        if not self.n4 and self.n6:
            return DualStackIP(
                None,
                None,
                netaddr.IPAddress(self.n6.last - index),
                self.n6.prefixlen,
            )
        raise ovn_exceptions.OvnInvalidConfigException("invalid configuration")


# This override allows for us to connect to multiple DBs
class Backend(ovs_idl.Backend):
    def __init__(self, connection):
        super().__init__(connection)

    @property
    def ovsdb_connection(self):
        return self._ovsdb_connection

    @ovsdb_connection.setter
    def ovsdb_connection(self, connection):
        if self._ovsdb_connection is None:
            self._ovsdb_connection = connection


class VSIdl(ovs_impl_idl.OvsdbIdl, Backend):
    def __init__(self, connection):
        super().__init__(connection)


class OvsVsctl:
    def __init__(self, sb, connection_string, inactivity_probe):
        log.info(f'OvsVsctl: {connection_string}, probe: {inactivity_probe}')
        self.sb = sb
        i = connection.OvsdbIdl.from_server(connection_string, "Open_vSwitch")
        c = connection.Connection(i, inactivity_probe)
        self.idl = VSIdl(c)

    def run(
        self,
        cmd="",
        prefix="ovs-vsctl ",
        stdout=None,
        timeout=DEFAULT_CTL_TIMEOUT,
    ):
        self.sb.run(cmd=prefix + cmd, stdout=stdout, timeout=timeout)

    def set_global_external_id(self, key, value):
        self.idl.db_set(
            "Open_vSwitch",
            self.idl._ovs.uuid,
            ("external_ids", {key: str(value)}),
        ).execute(check_error=True)

    def add_port(self, port, bridge, internal=True, ifaceid=None):
        name = port.name
        with self.idl.transaction(check_error=True) as txn:
            txn.add(self.idl.add_port(bridge, name))
            if internal:
                txn.add(
                    self.idl.db_set("Interface", name, ("type", "internal"))
                )
            if ifaceid:
                txn.add(
                    self.idl.iface_set_external_id(name, "iface-id", ifaceid)
                )

    def del_port(self, port):
        self.idl.del_port(port.name).execute(check_error=True)

    def bind_vm_port(self, lport):
        cmd = (
            f'ip netns add {lport.name}; '
            f'ip link set {lport.name} netns {lport.name}; '
            f'ip netns exec {lport.name} ip link set {lport.name} '
            f'address {lport.mac}; '
            f'ip netns exec {lport.name} ip link set {lport.name} up'
        )
        if lport.ip:
            cmd += (
                f'; ip netns exec {lport.name} ip addr add '
                f'{lport.ip}/{lport.plen} dev {lport.name}'
            )
            cmd += (
                f'; ip netns exec {lport.name} ip route add '
                f'default via {lport.gw}'
            )
        if lport.ip6:
            cmd += (
                f'; ip netns exec {lport.name} ip addr add '
                f'{lport.ip6}/{lport.plen6} dev {lport.name} nodad'
            )
            cmd += (
                f'; ip netns exec {lport.name} ip route add '
                f'default via {lport.gw6}'
            )
        self.run(cmd, prefix="")

    def unbind_vm_port(self, lport):
        self.run(f'ip netns del {lport.name}', prefix='')


LLONG_MAX = 2**63 - 1


# We have to subclass the base Transaction for NB in order to facilitate the
# "sync" command. This is heavily based on ovsdbapp's OvsVsctlTransaction class
# but with some NB-specific modifications, and removal of some OVS-specific
# assumptions.
class NBTransaction(transaction.Transaction):
    def __init__(
        self,
        api,
        ovsdb_connection,
        timeout=None,
        check_error=False,
        log_errors=True,
        wait_type=None,
        **kwargs,
    ):
        super().__init__(
            api,
            ovsdb_connection,
            timeout=timeout,
            check_error=check_error,
            log_errors=log_errors,
            **kwargs,
        )
        self.wait_type = wait_type.lower() if wait_type else None
        if (
            self.wait_type
            and self.wait_type != "sb"
            and self.wait_type != "hv"
        ):
            log.warning(f"Unrecognized wait type {self.wait_type}. Ignoring")
            self.wait_type = None

    def pre_commit(self, txn):
        if self.wait_type:
            if self.api._nb.nb_cfg == LLONG_MAX:
                txn.add(
                    self.api.db_set(
                        "NB_Global", self.api._nb.uuid, ("nb_cfg", 0)
                    )
                )
            self.api._nb.increment('nb_cfg')

    def post_commit(self, txn):
        super().post_commit(txn)
        try:
            self.do_post_commit(txn)
        except ovsdbapp_exceptions.TimeoutException:
            log.exception("Transaction timed out")

    def do_post_commit(self, txn):
        if not self.wait_type:
            return

        next_cfg = txn.get_increment_new_value()
        while not self.timeout_exceeded():
            self.api.idl.run()
            if self.nb_has_completed(next_cfg):
                break
            self.ovsdb_connection.poller.timer_wait(
                self.time_remaining() * 1000
            )
            self.api.idl.wait(self.ovsdb_connection.poller)
            self.ovsdb_connection.poller.block()
        else:
            raise ovsdbapp_exceptions.TimeoutException(
                commands=self.commands,
                timeout=self.timeout,
                cause='nbctl transaction did not end',
            )

    def nb_has_completed(self, next_cfg):
        if not self.wait_type:
            return True
        elif self.wait_type == "sb":
            cur_cfg = self.api._nb.sb_cfg
        else:  # self.wait_type == "hv":
            cur_cfg = min(self.api._nb.sb_cfg, self.api._nb.hv_cfg)

        return cur_cfg >= next_cfg


class NBIdl(nb_impl_idl.OvnNbApiIdlImpl, Backend):
    def __init__(self, connection):
        super().__init__(connection)

    def create_transaction(
        self,
        check_error=False,
        log_errors=True,
        timeout=None,
        wait_type=None,
        **kwargs,
    ):
        # Override of Base API method so we create NBTransactions.
        return NBTransaction(
            self,
            self.ovsdb_connection,
            timeout=timeout,
            check_error=check_error,
            log_errors=log_errors,
            wait_type=wait_type,
            **kwargs,
        )

    @property
    def _nb(self):
        return next(iter(self.db_list_rows('NB_Global').execute()))

    @property
    def _connection(self):
        return next(iter(self.db_list_rows('Connection').execute()))


class UUIDTransactionError(Exception):
    pass


class OvnNbctl:
    def __init__(self, sb, connection_string, inactivity_probe):
        log.info(f'OvnNbctl: {connection_string}, probe: {inactivity_probe}')
        i = connection.OvsdbIdl.from_server(
            connection_string, "OVN_Northbound"
        )
        c = connection.Connection(i, inactivity_probe)
        self.idl = NBIdl(c)

    def uuid_transaction(self, func):
        # Occasionally, due to RAFT leadership changes, a transaction can
        # appear to fail. In reality, they succeeded and the error is spurious.
        # When we encounter this sort of error, the result of the command will
        # not have the UUID of the row. Our strategy is to retry the
        # transaction with may_exist=True so that we can get the UUID.
        for _ in range(MAX_RETRY):
            cmd = func(may_exist=True)
            cmd.execute()
            try:
                return cmd.result.uuid
            except AttributeError:
                continue

        raise UUIDTransactionError("Failed to get UUID from transaction")

    def db_create_transaction(self, table, *, get_func, **columns):
        # db_create does not afford the ability to retry with "may_exist". We
        # therefore need to have a method of ensuring that the value was not
        # actually set in the DB before we can retry the transaction.
        for _ in range(MAX_RETRY):
            cmd = self.idl.db_create(table, **columns)
            cmd.execute()
            try:
                return cmd.result
            except AttributeError:
                cmd = get_func()
                cmd.execute()
                try:
                    return cmd.result
                except AttributeError:
                    continue

        raise UUIDTransactionError("Failed to get UUID from transaction")

    def set_global(self, option, value):
        self.idl.db_set(
            "NB_Global", self.idl._nb.uuid, ("options", {option: str(value)})
        ).execute()

    def set_global_name(self, value):
        self.idl.db_set(
            "NB_Global", self.idl._nb.uuid, ("name", str(value))
        ).execute()

    def set_inactivity_probe(self, value):
        self.idl.db_set(
            "Connection",
            self.idl._connection.uuid,
            ("inactivity_probe", value),
        ).execute()

    def lr_add(self, name, ext_ids: Optional[Dict] = None):
        ext_ids = {} if ext_ids is None else ext_ids

        log.info(f'Creating lrouter {name}')
        uuid = self.uuid_transaction(
            partial(self.idl.lr_add, name, external_ids=ext_ids)
        )
        return LRouter(name=name, uuid=uuid)

    def lr_port_add(
        self, router, name, mac, dual_ip=None, ext_ids: Optional[Dict] = None
    ):
        ext_ids = {} if ext_ids is None else ext_ids
        networks = []
        if dual_ip.ip4 and dual_ip.plen4:
            networks.append(f'{dual_ip.ip4}/{dual_ip.plen4}')
        if dual_ip.ip6 and dual_ip.plen6:
            networks.append(f'{dual_ip.ip6}/{dual_ip.plen6}')

        self.idl.lrp_add(
            router.uuid, name, str(mac), networks, external_ids=ext_ids
        ).execute()
        return LRPort(name=name, mac=mac, ip=dual_ip)

    def lr_port_set_gw_chassis(self, rp, chassis, priority=10):
        log.info(f'Setting gw chassis {chassis} for router port {rp.name}')
        self.idl.lrp_set_gateway_chassis(rp.name, chassis, priority).execute()

    def ls_add(
        self,
        name: str,
        net_s: DualStackSubnet,
        ext_ids: Optional[Dict] = None,
        other_config: Optional[Dict] = None,
    ) -> LSwitch:
        ext_ids = {} if ext_ids is None else ext_ids
        other_config = {} if other_config is None else other_config

        log.info(f'Creating lswitch {name}')
        uuid = self.uuid_transaction(
            partial(
                self.idl.ls_add,
                name,
                external_ids=ext_ids,
                other_config=other_config,
            )
        )
        return LSwitch(
            name=name,
            cidr=net_s.n4,
            cidr6=net_s.n6,
            uuid=uuid,
        )

    def ls_get_uuid(self, name, timeout):
        for _ in range(timeout):
            uuid = self.idl.db_get(
                "Logical_Switch", str(name), '_uuid'
            ).execute()
            if uuid is not None:
                return uuid
            time.sleep(1)

        return None

    def ls_port_add(
        self,
        lswitch: LSwitch,
        name: str,
        router_port: Optional[LRPort] = None,
        mac: Optional[str] = None,
        ip: Optional[DualStackIP] = None,
        gw: Optional[DualStackIP] = None,
        ext_gw: Optional[DualStackIP] = None,
        metadata=None,  # typehint: ovn_workload.ChassisNode
        passive: bool = False,
        security: bool = False,
        localnet: bool = False,
        ext_ids: Optional[Dict] = None,
    ):
        columns = dict()
        if router_port:
            columns["type"] = "router"
            columns["addresses"] = "router"
            columns["options"] = {"router-port": router_port.name}
        elif mac or ip or localnet:
            addresses = []
            if mac:
                addresses.append(mac)
            if localnet:
                addresses.append("unknown")
            if ip and ip.ip4:
                addresses.append(str(ip.ip4))
            if ip and ip.ip6:
                addresses.append(str(ip.ip6))

            addresses = " ".join(addresses)

            columns["addresses"] = addresses
            if security:
                columns["port_security"] = addresses

        if ext_ids is not None:
            columns["external_ids"] = ext_ids

        uuid = self.uuid_transaction(
            partial(self.idl.lsp_add, lswitch.uuid, name, **columns)
        )

        ip4 = "unknown" if localnet else ip.ip4 if ip else None
        plen4 = ip.plen4 if ip else None
        gw4 = gw.ip4 if gw else None
        ext_gw4 = ext_gw.ip4 if ext_gw else None

        ip6 = ip.ip6 if ip else None
        plen6 = ip.plen6 if ip else None
        gw6 = gw.ip6 if gw else None
        ext_gw6 = ext_gw.ip6 if ext_gw else None

        return LSPort(
            name=name,
            mac=mac,
            ip=ip4,
            plen=plen4,
            gw=gw4,
            ext_gw=ext_gw4,
            ip6=ip6,
            plen6=plen6,
            gw6=gw6,
            ext_gw6=ext_gw6,
            metadata=metadata,
            passive=passive,
            uuid=uuid,
        )

    def ls_port_del(self, port):
        self.idl.lsp_del(port.name).execute()

    def ls_port_set_set_options(self, port: LSPort, options: str):
        """Set 'options' column for Logical Switch Port.

        :param port: Logical Switch Port to modify
        :param options: Space-separated key-value pairs that are set as
                        options. Keys and values are separated by '='.
                        i.e.: 'opt1=val1 opt2=val2'
        :return: None
        """
        opts = dict(
            (k, v)
            for k, v in (element.split("=") for element in options.split())
        )
        self.idl.lsp_set_options(port.name, **opts).execute()

    def ls_port_set_set_type(self, port, lsp_type):
        self.idl.lsp_set_type(port.name, lsp_type).execute()

    def ls_port_enable(self, port: LSPort) -> None:
        """Set Logical Switch Port's state to 'enabled'."""
        self.idl.lsp_set_enabled(port.name, True).execute()

    def ls_port_set_ipv4_address(self, port: LSPort, addr: str) -> LSPort:
        """Set Logical Switch Port's IPv4 address.

        :param port: LSPort to modify
        :param addr: IPv4 address to set
        :return: Modified LSPort object with updated 'ip' attribute
        """
        addresses = [f"{port.mac} {addr}"]
        log.info(f"Setting addresses for port {port.uuid}: {addresses}")
        self.idl.lsp_set_addresses(port.uuid, addresses).execute()

        return port._replace(ip=addr)

    def port_group_create(self, name, ext_ids: Optional[Dict] = None):
        ext_ids = {} if ext_ids is None else ext_ids
        self.idl.pg_add(name, external_ids=ext_ids).execute()
        return PortGroup(name=name)

    def port_group_add(self, pg, lport):
        self.idl.pg_add_ports(pg.name, lport.uuid).execute()

    def port_group_add_ports(self, pg: PortGroup, lports: List[LSPort]):
        MAX_PORTS_IN_BATCH = 500
        for i in range(0, len(lports), MAX_PORTS_IN_BATCH):
            lports_slice = lports[i : i + MAX_PORTS_IN_BATCH]
            port_uuids = [p.uuid for p in lports_slice]
            self.idl.pg_add_ports(pg.name, port_uuids).execute()

    def port_group_del(self, pg):
        self.idl.pg_del(pg.name).execute()

    def address_set_create(self, name):
        self.idl.address_set_add(name).execute()
        return AddressSet(name=name)

    def address_set_add(self, addr_set, addr):
        self.idl.address_set_add_addresses(addr_set.name, addr)

    def address_set_add_addrs(self, addr_set, addrs):
        MAX_ADDRS_IN_BATCH = 500
        for i in range(0, len(addrs), MAX_ADDRS_IN_BATCH):
            addrs_slice = [str(a) for a in addrs[i : i + MAX_ADDRS_IN_BATCH]]
            self.idl.address_set_add_addresses(
                addr_set.name, addrs_slice
            ).execute()

    def address_set_remove(self, addr_set, addr):
        self.idl.address_set_remove_addresses(addr_set.name, addr)

    def address_set_del(self, addr_set):
        self.idl.address_set_del(addr_set.name)

    def acl_add(
        self,
        name="",
        direction="from-lport",
        priority=100,
        entity="switch",
        match="",
        verdict="allow",
        ext_ids: Optional[Dict] = None,
    ):
        ext_ids = {} if ext_ids is None else ext_ids
        if entity == "switch":
            self.idl.acl_add(
                name, direction, priority, match, verdict, **ext_ids
            ).execute()
        else:  # "port-group"
            self.idl.pg_acl_add(
                name, direction, priority, match, verdict, **ext_ids
            ).execute()

    def route_add(self, router, network, gw, policy="dst-ip"):
        if network.n4 and gw.ip4:
            self.idl.lr_route_add(
                router.uuid, network.n4, gw.ip4, policy=policy
            ).execute()
        if network.n6 and gw.ip6:
            self.idl.lr_route_add(
                router.uuid, network.n6, gw.ip6, policy=policy
            ).execute()

    def nat_add(self, router, external_ip, logical_net, nat_type="snat"):
        if external_ip.ip4 and logical_net.n4:
            self.idl.lr_nat_add(
                router.uuid, nat_type, external_ip.ip4, logical_net.n4
            ).execute()
        if external_ip.ip6 and logical_net.n6:
            self.idl.lr_nat_add(
                router.uuid, nat_type, external_ip.ip6, logical_net.n6
            ).execute()

    def create_lb(self, name, protocol):
        lb_name = f"{name}-{protocol}"
        # We can't use ovsdbapp's lb_add here because it is not possible to
        # create a load balancer with no VIPs.
        uuid = self.db_create_transaction(
            "Load_Balancer",
            name=lb_name,
            protocol=protocol,
            options={
                "reject": "true",
                "event": "false",
                "skip_snat": "false",
                "hairpin_snat_ip": "169.254.169.5 fd69::5",  # magic IPs.
                "neighbor_responder": "none",
            },
            get_func=partial(self.idl.lb_get, lb_name),
        )
        return LoadBalancer(name=lb_name, uuid=uuid)

    def create_lbg(self, name):
        uuid = self.db_create_transaction(
            "Load_Balancer_Group",
            name=name,
            get_func=partial(
                self.idl.db_get, "Load_Balancer_Group", name, "uuid"
            ),
        )
        return LoadBalancerGroup(name=name, uuid=uuid)

    def lbg_add_lb(self, lbg, lb):
        self.idl.db_add(
            "Load_Balancer_Group", lbg.uuid, "load_balancer", lb.uuid
        ).execute()

    def ls_add_lbg(self, ls, lbg):
        self.idl.db_add(
            "Logical_Switch", ls.uuid, "load_balancer_group", lbg.uuid
        ).execute()

    def lr_add_lbg(self, lr, lbg):
        self.idl.db_add(
            "Logical_Router", lr.uuid, "load_balancer_group", lbg.uuid
        ).execute()

    def lr_set_options(self, router, options):
        str_options = dict((k, str(v)) for k, v in options.items())
        self.idl.db_set(
            "Logical_Router", router.uuid, ("options", str_options)
        ).execute()

    def lb_set_vips(self, lb, vips):
        vips = dict((k, ",".join(v)) for k, v in vips.items())
        self.idl.db_set("Load_Balancer", lb.uuid, ("vips", vips)).execute()

    def lb_clear_vips(self, lb):
        self.idl.db_clear("Load_Balancer", lb.uuid, "vips").execute()

    def lb_add_to_routers(self, lb, routers):
        with self.idl.transaction(check_error=True) as txn:
            for r in routers:
                txn.add(self.idl.lr_lb_add(r, lb.uuid, may_exist=True))

    def lb_add_to_switches(self, lb, switches):
        with self.idl.transaction(check_error=True) as txn:
            for s in switches:
                txn.add(self.idl.ls_lb_add(s, lb.uuid, may_exist=True))

    def lb_remove_from_routers(self, lb, routers):
        with self.idl.transaction(check_error=True) as txn:
            for r in routers:
                txn.add(self.idl.lr_lb_del(r, lb.uuid, if_exists=True))

    def lb_remove_from_switches(self, lb, switches):
        with self.idl.transaction(check_error=True) as txn:
            for s in switches:
                txn.add(self.idl.ls_lb_del(s, lb.uuid, if_exists=True))

    def create_dhcp_options(
        self, cidr: str, ext_ids: Optional[Dict] = None
    ) -> DhcpOptions:
        """Create entry in DHCP_Options table.

        :param cidr: DHCP address pool (i.e. '192.168.1.0/24')
        :param ext_ids: Optional entries to 'external_ids' column
        :return: DhcpOptions object
        """
        ext_ids = {} if ext_ids is None else ext_ids

        log.info(f"Creating DHCP Options for {cidr}. External IDs: {ext_ids}")
        add_command = self.idl.dhcp_options_add(cidr, **ext_ids)
        add_command.execute()

        return DhcpOptions(add_command.result.uuid, cidr)

    def dhcp_options_set_options(self, uuid_: str, options: Dict) -> None:
        """Set 'options' column for 'DHCP_Options' entry."""
        log.info(f"Setting DHCP options for {uuid_}: {options}")
        self.idl.dhcp_options_set_options(uuid_, **options).execute()

    def sync(self, wait="hv", timeout=DEFAULT_CTL_TIMEOUT):
        with self.idl.transaction(
            check_error=True, timeout=timeout, wait_type=wait
        ):
            pass


class BaseOvnSbIdl(connection.OvsdbIdl):
    schema = "OVN_Southbound"

    @classmethod
    def from_server(cls, connection_string):
        helper = idlutils.get_schema_helper(connection_string, cls.schema)
        helper.register_table('Chassis')
        helper.register_table('Connection')
        return cls(connection_string, helper)


class SBIdl(sb_impl_idl.OvnSbApiIdlImpl, Backend):
    def __init__(self, connection):
        super().__init__(connection)

    @property
    def _connection(self):
        # Shortcut to retrieve the lone Connection record. This is used by
        # NBTransaction for synchronization purposes.
        return next(iter(self.db_list_rows('Connection').execute()))


class OvnSbctl:
    def __init__(self, sb, connection_string, inactivity_probe):
        log.info(f'OvnSbctl: {connection_string}, probe: {inactivity_probe}')
        i = BaseOvnSbIdl.from_server(connection_string)
        c = connection.Connection(i, inactivity_probe)
        self.idl = SBIdl(c)

    def set_inactivity_probe(self, value):
        self.idl.db_set(
            "Connection",
            self.idl._connection.uuid,
            ("inactivity_probe", value),
        ).execute()

    def chassis_bound(self, chassis=""):
        cmd = self.idl.db_find_rows("Chassis", ("name", "=", chassis))
        cmd.execute()
        return len(cmd.result) == 1


class NBIcIdl(nb_ic_impl_idl.OvnIcNbApiIdlImpl, Backend):
    def __init__(self, connection):
        super(NBIcIdl, self).__init__(connection)

    @property
    def _connection(self):
        return next(iter(self.db_list_rows('Connection').execute()))


class OvnIcNbctl:
    def __init__(self, sb, connection_string, inactivity_probe):
        log.info(f'OvnIcNbctl: {connection_string}, probe: {inactivity_probe}')
        i = connection.OvsdbIdl.from_server(
            connection_string, "OVN_IC_Northbound"
        )
        c = connection.Connection(i, inactivity_probe)
        self.idl = NBIcIdl(c)

    def uuid_transaction(self, func):
        for _ in range(MAX_RETRY):
            cmd = func(may_exist=True)
            cmd.execute()
            try:
                return cmd.result.uuid
            except AttributeError:
                continue

        raise UUIDTransactionError("Failed to get UUID from transaction")

    def ts_add(self):
        log.info('Creating transit switch')
        self.uuid_transaction(partial(self.idl.ts_add, 'ts'))
