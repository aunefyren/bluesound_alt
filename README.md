# Alternative Bluesound Integration for Home Assistant
![GitHub Release](https://img.shields.io/github/v/release/aunefyren/bluesound_alt?style=for-the-badge)
![GitHub issues](https://img.shields.io/github/issues/aunefyren/bluesound_alt?style=for-the-badge)
![GitHub Repo stars](https://img.shields.io/github/stars/aunefyren/bluesound_alt?style=for-the-badge)
![GitHub forks](https://img.shields.io/github/forks/aunefyren/bluesound_alt?style=for-the-badge)

> [!NOTE]
> Parts of this integration were co-written with Claude (Anthropic AI). The code has been reviewed and tested against real Bluesound hardware.

This project is an alternative integration for Bluesound/BluOS speakers, rewritten from scratch using modern Home Assistant patterns.

<br>
<br>

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=aunefyren&repository=bluesound_alt)  
Until it is in the HACS default list, add it as a custom repository first.

<br>
<br>

> [!IMPORTANT]
> This is a full rewrite, not a fork of the official Bluesound integration. It uses the modern HA integration architecture: UI-based setup (config flow), a coordinator per device, and native HA grouping. If you were using the previous version of this integration, you will need to re-add your devices through the UI.

<br>
<br>

## Why not the core Bluesound integration?
Home Assistant ships its own `bluesound` integration. Both run locally and follow the players by long-polling. Compared with core as of Home Assistant 2026.10, this integration:

* **Groups without breaking groups.** Joining from any speaker in a group adds to that group, a speaker leading another group is taken out of it first instead of nesting one group inside another, stereo pairs are refused instead of split, and a join the player refuses shows as an error. Ungrouping works even when the group's leader is not set up in Home Assistant.
* **Keeps grouped speakers useful.** A speaker in a group shows what the group is playing and keeps its playback controls; core shows it as idle.
* **Browses the player's own menus**: inputs and services such as Apple Music, TuneIn, Radio Paradise and BluOS Radio, next to Home Assistant's media sources. Core browses only the latter.
* **Exposes the player's settings** as entities (listening mode, tone controls, bass, treble, balance, subwoofer, volume limits, Bluetooth mode, display and indicator brightness, and more), plus a restart button and diagnostics.
* **Covers more sources and controls**: inputs of receivers with a BluOS module such as the NAD T758, repeat mode, and hiding sources you don't use.
* **Catches up faster**: a player that drops off is retried after 10 seconds (core: 3 minutes), and inputs and presets changed in the BluOS app show up straight away (core: every 15 minutes).

Core has things this doesn't: several players on one device at different ports, and translations beyond English and Norwegian. If you need those, use core.

<br>
<br>

## Main features
* UI-based setup, no `configuration.yaml` editing required, with players on the network discovered automatically
* Correct group join/unjoin with immediate state updates
* Per-speaker volume control within groups (individual volume, not just group volume)
* Group state works with groups of any size
* Source selection for the inputs connected to the player (analogue, optical, HDMI, Bluetooth, and the inputs of receivers such as the NAD T758), with unused inputs hideable per player
* Spotify, presets and streaming services in the media browser, and playable from automations
* Inputs linked to the media player that feeds them, such as a streamer on a cable: its track, artwork and controls show on the Bluesound player while the input plays
* Media browsing of the player's inputs and service menus, such as Apple Music, TuneIn, Radio Paradise and BluOS Radio
* Shuffle, repeat, seeking (where the track allows it), clearing the queue, and volume up and down in small steps
* Sleep timer (off, 15, 30, 45, 60 or 90 minutes), with the minutes left; in a group it is the group's
* Works with [Mini Media Player](https://github.com/kalkih/mini-media-player) grouping
* Playback, grouping and volume follow the player by long-polling, so changes show up instantly
* Diagnostics, with addresses, names and account details redacted
* Player settings as in the BluOS app: listening mode, tone, balance, subwoofer, output mode, volume limits, Bluetooth mode, indicator and display brightness
* Restart button for every player, and a link to the player's web page on its device page
* English and Norwegian (Bokmål)

<br>
<br>

## Installation instructions

1. Until it is in the HACS default list, add this repo to HACS as a custom repository

![Alt text](.github/assets/add-custom-repo-example.png)

<br>

2. Install `Bluesound Alt` in HACS
3. Restart Home Assistant
4. Your players show up under **Settings → Devices & services → Discovered**. Click **Add** on each one
5. A player that is not discovered, on another subnet or VLAN say, can be added by hand: **Add Integration**, search for `Bluesound Alt`, and enter its IP address and port (default: 11000)

<br>
<br>

### Example: play a preset from an automation

Presets are under **Presets** in the player's media browser. To play one from an automation or script, pick it with the media picker of the **Play media** action, which fills in its id, `bluesound:preset/<id>`:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.c700_amplifier
data:
  media_content_type: music
  media_content_id: bluesound:preset/1
```

<br>

### Linking an input to another media player

An input often carries another player: a streamer or computer on a cable, a TV on HDMI. When that player is in Home Assistant too, the input can be linked to it, under **Configure** on the player's integration entry, **Link an input to a media player**.

While the linked input plays and the linked media player has something on (playing, paused or buffering), the Bluesound player shows that player's track, artist, album, artwork, position and state, and passes it play, pause, stop, skip and seek. Volume, mute, inputs and grouping stay with the Bluesound player. Speakers grouped with it show the same. Switch to another input, or stop the linked player, and the Bluesound player shows itself again.

If the linked media player has a library to browse, as Plex's players do, the input opens it in the Bluesound player's media browser. Playing an album or playlist from there switches the Bluesound player to the input and has the linked player play it, in one tap; the media picker of an automation's **Play media** action works the same way. The input's own play button still just switches to it.

The link is optional, per input, and follows the input when it is renamed in the BluOS app. Other Bluesound Alt players cannot be linked, so two players never pass commands back and forth.

#### Alternative: Home Assistant's Universal Media Player

Without the link, Home Assistant's [Universal Media Player](https://www.home-assistant.io/integrations/universal/) does much the same in YAML, as a separate, combined media player. Here a NAD C700's `PlexAmp` input is fed by a Plex client:

```yaml
media_player:
  - platform: universal
    name: C700 Living Room
    unique_id: c700_living_room_universal
    device_class: receiver
    children:
      - media_player.plex_client
      - media_player.c700_amplifier
    # The Plex client leads while the C700 is on its PlexAmp input and the
    # client has something on; otherwise the C700 itself.
    active_child_template: "{{ 'media_player.plex_client' if is_state_attr('media_player.c700_amplifier', 'source', 'PlexAmp') and states('media_player.plex_client') in ['playing', 'paused', 'buffering'] else 'media_player.c700_amplifier' }}"
    # Volume and inputs always belong to the amplifier.
    attributes:
      volume_level: media_player.c700_amplifier|volume_level
      is_volume_muted: media_player.c700_amplifier|is_volume_muted
      source: media_player.c700_amplifier|source
      source_list: media_player.c700_amplifier|source_list
    commands:
      volume_set:
        action: media_player.volume_set
        target:
          entity_id: media_player.c700_amplifier
        data:
          volume_level: "{{ volume_level }}"
      volume_up:
        action: media_player.volume_up
        target:
          entity_id: media_player.c700_amplifier
      volume_down:
        action: media_player.volume_down
        target:
          entity_id: media_player.c700_amplifier
      volume_mute:
        action: media_player.volume_mute
        target:
          entity_id: media_player.c700_amplifier
        data:
          is_volume_muted: "{{ is_volume_muted }}"
      select_source:
        action: media_player.select_source
        target:
          entity_id: media_player.c700_amplifier
        data:
          source: "{{ source }}"
    browse_media_entity: media_player.c700_amplifier
```

The differences: the universal player is a third entity next to the two it combines; grouping stays on the Bluesound entity; speakers grouped with the C700 do not show the client's track; and the template names the input, so renaming it in the BluOS app means editing the YAML. The link avoids all of these. The universal player, in turn, can combine media players from any integrations, Bluesound or not.

<br>

### Example: Mini Media Player YAML

This integration works with [Mini Media Player](https://github.com/kalkih/mini-media-player) for speaker grouping. Use `platform: media_player` in the `speaker_group` config, this is the correct setting for the modern HA grouping API this integration uses.

```yaml
type: custom:mini-media-player
entity: media_player.c700_amplifier
artwork: material
hide:
  power: true
  icon: true
  source: false
speaker_group:
  platform: media_player
  supports_master: true
  show_group_count: true
  entities:
    - entity_id: media_player.c700_amplifier
      name: Living room stereo
    - entity_id: media_player.pulse_soundbar
      name: Living room soundbar
    - entity_id: media_player.pulse_flex_bathroom
      name: Bathroom speaker
    - entity_id: media_player.pulse_flex_office
      name: Office speaker
```

> [!NOTE]
> Previous versions of this integration used `platform: bluesound` in MMP. This no longer works correctly. Change it to `platform: media_player` with `supports_master: true`.

<br>

Result:

![Alt text](.github/assets/mini-media-player-example.png)

<br>
<br>

## Ideas for further development

* Alarm support
