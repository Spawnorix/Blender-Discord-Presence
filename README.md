# Blender Discord Presence

A Blender add-on that shows what you're working on as your Discord status: the file name, the current mode or workspace, and live render progress.

It talks to the Discord desktop app directly over its local IPC connection (named pipes on Windows, Unix sockets on macOS/Linux), with no third-party libraries.

## Features

- Shows the open file, editing mode, workspace, render engine and time elapsed
- Live render progress (`Rendering frame 12 / 250`)
- Runs on a background thread, so Blender never freezes
- Reconnects automatically if Discord is closed or restarted
- Stays under Discord's update rate limit
- Options to hide the file name, show unsaved changes, show the object count and set custom images

## Installation

1. Download `discord_presence.py`.
2. In Blender, go to **Edit → Preferences → Add-ons → Install** and choose the file.
3. Enable **Discord Rich Presence**.
4. Enter your Discord application's **Client ID** in the add-on's preferences.

Requires Blender 2.80 or later and the Discord desktop app.

## Built with

Python · Blender Python API (`bpy`) · sockets · threading · `struct`
