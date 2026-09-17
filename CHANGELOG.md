# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/). Releases up to 1.1.0 are described
on the [GitHub releases page](https://github.com/aunefyren/bluesound_alt/releases).

## [Unreleased]

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

### Development
- Test suite running against responses captured from real players, with a
  Docker test runner for machines without Home Assistant's Python.
- `dev/probe_api.py` captures a player's API responses for bug reports and
  test fixtures, with identifying details scrubbed; `dev/make_fixtures.py`
  turns them into fixtures.
- CI: ruff, pytest with coverage, and a Python 3.8 check for the probe script.
  Releases fail if the tag and `manifest.json` version disagree.
