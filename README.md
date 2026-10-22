# A Better Light — Mood Sync

A local macOS controller that makes Philips WiZ lights follow the mood of your screen or music. Built with Codex, using direct Wi-Fi commands instead of cloud round trips.

## Why I built it

I wanted more than manual color changes: a light that follows movie scenes, reacts to music, and fits into my daily routine. That experiment grew into **Luma**, a native Mac app.

## How I reverse-engineered it

Using community WiZ protocol references and live device responses, I explored the bulb’s local control flow over UDP port `38899`. I queried `getPilot` and `getSystemConfig`, then tested `setPilot` commands for power, brightness, RGB, white temperature, and scenes. Device errors helped refine the commands—for example, this bulb rejected an explicit `sceneId: 0` when setting colors.

For live sync, a native ScreenCaptureKit helper extracts screen colors or system-audio energy locally. The controller sends only the latest result, at up to 15 updates per second, without waiting for each acknowledgement. Actual visible latency depends on Wi-Fi and the bulb.

Protocol reference: [pywizlight](https://github.com/sbidy/pywizlight).

## Features

- Native Mac app with color, brightness, white temperature, and WiZ scenes.
- Saved moods, undo, timers, sleep fades, and daily routines.
- Movie screen-color sync and a separate audio-reactive music mode.
- Display selection, light discovery, multiple saved lights, and diagnostics.

## Run

Requires macOS 13+, Python 3, and Apple Command Line Tools. This build uses the Mac’s installed Python runtime.

```sh
python3 build_macos.py
open Luma.app
```

Add your bulb by IP or discovery. Allow Local Network access and, for live sync, Screen & System Audio Recording access for Luma. Keep the app running and Mac awake for routines. Closing the window keeps it running; quitting stops sync and restores the original light settings.

Tests: `python3 -m unittest -v`. No screen/audio recordings are saved or uploaded. Some protected video may appear black during capture.

## Demo

Video / GIF coming soon.

<!-- Add demo video or GIF here. -->
