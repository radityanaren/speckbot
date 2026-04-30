"""Chat channels module with plugin architecture."""

from speckbot.bus.channels.base import BaseChannel
from speckbot.bus.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
