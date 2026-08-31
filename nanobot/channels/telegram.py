"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
)
from telegram.error import BadRequest, InvalidToken, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    MessageReactionHandler,
    filters,
)
from telegram.request import HTTPXRequest

from nanobot.bus.events import OUTBOUND_META_REACTION, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.command.builtin import build_help_text
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.security.network import validate_url_target
from nanobot.utils.helpers import split_message

TELEGRAM_MAX_MESSAGE_LEN = 4000  # Telegram message character limit
# Telegram's actual API limit is 4096; we split raw markdown at 4000 as a
# safety margin for mid-stream edits (plain text).  For _stream_end, we split
# raw markdown into chunks whose rendered HTML fits Telegram's true 4096-char
# boundary so the final rendered message never overflows.
TELEGRAM_HTML_MAX_LEN = 4096
TELEGRAM_REPLY_CONTEXT_MAX_LEN = TELEGRAM_MAX_MESSAGE_LEN  # Max length for reply context in user message
# Bounded rolling buffer of reply-context observations for runtime diagnostics.
# Records only flags/lengths/ids — never raw message content.
TELEGRAM_REPLY_OBSERVATION_LIMIT = 100
# Long-poll liveness: a healthy getUpdates long poll completes one round trip
# every ~10s even when idle (Updater.start_polling uses a 10s timeout by
# default). The watchdog bot stamps each completed round trip; when none lands
# for TELEGRAM_POLL_STALL_SECONDS the supervisor treats polling as stalled and
# rebuilds the application (including its HTTPX pools). Recovery attempts back
# off exponentially after failures so the bot self-heals once the network
# recovers, and never spam the log.
TELEGRAM_POLL_STALL_SECONDS = 120.0
TELEGRAM_POLL_WATCH_INTERVAL = 1.0
TELEGRAM_RECOVERY_BACKOFF_INITIAL = 1.0
TELEGRAM_RECOVERY_BACKOFF_MAX = 60.0
TELEGRAM_RECOVERY_BACKOFF_FACTOR = 2


def _split_telegram_markdown(content: str, max_len: int) -> list[str]:
    """Split raw Telegram Markdown without leaving fenced code blocks unbalanced."""
    if not content:
        return []
    content = content.lstrip()
    if not content:
        return []
    if len(content) <= max_len:
        return [content]

    def fence_line(fence_pos: int) -> str:
        line_end = content.find("\n", fence_pos)
        if line_end < 0:
            return content[fence_pos:]
        return content[fence_pos:line_end]

    def split_inside_fenced_code_block(pos: int) -> tuple[bool, int, str]:
        if content[:pos].count("```") % 2 == 0:
            return False, -1, ""
        opening = content.rfind("```", 0, pos)
        if opening < 0:
            return True, -1, "```"
        return True, opening, fence_line(opening)

    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break

        cut = content[:max_len]
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len

        inside_code, opening, fence = split_inside_fenced_code_block(pos)
        if inside_code:
            if opening > 0:
                pos = opening
            else:
                closing = "\n```"
                min_code_pos = len(fence)
                if content.startswith(fence + "\n"):
                    min_code_pos += 1
                if pos < min_code_pos and min_code_pos + len(closing) > max_len:
                    chunks.append(content[:max_len])
                    content = content[max_len:].lstrip()
                    continue
                if pos + len(closing) > max_len:
                    budget = max_len - len(closing)
                    if budget > 0:
                        recut = content[:budget]
                        adjusted = recut.rfind("\n")
                        if adjusted <= 0:
                            adjusted = recut.rfind(" ")
                        pos = adjusted if adjusted > 0 else budget
                    else:
                        closing = "```"
                        pos = max_len - len(closing)
                chunks.append(content[:pos] + closing)
                remainder = content[pos:]
                if remainder.startswith("\n"):
                    remainder = remainder[1:]
                content = f"{fence}\n{remainder}"
                continue

        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def _escape_telegram_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tool_hint_to_telegram_blockquote(text: str) -> str:
    """Render tool hints as an expandable blockquote (collapsed by default)."""
    return f"<blockquote expandable>{_escape_telegram_html(text)}</blockquote>" if text else ""


def _strip_md(s: str) -> str:
    """Strip markdown inline formatting from text."""
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'__(.+?)__', r'\1', s)
    s = re.sub(r'~~(.+?)~~', r'\1', s)
    s = re.sub(r'`([^`]+)`', r'\1', s)
    return s.strip()


def _strip_md_block(text: str) -> str:
    """Strip block-level and inline markdown for readable plain-text preview.

    Used during streaming mid-edits so users see clean text instead of raw
    markdown syntax while the response is still being generated.
    """
    # Code blocks -> just the code
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', r'\1', text)
    # Headers -> plain text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    # Blockquotes
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    # Bold / italic / strikethrough
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'\1', text)
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    # Inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Links [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Bullet lists
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    # Numbered lists (normalize spacing)
    text = re.sub(r'^(\d+)\.\s+', r'\1. ', text, flags=re.MULTILINE)
    return text


def _render_table_box(table_lines: list[str]) -> str:
    """Convert markdown pipe-table to compact aligned text for <pre> display."""

    def dw(s: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)

    rows: list[list[str]] = []
    has_sep = False
    for line in table_lines:
        cells = [_strip_md(c) for c in line.strip().strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
            has_sep = True
            continue
        rows.append(cells)
    if not rows or not has_sep:
        return '\n'.join(table_lines)

    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([''] * (ncols - len(r)))
    widths = [max(dw(r[c]) for r in rows) for c in range(ncols)]

    def dr(cells: list[str]) -> str:
        return '  '.join(f'{c}{" " * (w - dw(c))}' for c, w in zip(cells, widths))

    out = [dr(rows[0])]
    out.append('  '.join('─' * w for w in widths))
    for row in rows[1:]:
        out.append(dr(row))
    return '\n'.join(out)


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)

    # 1.5. Convert markdown tables to box-drawing (reuse code_block placeholders)
    lines = text.split('\n')
    rebuilt: list[str] = []
    li = 0
    while li < len(lines):
        if re.match(r'^\s*\|.+\|', lines[li]):
            tbl: list[str] = []
            while li < len(lines) and re.match(r'^\s*\|.+\|', lines[li]):
                tbl.append(lines[li])
                li += 1
            box = _render_table_box(tbl)
            if box != '\n'.join(tbl):
                code_blocks.append(box)
                rebuilt.append(f"\x00CB{len(code_blocks) - 1}\x00")
            else:
                rebuilt.extend(tbl)
        else:
            rebuilt.append(lines[li])
            li += 1
    text = '\n'.join(rebuilt)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Headers # Title -> <b>Title</b> (preserve visual hierarchy)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'⟪B⟫\1⟪/B⟫', text, flags=re.MULTILINE)

    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = _escape_telegram_html(text)

    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 10.5. Numbered lists  1. item -> 1. item (keep number, normalize indent)
    text = re.sub(r'^(\d+)\.\s+', r'\1. ', text, flags=re.MULTILINE)

    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = _escape_telegram_html(code)
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = _escape_telegram_html(code)
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    # 13. Restore header bold markers (inserted in step 3, after HTML escaping)
    text = text.replace('⟪B⟫', '<b>').replace('⟪/B⟫', '</b>')

    return text


def _split_telegram_markdown_html(content: str, max_html_len: int) -> list[str]:
    """Split raw Telegram Markdown and return HTML chunks within Telegram's limit."""
    chunks: list[str] = []
    pending = _split_telegram_markdown(content, TELEGRAM_MAX_MESSAGE_LEN)
    while pending:
        chunk = pending.pop(0)
        html = _markdown_to_telegram_html(chunk)
        if len(html) <= max_html_len:
            chunks.append(html)
            continue

        # Markdown can expand when rendered as HTML (tags/entities). Re-split
        # the raw markdown with a smaller budget instead of slicing HTML tags.
        next_limit = max(1, int(len(chunk) * max_html_len / len(html)) - 8)
        next_limit = min(next_limit, len(chunk) - 1)
        if next_limit <= 0:
            chunks.extend(split_message(html, max_html_len))
            continue
        parts = _split_telegram_markdown(chunk, next_limit)
        if len(parts) == 1 and parts[0] == chunk:
            chunks.extend(split_message(html, max_html_len))
            continue
        pending = parts + pending
    return chunks


_SEND_MAX_RETRIES = 3
_SEND_RETRY_BASE_DELAY = 0.5  # seconds, doubled each retry
_STREAM_EDIT_INTERVAL_DEFAULT = 0.6  # min seconds between edit_message_text calls


@dataclass
class _StreamBuf:
    """Per-chat streaming accumulator for progressive message editing."""
    text: str = ""
    message_id: int | None = None
    last_edit: float = 0.0
    stream_id: str | None = None


