"""Message type constants for the bridge WebSocket protocol.

See PROTOCOL.md at the repository root for the full specification.
"""

TYPE_AUTH = "auth"
TYPE_AUTH_OK = "auth_ok"
TYPE_AUTH_FAIL = "auth_fail"
TYPE_CHAT = "chat"
TYPE_SERVER_START = "server_start"
TYPE_SERVER_STOP = "server_stop"
TYPE_PLAYER_JOIN = "player_join"
TYPE_PLAYER_LEAVE = "player_leave"
TYPE_PLAYERS_REQUEST = "players_request"
TYPE_PLAYERS_RESPONSE = "players_response"
TYPE_PROXY_LINK = "proxy_link"

# Optional "role" in the auth message. A connection with this role is the
# Velocity plugin; it is kept apart from the Paper plugin's connection even
# though both use the same server_id and secret.
ROLE_PROXY = "proxy"
PROXY_STATUS_UP = "up"
PROXY_STATUS_DOWN = "down"
