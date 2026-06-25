"""YAML configuration loader with environment variable resolution.

Provides utilities for loading, merging, validating, and type-safely
accessing YAML configuration files with ``$VAR`` / ``${VAR}`` env-var
interpolation in string values.
"""

from __future__ import annotations

import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Any, ClassVar

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

# Match ``$VAR`` or ``${VAR}`` — but not ``$$`` (escaped dollar).
_ENV_VAR_RE = re.compile(r"(?<!\$)\$(\w+|\{[^}]+\})")


def resolve_env_vars(obj: Any) -> Any:
    """Recursively resolve ``$VAR`` and ``${VAR}`` patterns in string values.

    Traverses nested ``dict`` / ``list`` structures and replaces every
    environment-variable reference it finds inside string scalars with the
    value of the corresponding environment variable.

    A literal ``$$`` is left as a single ``$``.  A reference to an unset
    variable is kept unchanged in the output.

    Parameters
    ----------
    obj :
        The object to transform — typically a parsed YAML ``dict``.

    Returns
    -------
        The same structure with all env-var references resolved.
    """

    def _resolve_one(raw: str) -> str:
        def _replacer(m: re.Match) -> str:
            inner = m.group(1)
            # Strip braces for ${VAR} syntax
            var_name = inner[1:-1] if inner.startswith("{") else inner
            return os.environ.get(var_name, m.group(0))

        return _ENV_VAR_RE.sub(_replacer, raw).replace("$$", "$")

    if isinstance(obj, dict):
        return {k: resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_env_vars(item) for item in obj]
    if isinstance(obj, str):
        return _resolve_one(obj)
    return obj


def load_yaml_config(path: str) -> dict:
    """Load a YAML file and resolve environment variables in its values.

    Parameters
    ----------
    path :
        Filesystem path to the YAML file.

    Returns
    -------
        The parsed configuration as a ``dict`` with all env-var references
        resolved.  Returns ``{}`` if the file does not exist.

    Raises
    ------
    yaml.YAMLError
        If the file exists but cannot be parsed.
    """
    try:
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.warning("Config file not found — returning empty dict: %s", path)
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise yaml.YAMLError(
            f"Expected a top-level mapping in {path!r}, got {type(raw).__name__}"
        )
    return resolve_env_vars(raw)


def deep_get(data: dict, key_path: str, default: Any = None) -> Any:
    """Access nested dict values via a dot-separated key path.

    Parameters
    ----------
    data :
        The dictionary to query.
    key_path :
        Dot-separated path, e.g. ``"memory.injection_enabled"``.
    default :
        Value returned when any key in the path is missing.

    Examples
    --------
    >>> config = {"memory": {"injection_enabled": True}}
    >>> deep_get(config, "memory.injection_enabled")
    True
    >>> deep_get(config, "memory.nonexistent", False)
    False
    """
    if not key_path:
        return default
    keys = key_path.split(".")
    current: Any = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def deep_set(data: dict, key_path: str, value: Any) -> None:
    """Set a nested dict value via a dot-separated key path.

    Intermediate dictionaries are created on demand.

    Parameters
    ----------
    data :
        The dictionary to mutate.
    key_path :
        Dot-separated path, e.g. ``"memory.storage_path"``.
    value :
        The value to assign at the leaf key.

    Examples
    --------
    >>> d = {}
    >>> deep_set(d, "a.b.c", 42)
    >>> d
    {'a': {'b': {'c': 42}}}
    """
    if not key_path:
        return
    keys = key_path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def merge_configs(base: dict, override: dict) -> dict:
    """Deep-merge two dictionaries, with *override* taking precedence.

    When both values for a key are dicts they are merged recursively.
    Otherwise the *override* value replaces the *base* value entirely.

    Parameters
    ----------
    base :
        Base configuration.
    override :
        Overriding configuration (higher priority).

    Returns
    -------
        A new merged dictionary (neither input is mutated).
    """
    merged = base.copy()
    for key, val in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(val, dict)
        ):
            merged[key] = merge_configs(merged[key], val)
        else:
            merged[key] = val
    return merged


