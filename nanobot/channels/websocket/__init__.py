"""WebSocket channel package.

Fork graft of the upstream ``nanobot.channels.websocket`` package at pinned
HKUDS/nanobot ``d0d0a44e57632c3d269e511339cff7ddb698e62e``, reduced to the
runtime module (no plugin manifest/validation machinery) with localized fork
compatibility — see ``runtime.py`` for the graft contract. Re-exports keep
``nanobot.channels.websocket`` importable as a module path so channel
discovery keeps finding ``WebSocketChannel``.
"""

from nanobot.channels.websocket.runtime import (
    WebSocketChannel,
    WebSocketConfig,
    publish_runtime_model_update,
)

__all__ = ["WebSocketChannel", "WebSocketConfig", "publish_runtime_model_update"]
