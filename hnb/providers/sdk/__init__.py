"""Provider SDK — everything a provider module needs, in one import.

    from hnb.providers.sdk import *

    class AcmeAdapter(ProviderAdapter):
        provider_id = "acme"
        display_name = "Acme"
        description = "Acme plugs on your network."
        ...

    ADAPTER = AcmeAdapter          # in plugins/acme/adapter.py

See ADDING_A_PROVIDER.md for the walk-through and PROVIDERS.md for the rules
(errors, timeouts, retries, state verification). Prove an adapter with
`hnb.providers.sdk.conformance`.
"""
from hnb.core.capabilities import (
    CapabilityError,
    CapabilitySpec,
    CapabilityType,
    Sensitivity,
    validate,
)
from hnb.core.model import DeviceKind
from hnb.providers.base import (
    AuthExpired,
    AuthResult,
    AuthStatus,
    ConfigField,
    DeviceRef,
    DeviceUnavailable,
    DiscoveredDevice,
    ExecuteResult,
    ExecuteStatus,
    HealthStatus,
    PairingError,
    ProviderAdapter,
    ProviderError,
    ProviderHealth,
    ProviderUnavailable,
    SecretHandle,
    StateReading,
)

__all__ = [
    "AuthExpired", "AuthResult", "AuthStatus", "CapabilityError", "CapabilitySpec", "CapabilityType",
    "ConfigField", "DeviceKind", "DeviceRef", "DeviceUnavailable", "DiscoveredDevice", "ExecuteResult",
    "ExecuteStatus", "HealthStatus", "PairingError", "ProviderAdapter", "ProviderError", "ProviderHealth",
    "ProviderUnavailable", "SecretHandle", "Sensitivity", "StateReading", "validate",
]
