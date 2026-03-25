"""Message bus module for decoupled channel-agent communication."""

from speckbot.bus.events import InboundMessage, OutboundMessage
from speckbot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
