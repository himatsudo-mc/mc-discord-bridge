# Bridge Communication Protocol

Version: 2.0

This document specifies the WebSocket protocol used between the Minecraft
plugin (`minecraft-plugin/`) and the Discord bot (`discord-bot/`).

## Changes since 1.0

- The `auth` message gained an optional `server_id` field so the bot can
  bridge multiple Minecraft servers, each to its own Discord channel, from a
  single process. See "Multi-Server" below.
- Delivery is no longer drop-on-disconnect: both sides now hold a small
  bounded queue and flush it on reconnect. See "Fault Tolerance" below.

## Roles

- **Server**: the Discord bot (`discord-bot`). It hosts a WebSocket endpoint
  (`ws://<bridge.host>:<bridge.port>/`) using aiohttp.
- **Client**: the Minecraft plugin (`minecraft-plugin`). It connects to the
  bot on plugin enable and reconnects with backoff whenever the connection
  drops.

The bot can hold one connection per configured `server_id` simultaneously
(see "Multi-Server" below). If a new connection authenticates with a
`server_id` that already has an active connection, the bot closes the old
one for *that server_id only* - other servers' connections are unaffected.

## Transport

- Plain WebSocket (`ws://`). If the bot and plugin communicate across an
  untrusted network, run the bot behind a TLS-terminating reverse proxy and
  use `wss://` instead — the protocol itself is transport-agnostic.
- All frames are text frames containing a single flat JSON object with a
  `type` field.

## Authentication

Both sides are configured with a shared secret token (`bridge.secret` on each
side; see "Multi-Server" if the bot has more than one `links[]` entry).

1. Immediately after the WebSocket handshake completes, the client must send:

   ```json
   {"type": "auth", "token": "<shared-secret>", "server_id": "<id>"}
   ```

   `server_id` is optional but strongly recommended:
   - If present, the bot looks up the link configured with that `server_id`
     and checks `token` against that link's own secret.
   - If omitted, the bot accepts the connection only if it has exactly one
     configured link (single-server setups, including plugins predating
     this field); with more than one link configured, an omitted
     `server_id` is rejected as ambiguous.

2. If authentication succeeds, the server replies:

   ```json
   {"type": "auth_ok"}
   ```

3. If the token is missing/wrong, the `server_id` doesn't match any
   configured link, `server_id` was omitted while multiple links are
   configured, or no message arrives within `bridge.auth_timeout` seconds
   (default 10s), the server replies:

   ```json
   {"type": "auth_fail"}
   ```

   and closes the connection with close code `4001`.

4. Any message received before authentication succeeds (other than `auth`)
   is ignored.

## Message Types

### Client → Server (Minecraft → Discord)

| type           | fields                          | meaning                                   |
|----------------|----------------------------------|--------------------------------------------|
| `auth`         | `token`, `server_id` (optional)   | Authenticate the connection.               |
| `chat`         | `player`, `message`              | A player sent a chat message in-game.      |
| `server_start` | `server_name`                    | The server finished starting up.           |
| `server_stop`  | `server_name`                    | The server is shutting down.               |
| `player_join`  | `player`                         | A player joined the server.                |
| `player_leave` | `player`                         | A player left the server.                  |

| `players_response` | `request_id`, `players`, `max` | Reply to `players_request`. `players` is a comma-separated list of online player names (empty if none); `max` is the max player slots. |

Example:

```json
{"type": "chat", "player": "Steve", "message": "hello world"}
```

### Server → Client (Discord → Minecraft)

| type        | fields            | meaning                                    |
|-------------|-------------------|---------------------------------------------|
| `auth_ok`   | –                 | Authentication succeeded.                    |
| `auth_fail` | –                 | Authentication failed; connection is closed. |
| `chat`      | `user`, `message` | A message was posted in the bridged Discord channel. |
| `players_request` | `request_id` | Asks the plugin for its online player list (sent when someone runs `!players`). The plugin answers with `players_response` carrying the same `request_id`. |

Example:

```json
{"type": "chat", "user": "SomeDiscordUser", "message": "hi from discord"}
```

## Formatting

Message *content* is transmitted as plain, unformatted text. Each side is
responsible for applying its own display formatting from its local config:

