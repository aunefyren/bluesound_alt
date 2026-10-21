# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/). Releases up to 1.1.0 are described
on the [GitHub releases page](https://github.com/aunefyren/bluesound_alt/releases).

## [Unreleased]

### Changed
- The source list holds only the inputs connected to the player: analogue,
  optical, HDMI, Bluetooth and a receiver's own inputs. Spotify, presets and
  the streaming services are played from the media browser instead, where
  Spotify is a one-tap item and presets come first, under Presets. Whatever
  plays still shows as the source, Spotify and Apple Music included.
- A player without inputs, such as a Pulse Flex, or with all of them hidden,
  no longer offers source selection, rather than an empty list.
- A preset is played from an automation with `media_player.play_media` and
  the id `bluesound:preset/<id>`, which the media picker fills in. Selecting
  Spotify or a preset by name as a source still works, so existing
  automations do not break.
- Only inputs are offered under Configure. Spotify or a preset hidden there
  before stays saved, and does nothing.

### Fixed
- Turning Bluetooth on or off, in the BluOS app or Home Assistant, adds or
  removes it from the source list straight away. It used to take a restart
  of Home Assistant, as the player does not count it as a change of sources.
- The media browser's top level shows its icons, and presets their artwork.
  The player serves them over plain HTTP, which a browser on an HTTPS
  Home Assistant, or a phone away from home, cannot load; they now come
  through Home Assistant.

## [1.4.0] - 2026-10-20

### Added
- The integration's own icon and logo, shown in Home Assistant 2026.3 and
  newer.
- Sources can be hidden from a player's source list under Configure on the
  player's integration entry, Spotify on a player without an account, say,
  or an unused input. A hidden source stays hidden when renamed in the BluOS
  app, and still shows as the source while it plays.

### Changed
- Licensed under the GNU General Public License v3.0. The repository had no
  licence before.
- HACS no longer hides the integration from users outside Norway.
- HACS installs and updates the integration on Home Assistant 2026.3 or
  newer, the first release that shows its icon and logo.
- xmltodict is required at a minimum version (0.13.0) rather than pinned, so
  it no longer clashes with the xmltodict Home Assistant itself ships (1.0.4
  in 2026.10).
- The integration declares itself a device integration, one entry per
  player, as Home Assistant expects.

### Fixed
- The source list follows the player: inputs renamed, hidden or added in the
  BluOS app, and presets added or removed, show up straight away rather than
  after restarting Home Assistant.
- On a receiver input the input's name is the title, rather than "External
  Source", so cards that show only the title say which input is on
  ([#13](https://github.com/aunefyren/bluesound_alt/issues/13)).
- NAD players, such as the C700 and T758, show NAD as their maker rather
  than Bluesound.
- Seeking is only offered while the player says it can seek, so cards no
  longer offer it on radio or an input.
- Selecting a source the player does not have reports an error instead of
  doing nothing.
- Changing a player's settings works on BluOS 4.18, whose settings name their
  full address; every change failed with an invalid URL.

### Development
- CI runs on Python 3.14, so it tests against current Home Assistant; on
  Python 3.13 it was held back at 2026.2.
- Captures from BluOS 4.18.18: a C700 with the new settings URLs, and a group
  playing Apple Music, which shows as the source and is browsed like TuneIn.

## [1.3.0] - 2026-10-12

### Added
- Players on the network are discovered: they show up under Discovered in
  Settings, ready to add with a click. A player already set up that turns up
  at a new address, after a new DHCP lease say, is updated to it.
- The player's settings, wherever its menus offer them, as the BluOS app
  shows them:
  - Audio: listening mode (Off, Music, Movie, Late Night), tone controls,
    bass, treble, balance, subwoofer, output mode, replay gain, minimum and
    maximum volume, and the soundbar's Surround Upmixer, Virtualizer, Volume
    Leveler and Deep Bass.
  - Player: indicator brightness, and on players with a display (C700) its
    brightness, automatic brightness and dark mode.
  - Sources: Bluetooth mode (Manual, Automatic, Guest, Disabled).

  A setting that does not apply right now shows as unavailable, such as bass
  and treble while tone controls are off. Listening mode, volume limits,
  subwoofer and Bluetooth mode changed in the BluOS app show up straight
  away, the others within 30 seconds.
- A Restart button on every player, which reboots it as the BluOS app's
  Diagnostics page does.

### Fixed
- Adding a player that is already set up, at its new address, now updates
  the address instead of only saying it is already configured.
- Pausing a player that is already paused, for instance from the BluOS app,
  no longer resumes it.

## [1.2.1] - 2026-10-12

### Added
- The inputs of receivers with a BluOS module, such as the NAD T758, appear
  in the source list and can be selected, and the active one shows as the
  source ([#13](https://github.com/aunefyren/bluesound_alt/issues/13)).

## [1.2.0] - 2026-09-17

### Added
- Media browsing: the player's inputs, TuneIn, Radio Paradise and BluOS Radio
  menus, alongside Home Assistant's own media sources. Menus that group their
  stations into categories are listed in full, and long menus continue past
  their first page (up to 10 pages).
- Stations and inputs picked in the media browser play on the player.
- Presets appear in the source list and can be selected.
- AirPlay and HTTP streams show up as the current source while they play.
- Diagnostics, with addresses, MAC addresses, player names and account
  identifiers in stream URLs redacted.

### Fixed
- Home Assistant's startup no longer waits on the players' push connections.
- A player that is off or unreachable when Home Assistant starts is retried
  until it comes back, instead of failing to set up for good.
- A player no longer stops updating after an error, a malformed or empty
  response, or a dropped connection. It shows as unavailable until it answers
  again.
- Grouped speakers show the input the group is playing as their source.
- Grouped speakers show their own volume straight after startup, rather than
  the group's until their volume was next changed.
- Group members are complete regardless of the order players were set up in.
- Setting up a player that times out reports "cannot connect", and an address
  that answers with something other than a player reports "invalid response";
  both used to report an unexpected error.
- Grouping reworked so groups are never nested and nothing fails silently.
  Thanks to [@eKristensen](https://github.com/eKristensen) for tracking down
  the first two on real hardware.
  - Joining from a grouped speaker's card adds to its group; it used to do
    nothing.
  - Selecting a speaker that leads another group moves just that speaker; it
    used to nest the other group underneath, stopping it and hiding its
    speakers from the BluOS app. Where the speaker's playback can move, the
    rest of its old group plays on.
  - Adding speakers to a group from Home Assistant's group dialog, which sends
    the speakers already in the group too, leaves those where they are.
  - Ungrouping a speaker whose group leader is not set up in Home Assistant
    works; it used to do nothing.
  - Ungrouping a group's leader lets the others play on where its playback can
    move to them, rather than always splitting the group up.
  - The secondary speaker of a stereo pair or other fixed group is refused
    instead of being split from its pair.
- Commands a player refuses (it answers with an error while reporting success)
  now fail visibly in Home Assistant instead of appearing to work.
- A group leader's card shows speakers added to its group, including from the
  BluOS app; it could keep showing the old group until the next regroup.
  Grouping and each speaker's own volume are now followed through /SyncStatus,
  as the BluOS API recommends.
- The volume slider no longer jumps back to the old level for a moment after
  being moved.

### Development
- Test suite running against responses captured from real players, with a
  Docker test runner for machines without Home Assistant's Python.
- `dev/probe_api.py` captures a player's API responses for bug reports and
  test fixtures, with identifying details scrubbed; `dev/make_fixtures.py`
  turns them into fixtures.
- `dev/probe_grouping.py` records how players regroup, on real hardware. The
  grouping tests run against a model of the players checked step by step
  against those recordings.
- CI: ruff, pytest with coverage, and a Python 3.8 check for the probe script.
  Releases fail if the tag and `manifest.json` version disagree.
