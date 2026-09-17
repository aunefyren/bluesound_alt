"""Constants for the Bluesound Alt integration."""

DOMAIN = "bluesound_alt"

DEFAULT_PORT = 11000
POLL_INTERVAL = 1
LONG_POLL_TIMEOUT = 100
NODE_OFFLINE_CHECK_TIMEOUT = 10
# Seconds to wait before retrying after a player stops answering.
RETRY_DELAY = 10
# Upper bound on pages fetched for one browse menu. BluOS pages long lists
# (around 20 items a page); this stops a huge catalogue loading all at once.
MAX_BROWSE_PAGES = 10
