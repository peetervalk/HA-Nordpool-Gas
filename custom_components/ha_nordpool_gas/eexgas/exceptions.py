"""Exceptions raised by the EEX gas client."""


class EexGasError(Exception):
    """Base class for EEX gas client errors."""


class EexGasConnectionError(EexGasError):
    """The file could not be downloaded: network error, timeout or HTTP error status."""


class EexGasResponseError(EexGasError):
    """The download succeeded but did not contain an EEX NGP file."""
