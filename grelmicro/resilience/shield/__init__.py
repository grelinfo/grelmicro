"""Shield resilience pattern."""

from __future__ import annotations

from grelmicro.resilience.shield._api import ApiShieldConfig
from grelmicro.resilience.shield._decorator import shield
from grelmicro.resilience.shield._internal import InternalShieldConfig
from grelmicro.resilience.shield._shield import Shield, ShieldConfig
from grelmicro.resilience.shield._slow import SlowShieldConfig

__all__ = [
    "ApiShieldConfig",
    "InternalShieldConfig",
    "Shield",
    "ShieldConfig",
    "SlowShieldConfig",
    "shield",
]
