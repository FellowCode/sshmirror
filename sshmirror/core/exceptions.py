class ErrorLocalVersion(Exception):
    pass


class UserAbort(Exception):
    pass


class VersionAlreadyExists(Exception):
    pass


class IncompatibleVersionFormat(Exception):
    pass


class RemoteSyncLockError(Exception):
    pass