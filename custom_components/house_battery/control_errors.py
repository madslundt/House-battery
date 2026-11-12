"""Errors shared by serialized battery control adapters."""


class CommandSuperseded(Exception):
    """A safety request revoked an older control transaction."""
