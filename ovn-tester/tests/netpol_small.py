from tests.netpol import NetPol


class NetpolSmall(NetPol):
    def __init__(self, config):
        super(NetpolSmall, self).__init__('netpol_small', config)

    def run(self, ovn, global_cfg):
        self.init(ovn)
        super(NetpolSmall, self).run(ovn, global_cfg, True)
