# nanobot

This repository is an independent, personal fork of nanobot. It keeps a small,
readable agent loop with tools, persistent memory, model routing, MCP support,
and scheduled tasks. It is maintained for a private deployment rather than as a
supported public distribution.

## Start here

- [Install and quick start](./docs/quick-start.md)
- [Configuration](./docs/configuration.md)
- [Providers and models](./docs/providers.md)
- [Architecture](./docs/architecture.md)
- [Development](./docs/development.md)

## Features

- Interactive CLI and gateway runtime
- Telegram, Discord, Email, and WebSocket channels
- OpenAI-compatible, Anthropic, OpenAI Codex, and other provider backends
- Model presets and fallback models
- Filesystem, shell, web, MCP, cron, image-generation, and CLI-app tools
- Persistent sessions, memory consolidation, and long-running goals
- Optional provider and channel plugins

## Install

Python 3.11 or newer is required. From a checkout of this repository:

```bash
git clone https://github.com/agbocsardi/nanobot.git
cd nanobot
uv sync
```

For development dependencies, use:

```bash
uv sync --extra dev
```

Initialize the local configuration and workspace:

```bash
uv run nanobot onboard
```

Set a provider and model in `~/.nanobot/config.json`, then test one message:

```bash
uv run nanobot status
uv run nanobot agent -m "Hello!"
```

For an interactive conversation:

```bash
uv run nanobot agent
```

See [Troubleshooting](./docs/troubleshooting.md) for common setup and provider
errors.

## Channels

The gateway currently supports Telegram, Discord, Email, and a minimal
programmatic WebSocket channel. Configure a channel in
`~/.nanobot/config.json`, then run:

```bash
uv run nanobot channels status
uv run nanobot gateway
```

See the [channel documentation](./docs/chat-apps.md) and
[channel plugin guide](./docs/channel-plugin-guide.md).

## Documentation

- [Concepts](./docs/concepts.md)
- [Architecture](./docs/architecture.md)
- [Providers and models](./docs/providers.md)
- [Provider cookbook](./docs/provider-cookbook.md)
- [Configuration](./docs/configuration.md)
- [Memory](./docs/memory.md)
- [Python SDK](./docs/python-sdk.md)
- [WebSocket channel](./docs/websocket.md)
- [Deployment](./docs/deployment.md)
- [Contributing](./CONTRIBUTING.md)

## Development

Install the development dependencies and run the checks with:

```bash
uv sync --extra dev
uv run ruff check nanobot/
uv run python -m pytest
```

Repository workflow notes are in [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

nanobot is released under the [MIT License](./LICENSE).
