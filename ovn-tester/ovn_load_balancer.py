import itertools

VALID_PROTOCOLS = ['tcp', 'udp', 'sctp']


class InvalidProtocol(Exception):
    def __init__(self, invalid_protocols):
        self.args = invalid_protocols

    def __str__(self):
        return f"Invalid Protocol: {self.args}"


async def create_load_balancer(lb_name, nbctl, vips=None,
                               protocols=VALID_PROTOCOLS):
    lb = OvnLoadBalancer(lb_name, nbctl)
    valid_protocols = [prot for prot in protocols if prot in VALID_PROTOCOLS]
    if len(valid_protocols) == 0:
        raise InvalidProtocol(protocols)
    for protocol in valid_protocols:
        lb.lbs.append(await nbctl.create_lb(lb_name, protocol))
    if vips:
        await lb.add_vips(vips)
    return lb


class OvnLoadBalancer(object):
    def __init__(self, lb_name, nbctl):
        '''
        Create load balancers with optional vips.
        lb_name: String used as basis for load balancer name.
        nbctl: Connection used for ovn-nbctl commands
        vips: Optional dictionary mapping VIPs to a list of backend IPs.
        protocols: List of protocols to use when creating Load Balancers.
        '''
        self.nbctl = nbctl
        self.name = lb_name
        self.vips = dict()
        self.lbs = []

    async def add_vips(self, vips):
        '''
        Add VIPs to a load balancer.
        vips: Dictionary with key being a VIP string, and value being a list of
        backend IP address strings. It's perfectly acceptable for the VIP to
        have no backends.
        '''
        MAX_VIPS_IN_BATCH = 500
        for i in range(0, len(vips), MAX_VIPS_IN_BATCH):
            updated_vips = {}
            for vip, backends in itertools.islice(vips.items(),
                                                  i, i + MAX_VIPS_IN_BATCH):
                cur_backends = self.vips.setdefault(vip, [])
                if backends:
                    cur_backends.extend(backends)
                updated_vips[vip] = cur_backends

            for lb in self.lbs:
                await self.nbctl.lb_set_vips(lb.uuid, updated_vips)

    async def clear_vips(self):
        '''
        Clear all VIPs from the load balancer.
        '''
        self.vips.clear()
        for lb in self.lbs:
            await self.nbctl.lb_clear_vips(lb.uuid)

    async def add_backends_to_vip(self, backends, vips=None):
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
            await self.nbctl.lb_set_vips(lb.uuid, self.vips)

    async def add_to_routers(self, routers):
        for lb in self.lbs:
            await self.nbctl.lb_add_to_routers(lb.uuid, routers)

    async def add_to_switches(self, switches):
        for lb in self.lbs:
            await self.nbctl.lb_add_to_switches(lb.uuid, switches)

    async def remove_from_routers(self, routers):
        for lb in self.lbs:
            await self.nbctl.lb_remove_from_routers(lb.uuid, routers)

    async def remove_from_switches(self, switches):
        for lb in self.lbs:
            await self.nbctl.lb_remove_from_switches(lb.uuid, switches)
