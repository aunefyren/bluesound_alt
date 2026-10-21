"""Constants for the Bluesound Alt integration."""

DOMAIN = "bluesound_alt"

DEFAULT_PORT = 11000
# Option: keys of the sources left out of the source list (see
# BluesoundCoordinator.source_options).
CONF_HIDDEN_SOURCES = "hidden_sources"
# Option: input key -> entity id of a media player that stands in for the
# player while that input plays, with its track, artwork and controls.
CONF_LINKED_PLAYERS = "linked_players"
# Every player also serves a web page, where it is rebooted from.
WEB_PORT = 80
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
# How often to re-read the audio settings. The BluOS app changes tone and EQ
# without anything in /Status or /SyncStatus showing it.
SETTINGS_POLL_SECONDS = 30
# Settings menus read for a player's settings: Audio, Player and Sources.
SETTINGS_MENUS = ("audio", "player", "capture")
# The sleep timer's steps, in minutes. /Sleep moves the timer to the next step
# above the minutes left, and from the last one to off.
SLEEP_STEPS = (15, 30, 45, 60, 90)
