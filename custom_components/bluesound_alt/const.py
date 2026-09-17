"""Constants for the Bluesound Alt integration."""

DOMAIN = "bluesound_alt"

DEFAULT_PORT = 11000
POLL_INTERVAL = 1
LONG_POLL_TIMEOUT = 100
# The BluOS API asks for at least this many seconds between long polls of one
# resource, even when an answer comes back sooner.
LONG_POLL_MIN_INTERVAL = 1
NODE_OFFLINE_CHECK_TIMEOUT = 10
# Seconds to wait before retrying after a player stops answering.
RETRY_DELAY = 10
# Upper bound on pages fetched for one browse menu. BluOS pages long lists
# (around 20 items a page); this stops a huge catalogue loading all at once.
MAX_BROWSE_PAGES = 10
# How long to wait for players to confirm a regroup before relying on it, and
# how often to look. Real players settle well within a second.
GROUPING_WAIT_SECONDS = 5
GROUPING_POLL_SECONDS = 0.25
