"""Exception types shared by pure modules (no I/O imports here)."""


class ConfigError(Exception):
    """The configuration or rules are invalid. The message is meant for the user."""