- Minecraft plugin: `format.discord-to-minecraft` (`config.yml`), e.g.
  `"&9[Discord] &b{user}&f: {message}"`. Color codes are resolved against the
  template *before* `{user}`/`{message}` are substituted, so a Discord
  message can never inject an in-game color code.
- Discord bot: `format.minecraft_to_discord` (`config.yml`), e.g.
  `"**{player}**: {message}"`. Player names and message text are passed
  through `discord.utils.escape_mentions` / `escape_markdown`, and messages
  are sent with `allowed_mentions=AllowedMentions.none()`, so an in-game
  message can never ping `@everyone`, a role, or a user.

`player_join`/`player_leave` notices are formatted the same way as chat, via
`format.player_join`/`format.player_leave` (`config.yml`, Discord bot side
only), e.g. `"\U0001F7E2 **{player}** が参加しました"`. Player names go
through the same escaping and are sent to `discord.notify_channel_id` with
`allowed_mentions=AllowedMentions.none()`.

`players_request` is request/response and is never queued: if the plugin is
offline or does not answer within 5 seconds, the bot tells the Discord user
the list is unavailable instead of showing stale data.

## Loop Prevention

- The Discord bot ignores its own messages and any message authored by a
  bot account.
- The Discord bot ignores messages in the chat channel that start with
  `discord.command_prefix` (default `!`) — those are treated as bot commands,
  not chat, and are never relayed to Minecraft.
- The Minecraft plugin never re-broadcasts a message it received from
  Discord back to Discord (chat received over the bridge is only broadcast
  in-game).

## Fault Tolerance

- **Bot offline / unreachable**: the plugin's WebSocket I/O runs off the
  server main thread and sends are non-blocking. If the socket isn't open
  and authenticated (or the send itself fails), the message is queued
  instead of dropped, bounded by `bridge.max-queue-size` (oldest entries are
  evicted first once full) and flushed in order once connected again,
  skipping any entry older than `bridge.max-queue-age-seconds`. The plugin
  retries connecting on an exponential backoff (`bridge.reconnect.initial-delay`
  up to `bridge.reconnect.max-delay`, in seconds). It also pings the bot
  every `bridge.connection-lost-timeout-seconds` and reconnects if no pong
  arrives in time, so a half-open/blackholed TCP connection is detected
  promptly instead of appearing open indefinitely.
- **Plugin offline / unreachable**: the bot queues outgoing chat messages
  for that `server_id` (same bounding/TTL behavior as above, configured via
  `bridge.max_queue_size`/`bridge.max_queue_age_seconds` in the bot's
  config.yml) instead of dropping them, and flushes the queue once that
  server_id reconnects.
- **Reconnect race window**: the bot only replaces or clears a given
  `server_id`'s connection slot while holding that server_id's internal
  lock, and flushes its queue as part of the same locked registration - so a
  message sent while a reconnect is in flight is always either delivered to
  the fresh connection or queued, never silently lost in the gap.
- **A single bad message never tears down the connection**: if relaying one
  inbound message to Discord fails (e.g. a transient Discord API error),
  the bot logs and drops that one message but keeps the connection - and the
  rest of the session - alive.
- **Shutdown**: on `onDisable`, the plugin sends `server_stop` and closes the
  socket on a background thread bounded by `bridge.shutdown-flush-timeout-ms`
  (default 3000ms), so an unreachable bot cannot delay server shutdown.

## Multi-Server

A single Discord bot process can bridge multiple Minecraft servers, each to
its own Discord channel(s), by configuring multiple entries under `links:`
in the bot's `config.yml` (see `discord-bot/config.example.yml`). Each entry
has its own `server_id`, `secret`, `chat_channel_id`, and `notify_channel_id`.

- `server_id` is a stable identifier - distinct from the cosmetic
  `server.name` used in start/stop notifications - that must be unique
  among all plugin instances pointed at one bot, and must equal one of the
  bot's configured `links[].server_id` values exactly. Configure it via
  `server.id` in the plugin's `config.yml`.
- A bot with no `links:` configured (today's single-server config.yml shape)
  behaves as if it had exactly one link with `server_id: "default"`; a
  plugin with no `server.id` configured defaults to `"default"` as well, so
  existing single-server deployments keep working unchanged.
- Each server_id's connection, queue, and Discord channel routing are fully
  independent - reconnecting, disconnecting, or flooding one server_id never
  affects another's delivery.
