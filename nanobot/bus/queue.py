"""Async message queue for decoupled channel-agent communication."""

import asyncio

from nanobot.bus.events import DeliveryResult, InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

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
