"""Shared constants for the Bluesound Alt tests.

Addresses refer to the players captured under tests/fixtures/home: a NAD C700
leading a group of two Pulse Flex 2i, all playing its S/PDIF input, and a
standalone Pulse Soundbar+ on HDMI eARC.
"""

from __future__ import annotations

LABEL = "home"
# The group master again, captured with browse pages followed.
PAGING_LABEL = "paging"
# A NAD T758 receiver, on its own "Squeeze" input. Its inputs are listed in
# /SyncStatus rather than /Browse (issue #13).
T758_LABEL = "t758"
T758 = ("192.0.2.1", 11000)
T758_MAC = "00005e005301"
# The C700 on its own, on BluOS 4.18.18: settings schema 36, whose settings
# name an absolute URL with the player's address rather than a path.
BLUOS_418_LABEL = "bluos-4.18"
BLUOS_418 = ("192.0.2.1", 11000)
BLUOS_418_MAC = "00005e005301"
# The home group (C700 leading both Flexes) playing Apple Music, on BluOS
# 4.18.18. Same addresses as the home capture. Apple Music is a menu to
# browse, not a source, and only the C700 lists it.
APPLE_MUSIC_LABEL = "apple-music"

MASTER = ("192.0.2.1", 11000)
SLAVE_1 = ("192.0.2.2", 11000)
SLAVE_2 = ("192.0.2.3", 11000)
SOUNDBAR = ("192.0.2.4", 11000)

# Fake MACs as dev/probe_api.py assigns them, in the integration's own form.
MASTER_MAC = "00005e005301"
SLAVE_1_MAC = "00005e005302"
SLAVE_2_MAC = "00005e005303"
SOUNDBAR_MAC = "00005e005304"
