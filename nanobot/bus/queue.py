"""Async message queue for decoupled channel-agent communication."""

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar, overload

from loguru import logger

from nanobot.bus.events import DeliveryResult, InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from nanobot.events import AgentEvent

_EventT = TypeVar("_EventT", bound="AgentEvent")
EventHandler = Callable[["AgentEvent"], Awaitable[None] | None]


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        # Fork additions (WebUI integration): ordered local typed-event
        # subscribers, mirroring the pinned upstream MessageBus (d0d0a44e).
        # The fork's own queue/delivery contract below is unchanged.
        self._handlers: list[EventHandler] = []
        self._pending: set[asyncio.Task[None]] = set()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def publish_event(
        self,
        event: "AgentEvent",
        *,
        channel: str,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Queue a typed event for its channel, keeping the delivery contract."""
        from nanobot.bus.outbound_events import outbound_message_for_event

        await self.publish_outbound(outbound_message_for_event(
            channel=channel, chat_id=chat_id, event=event, metadata=metadata,
        ))

    async def publish_outbound_tracked(
        self, msg: OutboundMessage,
    ) -> asyncio.Future[DeliveryResult]:
        """Queue the message and retain its channel acknowledgement future."""
        if msg.delivery is None:
            msg.delivery = asyncio.get_running_loop().create_future()
        await self.publish_outbound(msg)
        return msg.delivery

    @staticmethod
    def acknowledge(msg: OutboundMessage, result: DeliveryResult) -> None:
        if msg.delivery is not None and not msg.delivery.done():
            msg.delivery.set_result(result)

    @staticmethod
    async def wait_delivery(msg: OutboundMessage, timeout: float = 60) -> DeliveryResult:
        """A missing or late acknowledgement is unknown, never success."""
        if msg.delivery is None:
            return DeliveryResult("unknown", "No channel acknowledgement requested")
        try:
            return await asyncio.wait_for(asyncio.shield(msg.delivery), timeout)
        except asyncio.TimeoutError:
            return DeliveryResult("unknown", "Channel acknowledgement timed out")

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    @overload
    def subscribe(
        self,
        handler: Callable[[_EventT], Awaitable[None] | None],
        event_type: type[_EventT],
    ) -> Callable[[], None]: ...

    @overload
    def subscribe(
        self,
        handler: EventHandler,
        event_type: None = None,
    ) -> Callable[[], None]: ...

    def subscribe(
        self,
        handler: Callable[..., Awaitable[None] | None],
        event_type: "type[AgentEvent] | None" = None,
    ) -> Callable[[], None]:
        """Connect an ordered, awaited typed-event handler (fork addition).

        Mirrors the pinned upstream MessageBus so the WebUI coordinator and
        runtime event bridge can subscribe to AgentEvents; message delivery
        is unaffected.
        """
        active = True

        def entry(event: "AgentEvent") -> Awaitable[None] | None:
            if active and (event_type is None or isinstance(event, event_type)):
                return handler(event)
            return None

        self._handlers.append(entry)

        def _unsubscribe() -> None:
            nonlocal active
            active = False
            with contextlib.suppress(ValueError):
                self._handlers.remove(entry)

        return _unsubscribe

    async def publish(self, event: "AgentEvent") -> None:
        """Await local typed-event subscribers in registration order."""
        for handler in list(self._handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("event handler failed for {}", type(event).__name__)

    def publish_nowait(self, event: "AgentEvent") -> asyncio.Task[None] | None:
        """Schedule local typed-event dispatch without awaiting handlers."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("dropping event without a running loop: {}", type(event).__name__)
            return None
        task = loop.create_task(self.publish(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def drain(self) -> None:
        """Finish scheduled typed-event dispatches before disconnecting."""
        while self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