@dataclass
class _QueuedTelegramUpdate:
    """Telegram update staged for per-session ordered processing."""

    kind: Literal["command", "message", "reaction"]
    update: Update
    context: Any
    sort_key: tuple[int, int]


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""
    mode: Literal["polling", "webhook"] = "polling"
    allow_from: list[str] = Field(default_factory=list)
    proxy: str | None = None
    reply_to_message: bool = False
    react_emoji: str = "👀"
    group_policy: Literal["open", "mention"] = "mention"
    connection_pool_size: int = 32
    pool_timeout: float = 5.0
    streaming: bool = True
    # Enable inline keyboard buttons in Telegram messages.
    inline_keyboards: bool = False
    # Bot API 10.1 sendRichMessage for richer markdown rendering.
    # Fork-native default on (Telegram Web now renders rich messages).
    rich_messages: bool = True
    # Edit streamed replies into rich messages at stream end (needs rich_messages).
    rich_streaming: bool = True
    stream_edit_interval: float = Field(default=_STREAM_EDIT_INTERVAL_DEFAULT, ge=0.1)
    webhook_url: str = ""
    webhook_listen_host: str = "127.0.0.1"
    webhook_listen_port: int = Field(default=8081, ge=1, le=65535)
    webhook_path: str = "/telegram"
    webhook_secret_token: str = ""
    webhook_max_connections: int = Field(default=4, ge=1, le=100)

    @field_validator("webhook_path")
    @classmethod
    def webhook_path_must_start_with_slash(cls, value: str) -> str:
        value = value.strip() or "/telegram"
        if not value.startswith("/"):
            raise ValueError('webhook_path must start with "/"')
        return value

    @model_validator(mode="after")
    def validate_webhook_config(self) -> "TelegramConfig":
        if self.mode != "webhook":
            return self

        url = self.webhook_url.strip()
        if not url:
            raise ValueError("webhook_url is required when Telegram mode is webhook")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("webhook_url must be a public HTTPS URL")
        secret = self.webhook_secret_token.strip()
        if not secret:
            raise ValueError("webhook_secret_token is required when Telegram mode is webhook")
        if len(secret) > 256 or re.match(r"^[A-Za-z0-9_-]+$", secret) is None:
            raise ValueError(
                "webhook_secret_token must be 1-256 characters using only A-Z, a-z, 0-9, _ and -"
            )
        return self


