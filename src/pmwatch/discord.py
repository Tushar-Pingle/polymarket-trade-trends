"""Discord delivery — renders alerts and reports into webhook embeds.

This is the only module that talks to Discord. It knows how to turn the
domain-level :class:`~pmwatch.consensus.Alert` objects (and the weekly
leaderboard comparison) into Discord webhook payloads, and how to post them
with a light retry.

Two operational niceties:

* ``dry_run=True`` renders everything to the log instead of posting, which is
  what the ``--dry-run`` CLI flags use for safe local testing and what the
  backtest uses by default.
* Errors can be routed to a separate webhook (``DISCORD_ERROR_WEBHOOK_URL``) so
  health/failure noise doesn't drown the trading alerts; it falls back to the
  main webhook if that isn't configured.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from .config import Config
from .consensus import Alert, WalletRef
from .ledger import Leaderboard
from .logging_conf import get_logger
from .models import Side

log = get_logger(__name__)

# Emoji cues so the *kind* of alert is readable at a glance in the feed.
_KIND_EMOJI = {"single": "👤", "consensus": "🔥", "exit": "🚪"}
_SIDE_EMOJI = {Side.BUY: "🟢", Side.SELL: "🔴"}


class DiscordNotifier:
    """Posts alerts and reports to Discord (or logs them in dry-run mode)."""

    def __init__(self, cfg: Config, *, dry_run: bool = False) -> None:
        self._cfg = cfg
        self._dry_run = dry_run
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # Alerts
    # ------------------------------------------------------------------ #
    def send_alert(self, alert: Alert) -> None:
        """Render and deliver one trading alert."""
        embed = self._alert_embed(alert)
        self._post(self._cfg.discord.webhook_url, {"embeds": [embed]}, label=f"{alert.kind} alert")

    def _alert_embed(self, alert: Alert) -> dict[str, Any]:
        side_emoji = _SIDE_EMOJI.get(alert.side, "")
        kind_emoji = _KIND_EMOJI.get(alert.kind, "")
        title = f"{kind_emoji}{side_emoji} {alert.side.value} {alert.outcome} — {alert.title}"[:250]

        # Body: the human guidance note plus any risk flags.
        description_lines = [alert.note] if alert.note else []
        description_lines.extend(alert.flags)

        fields: list[dict[str, Any]] = []

        # Edge-left line: how much room is left vs. the leaders' entry. For single
        # alerts current_price is the fill price; for consensus it's the live price.
        if alert.current_price is not None and alert.wallets:
            avg_entry = sum(w.entry_price for w in alert.wallets) / len(alert.wallets)
            edge = alert.current_price - avg_entry
            fields.append(
                {
                    "name": "Price / edge",
                    "value": (
                        f"avg entry **{avg_entry:.0%}** · now **{alert.current_price:.0%}** "
                        f"({'+' if edge >= 0 else ''}{edge * 100:.1f}pts {'left' if edge <= 0 else 'moved'})"
                    ),
                    "inline": False,
                }
            )

        # One field per involved wallet (capped to keep the embed within limits).
        for ref in alert.wallets[:10]:
            fields.append({"name": _wallet_field_name(ref), "value": _wallet_field_value(ref), "inline": True})

        if len(alert.wallets) > 10:
            fields.append({"name": "…", "value": f"+{len(alert.wallets) - 10} more leaders", "inline": True})

        return {
            "title": title,
            "url": alert.market_url,
            "description": "\n".join(description_lines)[:2000],
            "color": alert.color,
            "fields": fields,
            "footer": {"text": f"{alert.niche_display} · {alert.kind} · ${alert.total_size_usd:,.0f} notional"},
        }

    # ------------------------------------------------------------------ #
    # Weekly comparative report
    # ------------------------------------------------------------------ #
    def send_report(self, boards: dict[str, Leaderboard]) -> None:
        """Post a side-by-side comparison of niches to help you pick where to lean."""
        embed = build_report_embed(self._cfg, boards)
        self._post(self._cfg.discord.webhook_url, {"embeds": [embed]}, label="weekly report")

    # ------------------------------------------------------------------ #
    # Errors / health
    # ------------------------------------------------------------------ #
    def send_error(self, message: str) -> None:
        """Post an operational error so a silent cron/worker failure is visible."""
        embed = {
            "title": "⚠️ pmwatch error",
            "description": message[:2000],
            "color": self._cfg.alerts.colors.error,
        }
        self._post(self._cfg.discord.error_webhook_url, {"embeds": [embed]}, label="error alert")

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def _post(self, webhook_url: str | None, payload: dict[str, Any], *, label: str) -> None:
        """POST a webhook payload, or log it when in dry-run / when unconfigured."""
        payload = {"username": self._cfg.discord.username, **payload}

        if self._dry_run or not webhook_url:
            reason = "dry-run" if self._dry_run else "no webhook configured"
            log.info("[%s] would send %s:\n%s", reason, label, json.dumps(payload, indent=2))
            return

        # Light retry: Discord occasionally 429s; honour Retry-After when given.
        for _attempt in range(4):
            resp = self._session.post(webhook_url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                return
            if resp.status_code == 429:
                retry_after = _discord_retry_after(resp)
                log.warning("Discord rate-limited; backing off %.2fs", retry_after)
                time.sleep(retry_after)
                continue
            log.error("Discord webhook failed (%s): %s", resp.status_code, resp.text[:200])
            return
        log.error("Discord webhook failed after retries: %s", label)


def build_report_embed(cfg: Config, boards: dict[str, Leaderboard]) -> dict[str, Any]:
    """Build the weekly comparison embed.

    For each niche we summarise the board's depth, average score and average
    trust, then flag whether the "top-10 are all positive" — the cohort-health
    condition that tells you a niche currently has a trustworthy set of leaders
    to follow. The niche with the best combined score+trust is highlighted as
    the lean-here suggestion.
    """
    fields: list[dict[str, Any]] = []
    best_niche: str | None = None
    best_metric = float("-inf")

    for key, board in boards.items():
        display = cfg.niches[key].display_name if key in cfg.niches else key
        if not board.board:
            fields.append({"name": display, "value": "_no leaders ranked yet_", "inline": False})
            continue

        avg_score = sum(s.score for s in board.board) / len(board.board)
        avg_trust = sum(s.trust for s in board.board) / len(board.board)
        all_positive = all(s.score > 0 for s in board.board)
        avg_win = sum(s.win_rate for s in board.board) / len(board.board)

        # Combined lean metric: reward both quality (score) and integrity (trust).
        metric = avg_score * max(avg_trust, 0.0)
        if metric > best_metric:
            best_metric, best_niche = metric, key

        top = board.board[0]
        fields.append(
            {
                "name": display,
                "value": (
                    f"board: **{len(board.board)}** · avg score **{avg_score:.1f}** · "
                    f"avg trust **{avg_trust:.2f}** · avg win-rate **{avg_win:.0%}**\n"
                    f"top: `{cfg.alias_for(top.wallet)}` (score {top.score:.1f}, trust {top.trust:.2f})\n"
                    f"top-10 all positive: {'✅' if all_positive else '❌'}"
                ),
                "inline": False,
            }
        )

    description = "Weekly leader-cohort comparison across niches."
    if best_niche is not None:
        description += f"\n**Lean suggestion:** {cfg.niches[best_niche].display_name} (best score × trust)."

    return {
        "title": "📊 Polymarket leaderboard — weekly report",
        "description": description,
        "color": cfg.alerts.colors.info,
        "fields": fields,
    }


def _wallet_field_name(ref: WalletRef) -> str:
    rank = f"#{ref.rank}" if ref.rank is not None else "bench"
    return f"{ref.alias} ({rank})"[:250]


def _wallet_field_value(ref: WalletRef) -> str:
    parts = [f"entry {ref.entry_price:.0%}", f"${ref.entry_price * ref.size:,.0f}"]
    if ref.trust is not None:
        parts.append(f"trust {ref.trust:.2f}")
    return " · ".join(parts)


def _discord_retry_after(resp: requests.Response) -> float:
    """Extract Discord's Retry-After (header or JSON body), defaulting to 1s."""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        return float(resp.json().get("retry_after", 1.0))
    except (ValueError, AttributeError):
        return 1.0