def validate_config_version(data: dict, expected: int = 10) -> None:
    """Check that *data* contains a valid ``config_version`` field.

    Parameters
    ----------
    data :
        Parsed configuration dictionary.
    expected :
        The expected config version (default 10).

    Warns
    -----
    If the version field is missing.
    If the version is below *expected*.
    """
    version = data.get("config_version")
    if version is None:
        warnings.warn(
            "config_version is missing from the configuration file — "
            "the file format may not be fully compatible.",
            UserWarning,
            stacklevel=2,
        )
        logger.warning("config_version missing from configuration data.")
        return
    if version < expected:
        logger.warning(
            "Config version %s is lower than expected %s. "
            "Some settings may behave differently.",
            version,
            expected,
        )


# ---------------------------------------------------------------------------
# ConfigSection base class and concrete sections
# ---------------------------------------------------------------------------


@dataclass
class ConfigSection:
    """Base dataclass for type-safe configuration section parsing.

    Subclasses should declare typed fields that mirror keys in the
    corresponding YAML section.  Field names can differ from YAML keys —
    override ``_field_map`` (a ``dict[str, str]`` of ``"yaml_key" ->
    "field_name"``) in the subclass when mapping is needed.
    """

    _field_map: ClassVar[dict[str, str]] = {}

    @classmethod
    def from_dict(cls, data: dict) -> ConfigSection:
        """Create an instance from a parsed YAML dict, ignoring unknown keys.

        Parameters
        ----------
        data :
            Raw dictionary values for this section.
        """
        if not data:
            data = {}
        field_map = cls._field_map
        # Build a mapping from field name -> YAML key for reverse lookup
        yaml_to_field = {v: k for k, v in field_map.items()}
        filtered = {}
        for yaml_key, raw_value in data.items():
            # Use the annotation field name if a mapping exists, otherwise
            # assume the YAML key matches the Python field name directly.
            field_name = yaml_to_field.get(yaml_key, yaml_key)
            if field_name in cls.__annotations__:
                annotation = cls.__annotations__[field_name]
                filtered[field_name] = _coerce_value(raw_value, annotation)
            else:
                # Silently skip unknown keys
                pass
        return cls(**filtered)  # type: ignore[call-arg]


def _coerce_value(value: Any, annotation: type) -> Any:
    """Best-effort coercion of *value* to *annotation*."""
    if annotation is Any or value is None:
        return value
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        if isinstance(value, list):
            return value
        return [value] if value is not None else []
    if origin is dict:
        return value if isinstance(value, dict) else {}
    # Primitive coercions for JSON -> Python
    if annotation is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    if annotation is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if annotation is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return value


# ---------------------------------------------------------------------------
# Concrete section dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig(ConfigSection):
    """Configuration for a single model entry."""

    name: str = ""
    display_name: str = ""
    use: bool = True
    model: str = ""
    api_key: str = ""
    api_base: str = ""
    base_url: str = ""
    timeout: int = 60
    max_retries: int = 3
    max_tokens: int = 4096
    temperature: float = 0.7
    supports_thinking: bool = False
    supports_vision: bool = False
    supports_reasoning_effort: bool = False
    when_thinking_enabled: dict = field(default_factory=dict)
    when_thinking_disabled: dict = field(default_factory=dict)
    use_responses_api: bool = False
    output_version: str = ""


@dataclass
class SandboxConfig(ConfigSection):
    """Configuration for the execution sandbox."""

    use: bool = False
    allow_host_bash: bool = False
    bash_output_max_chars: int = 10000
    read_file_output_max_chars: int = 5000
    ls_output_max_chars: int = 2000
    mounts: list[dict] = field(default_factory=list)


@dataclass
class MemoryConfig(ConfigSection):
    """Configuration for the layered memory system."""

    enabled: bool = True
    storage_path: str = "./memory"
    debounce_seconds: float = 30.0
    model_name: str = ""
    max_facts: int = 500
    fact_confidence_threshold: float = 0.6
    injection_enabled: bool = False
    max_injection_tokens: int = 512


@dataclass
class SummarizationConfig(ConfigSection):
    """Configuration for conversation summarization."""

    enabled: bool = True
    model_name: str = ""
    trigger: list[dict] = field(default_factory=list)
    keep: dict = field(default_factory=dict)
    trim_tokens_to_summarize: int = 8000


@dataclass
class DatabaseConfig(ConfigSection):
    """Configuration for the application database (Runs, Threads, Events).

    Separate from the LangGraph *checkpointer* config, though both default
    to the same ``deerflow.db`` file for single-node SQLite deployments.
    """

    section = "database"
    backend: str = "sqlite"  # memory | sqlite | postgres
    sqlite_dir: str = ""  # empty → resolved via Paths.data_dir
    postgres_url: str = ""
