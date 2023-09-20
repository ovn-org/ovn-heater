import itertools

VALID_PROTOCOLS = ['tcp', 'udp', 'sctp']


class InvalidProtocol(Exception):
    def __init__(self, invalid_protocols):
        self.args = invalid_protocols

    def __str__(self):
        return f"Invalid Protocol: {self.args}"


class OvnLoadBalancer:
    def __init__(self, lb_name, nbctl, vips=None, protocols=VALID_PROTOCOLS):
        '''
        Create load balancers with optional vips.
        lb_name: String used as basis for load balancer name.
        nbctl: Connection used for ovn-nbctl commands
        vips: Optional dictionary mapping VIPs to a list of backend IPs.
        protocols: List of protocols to use when creating Load Balancers.
        '''
        self.nbctl = nbctl
        self.protocols = [
            prot for prot in protocols if prot in VALID_PROTOCOLS
        ]
        if len(self.protocols) == 0:
            raise InvalidProtocol(protocols)
        self.name = lb_name
        self.vips = {}
        self.lbs = []
        for protocol in self.protocols:
            self.lbs.append(self.nbctl.create_lb(self.name, protocol))
        if vips:
            self.add_vips(vips)

    def add_vip(self, vip, vport, backends, backend_port, version):
        self.add_vips(
            OvnLoadBalancer.get_vip_map(
                vip, vport, backends, backend_port, version
            )
        )

    def add_vips(self, vips):
        '''
        Add VIPs to a load balancer.
        vips: Dictionary with key being a VIP string, and value being a list of
        backend IP address strings. It's perfectly acceptable for the VIP to
        have no backends.
        '''
        MAX_VIPS_IN_BATCH = 500
        for i in range(0, len(vips), MAX_VIPS_IN_BATCH):
            updated_vips = {}
            for vip, backends in itertools.islice(
                vips.items(), i, i + MAX_VIPS_IN_BATCH
            ):
                cur_backends = self.vips.setdefault(vip, [])
                if backends:
                    cur_backends.extend(backends)
                updated_vips[vip] = cur_backends

            for lb in self.lbs:
                self.nbctl.lb_set_vips(lb, updated_vips)

    def clear_vips(self):
        '''
        Clear all VIPs from the load balancer.
        '''
        self.vips.clear()
        for lb in self.lbs:
            self.nbctl.lb_clear_vips(lb)

    def add_backends_to_vip(self, backends, vips=None):
        '''
        Add backends to existing load balancer VIPs.
        backends: A list of IP addresses to add as backends to VIPs.
        vips: An iterable of VIPs to which backends should be added. If this is
        'None' then the backends are added to all VIPs.
        '''
        for cur_vip, cur_backends in self.vips.items():
            if not vips or cur_vip in vips:
                cur_backends.extend(backends)

        for lb in self.lbs:
            self.nbctl.lb_set_vips(lb, self.vips)

    def add_to_routers(self, routers):
        for lb in self.lbs:
            self.nbctl.lb_add_to_routers(lb, routers)

    def add_to_switches(self, switches):
        for lb in self.lbs:
            self.nbctl.lb_add_to_switches(lb, switches)

    def remove_from_routers(self, routers):
        for lb in self.lbs:
            self.nbctl.lb_remove_from_routers(lb, routers)

    def remove_from_switches(self, switches):
        for lb in self.lbs:
            self.nbctl.lb_remove_from_switches(lb, switches)

    @staticmethod
    def get_vip_map(vip, vport, backends, backend_port, version):
        if version == 6:
            return {
                f'[{vip}]:{vport}': [
                    f'[{b.ip6}]:{backend_port}' for b in backends
                ]
            }
        else:
            return {
                f'{vip}:{vport}': [f'{b.ip}:{backend_port}' for b in backends]
            }


class OvnLoadBalancerGroup:
    def __init__(self, group_name, nbctl):
        self.nbctl = nbctl
        self.name = group_name
        self.lbg = self.nbctl.create_lbg(self.name)

    def add_lb(self, ovn_lb):
        for lb in ovn_lb.lbs:
            self.nbctl.lbg_add_lb(self.lbg, lb)
