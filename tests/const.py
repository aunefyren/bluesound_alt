"""Shared constants for the Bluesound Alt tests.

Addresses refer to the players captured under tests/fixtures/home: a NAD C700
leading a group of two Pulse Flex 2i, all playing its S/PDIF input, and a
standalone Pulse Soundbar+ on HDMI eARC.
"""

from __future__ import annotations

LABEL = "home"
# The group master again, captured with browse pages followed.
PAGING_LABEL = "paging"

MASTER = ("192.0.2.1", 11000)
SLAVE_1 = ("192.0.2.2", 11000)
SLAVE_2 = ("192.0.2.3", 11000)
SOUNDBAR = ("192.0.2.4", 11000)

# Fake MACs as dev/probe_api.py assigns them, in the integration's own form.
MASTER_MAC = "00005e005301"
SLAVE_1_MAC = "00005e005302"
SLAVE_2_MAC = "00005e005303"
SOUNDBAR_MAC = "00005e005304"
