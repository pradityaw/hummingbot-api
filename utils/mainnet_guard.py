import os
from typing import Any, Iterable, Mapping, Sequence

ALLOW_MAINNET_ENV = "HB_ALLOW_MAINNET"


class MainnetConnectorBlockedError(Exception):
    """Raised when non-testnet connectors are used without explicit opt-in."""

    def __init__(self, connector_names: Sequence[str]):
        sorted_names = sorted(set(connector_names))
        self.connector_names = sorted_names
        names_text = ", ".join(sorted_names)
        super().__init__(
            f"Non-testnet connector(s) blocked: {names_text}. "
            f"Connector names must end with '_testnet' unless {ALLOW_MAINNET_ENV}=1 is set."
        )


def is_mainnet_allowed() -> bool:
    return os.environ.get(ALLOW_MAINNET_ENV) == "1"


def is_testnet_connector(connector_name: str) -> bool:
    return connector_name.endswith("_testnet")


def find_non_testnet_connectors(connector_names: Iterable[str]) -> list[str]:
    return sorted({name for name in connector_names if name and not is_testnet_connector(name)})


def validate_testnet_connectors(connector_names: Iterable[str]) -> None:
    blocked = find_non_testnet_connectors(connector_names)
    if blocked and not is_mainnet_allowed():
        raise MainnetConnectorBlockedError(blocked)


def extract_connector_names_from_mapping(mapping: Mapping[str, Any]) -> set[str]:
    """Extract connector names from a controller or deployment config mapping."""
    names: set[str] = set()
    for key, value in mapping.items():
        if key == "connector_name" and isinstance(value, str) and value:
            names.add(value)
        elif key.endswith("_connector") and isinstance(value, str) and value:
            names.add(value)
        elif isinstance(value, Mapping):
            nested_connector = value.get("connector_name")
            if isinstance(nested_connector, str) and nested_connector:
                names.add(nested_connector)
    return names
