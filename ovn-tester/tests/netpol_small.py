from tests.netpol import NetPol


class NetpolSmall(NetPol):
    def __init__(self, config, central_node, worker_nodes):
        super(NetpolSmall, self).__init__(
                'netpol_small', config, central_node, worker_nodes)

    async def run(self, ovn, global_cfg):
        await self.init(ovn)
        await super(NetpolSmall, self).run(ovn, global_cfg, True)
