class OvnTestException(Exception):
    pass


class OvnInvalidConfigException(OvnTestException):
    pass


class OvnPingTimeoutException(OvnTestException):
    pass


class OvnChassisTimeoutException(OvnTestException):
    pass


class OvnConvergenceTimeoutException(OvnTestException):
    pass


class SSHError(OvnTestException):
    pass
