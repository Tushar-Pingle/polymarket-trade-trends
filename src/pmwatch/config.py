"""Configuration loading and strict validation.

``config.yaml`` is the single source of truth for every tunable. This module
parses it into typed, frozen dataclasses and validates it eagerly so that a
typo or wrong type fails *immediately at startup* with an actionable message,
rather than surfacing as a confusing ``KeyError`` deep inside a watcher loop
hours later.

Usage
-----
>>> from pmwatch.config import load_config
>>> cfg = load_config("config.yaml")
>>> cfg.alerts.consensus.min_wallets
3
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when configuration is missing required keys or has bad types."""


# --------------------------------------------------------------------------- #
# Small validation helpers. Each raises ConfigError with the offending path so
# the user can jump straight to the line in config.yaml.
# --------------------------------------------------------------------------- #
def _require(mapping: dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing required config key: {path}.{key}")
    return mapping[key]


def _as_float(value: Any, path: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Config {path} must be a number, got {value!r}") from exc


def _as_int(value: Any, path: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Config {path} must be an integer, got {value!r}") from exc


def _as_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Config {path} must be a non-empty string, got {value!r}")
    return value


# --------------------------------------------------------------------------- #
# Typed configuration sections.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ApiConfig:
    data_base_url: str
    gamma_base_url: str
    timeout_seconds: float
    max_retries: int
    rate_limit_per_sec: float


@dataclass(frozen=True)
class DiscordConfig:
    webhook_env: str
    error_webhook_env: str
    username: str
    # Resolved at load time from the environment (.env). May be None if unset,
    # which is tolerated for --dry-run flows that never actually post.
    webhook_url: str | None
    error_webhook_url: str | None


@dataclass(frozen=True)
class SingleAlertConfig:
    enabled: bool
    high_rank_threshold: int


@dataclass(frozen=True)
class ConsensusAlertConfig:
    min_wallets: int
    window_hours: int
    escalation_step: int
    alert_on_sell: bool


@dataclass(frozen=True)
class AlertColors:
    muted: int
    warning: int
    strong: int
    max: int
    info: int
    error: int


@dataclass(frozen=True)
class AlertsConfig:
    min_trade_size_usd: float
    single: SingleAlertConfig
    consensus: ConsensusAlertConfig
    colors: AlertColors


@dataclass(frozen=True)
class PollConfig:
    interval_seconds: int
    trades_limit: int


@dataclass(frozen=True)
class ScoringConfig:
    win_points: float
    loss_points: float
    dump_penalty: float
    roi_weight: float


@dataclass(frozen=True)
class LeaderboardConfig:
    board_size: int
    bench_size: int
    decay_half_life_days: float
    scoring: ScoringConfig


@dataclass(frozen=True)
class DiscoveryConfig:
    markets_per_niche: int
    holders_per_market: int


@dataclass(frozen=True)
class Niche:
    key: str
    display_name: str
    gamma_tags: list[str]


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: str
    data_dir: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    json: bool


@dataclass(frozen=True)
class Config:
    """The fully-parsed, validated application configuration."""

    api: ApiConfig
    discord: DiscordConfig
    alerts: AlertsConfig
    poll: PollConfig
    leaderboard: LeaderboardConfig
    discovery: DiscoveryConfig
    niches: dict[str, Niche]
    storage: StorageConfig
    logging: LoggingConfig
    wallet_aliases: dict[str, str] = field(default_factory=dict)

    def niche(self, key: str) -> Niche:
        """Look up a niche by key, raising a clear error if it doesn't exist."""
        if key not in self.niches:
            valid = ", ".join(sorted(self.niches)) or "(none configured)"
            raise ConfigError(f"Unknown niche {key!r}. Configured niches: {valid}")
        return self.niches[key]

    def alias_for(self, wallet: str) -> str:
        """Friendly name for a wallet if configured, else a shortened address."""
        addr = wallet.lower()
        if addr in self.wallet_aliases:
            return self.wallet_aliases[addr]
        return f"{addr[:6]}…{addr[-4:]}" if len(addr) >= 10 else addr


def load_config(path: str | Path = "config.yaml", *, load_env: bool = True) -> Config:
    """Load, validate, and return the application configuration.

    Parameters
    ----------
    path:
        Path to the YAML config file.
    load_env:
        When True (default) also load a sibling ``.env`` file so that the
        Discord webhook(s) referenced by name in the config resolve from the
        environment.

    Raises
    ------
    ConfigError
        If the file is missing, malformed, or fails validation.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    if load_env:
        # Load .env next to the config (and the process CWD) so DISCORD_WEBHOOK_URL
        # etc. are available. Real environment variables always take precedence.
        load_dotenv(config_path.parent / ".env")
        load_dotenv()

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Top-level config.yaml must be a mapping")

    api = _parse_api(_require(raw, "api", "<root>"))
    discord = _parse_discord(_require(raw, "discord", "<root>"))
    alerts = _parse_alerts(_require(raw, "alerts", "<root>"))
    poll = _parse_poll(_require(raw, "poll", "<root>"))
    leaderboard = _parse_leaderboard(_require(raw, "leaderboard", "<root>"))
    discovery = _parse_discovery(_require(raw, "discovery", "<root>"))
    niches = _parse_niches(_require(raw, "niches", "<root>"))
    storage = _parse_storage(_require(raw, "storage", "<root>"))
    logging_cfg = _parse_logging(raw.get("logging", {}))
    aliases = {str(k).lower(): str(v) for k, v in (raw.get("wallet_aliases") or {}).items()}

    return Config(
        api=api,
        discord=discord,
        alerts=alerts,
        poll=poll,
        leaderboard=leaderboard,
        discovery=discovery,
        niches=niches,
        storage=storage,
        logging=logging_cfg,
        wallet_aliases=aliases,
    )


# --------------------------------------------------------------------------- #
# Section parsers (kept separate for readability and targeted error messages).
# --------------------------------------------------------------------------- #
def _parse_api(raw: dict[str, Any]) -> ApiConfig:
    return ApiConfig(
        data_base_url=_as_str(_require(raw, "data_base_url", "api"), "api.data_base_url").rstrip("/"),
        gamma_base_url=_as_str(_require(raw, "gamma_base_url", "api"), "api.gamma_base_url").rstrip("/"),
        timeout_seconds=_as_float(raw.get("timeout_seconds", 15), "api.timeout_seconds"),
        max_retries=_as_int(raw.get("max_retries", 4), "api.max_retries"),
        rate_limit_per_sec=_as_float(raw.get("rate_limit_per_sec", 5), "api.rate_limit_per_sec"),
    )


def _parse_discord(raw: dict[str, Any]) -> DiscordConfig:
    webhook_env = _as_str(raw.get("webhook_env", "DISCORD_WEBHOOK_URL"), "discord.webhook_env")
    error_env = _as_str(raw.get("error_webhook_env", "DISCORD_ERROR_WEBHOOK_URL"), "discord.error_webhook_env")
    webhook_url = os.environ.get(webhook_env) or None
    # The dedicated error webhook is optional; fall back to the main one.
    error_url = os.environ.get(error_env) or webhook_url
    return DiscordConfig(
        webhook_env=webhook_env,
        error_webhook_env=error_env,
        username=str(raw.get("username", "Polymarket Leader Watch")),
        webhook_url=webhook_url,
        error_webhook_url=error_url,
    )


def _parse_alerts(raw: dict[str, Any]) -> AlertsConfig:
    single_raw = _require(raw, "single", "alerts")
    consensus_raw = _require(raw, "consensus", "alerts")
    colors_raw = _require(raw, "colors", "alerts")

    single = SingleAlertConfig(
        enabled=bool(single_raw.get("enabled", True)),
        high_rank_threshold=_as_int(single_raw.get("high_rank_threshold", 3), "alerts.single.high_rank_threshold"),
    )
    consensus = ConsensusAlertConfig(
        min_wallets=_as_int(consensus_raw.get("min_wallets", 3), "alerts.consensus.min_wallets"),
        window_hours=_as_int(consensus_raw.get("window_hours", 24), "alerts.consensus.window_hours"),
        escalation_step=_as_int(consensus_raw.get("escalation_step", 2), "alerts.consensus.escalation_step"),
        alert_on_sell=bool(consensus_raw.get("alert_on_sell", True)),
    )
    if consensus.min_wallets < 1:
        raise ConfigError("alerts.consensus.min_wallets must be >= 1")
    colors = AlertColors(
        muted=_as_int(colors_raw.get("muted", 9807270), "alerts.colors.muted"),
        warning=_as_int(colors_raw.get("warning", 16766720), "alerts.colors.warning"),
        strong=_as_int(colors_raw.get("strong", 15105570), "alerts.colors.strong"),
        max=_as_int(colors_raw.get("max", 15158332), "alerts.colors.max"),
        info=_as_int(colors_raw.get("info", 3447003), "alerts.colors.info"),
        error=_as_int(colors_raw.get("error", 9109504), "alerts.colors.error"),
    )
    return AlertsConfig(
        min_trade_size_usd=_as_float(raw.get("min_trade_size_usd", 1.0), "alerts.min_trade_size_usd"),
        single=single,
        consensus=consensus,
        colors=colors,
    )


def _parse_poll(raw: dict[str, Any]) -> PollConfig:
    return PollConfig(
        interval_seconds=_as_int(raw.get("interval_seconds", 20), "poll.interval_seconds"),
        trades_limit=_as_int(raw.get("trades_limit", 100), "poll.trades_limit"),
    )


def _parse_leaderboard(raw: dict[str, Any]) -> LeaderboardConfig:
    scoring_raw = _require(raw, "scoring", "leaderboard")
    scoring = ScoringConfig(
        win_points=_as_float(scoring_raw.get("win_points", 10), "leaderboard.scoring.win_points"),
        loss_points=_as_float(scoring_raw.get("loss_points", -8), "leaderboard.scoring.loss_points"),
        dump_penalty=_as_float(scoring_raw.get("dump_penalty", -15), "leaderboard.scoring.dump_penalty"),
        roi_weight=_as_float(scoring_raw.get("roi_weight", 20), "leaderboard.scoring.roi_weight"),
    )
    return LeaderboardConfig(
        board_size=_as_int(raw.get("board_size", 10), "leaderboard.board_size"),
        bench_size=_as_int(raw.get("bench_size", 40), "leaderboard.bench_size"),
        decay_half_life_days=_as_float(raw.get("decay_half_life_days", 30), "leaderboard.decay_half_life_days"),
        scoring=scoring,
    )


def _parse_discovery(raw: dict[str, Any]) -> DiscoveryConfig:
    return DiscoveryConfig(
        markets_per_niche=_as_int(raw.get("markets_per_niche", 15), "discovery.markets_per_niche"),
        holders_per_market=_as_int(raw.get("holders_per_market", 50), "discovery.holders_per_market"),
    )


def _parse_niches(raw: dict[str, Any]) -> dict[str, Niche]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("config.niches must be a non-empty mapping of niche definitions")
    niches: dict[str, Niche] = {}
    for key, body in raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"niches.{key} must be a mapping")
        tags = body.get("gamma_tags", [])
        if not isinstance(tags, list) or not tags:
            raise ConfigError(f"niches.{key}.gamma_tags must be a non-empty list")
        niches[str(key)] = Niche(
            key=str(key),
            display_name=str(body.get("display_name", key)),
            gamma_tags=[str(t) for t in tags],
        )
    return niches


def _parse_storage(raw: dict[str, Any]) -> StorageConfig:
    return StorageConfig(
        sqlite_path=_as_str(raw.get("sqlite_path", "./var/pmwatch.sqlite"), "storage.sqlite_path"),
        data_dir=_as_str(raw.get("data_dir", "./data"), "storage.data_dir"),
    )


def _parse_logging(raw: dict[str, Any]) -> LoggingConfig:
    return LoggingConfig(
        level=str(raw.get("level", "INFO")),
        json=bool(raw.get("json", False)),
    )