class _StallWatchBot(ExtBot):
    """ExtBot whose ``get_updates`` stamps a channel-level completion timestamp.

    A healthy long poll completes one ``getUpdates`` round trip roughly every
    10s even when idle, so the timestamp is a cheap liveness probe: both a
    successful poll and a poll that raised prove the attempt finished. The
    supervisor compares this stamp against the clock to distinguish a silently
    stalled polling loop (hung TCP connection) from a healthy one.
    """

    def __init__(
        self,
        on_get_updates_done: Callable[[], None],
        **bot_kwargs: Any,
    ) -> None:
        super().__init__(**bot_kwargs)
        self._on_get_updates_done = on_get_updates_done

    async def get_updates(self, *args: Any, **kwargs: Any) -> tuple[Update, ...]:
        try:
            return await super().get_updates(*args, **kwargs)
        finally:
            # Completion and exception both count as a finished round trip.
            self._on_get_updates_done()


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling or webhook mode.

    Long polling is the default. Webhook mode requires a public HTTPS URL and a
    Telegram secret token.
    """

    name = "telegram"
    display_name = "Telegram"

    # Commands registered with Telegram's command menu
    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("restart", "Restart the bot"),
        BotCommand("status", "Show bot status"),
        BotCommand("history", "Show recent conversation messages"),
        BotCommand("goal", "Start a sustained objective (long-running task)"),
        BotCommand("pairing", "Manage DM pairing (approve/deny/list)"),
        BotCommand("model", "Switch runtime model preset"),
        BotCommand("skill", "List enabled skills"),
        BotCommand("remember", "Save a short note into curated memory"),
        BotCommand("policy", "List, approve or deny pending tool approvals"),
        BotCommand("tasks", "List background tasks"),
        BotCommand("task", "Inspect/stop/result/retry one task"),
        BotCommand("dream", "Run Dream memory consolidation now"),
        BotCommand("dream_log", "Show the latest Dream memory change"),
        BotCommand("dream_restore", "Restore Dream memory to an earlier version"),
        BotCommand("help", "Show available commands"),
    ]

    # Regex for slash commands routed to AgentLoop via ``_forward_command``.
    # Hyphenated ``dream-*`` commands stay on a separate handler (below).
    TELEGRAM_BUS_SLASH_COMMAND_RE = re.compile(
        r"^/(?:new|stop|restart|status|dream|history|goal|pairing|model|skill|remember|policy|tasks|task)(?:@\w+)?(?:\s+.*)?$"
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return TelegramConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = TelegramConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        self._media_group_buffers: dict[str, dict] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._message_threads: dict[tuple[str, int], int] = {}
        self._pending_receipts: set[tuple[str, int]] = set()
        self._sent_messages: dict[tuple[str, int], str] = {}
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None
        self._stream_bufs: dict[str, _StreamBuf] = {}  # chat_id -> streaming state
        self._inbound_buffers: dict[str, list[_QueuedTelegramUpdate]] = {}
        self._inbound_workers: dict[str, asyncio.Task] = {}
        self._rich_send_disabled: bool = False  # Latch off if Bot API < 10.1
        self._reply_observations: deque[dict[str, Any]] = deque(
            maxlen=TELEGRAM_REPLY_OBSERVATION_LIMIT
        )
        self._reply_observations_total = 0

        # Polling stall recovery state (see _supervise_polling).
        self._last_get_updates_finished_at: float | None = None
        self._recovery_requested = False
        self._recovering = False  # Single-flight guard for recovery
        self._recovery_backoff = TELEGRAM_RECOVERY_BACKOFF_INITIAL
        self._supervisor: asyncio.Task | None = None
        self._failed = False  # Terminal failure (e.g. rejected token): no retry

    def is_allowed(self, sender_id: str) -> bool:
        """Preserve Telegram's legacy id|username allowlist matching."""
        if super().is_allowed(sender_id):
            return True

        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list or "*" in allow_list:
            return False

        sender_str = str(sender_id)
        if sender_str.count("|") != 1:
            return False

        sid, username = sender_str.split("|", 1)
        if not sid.isdigit() or not username:
            return False

        return sid in allow_list or username in allow_list

    @staticmethod
    def _normalize_telegram_command(content: str) -> str:
        """Map Telegram-safe command aliases back to canonical nanobot commands."""
        if not content.startswith("/"):
            return content
        if content == "/dream_log" or content.startswith("/dream_log "):
            return content.replace("/dream_log", "/dream-log", 1)
        if content == "/dream_restore" or content.startswith("/dream_restore "):
            return content.replace("/dream_restore", "/dream-restore", 1)
        return content

    async def start(self) -> None:
        """Start the Telegram bot, rebuilding the app whenever polling stalls."""
        if not self.config.token:
            self.logger.error("bot token not configured")
            return

        self._running = True
        self._failed = False
        self._last_get_updates_finished_at = None
        self._recovery_requested = False
        self._recovery_backoff = TELEGRAM_RECOVERY_BACKOFF_INITIAL
        self._supervisor = asyncio.create_task(self._supervise_polling())

        try:
            try:
                await self._start_app()
            except InvalidToken:
                # A rejected token is a config error, not a transient network
                # failure - retrying forever would only spam the log. The
                # scrubbed message keeps PTB's token-bearing text out of logs.
                self._failed = True
                self._running = False
                self.logger.error("bot token rejected by Telegram")
                raise RuntimeError("Telegram bot token was rejected by the server") from None
            except Exception as e:
                if self._is_transient_startup_error(e):
                    self.logger.error(
                        "startup failed: {}; supervisor will retry with backoff",
                        self._format_telegram_error(e),
                    )
                else:
                    self._failed = True
                    self._running = False
                    self.logger.error("startup failed: {}", self._format_telegram_error(e))
                    raise

            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)
        finally:
            await self._teardown_app()
            await self._cancel_supervisor()

    def _build_app(self) -> Application:
        """Build the Telegram application: pools, watchdog bot, handlers."""
        proxy = self.config.proxy or None

        # Separate pools so long-polling (getUpdates) never starves outbound sends.
        api_request = HTTPXRequest(
            connection_pool_size=self.config.connection_pool_size,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        poll_request = HTTPXRequest(
            connection_pool_size=4,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        # The watchdog bot records every getUpdates round trip so the supervisor
        # can tell a silent stall apart from a healthy (idle) long poll.
        watchdog_bot = _StallWatchBot(
            token=self.config.token,
            request=api_request,
            get_updates_request=poll_request,
            on_get_updates_done=self._note_poll_ok,
        )
        app = Application.builder().bot(watchdog_bot).build()
        # Set early so a later failure in this method still tears down the
        # freshly built app instead of leaking it.
        self._app = app
        self._app.add_error_handler(self._on_error)

        # Add command handlers (using Regex to support @username suffixes before bot initialization)
        self._app.add_handler(MessageHandler(filters.Regex(r"^/start(?:@\w+)?$"), self._on_start))
        self._app.add_handler(
            MessageHandler(
                filters.Regex(TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE),
                self._forward_command,
            )
        )
        self._app.add_handler(MessageReactionHandler(self._on_message_reaction))
        self._app.add_handler(
            MessageHandler(
                filters.Regex(r"^/(dream-log|dream_log|dream-restore|dream_restore)(?:@\w+)?(?:\s+.*)?$"),
                self._forward_command,
            )
        )
        self._app.add_handler(MessageHandler(filters.Regex(r"^/help(?:@\w+)?$"), self._on_help))

        # Add message handler for text, photos, video, voice, documents, and locations
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE
                 | filters.ANIMATION | filters.VOICE | filters.AUDIO
                 | filters.Document.ALL | filters.LOCATION)
                & ~filters.COMMAND,
                self._on_message
            )
        )

        # Conditionally register inline keyboard callback handler
        if self.config.inline_keyboards:
            self._app.add_handler(CallbackQueryHandler(self._on_callback_query))
            self._allowed_updates = [
                "message",
                "edited_message",
                "message_reaction",
                "callback_query",
            ]
            self.logger.debug("inline keyboards enabled")
        else:
            self._allowed_updates = ["message", "edited_message", "message_reaction"]
        return app

    async def _start_app(self) -> None:
        """Build, initialize and start the application; never leak a half-built one."""
        try:
            app = self._build_app()

            if self.config.mode == "webhook":
                self.logger.info("Starting bot (webhook mode)...")
            else:
                self.logger.info("Starting bot (polling mode)...")

            # Initialize and start receiving updates
            await app.initialize()
            await app.start()

            # Get bot info and register command menu
            bot_info = await app.bot.get_me()
            self._bot_user_id = getattr(bot_info, "id", None)
            self._bot_username = getattr(bot_info, "username", None)
            self.logger.info("bot @{} connected", bot_info.username)

            try:
                await app.bot.set_my_commands(self.BOT_COMMANDS)
                self.logger.debug("bot commands registered")
            except Exception as e:
                self.logger.warning("Failed to register bot commands: {}", e)

            if self.config.mode == "webhook":
                # ``url_path`` is the local HTTP route. ``webhook_url`` is the
                # public HTTPS URL Telegram calls; reverse proxies may rewrite it.
                await app.updater.start_webhook(
                    listen=self.config.webhook_listen_host,
                    port=self.config.webhook_listen_port,
                    url_path=self.config.webhook_path.lstrip("/"),
                    webhook_url=self.config.webhook_url.strip(),
                    allowed_updates=self._allowed_updates,
                    drop_pending_updates=False,
                    secret_token=self.config.webhook_secret_token.strip(),
                    max_connections=self.config.webhook_max_connections,
                )
            else:
                # Seed the watchdog so a brand-new app is not treated as stalled
                # before its first poll round trip completes.
                self._note_poll_ok()
                # Start polling (this runs until stopped)
                await app.updater.start_polling(
                    allowed_updates=self._allowed_updates,
                    drop_pending_updates=False,  # Process pending messages on startup
                    error_callback=self._on_polling_error,
                )
        except BaseException:
            # Tear down even partial state (initialize/start may not have
            # finished); CancelledError is BaseException so stop() cancelling a
            # mid-startup recovery cannot leak the app either.
            await self._teardown_app()
            raise

    async def _teardown_app(self) -> None:
        """Shut down the application, tolerating partially started state."""
        app, self._app = self._app, None
        if not app:
            return
        for step in (app.updater.stop, app.stop, app.shutdown):
            try:
                await step()
            except Exception as e:
                self.logger.debug("teardown step failed: {}", e)
        # Application.shutdown() skips the HTTPX pools when initialize() never
        # finished, so a failed startup would leak one pool per retry.
        # Bot.shutdown() closes them explicitly (idempotent).
        try:
            await app.bot.shutdown()
        except Exception as e:
            self.logger.debug("bot shutdown failed: {}", e)

    def _note_poll_ok(self) -> None:
        """Stamp the last completed getUpdates round trip (success or failure)."""
        self._last_get_updates_finished_at = time.monotonic()

    def _needs_recovery(self) -> bool:
        """True when polling needs a rebuild: stall, lost app, or explicit request."""
        if self._recovery_requested:
            return True
        if self.config.mode == "webhook":
            return False  # webhooks have no getUpdates heartbeat to watch
        if self._last_get_updates_finished_at is None:
            # No round trip ever completed: either startup is still in flight
            # (an app exists) or the last one failed (no app). Only the latter
            # needs recovery.
            return self._app is None
        return time.monotonic() - self._last_get_updates_finished_at > TELEGRAM_POLL_STALL_SECONDS

    async def _supervise_polling(self) -> None:
        """Watch long-poll liveness and rebuild the app when polling stalls.

        Recovery is single-flight by construction: the polling error callback
        only sets ``_recovery_requested`` and this task performs the actual
        teardown/rebuild, so a burst of errors cannot double-recover.
        """
        with suppress(asyncio.CancelledError):
            while self._running and not self._failed:
                await asyncio.sleep(TELEGRAM_POLL_WATCH_INTERVAL)
                if self._recovering or not self._needs_recovery():
                    continue
                self._recovery_requested = False
                try:
                    await self._recover()
                except Exception as e:
                    self.logger.error("polling recovery failed: {}", self._format_telegram_error(e))

    async def _recover(self) -> None:
        """Tear down and rebuild the app, backing off between attempts.

        Single-flight: the polling error callback only sets
        ``_recovery_requested``, and even if two triggers arrive at once, only
        the first recovery runs.
        """
        if self._recovering:
            return
        self._recovering = True
        try:
            await self._teardown_app()
            if not self._running or self._failed:
                return
            await self._idle(self._recovery_backoff)
            if not self._running or self._failed:
                return
            try:
                await self._start_app()
            except InvalidToken:
                self._failed = True
                self._running = False
                self.logger.error("bot token rejected by Telegram")
            except Exception as e:
                if not self._is_transient_startup_error(e):
                    self._failed = True
                    self._running = False
                    self.logger.error("recovery failed: {}", self._format_telegram_error(e))
                else:
                    self._recovery_backoff = min(
                        self._recovery_backoff * TELEGRAM_RECOVERY_BACKOFF_FACTOR,
                        TELEGRAM_RECOVERY_BACKOFF_MAX,
                    )
                    self.logger.warning(
                        "recovery failed: {}; backing off to {:.0f}s",
                        self._format_telegram_error(e),
                        self._recovery_backoff,
                    )
            else:
                # A successful rebuild means fresh getUpdates round trips;
                # reset the backoff for the next healthy period.
                self._recovery_backoff = TELEGRAM_RECOVERY_BACKOFF_INITIAL
        finally:
            self._recovering = False

    async def _idle(self, seconds: float) -> None:
        """Sleep in short steps so stop() stays responsive."""
        deadline = time.monotonic() + seconds
        while self._running and not self._failed and time.monotonic() < deadline:
            await asyncio.sleep(TELEGRAM_POLL_WATCH_INTERVAL)

    @staticmethod
    def _is_transient_startup_error(exc: Exception) -> bool:
        """Report whether a startup failure is worth retrying.

        HTTPXRequest wraps every httpx failure into NetworkError/TimedOut, so
        anything else is terminal: a bad proxy raises ValueError, an already
        bound webhook port raises OSError.
        """
        return isinstance(exc, NetworkError | TimedOut | asyncio.TimeoutError)

    async def _cancel_supervisor(self) -> None:
        """Cancel and await the supervisor task (idempotent)."""
        task, self._supervisor = self._supervisor, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False

        # Cancel all typing indicators
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        for task in self._media_group_tasks.values():
            task.cancel()
        self._media_group_tasks.clear()
        self._media_group_buffers.clear()

        for task in self._inbound_workers.values():
            task.cancel()
        self._inbound_workers.clear()
        self._inbound_buffers.clear()

        # Stop the supervisor first so it cannot rebuild the app we tear down
        # right after; the supervisor itself never leaks a mid-startup app.
        await self._cancel_supervisor()

        if self._app:
            self.logger.info("Stopping bot...")
            await self._teardown_app()

    @staticmethod
    def _get_media_type(path: str) -> str:
        """Guess media type from file extension."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "photo"
        if ext in ("mp4", "mov", "avi", "mkv", "webm", "3gp"):
            return "video"
        if ext == "ogg":
            return "voice"
        if ext in ("mp3", "m4a", "wav", "aac"):
            return "audio"
        return "document"

    @staticmethod
    def _is_remote_media_url(path: str) -> bool:
        return path.startswith(("http://", "https://"))

    @staticmethod
    def _is_rich_capability_error(exc: Exception) -> bool:
        """True when the error indicates sendRichMessage is unavailable."""
        err = str(exc).lower()
        return (
            "method not found" in err
            or "unknown method" in err
            or "bad request: invalid parameter" in err
        )

    async def _try_send_rich(
        self,
        chat_id: int,
        content: str,
        reply_params=None,
        thread_kwargs: dict | None = None,
        reply_markup=None,
    ) -> bool:
        """Attempt sendRichMessage (Bot API 10.1). Returns True on success."""
        if not self._app:
            return False

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {
                "markdown": content,
            },
        }
        if reply_params is not None:
            # sendRichMessage uses reply_parameters (object), not reply_to_message_id.
            if hasattr(reply_params, "message_id"):
                payload["reply_parameters"] = {
                    "message_id": reply_params.message_id,
                    "allow_sending_without_reply": True,
                }
            else:
                payload["reply_parameters"] = reply_params
        if thread_kwargs:
            payload.update({k: v for k, v in thread_kwargs.items() if v is not None})
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            result = await self._call_with_retry(
                self._app.bot.do_api_request,
                "sendRichMessage",
                api_kwargs=payload,
            )
            if isinstance(result, dict) and result.get("message_id") is not None:
                self._remember_sent_message(chat_id, int(result["message_id"]), content)
            return True
        except BadRequest as exc:
            if self._is_rich_capability_error(exc):
                self.logger.debug("sendRichMessage not available, disabling")
                self._rich_send_disabled = True
            else:
                self.logger.debug("sendRichMessage rejected: {}", exc)
            return False
        except Exception as exc:
            err_str = str(exc).lower()
            is_timeout = "timed out" in err_str or isinstance(exc, TimedOut)
            if is_timeout:
                self.logger.debug("sendRichMessage timeout, falling back to legacy path")
                return False
            self.logger.debug("sendRichMessage failed: {}", exc)
            return False

    async def _try_edit_rich(
        self,
        chat_id: int,
        message_id: int,
        content: str,
    ) -> bool:
        """Attempt editMessageText with rich_message (Bot API 10.1). Returns True on success."""
        if not self._app:
            return False
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": content},
        }
        try:
            await self._call_with_retry(
                self._app.bot.do_api_request,
                "editMessageText",
                api_kwargs=payload,
            )
            return True
        except BadRequest as exc:
            if self._is_not_modified_error(exc):
                self.logger.debug("rich edit already applied for {}", message_id)
                return True
            if self._is_rich_capability_error(exc):
                self.logger.debug("editMessageText rich_message not available, disabling")
                self._rich_send_disabled = True
            else:
                self.logger.debug("editMessageText rich_message rejected: {}", exc)
            return False
        except Exception as exc:
            err_str = str(exc).lower()
            if "timed out" in err_str or isinstance(exc, TimedOut):
                self.logger.debug("editMessageText rich_message timeout, falling back to legacy path")
                return False
            self.logger.debug("editMessageText rich_message failed: {}", exc)
            return False

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            self.logger.warning("bot not running")
            return

        if reaction := msg.metadata.get(OUTBOUND_META_REACTION):
            await self._set_agent_reaction(
                msg.chat_id,
                int(reaction["message_id"]),
                str(reaction.get("emoji") or ""),
            )
            return

        # Only stop typing indicator and remove reaction for final responses
        if not msg.metadata.get("_progress", False):
            self._stop_typing(msg.chat_id)
            if reply_to_message_id := msg.metadata.get("message_id"):
                with suppress(ValueError):
                    await self._remove_reaction(msg.chat_id, int(reply_to_message_id))

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            self.logger.exception("Invalid chat_id: {}", msg.chat_id)
            return
        reply_to_message_id = msg.metadata.get("message_id")
        message_thread_id = msg.metadata.get("message_thread_id")
        if message_thread_id is None and reply_to_message_id is not None:
            message_thread_id = self._message_threads.get((msg.chat_id, reply_to_message_id))
        thread_kwargs = {}
        if message_thread_id is not None:
            thread_kwargs["message_thread_id"] = message_thread_id

        reply_params = None
        if self.config.reply_to_message:
            if reply_to_message_id:
                reply_params = ReplyParameters(
                    message_id=reply_to_message_id,
                    allow_sending_without_reply=True
                )

        # Send media files
        for media_path in (msg.media or []):
            try:
                media_type = self._get_media_type(media_path)
                sender = {
                    "photo": self._app.bot.send_photo,
                    "video": self._app.bot.send_video,
                    "voice": self._app.bot.send_voice,
                    "audio": self._app.bot.send_audio,
                }.get(media_type, self._app.bot.send_document)
                param = {
                    "photo": "photo",
                    "video": "video",
                    "voice": "voice",
                    "audio": "audio",
                }.get(media_type, "document")
                extra: dict[str, Any] = {}
                if media_type == "video":
                    extra["supports_streaming"] = True

                # Telegram Bot API accepts HTTP(S) URLs directly for media params.
                if self._is_remote_media_url(media_path):
                    ok, error = validate_url_target(media_path)
                    if not ok:
                        raise ValueError(f"unsafe media URL: {error}")
                    sent = await self._call_with_retry(
                        sender,
                        chat_id=chat_id,
                        **{param: media_path},
                        reply_parameters=reply_params,
                        **thread_kwargs,
                        **extra,
                    )
                    if (sent_message_id := getattr(sent, "message_id", None)) is not None:
                        self._remember_sent_message(
                            str(chat_id), sent_message_id, f"[{media_type}: {media_path}]"
                        )
                    continue

                media_bytes = Path(media_path).read_bytes()
                filename = Path(media_path).name
                send_kwargs = {param: media_bytes, "filename": filename}
                sent = await self._call_with_retry(
                    sender,
                    chat_id=chat_id,
                    reply_parameters=reply_params,
                    **thread_kwargs,
                    **extra,
                    **send_kwargs,
                )
                if (sent_message_id := getattr(sent, "message_id", None)) is not None:
                    self._remember_sent_message(
                        str(chat_id), sent_message_id, f"[{media_type}: {filename}]"
                    )
            except Exception:
                filename = media_path.rsplit("/", 1)[-1]
                self.logger.exception("Failed to send media {}", media_path)
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=f"[Failed to send: {filename}]",
                    reply_parameters=reply_params,
                    **thread_kwargs,
                )

        # Send text content
        if msg.content and msg.content != "[empty message]":
            render_as_blockquote = bool(msg.metadata.get("_tool_hint"))
            buttons = getattr(msg, "buttons", None) or []
            reply_markup = self._build_keyboard(buttons) if buttons else None
            text = msg.content
            # Fallback: no native keyboard → splice labels into the message so the choices survive.
            if buttons and reply_markup is None:
                text = f"{text}\n\n{self._buttons_as_text(buttons)}"

            # Bot API 10.1 rich fast-path: send raw markdown via sendRichMessage.
            # All non-blockquote content tries rich first; _rich_send_disabled
            # latches off permanently if the server doesn't support it.
            if (
                not render_as_blockquote
                and self.config.rich_messages
                and not getattr(self, "_rich_send_disabled", False)
            ):
                rich_ok = await self._try_send_rich(
                    chat_id, text, reply_params, thread_kwargs, reply_markup,
                )
                if rich_ok:
                    return

            chunks = _split_telegram_markdown(text, TELEGRAM_MAX_MESSAGE_LEN)
            for i, chunk in enumerate(chunks):
                is_last = (i == len(chunks) - 1)
                await self._send_text(
                    chat_id, chunk, reply_params, thread_kwargs,
                    render_as_blockquote=render_as_blockquote,
                    reply_markup=reply_markup if is_last else None,
                )

    async def _call_with_retry(self, fn, *args, **kwargs):
        """Call an async Telegram API function with retry on pool/network timeout and RetryAfter."""
        from telegram.error import RetryAfter

        for attempt in range(1, _SEND_MAX_RETRIES + 1):
            try:
                return await fn(*args, **kwargs)
            except TimedOut:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                delay = _SEND_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                self.logger.warning(
                    "timeout (attempt {}/{}), retrying in {:.1f}s",
                    attempt, _SEND_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
            except RetryAfter as e:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                delay = float(e.retry_after)
                self.logger.warning(
                    "Flood Control (attempt {}/{}), retrying in {:.1f}s",
                    attempt, _SEND_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        reply_params=None,
        thread_kwargs: dict | None = None,
        render_as_blockquote: bool = False,
        reply_markup=None,
    ) -> None:
        """Send a plain text message with HTML fallback."""
        try:
            html = _tool_hint_to_telegram_blockquote(text) if render_as_blockquote else _markdown_to_telegram_html(text)
            sent = await self._call_with_retry(
                self._app.bot.send_message,
                chat_id=chat_id, text=html, parse_mode="HTML",
                reply_parameters=reply_params,
                reply_markup=reply_markup,
                **(thread_kwargs or {}),
            )
            self._remember_sent_message(str(chat_id), sent.message_id, text)
        except BadRequest as e:
            self.logger.warning("HTML parse failed, falling back to plain text: {}", e)
            try:
                sent = await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=chat_id,
                    text=text,
                    reply_parameters=reply_params,
                    reply_markup=reply_markup,
                    **(thread_kwargs or {}),
                )
                self._remember_sent_message(str(chat_id), sent.message_id, text)
            except Exception:
                self.logger.exception("Error sending message")
                raise

    @staticmethod
    def _is_not_modified_error(exc: Exception) -> bool:
        return isinstance(exc, BadRequest) and "message is not modified" in str(exc).lower()

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Progressive message editing: send on first delta, edit on subsequent ones."""
        if not self._app:
            return
        meta = metadata or {}
        int_chat_id = int(chat_id)
        stream_id = meta.get("_stream_id")

        if meta.get("_stream_end"):
            buf = self._stream_bufs.get(chat_id)
            if not buf or not buf.message_id or not buf.text:
                return
            if stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id:
                return
            # A stream segment ends before tool execution too. Keep the activity
            # indicator alive until the runner marks the final segment complete.
            if not meta.get("_resuming", False):
                self._stop_typing(chat_id)
                if reply_to_message_id := meta.get("message_id"):
                    with suppress(ValueError):
                        await self._remove_reaction(chat_id, int(reply_to_message_id))
            thread_kwargs = {}
            if message_thread_id := meta.get("message_thread_id"):
                thread_kwargs["message_thread_id"] = message_thread_id
            raw_text = buf.text

            # Fork-native: edit the streamed preview into a rich message
            # (Bot API 10.1). buf.message_id is guaranteed set by the early
            # return above; gated by rich_streaming. Falls back to the legacy
            # HTML edit below on any failure.
            if (
                buf.message_id
                and self.config.rich_messages
                and self.config.rich_streaming
                and not getattr(self, "_rich_send_disabled", False)
            ):
                if await self._try_edit_rich(int_chat_id, buf.message_id, raw_text):
                    self._remember_sent_message(chat_id, buf.message_id, raw_text)
                    self._stream_bufs.pop(chat_id, None)
                    return

            # Legacy path: edit existing streaming message with HTML
            html_chunks = _split_telegram_markdown_html(raw_text, TELEGRAM_HTML_MAX_LEN)
            primary_html = html_chunks[0]
            extra_html_chunks = html_chunks[1:]
            try:
                await self._call_with_retry(
                    self._app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=primary_html, parse_mode="HTML",
                )
                self._remember_sent_message(
                    chat_id, buf.message_id, self._visible_html_text(primary_html)
                )
            except BadRequest as e:
                # Only fall back to plain text on actual HTML parse/format errors.
                # Network errors (TimedOut, NetworkError) should propagate immediately
                # to avoid doubling connection demand during pool exhaustion.
                if self._is_not_modified_error(e):
                    self.logger.debug("Final stream edit already applied for {}", chat_id)
                    self._stream_bufs.pop(chat_id, None)
                    return
                self.logger.debug("Final stream edit failed (HTML), trying plain: {}", e)
                # Fall back to raw markdown (not HTML) so users don't see raw tags.
                primary_plain = split_message(raw_text, TELEGRAM_MAX_MESSAGE_LEN)[0] if len(raw_text) > TELEGRAM_MAX_MESSAGE_LEN else raw_text
                try:
                    await self._call_with_retry(
                        self._app.bot.edit_message_text,
                        chat_id=int_chat_id, message_id=buf.message_id,
                        text=primary_plain,
                    )
                    self._remember_sent_message(chat_id, buf.message_id, primary_plain)
                except Exception as e2:
                    if self._is_not_modified_error(e2):
                        self.logger.debug("Final stream plain edit already applied for {}", chat_id)
                    else:
                        self.logger.warning("Final stream edit failed: {}", e2)
                        raise  # Let ChannelManager handle retry
            for extra_html_chunk in extra_html_chunks:
                try:
                    sent = await self._call_with_retry(
                        self._app.bot.send_message,
                        chat_id=int_chat_id, text=extra_html_chunk,
                        parse_mode="HTML",
                        **thread_kwargs,
                    )
                    self._remember_sent_message(
                        chat_id, sent.message_id, self._visible_html_text(extra_html_chunk)
                    )
                except Exception:
                    # Fall back to _send_text which handles HTML→plain gracefully.
                    await self._send_text(int_chat_id, extra_html_chunk)
            self._stream_bufs.pop(chat_id, None)
            return

        buf = self._stream_bufs.get(chat_id)
        if buf is None or (stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id):
            buf = _StreamBuf(stream_id=stream_id)
            self._stream_bufs[chat_id] = buf
        elif buf.stream_id is None:
            buf.stream_id = stream_id
        buf.text += delta

        if not buf.text.strip():
            return

        now = time.monotonic()
        thread_kwargs = {}
        if message_thread_id := meta.get("message_thread_id"):
            thread_kwargs["message_thread_id"] = message_thread_id
        reply_params = None
        if self.config.reply_to_message and (reply_to_message_id := meta.get("message_id")):
            reply_params = ReplyParameters(
                message_id=int(reply_to_message_id),
                allow_sending_without_reply=True,
            )
        if buf.message_id is None:
            preview = _strip_md_block(buf.text)
            try:
                sent = await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=int_chat_id, text=preview,
                    reply_parameters=reply_params,
                    **thread_kwargs,
                )
                buf.message_id = sent.message_id
                self._remember_sent_message(chat_id, sent.message_id, buf.text)
                buf.last_edit = now
            except Exception as e:
                self.logger.warning("Stream initial send failed: {}", e)
                raise  # Let ChannelManager handle retry
        elif (now - buf.last_edit) >= self.config.stream_edit_interval:
            if len(buf.text) > TELEGRAM_MAX_MESSAGE_LEN:
                await self._flush_stream_overflow(int_chat_id, buf, thread_kwargs)
                buf.last_edit = now
                return
            preview = _strip_md_block(buf.text)
            try:
                await self._call_with_retry(
                    self._app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=preview,
                )
                self._remember_sent_message(chat_id, buf.message_id, buf.text)
                buf.last_edit = now
            except Exception as e:
                if self._is_not_modified_error(e):
                    buf.last_edit = now
                    return
                self.logger.warning("Stream edit failed: {}", e)
                raise  # Let ChannelManager handle retry

    async def _flush_stream_overflow(
        self,
        chat_id: int,
        buf: "_StreamBuf",
        thread_kwargs: dict,
    ) -> None:
        """Split an oversized stream buffer mid-flight.

        Edits the current stream message with the first chunk, sends any
        intermediate chunks as standalone messages, then opens a new message
        for the tail so subsequent deltas continue streaming into it.
        """
        chunks = _split_telegram_markdown(buf.text, TELEGRAM_MAX_MESSAGE_LEN)
        if len(chunks) <= 1:
            return
        try:
            await self._call_with_retry(
                self._app.bot.edit_message_text,
                chat_id=chat_id, message_id=buf.message_id,
                text=chunks[0],
            )
            if buf.message_id is not None:
                self._remember_sent_message(str(chat_id), buf.message_id, chunks[0])
        except Exception as e:
            if not self._is_not_modified_error(e):
                self.logger.warning("Stream overflow edit failed: {}", e)
                raise
        for chunk in chunks[1:-1]:
            sent = await self._call_with_retry(
                self._app.bot.send_message,
                chat_id=chat_id, text=chunk, **thread_kwargs,
            )
            self._remember_sent_message(str(chat_id), sent.message_id, chunk)
        tail = chunks[-1]
        sent = await self._call_with_retry(
            self._app.bot.send_message,
            chat_id=chat_id, text=tail, **thread_kwargs,
        )
        buf.message_id = sent.message_id
        buf.text = tail
        self._remember_sent_message(str(chat_id), sent.message_id, tail)

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, update.message, user)
            return
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command for allowed users only."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, update.message, user)
            return
        await update.message.reply_text(build_help_text())

    @staticmethod
    def _sender_id(user) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    async def _send_pairing_code_if_private(self, sender_id: str, message, user) -> None:
        if message.chat.type != "private":
            return
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(message.chat_id),
            content="",
            metadata=self._build_message_metadata(message, user),
            is_dm=True,
        )

    @staticmethod
    def _derive_topic_session_key(message) -> str | None:
        """Derive topic-scoped session key for Telegram chats with threads."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return None
        return f"telegram:{message.chat_id}:topic:{message_thread_id}"

    @staticmethod
    def _build_message_metadata(
        message,
        user,
        reply_details: dict | None = None,
        forward_details: dict | None = None,
    ) -> dict:
        """Build common Telegram inbound metadata payload."""
        reply_to = TelegramChannel._reply_source(message)
        metadata = {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": message.chat.type != "private",
            "message_thread_id": getattr(message, "message_thread_id", None),
            "is_forum": bool(getattr(message.chat, "is_forum", False)),
            "reply_to_message_id": (
                reply_details.get("message_id")
                if reply_details is not None
                else getattr(reply_to, "message_id", None) if reply_to else None
            ),
        }
        if reply_details is not None:
            metadata["reply_to"] = reply_details
        if forward_details is not None:
            metadata["forward_origin"] = forward_details
        return metadata

    @staticmethod
    def _extract_origin_details(origin) -> dict | None:
        """Extract JSON-safe attribution from a Telegram message origin."""
        if origin is None:
            return None

        origin_type = getattr(origin, "type", None)
        if hasattr(origin_type, "value"):
            origin_type = origin_type.value
        details = {"type": str(origin_type or origin.__class__.__name__).lower()}
        if (date := getattr(origin, "date", None)) is not None:
            details["date"] = date.isoformat() if hasattr(date, "isoformat") else str(date)
        if (message_id := getattr(origin, "message_id", None)) is not None:
            details["message_id"] = message_id
        if author_signature := getattr(origin, "author_signature", None):
            details["author_signature"] = author_signature
        if sender_name := getattr(origin, "sender_user_name", None):
            details["sender_name"] = sender_name

        sender = getattr(origin, "sender_user", None)
        if sender is not None:
            details["sender"] = {
                "id": getattr(sender, "id", None),
                "username": getattr(sender, "username", None),
                "first_name": getattr(sender, "first_name", None),
            }
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is not None:
            details["chat"] = {
                "id": getattr(chat, "id", None),
                "type": getattr(chat, "type", None),
                "title": getattr(chat, "title", None),
                "username": getattr(chat, "username", None),
            }
        return details

    @staticmethod
    def _extract_forward_details(message) -> dict | None:
        """Extract JSON-safe attribution for a forwarded Telegram message."""
        return TelegramChannel._extract_origin_details(
            getattr(message, "forward_origin", None)
        )

    @staticmethod
    def _format_forward_context(details: dict) -> str:
        """Render forward attribution as clearly delimited prompt context."""
        lines = [
            "[Telegram Forward Context]",
            "The message content below was forwarded and was not authored by the sender.",
            f"Origin type: {details['type']}",
        ]
        sender = details.get("sender") or {}
        chat = details.get("chat") or {}
        if sender.get("username"):
            lines.append(f"Original author: @{sender['username']}")
        elif sender.get("first_name"):
            lines.append(f"Original author: {sender['first_name']}")
        elif details.get("sender_name"):
            lines.append(f"Original author: {details['sender_name']}")
        elif chat.get("title"):
            lines.append(f"Original chat: {chat['title']}")
        elif chat.get("username"):
            lines.append(f"Original chat: @{chat['username']}")
        if details.get("author_signature"):
            lines.append(f"Author signature: {details['author_signature']}")
        if details.get("message_id") is not None:
            lines.append(f"Original message ID: {details['message_id']}")
        if details.get("date"):
            lines.append(f"Original date: {details['date']}")
        lines.append("[/Telegram Forward Context]")
        return "\n".join(lines)

    @staticmethod
    def _reply_source(message):
        """Return Telegram's available direct or external reply object."""
        return getattr(message, "reply_to_message", None) or getattr(message, "external_reply", None)

    @staticmethod
    def _describe_reply_media(reply) -> list[dict]:
        """Return JSON-safe descriptors for media attached to a replied-to message."""
        media = []
        for attribute, media_type in (
            ("photo", "photo"),
            ("document", "document"),
            ("voice", "voice"),
            ("audio", "audio"),
            ("video", "video"),
            ("video_note", "video_note"),
            ("animation", "animation"),
            ("sticker", "sticker"),
        ):
            value = getattr(reply, attribute, None)
            if not value:
                continue
            item = value[-1] if attribute == "photo" else value
            descriptor = {"type": media_type}
            for field in (
                "file_unique_id",
                "file_name",
                "mime_type",
                "file_size",
                "duration",
                "width",
                "height",
            ):
                if (field_value := getattr(item, field, None)) is not None:
                    descriptor[field] = field_value
            media.append(descriptor)
        return media

    @staticmethod
    def _rich_text_to_plain(value: Any) -> str:
        """Flatten a Bot API RichText value while discarding presentation metadata."""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(TelegramChannel._rich_text_to_plain(item) for item in value)
        if not isinstance(value, Mapping):
            return ""

        text = TelegramChannel._rich_text_to_plain(value.get("text"))
        if text:
            return text
        if value.get("type") == "custom_emoji":
            return str(value.get("alternative_text") or "")
        if value.get("type") == "mathematical_expression":
            return str(value.get("expression") or "")
        if value.get("type") == "button":
            button = value.get("button")
            if isinstance(button, Mapping):
                return TelegramChannel._rich_text_to_plain(button.get("text"))
        return ""

    @classmethod
    def _rich_message_to_plain(cls, rich_message: Any) -> str:
        """Extract readable text from a Bot API RichMessage object."""
        if not isinstance(rich_message, Mapping):
            return ""

        def flatten_caption(caption: Any) -> str:
            if isinstance(caption, Mapping):
                parts = [
                    cls._rich_text_to_plain(caption.get("text")),
                    cls._rich_text_to_plain(caption.get("credit")),
                ]
                return "\n".join(part for part in parts if part)
            return cls._rich_text_to_plain(caption)

        def flatten_block(block: Any) -> str:
            if not isinstance(block, Mapping):
                return ""
            block_type = block.get("type")
            parts: list[str] = []

            if block_type == "divider":
                return "---"
            if block_type == "mathematical_expression":
                return str(block.get("expression") or "")
            if block_type == "list":
                for item in block.get("items") or []:
                    if not isinstance(item, Mapping):
                        continue
                    item_text = "\n".join(
                        part
                        for part in (
                            flatten_block(child) for child in item.get("blocks") or []
                        )
                        if part
                    )
                    if item_text:
                        label = str(item.get("label") or "-")
                        parts.append(f"{label} {item_text}")
                return "\n".join(parts)
            if block_type == "table":
                for row in block.get("cells") or []:
                    cells = [
                        cls._rich_text_to_plain(cell.get("text"))
                        for cell in row
                        if isinstance(cell, Mapping)
                    ]
                    cells = [cell for cell in cells if cell]
                    if cells:
                        parts.append(" | ".join(cells))
                caption = cls._rich_text_to_plain(block.get("caption"))
                if caption:
                    parts.append(caption)
                return "\n".join(parts)
            if block_type == "buttons":
                button_texts = []
                for button in block.get("buttons") or []:
                    if not isinstance(button, Mapping):
                        continue
                    text = cls._rich_text_to_plain(button.get("text"))
                    if text:
                        button_texts.append(text)
                return " | ".join(button_texts)

            for field in ("text", "summary", "credit"):
                text = cls._rich_text_to_plain(block.get(field))
                if text:
                    parts.append(text)
            for child in block.get("blocks") or []:
                text = flatten_block(child)
                if text:
                    parts.append(text)
            caption = flatten_caption(block.get("caption"))
            if caption:
                parts.append(caption)
            return "\n".join(parts)

        plain = "\n".join(
            part
            for part in (
                flatten_block(block) for block in rich_message.get("blocks") or []
            )
            if part
        )
        return plain.strip()

    async def _extract_reply_details(self, message) -> dict | None:
        """Extract structured context from the message being replied to."""
        reply = self._reply_source(message)
        quote = getattr(getattr(message, "quote", None), "text", None)
        if reply is None and not quote:
            return None

        bot_id = self._bot_user_id
        if bot_id is None:
            try:
                bot_id, _ = await self._ensure_bot_identity()
            except Exception as e:
                self.logger.warning("Failed to identify bot while extracting reply context: {}", e)

        reply_user = getattr(reply, "from_user", None) if reply is not None else None
        origin_details = self._extract_origin_details(
            getattr(reply, "origin", None) if reply is not None else None
        )
        sender_id = getattr(reply_user, "id", None)
        if sender_id is None and origin_details is not None:
            sender_id = (origin_details.get("sender") or {}).get("id")
        message_id = getattr(reply, "message_id", None) if reply is not None else None
        text = getattr(reply, "text", None) if reply is not None else None
        caption = getattr(reply, "caption", None) if reply is not None else None
        if not text and not caption and reply is not None:
            api_kwargs = getattr(reply, "api_kwargs", None)
            rich_message = api_kwargs.get("rich_message") if isinstance(api_kwargs, Mapping) else None
            rich_text = self._rich_message_to_plain(rich_message)
            if rich_text:
                text = rich_text
        if not text and not caption and reply is not None and message_id is not None:
            chat = getattr(reply, "chat", None) or getattr(message, "chat", None)
            chat_id = getattr(chat, "id", None)
            if chat_id is not None:
                text = self._sent_messages.get((str(chat_id), int(message_id)))

        details = {
            "message_id": message_id,
            "sent_by_bot": sender_id == bot_id if sender_id is not None and bot_id is not None else None,
            "text": self._truncate_reply_text(text),
            "caption": self._truncate_reply_text(caption),
            "quote": self._truncate_reply_text(quote),
            "media": self._describe_reply_media(reply) if reply is not None else [],
        }
        if reply is not None and not any(
            (details["text"], details["caption"], details["quote"], details["media"])
        ):
            details["content_unavailable"] = True
        if reply_user is not None:
            details["sender"] = {
                "id": sender_id,
                "username": getattr(reply_user, "username", None),
                "first_name": getattr(reply_user, "first_name", None),
            }
        if origin_details is not None:
            details["origin"] = origin_details
        return details

    @staticmethod
    def _truncate_reply_text(text: str | None) -> str | None:
        if text and len(text) > TELEGRAM_REPLY_CONTEXT_MAX_LEN:
            return text[:TELEGRAM_REPLY_CONTEXT_MAX_LEN] + "..."
        return text

    @staticmethod
    def _format_reply_context(details: dict) -> str:
        """Render structured reply details as clearly delimited prompt context."""
        sent_by_bot = details.get("sent_by_bot")
        bot_label = "yes" if sent_by_bot is True else "no" if sent_by_bot is False else "unknown"
        lines = [
            "[Telegram Reply Context]",
            f"Message ID: {details.get('message_id') or 'unknown'}",
            f"Sent by this bot: {bot_label}",
        ]
        origin = details.get("origin") or {}
        sender = details.get("sender") or origin.get("sender") or {}
        origin_chat = origin.get("chat") or {}
        if sender.get("username"):
            lines.append(f"Author: @{sender['username']}")
        elif sender.get("first_name"):
            lines.append(f"Author: {sender['first_name']}")
        elif sender.get("id") is not None:
            lines.append(f"Author ID: {sender['id']}")
        elif origin.get("sender_name"):
            lines.append(f"Author: {origin['sender_name']}")
        elif origin_chat.get("title"):
            lines.append(f"Source chat: {origin_chat['title']}")
        elif origin_chat.get("username"):
            lines.append(f"Source chat: @{origin_chat['username']}")
        if details.get("text"):
            lines.append(f"Text: {details['text']}")
        if details.get("caption"):
            lines.append(f"Caption: {details['caption']}")
        if details.get("quote"):
            lines.append(f"Selected quote: {details['quote']}")
        for media in details.get("media") or []:
            description = media["type"]
            if media.get("file_name"):
                description += f" ({media['file_name']})"
            elif media.get("mime_type"):
                description += f" ({media['mime_type']})"
            lines.append(f"Media: {description}")
        if details.get("content_unavailable"):
            lines.extend(
                [
                    "Original replied-to message unavailable.",
                    "Do not infer its content; ask the user to copy or clarify the original message.",
                ]
            )
        lines.append("[/Telegram Reply Context]")
        return "\n".join(lines)

    async def _extract_reply_context(self, message) -> str | None:
        """Extract prompt context from the message being replied to, if any."""
        details = await self._extract_reply_details(message)
        if details is None:
            return None
        return self._format_reply_context(details)

    def _record_reply_observation(
        self,
        *,
        chat_id: str,
        message_id: int | None,
        reply,
        details: dict,
        context_attached: bool,
    ) -> None:
        """Append a bounded reply-context observation record.

        The record contains only flags, lengths, and ids — never raw message
        bodies. Used by the runtime inspector for the telegram reply-context
        diagnostics section (#18).
        """
        media = details.get("media") or []
        self._reply_observations.append({
            "ts": time.time(),
            "chat_id": chat_id,
            "message_id": message_id,
            "has_reply_source": reply is not None,
            "reply_to_message_id": details.get("message_id"),
            "reply_id_present": details.get("message_id") is not None,
            "replied_to_bot": details.get("sent_by_bot"),
            "context_attached": context_attached,
            "text_len": len(details.get("text") or ""),
            "caption_len": len(details.get("caption") or ""),
            "quote_len": len(details.get("quote") or ""),
            "media_count": len(media),
            "media_file_id_present": any(
                item.get("file_unique_id") for item in media
            ),
            "content_unavailable": bool(details.get("content_unavailable")),
        })
        self._reply_observations_total += 1

    def reply_context_observations(self) -> dict[str, Any]:
        """Read-only reply-context diagnostics snapshot (flags/lengths/ids only).

        Returns the rolling bounded buffer plus a cumulative total; never
        exposes raw message content. The runtime inspector uses this for the
        telegram reply-context diagnostics section.
        """
        return {
            "total_seen": self._reply_observations_total,
            "limit": TELEGRAM_REPLY_OBSERVATION_LIMIT,
            "entries": list(self._reply_observations),
        }

    async def _download_message_media(
        self, msg, *, add_failure_content: bool = False
    ) -> tuple[list[str], list[str]]:
        """Download media from a message (current or reply). Returns (media_paths, content_parts)."""
        media_file = None
        media_type = None
        if getattr(msg, "photo", None):
            media_file = msg.photo[-1]
            media_type = "image"
        elif getattr(msg, "voice", None):
            media_file = msg.voice
            media_type = "voice"
        elif getattr(msg, "audio", None):
            media_file = msg.audio
            media_type = "audio"
        elif getattr(msg, "document", None):
            media_file = msg.document
            media_type = "file"
        elif getattr(msg, "video", None):
            media_file = msg.video
            media_type = "video"
        elif getattr(msg, "video_note", None):
            media_file = msg.video_note
            media_type = "video"
        elif getattr(msg, "animation", None):
            media_file = msg.animation
            media_type = "animation"
        if not media_file or not self._app:
            return [], []
        try:
            file = await self._app.bot.get_file(media_file.file_id)
            ext = self._get_extension(
                media_type,
                getattr(media_file, "mime_type", None),
                getattr(media_file, "file_name", None),
            )
            media_dir = get_media_dir("telegram")
            unique_id = getattr(media_file, "file_unique_id", media_file.file_id)
            file_path = media_dir / f"{unique_id}{ext}"
            await file.download_to_drive(str(file_path))
            path_str = str(file_path)
            if media_type in ("voice", "audio"):
                transcription = await self.transcribe_audio(file_path)
                if transcription:
                    self.logger.info("Transcribed {}: {}...", media_type, transcription[:50])
                    return [path_str], [f"[transcription: {transcription}]"]
                return [path_str], [f"[{media_type}: {path_str}]"]
            return [path_str], [f"[{media_type}: {path_str}]"]
        except Exception as e:
            self.logger.warning("Failed to download message media: {}", e)
            if add_failure_content:
                return [], [f"[{media_type}: download failed]"]
            return [], []

    async def _ensure_bot_identity(self) -> tuple[int | None, str | None]:
        """Load bot identity once and reuse it for mention/reply checks."""
        if self._bot_user_id is not None or self._bot_username is not None:
            return self._bot_user_id, self._bot_username
        if not self._app:
            return None, None
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        return self._bot_user_id, self._bot_username

    @staticmethod
    def _has_mention_entity(
        text: str,
        entities,
        bot_username: str,
        bot_id: int | None,
    ) -> bool:
        """Check Telegram mention entities against the bot username."""
        handle = f"@{bot_username}".lower()
        for entity in entities or []:
            entity_type = getattr(entity, "type", None)
            if entity_type == "text_mention":
                user = getattr(entity, "user", None)
                if user is not None and bot_id is not None and getattr(user, "id", None) == bot_id:
                    return True
                continue
            if entity_type != "mention":
                continue
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if offset is None or length is None:
                continue
            if text[offset : offset + length].lower() == handle:
                return True
        return handle in text.lower()

    async def _is_group_message_for_bot(self, message) -> bool:
        """Allow group messages when policy is open, @mentioned, or replying to the bot."""
        if message.chat.type == "private" or self.config.group_policy == "open":
            return True

        bot_id, bot_username = await self._ensure_bot_identity()
        if bot_username:
            text = message.text or ""
            caption = message.caption or ""
            if self._has_mention_entity(
                text,
                getattr(message, "entities", None),
                bot_username,
                bot_id,
            ):
                return True
            if self._has_mention_entity(
                caption,
                getattr(message, "caption_entities", None),
                bot_username,
                bot_id,
            ):
                return True

        reply_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
        return bool(bot_id and reply_user and reply_user.id == bot_id)

    def _remember_thread_context(self, message) -> None:
        """Cache Telegram thread context by chat/message id for follow-up replies."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return
        key = (str(message.chat_id), message.message_id)
        self._message_threads[key] = message_thread_id
        if len(self._message_threads) > 1000:
            self._message_threads.pop(next(iter(self._message_threads)))

    @staticmethod
    def _queue_key_for_message(message) -> str:
        """Return the final nanobot session key used for ordered Telegram ingress."""
        return TelegramChannel._derive_topic_session_key(message) or f"telegram:{message.chat_id}"

    @staticmethod
    def _sort_key_for_update(update: Update) -> tuple[int, int]:
        """Sort by Telegram update id, falling back to message id in tests."""
        message = TelegramChannel._message_for_update(update)
        reaction = getattr(update, "message_reaction", None)
        message_id = int(
            getattr(message, "message_id", None)
            or getattr(reaction, "message_id", 0)
            or 0
        )
        update_id = int(getattr(update, "update_id", 0) or 0)
        return (update_id or message_id, message_id)

    @staticmethod
    def _message_for_update(update: Update):
        """Return a new or edited Telegram message from an update."""
        return getattr(update, "message", None) or getattr(update, "edited_message", None)

    def _enqueue_ordered_update(
        self,
        *,
        kind: Literal["command", "message", "reaction"],
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Stage a Telegram update behind a short per-session reorder window."""
        message = self._message_for_update(update)
        reaction = getattr(update, "message_reaction", None)
        key = (
            self._queue_key_for_message(message)
            if message is not None
            else f"telegram:{reaction.chat.id}"
        )
        self._inbound_buffers.setdefault(key, []).append(
            _QueuedTelegramUpdate(
                kind=kind,
                update=update,
                context=context,
                sort_key=self._sort_key_for_update(update),
            )
        )
        if key not in self._inbound_workers:
            self._inbound_workers[key] = asyncio.create_task(
                self._drain_ordered_updates(key)
            )

    async def _drain_ordered_updates(self, key: str) -> None:
        """Drain one Telegram session buffer in stable message order."""
        try:
            while self._running:
                await asyncio.sleep(0.2)
                batch = self._inbound_buffers.get(key, [])
                if not batch:
                    break
                self._inbound_buffers[key] = []
                batch.sort(key=lambda item: item.sort_key)
                for item in batch:
                    try:
                        if item.kind == "command":
                            await self._process_forward_command(item.update, item.context)
                        elif item.kind == "reaction":
                            await self._process_message_reaction(item.update, item.context)
                        else:
                            await self._process_message_update(item.update, item.context)
                    except Exception as e:
                        self.logger.warning(
                            "Telegram queued update handling failed for {}: {}",
                            key,
                            e,
                        )
            if not self._inbound_buffers.get(key):
                self._inbound_buffers.pop(key, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.warning("Telegram ordered update worker failed for {}: {}", key, e)
        finally:
            if not self._inbound_buffers.get(key):
                self._inbound_workers.pop(key, None)

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus for unified handling in AgentLoop."""
        if not update.message or not update.effective_user:
            return
        if not self._running:
            await self._process_forward_command(update, context)
            return
        self._enqueue_ordered_update(kind="command", update=update, context=context)

    async def _process_forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process a queued slash command."""
        message = update.message
        user = update.effective_user
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, message, user)
            return
        self._remember_thread_context(message)

        # Strip @bot_username suffix if present
        content = message.text or ""
        if content.startswith("/") and "@" in content:
            cmd_part, *rest = content.split(" ", 1)
            cmd_part = cmd_part.split("@")[0]
            content = f"{cmd_part} {rest[0]}" if rest else cmd_part
        content = self._normalize_telegram_command(content)

        reply_details = await self._extract_reply_details(message)
        forward_details = self._extract_forward_details(message)
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(message.chat_id),
            content=content,
            metadata=self._build_message_metadata(
                message, user, reply_details, forward_details
            ),
            session_key=self._derive_topic_session_key(message),
            is_dm=message.chat.type == "private",
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not self._message_for_update(update) or not update.effective_user:
            return
        if not self._running:
            await self._process_message_update(update, context)
            return
        self._enqueue_ordered_update(kind="message", update=update, context=context)

    async def _process_message_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process a queued Telegram message update."""

        message = self._message_for_update(update)
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            await self._send_pairing_code_if_private(sender_id, message, user)
            return
        self._remember_thread_context(message)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        if not await self._is_group_message_for_bot(message):
            return

        # Build content from text and/or media
        content_parts = []
        media_paths = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Location content
        if message.location:
            lat = message.location.latitude
            lon = message.location.longitude
            content_parts.append(f"[location: {lat}, {lon}]")

        # Download current message media
        current_media_paths, current_media_parts = await self._download_message_media(
            message, add_failure_content=True
        )
        media_paths.extend(current_media_paths)
        content_parts.extend(current_media_parts)
        if current_media_paths:
            self.logger.debug("Downloaded message media to {}", current_media_paths[0])

        # Reply context: text and/or media from the replied-to message
        reply = self._reply_source(message)
        reply_details = await self._extract_reply_details(message)
        if reply_details is not None:
            reply_ctx = self._format_reply_context(reply_details)
            reply_media, reply_media_parts = (
                await self._download_message_media(reply) if reply is not None else ([], [])
            )
            if reply_media:
                media_paths = reply_media + media_paths
                self.logger.debug("Attached replied-to media: {}", reply_media[0])
            if reply_media_parts and not reply_details["media"]:
                reply_ctx = reply_ctx.replace(
                    "[/Telegram Reply Context]",
                    f"Media: {reply_media_parts[0]}\n[/Telegram Reply Context]",
                )
            content_parts.insert(0, reply_ctx)
            # Diagnostics: record a bounded observation (flags/lengths/ids only).
            self._record_reply_observation(
                chat_id=str(chat_id),
                message_id=getattr(message, "message_id", None),
                reply=reply,
                details=reply_details,
                context_attached=True,
            )
        forward_details = self._extract_forward_details(message)
        if forward_details is not None:
            content_parts.insert(0, self._format_forward_context(forward_details))
        edit_date = getattr(message, "edit_date", None)
        if edit_date is not None:
            formatted_edit_date = (
                edit_date.isoformat() if hasattr(edit_date, "isoformat") else str(edit_date)
            )
            content_parts.insert(
                0,
                "\n".join(
                    [
                        "[Telegram Edited Message]",
                        f"Message ID: {message.message_id}",
                        f"Edited at: {formatted_edit_date}",
                        "This content replaces the sender's earlier version of the message.",
                        "[/Telegram Edited Message]",
                    ]
                ),
            )
        content = "\n".join(content_parts) if content_parts else "[empty message]"

        self.logger.debug("message from {}: {}...", sender_id, content[:50])

        str_chat_id = str(chat_id)
        metadata = self._build_message_metadata(
            message, user, reply_details, forward_details
        )
        if edit_date is not None:
            metadata["is_edit"] = True
            metadata["edit_date"] = formatted_edit_date
        session_key = self._derive_topic_session_key(message)

        # Telegram media groups: buffer briefly, forward as one aggregated turn.
        if media_group_id := getattr(message, "media_group_id", None):
            key = f"{str_chat_id}:{media_group_id}"
            if key not in self._media_group_buffers:
                self._media_group_buffers[key] = {
                    "sender_id": sender_id, "chat_id": str_chat_id,
                    "contents": [], "media": [],
                    "metadata": metadata,
                    "session_key": session_key,
                }
                self._start_typing(str_chat_id)
                await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)
            buf = self._media_group_buffers[key]
            if content and content != "[empty message]":
                buf["contents"].append(content)
            buf["media"].extend(media_paths)
            if key not in self._media_group_tasks:
                self._media_group_tasks[key] = asyncio.create_task(self._flush_media_group(key))
            return

        # Start typing indicator before processing
        self._start_typing(str_chat_id)
        await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)

        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
            session_key=session_key,
        )

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            if not (buf := self._media_group_buffers.pop(key, None)):
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            await self._handle_message(
                sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                content=content, media=list(dict.fromkeys(buf["media"])),
                metadata=buf["metadata"],
                session_key=buf.get("session_key"),
            )
        finally:
            self._media_group_tasks.pop(key, None)

    def _remember_sent_message(self, chat_id: str | int, message_id: int, content: str) -> None:
        """Cache recent bot messages so reaction events can identify their target."""
        key = (str(chat_id), int(message_id))
        self._sent_messages[key] = (
            content[:TELEGRAM_REPLY_CONTEXT_MAX_LEN] + "..."
            if len(content) > TELEGRAM_REPLY_CONTEXT_MAX_LEN
            else content
        )
        if len(self._sent_messages) > 1000:
            self._sent_messages.pop(next(iter(self._sent_messages)))

    @staticmethod
    def _visible_html_text(content: str) -> str:
        """Reduce Telegram HTML to the text a user sees for reaction context."""
        return unescape(re.sub(r"<[^>]+>", "", content))

    @staticmethod
    def _reaction_values(reactions) -> list[str]:
        values = []
        for reaction in reactions or []:
            if emoji := getattr(reaction, "emoji", None):
                values.append(str(emoji))
            elif custom_id := getattr(reaction, "custom_emoji_id", None):
                values.append(f"custom:{custom_id}")
            elif getattr(reaction, "type", None) == "paid":
                values.append("paid")
        return values

    async def _on_message_reaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Stage a reaction change in the ordered ingress queue."""
        if not update.message_reaction:
            return
        if not self._running:
            await self._process_message_reaction(update, context)
            return
        self._enqueue_ordered_update(kind="reaction", update=update, context=context)

    async def _process_message_reaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward a user's reaction change as an immediate agent turn."""
        event = update.message_reaction
        user = getattr(event, "user", None) or update.effective_user
        if event is None or user is None:
            return
        if getattr(user, "is_bot", False) or getattr(user, "id", None) == self._bot_user_id:
            return

        sender_id = self._sender_id(user)
        if not self.is_allowed(sender_id):
            return
        chat_id = str(event.chat.id)
        message_id = int(event.message_id)
        target_content = self._sent_messages.get((chat_id, message_id))
        if event.chat.type != "private" and target_content is None:
            return

        old_reactions = self._reaction_values(event.old_reaction)
        new_reactions = self._reaction_values(event.new_reaction)
        if old_reactions == new_reactions:
            return
        if not old_reactions:
            action = "added"
        elif not new_reactions:
            action = "removed"
        else:
            action = "changed"

        lines = [
            "[Telegram Reaction Event]",
            f"Action: {action}",
            f"Message ID: {message_id}",
            f"Previous reactions: {', '.join(old_reactions) or 'none'}",
            f"New reactions: {', '.join(new_reactions) or 'none'}",
        ]
        if target_content:
            lines.append(f"Reacted-to message: {target_content}")
        lines.append("[/Telegram Reaction Event]")
        metadata = {
            "message_id": message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": event.chat.type != "private",
            "reaction": {
                "action": action,
                "message_id": message_id,
                "old": old_reactions,
                "new": new_reactions,
                "target_content": target_content,
            },
        }
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content="\n".join(lines),
            metadata=metadata,
            is_dm=event.chat.type == "private",
        )

    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _add_reaction(self, chat_id: str, message_id: int, emoji: str) -> None:
        """Add emoji reaction to a message (best-effort, non-blocking)."""
        if not self._app or not emoji:
            return
        try:
            await self._app.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
            self._pending_receipts.add((str(chat_id), message_id))
        except Exception as e:
            self.logger.debug("reaction failed: {}", e)

    async def _remove_reaction(self, chat_id: str, message_id: int) -> None:
        """Remove emoji reaction from a message (best-effort, non-blocking)."""
        key = (str(chat_id), message_id)
        if not self._app or key not in self._pending_receipts:
            return
        try:
            await self._app.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=message_id,
                reaction=[],
            )
        except Exception as e:
            self.logger.debug("reaction removal failed: {}", e)
        finally:
            self._pending_receipts.discard(key)

    async def _set_agent_reaction(self, chat_id: str, message_id: int, emoji: str) -> None:
        """Set an intentional reaction and prevent receipt cleanup from removing it."""
        if not self._app:
            raise RuntimeError("Telegram bot is not running")
        reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
        await self._app.bot.set_message_reaction(
            chat_id=int(chat_id),
            message_id=message_id,
            reaction=reaction,
        )
        self._pending_receipts.discard((str(chat_id), message_id))

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            with suppress(asyncio.CancelledError):
                while self._app:
                    await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                    await asyncio.sleep(4)
        except Exception as e:
            self.logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

    @staticmethod
    def _format_telegram_error(exc: Exception) -> str:
        """Return a short, readable error summary for logs."""
        if isinstance(exc, InvalidToken):
            # PTB embeds the token itself in InvalidToken text; never log it.
            return "bot token rejected by Telegram"
        text = str(exc).strip()
        if text:
            return text
        if exc.__cause__ is not None:
            cause = exc.__cause__
            cause_text = str(cause).strip()
            if cause_text:
                return f"{exc.__class__.__name__} ({cause_text})"
            return f"{exc.__class__.__name__} ({cause.__class__.__name__})"
        return exc.__class__.__name__

    def _on_polling_error(self, exc: Exception) -> None:
        """Log long-poll failures; network errors trigger a supervised rebuild."""
        if isinstance(exc, InvalidToken):
            # A rejected token is a config error: fail the channel and stop the
            # recovery loop instead of retrying (and spamming) forever.
            self.logger.error("polling error: bot token rejected by Telegram (InvalidToken)")
            self._failed = True
            self._running = False
            return
        summary = self._format_telegram_error(exc)
        self.logger.error("polling error: {} ({})", summary, exc.__class__.__name__)
        if isinstance(exc, (NetworkError, TimedOut)):
            # Network-class failures recover by rebuilding the connection pools.
            self._recovery_requested = True

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        summary = self._format_telegram_error(context.error)

        if isinstance(context.error, (NetworkError, TimedOut)):
            self.logger.warning("network issue: {}", summary)
        else:
            self.logger.error("error: {}", summary)

    def _get_extension(
        self,
        media_type: str,
        mime_type: str | None,
        filename: str | None = None,
    ) -> str:
        """Get file extension based on media type or original filename."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
                "video/x-matroska": ".mkv", "video/3gpp": ".3gp",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "video": ".mp4", "file": ""}
        if ext := type_map.get(media_type, ""):
            return ext

        if filename:
            return "".join(Path(filename).suffixes)

        return ""

    def _build_keyboard(self, buttons: list) -> InlineKeyboardMarkup | None:
        """Build inline keyboard markup if inline_keyboards is enabled."""
        if not buttons or not self.config.inline_keyboards:
            return None
        keyboard = [
            [InlineKeyboardButton(label, callback_data=self._safe_callback_data(label)) for label in row]
            for row in buttons
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def _safe_callback_data(label: str) -> str:
        # Telegram caps callback_data at 64 bytes UTF-8; truncate at a char boundary so the keyboard still sends.
        encoded = label.encode("utf-8")
        if len(encoded) <= 64:
            return label
        return encoded[:64].decode("utf-8", errors="ignore")

    @staticmethod
    def _buttons_as_text(buttons: list[list[str]]) -> str:
        # Buttons are semantic options; when we can't render a keyboard, the user still needs to see them.
        return "\n".join(" ".join(f"[{label}]" for label in row) for row in buttons if row)

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard button clicks (callback queries)."""
        if not update.callback_query or not update.effective_user:
            return
        query = update.callback_query
        user = update.effective_user
        chat_id = query.message.chat_id if query.message else None
        sender_id = self._sender_id(user)
        if not chat_id:
            self.logger.warning("Callback query without chat_id")
            return
        if not self.is_allowed(sender_id):
            return
        button_label = query.data or ""
        await query.answer()
        if query.message:
            with suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)
        self.logger.debug("Inline button tap from {}: {}", sender_id, button_label)
        self._start_typing(str(chat_id))
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=button_label,
            metadata={
                "callback_query_id": query.id,
                "button_label": button_label,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_callback": True,
            },
        )
