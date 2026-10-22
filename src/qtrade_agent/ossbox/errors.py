"""Safe, public error codes for collector-owned configuration."""


class OssboxSettingsError(ValueError):
    def __init__(self, reason: str, *, unavailable: bool = False):
        super().__init__(reason)
        self.reason, self.unavailable = reason, unavailable
