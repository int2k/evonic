"""Channel Manager — lifecycle management for active channels."""

import logging
from typing import Dict, Type, Optional
from backend.channels.base import BaseChannel
from backend.channels.telegram import TelegramChannel
from backend.channels.whatsapp import WhatsAppChannel
from backend.channels.discord import DiscordChannel
from backend.channels.lark import LarkChannel
from models.db import db

_logger = logging.getLogger(__name__)

# Map of channel type -> class
CHANNEL_TYPES: Dict[str, Type[BaseChannel]] = {
    'telegram': TelegramChannel,
    'whatsapp': WhatsAppChannel,
    'discord': DiscordChannel,
    'lark': LarkChannel,
}


class ChannelManager:
    def __init__(self):
        self._active: Dict[str, BaseChannel] = {}  # channel_id -> instance

    def start_channel(self, channel_id: str) -> bool:
        """Start a channel by its DB ID. Skips disabled channels."""
        if channel_id in self._active and self._active[channel_id].is_running:
            return True  # already running

        channel_data = db.get_channel(channel_id)
        if not channel_data:
            _logger.warning("Channel %s not found in database — cannot start", channel_id)
            return False

        if not channel_data.get('enabled'):
            _logger.info("Skipping disabled channel %s", channel_id)
            return False

        # Don't start channels for disabled agents
        agent = db.get_agent(channel_data['agent_id'])
        if agent and not agent.get('enabled', True):
            _logger.info("Skipping channel %s — agent %s is disabled", channel_id, channel_data['agent_id'])
            return False

        chan_type = channel_data.get('type')
        cls = CHANNEL_TYPES.get(chan_type)
        if not cls:
            _logger.error("Unknown channel type '%s' for channel %s", chan_type, channel_id)
            raise ValueError(f"Unknown channel type: {chan_type}")

        config = channel_data.get('config', {})
        if isinstance(config, str):
            import json
            config = json.loads(config)

        _logger.info("Starting channel %s (type: %s, agent: %s)", channel_id, chan_type, channel_data['agent_id'])
        instance = cls(channel_id, channel_data['agent_id'], config)
        instance.start()
        self._active[channel_id] = instance
        _logger.info("Channel %s (%s) started successfully for agent %s", channel_id, chan_type, channel_data['agent_id'])
        return True

    def stop_channel(self, channel_id: str) -> bool:
        """Stop a running channel."""
        instance = self._active.get(channel_id)
        if not instance:
            _logger.warning("Channel %s not found in active list — nothing to stop", channel_id)
            return False
        _logger.info("Stopping channel %s", channel_id)
        instance.stop()
        del self._active[channel_id]
        _logger.info("Channel %s stopped", channel_id)
        return True

    def get_channel_instance(self, channel_id: str) -> Optional[BaseChannel]:
        """Return the active channel instance for the given channel_id, or None."""
        return self._active.get(channel_id)

    def is_running(self, channel_id: str) -> bool:
        instance = self._active.get(channel_id)
        return instance.is_running if instance else False

    def start_all_enabled(self):
        """Start all enabled channels from DB (called at app startup).
        Skips channels belonging to disabled agents."""
        _logger.info("Starting all enabled channels...")
        agents = db.get_agents()
        for agent in agents:
            if not agent.get('enabled', True):
                continue  # skip disabled agents entirely
            channels = db.get_channels(agent['id'])
            for ch in channels:
                if ch.get('enabled'):
                    try:
                        self.start_channel(ch['id'])
                    except Exception as e:
                        _logger.error("Failed to start channel %s (type: %s): %s",
                                      ch['id'], ch.get('type', 'unknown'), e)
        _logger.info("Finished starting all enabled channels")

    def stop_all(self):
        """Stop all running channels."""
        _logger.info("Stopping all channels (%d active)...", len(self._active))
        for channel_id in list(self._active.keys()):
            self.stop_channel(channel_id)
        _logger.info("All channels stopped")


# Global instance
channel_manager = ChannelManager()
