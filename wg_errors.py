"""Structured exceptions for WireGuard connection management.

Each class has a stable `category` string that is written to logs and to the
history database, so failures can be grouped without parsing messages.
"""

from __future__ import annotations


class WireGuardError(Exception):
    category = "wireguard_error"


class NoConfigsError(WireGuardError):
    category = "no_configs"


class NotImportedError(WireGuardError):
    """A config with no unique WireGuard-app tunnel (same endpoint) to control."""

    category = "not_imported"


class ConflictingVpnError(WireGuardError):
    category = "conflicting_vpn"


class TunnelUpError(WireGuardError):
    category = "tunnel_start_failed"


class InterfaceError(WireGuardError):
    category = "interface_error"


class RoutingError(WireGuardError):
    category = "routing_error"


class DnsError(WireGuardError):
    category = "dns_error"


class ConnectivityError(WireGuardError):
    category = "connectivity_error"


class PublicIpError(WireGuardError):
    category = "public_ip_error"


class RestoreError(WireGuardError):
    category = "restore_failed"


class SubprocessError(WireGuardError):
    category = "subprocess_failed"


class InvalidTransition(WireGuardError):
    category = "invalid_transition"


class RotationInProgress(WireGuardError):
    category = "rotation_in_progress"


class LockHeldError(WireGuardError):
    category = "lock_held"


class HaltedError(WireGuardError):
    """Connection management stopped because the host network could not be
    restored safely. Nothing reconnects until a person intervenes."""

    category = "halted"
