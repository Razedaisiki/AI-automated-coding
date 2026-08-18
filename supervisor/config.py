"""`supervisor.toml` 配置加载（M1）。

优先标准库 tomllib（Python 3.11+），回退 tomli（Python 3.8-3.10）。
严格校验类型与取值范围；错误信息明确，不裸崩。
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore

from .models import AGENT_SCHEMA_VERSION


class ConfigError(ValueError):
    """Invalid or unreadable supervisor.toml."""


@dataclass
class DshConfig:
    executable: str = "dsh"
    profile: str = "headless"


@dataclass
class LimitsConfig:
    max_parent_activations: int = 20
    max_crash_restarts: int = 5
    max_clean_restarts: int = 10
    max_timeouts: int = 3
    max_ci_wakeups: int = 10
    parent_timeout_seconds: int = 2700
    terminate_grace_seconds: int = 10
    max_active_wall_seconds: int = 14400


@dataclass
class RestartConfig:
    backoff_seconds: List[float] = field(default_factory=lambda: [2, 5, 15, 30, 60])


@dataclass
class CiConfig:
    enabled: bool = False
    provider: str = "github"
    poll_seconds: int = 30
    discovery_grace_seconds: int = 180
    max_wait_seconds: int = 7200


@dataclass
class HumanConfig:
    pause_active_wall_clock: bool = True


@dataclass
class Config:
    version: int = 1
    dsh: DshConfig = field(default_factory=DshConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    restart: RestartConfig = field(default_factory=RestartConfig)
    ci: CiConfig = field(default_factory=CiConfig)
    human: HumanConfig = field(default_factory=HumanConfig)


def default_config() -> Config:
    return Config()


def _require_int(raw: Any, path: str, minimum: int = 0, positive: bool = False) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"config {path} must be an integer, got {raw!r}")
    if positive and raw <= 0:
        raise ConfigError(f"config {path} must be > 0, got {raw}")
    if raw < minimum:
        raise ConfigError(f"config {path} must be >= {minimum}, got {raw}")
    return raw


def _require_str(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"config {path} must be a non-empty string, got {raw!r}")
    return raw


def _require_bool(raw: Any, path: str) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(f"config {path} must be a boolean, got {raw!r}")
    return raw


def _warn(msg: str) -> None:
    print(f"supervisor: warning: {msg}", file=sys.stderr)


def _build_limits(sec_raw: Any) -> LimitsConfig:
    if sec_raw is None:
        return LimitsConfig()
    if not isinstance(sec_raw, dict):
        raise ConfigError("config [limits] must be a table")
    unknown = set(sec_raw) - {
        "max_parent_activations", "max_crash_restarts", "max_clean_restarts",
        "max_timeouts", "max_ci_wakeups", "parent_timeout_seconds",
        "terminate_grace_seconds", "max_active_wall_seconds",
    }
    for k in unknown:
        _warn(f"unknown key in [limits]: {k}")
    return LimitsConfig(
        max_parent_activations=_require_int(
            sec_raw.get("max_parent_activations", 20), "[limits].max_parent_activations"),
        max_crash_restarts=_require_int(
            sec_raw.get("max_crash_restarts", 5), "[limits].max_crash_restarts"),
        max_clean_restarts=_require_int(
            sec_raw.get("max_clean_restarts", 10), "[limits].max_clean_restarts"),
        max_timeouts=_require_int(
            sec_raw.get("max_timeouts", 3), "[limits].max_timeouts"),
        max_ci_wakeups=_require_int(
            sec_raw.get("max_ci_wakeups", 10), "[limits].max_ci_wakeups"),
        parent_timeout_seconds=_require_int(
            sec_raw.get("parent_timeout_seconds", 2700), "[limits].parent_timeout_seconds",
            positive=True),
        terminate_grace_seconds=_require_int(
            sec_raw.get("terminate_grace_seconds", 10), "[limits].terminate_grace_seconds"),
        max_active_wall_seconds=_require_int(
            sec_raw.get("max_active_wall_seconds", 14400), "[limits].max_active_wall_seconds"),
    )


def _build_restart(sec_raw: Any) -> RestartConfig:
    if sec_raw is None:
        return RestartConfig()
    if not isinstance(sec_raw, dict):
        raise ConfigError("config [restart] must be a table")
    for k in set(sec_raw) - {"backoff_seconds"}:
        _warn(f"unknown key in [restart]: {k}")
    backoff = sec_raw.get("backoff_seconds", [2, 5, 15, 30, 60])
    if not isinstance(backoff, list) or not backoff:
        raise ConfigError("config [restart].backoff_seconds must be a non-empty array")
    for i, v in enumerate(backoff):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(
                f"config [restart].backoff_seconds[{i}] must be a non-negative number, got {v!r}")
    return RestartConfig(backoff_seconds=[float(v) for v in backoff])


def _build_ci(sec_raw: Any) -> CiConfig:
    if sec_raw is None:
        return CiConfig()
    if not isinstance(sec_raw, dict):
        raise ConfigError("config [ci] must be a table")
    unknown = set(sec_raw) - {
        "enabled", "provider", "poll_seconds",
        "discovery_grace_seconds", "max_wait_seconds",
    }
    for k in unknown:
        _warn(f"unknown key in [ci]: {k}")
    return CiConfig(
        enabled=_require_bool(sec_raw.get("enabled", False), "[ci].enabled"),
        provider=_require_str(sec_raw.get("provider", "github"), "[ci].provider"),
        poll_seconds=_require_int(
            sec_raw.get("poll_seconds", 30), "[ci].poll_seconds", positive=True),
        discovery_grace_seconds=_require_int(
            sec_raw.get("discovery_grace_seconds", 180), "[ci].discovery_grace_seconds"),
        max_wait_seconds=_require_int(
            sec_raw.get("max_wait_seconds", 7200), "[ci].max_wait_seconds", positive=True),
    )


def _build_human(sec_raw: Any) -> HumanConfig:
    if sec_raw is None:
        return HumanConfig()
    if not isinstance(sec_raw, dict):
        raise ConfigError("config [human] must be a table")
    for k in set(sec_raw) - {"pause_active_wall_clock"}:
        _warn(f"unknown key in [human]: {k}")
    return HumanConfig(
        pause_active_wall_clock=_require_bool(
            sec_raw.get("pause_active_wall_clock", True), "[human].pause_active_wall_clock"),
    )


def load_config(path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}")

    version = data.get("version", 1)
    if version != AGENT_SCHEMA_VERSION:
        raise ConfigError(f"unsupported supervisor.toml version={version!r} (expected 1)")

    for k in set(data) - {
        "version", "dsh", "limits", "restart", "ci", "human",
    }:
        _warn(f"unknown top-level key in supervisor.toml: {k}")

    dsh_raw = data.get("dsh")
    if dsh_raw is not None:
        if not isinstance(dsh_raw, dict):
            raise ConfigError("config [dsh] must be a table")
        for k in set(dsh_raw) - {"executable", "profile"}:
            _warn(f"unknown key in [dsh]: {k}")
        dsh = DshConfig(
            executable=_require_str(dsh_raw.get("executable", "dsh"), "[dsh].executable"),
            profile=_require_str(dsh_raw.get("profile", "headless"), "[dsh].profile"),
        )
    else:
        dsh = DshConfig()

    return Config(
        version=version,
        dsh=dsh,
        limits=_build_limits(data.get("limits")),
        restart=_build_restart(data.get("restart")),
        ci=_build_ci(data.get("ci")),
        human=_build_human(data.get("human")),
    )