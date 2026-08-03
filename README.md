<a href="https://discord.gg/wRCgB7vBQv">
    <img src="https://img.shields.io/discord/811542332678996008?color=7289DA&label=Support&logo=discord&style=for-the-badge" alt="Discord">
</a>

# Vocard Bot
Vocard is a highly customizable Discord music bot, designed to deliver a user-friendly experience. It offers support for a wide range of streaming platforms including Youtube, Soundcloud, Spotify, Twitch, and more.

## Features
* Fast song loading
* Works with slash and message commands
* Lightweight design
* Smooth playback
* Clean and nice interface
* Supports many music platforms (YouTube, SoundCloud, Spotify, Apple Music etc.)
* Built-in playlist support
* Fully customizable settings
* Lyrics support
* Various sound effects
* Multiple languages available
* Easy to update
* Supports docker
* [One Click Installer](https://github.com/ChocoMeow/Vocard-Installer)
* [Premium dashboard](https://github.com/ChocoMeow/Vocard-Dashboard)

## Screenshot
![features](https://github.com/user-attachments/assets/2a1baf75-d1c8-41d1-a66f-7011e96d5feb)

## Requirements
* [Python 3.11+](https://www.python.org/downloads/)
* [Lavalink Server (Requires 4.0.0+)](https://github.com/freyacodes/Lavalink)

## Setup

1. Start MongoDB and a Lavalink 4 server.
2. Create your local environment file and fill in at least the Discord token:

   ```bash
   cp .env.example .env
   ```

3. Install the dependencies and run the bot:

   ```bash
   python3 -m pip install -r requirements.txt
   python3 main.py
   ```

You no longer need to create `settings.json`. Runtime environment variables (including
values loaded from `.env`) take precedence over an optional `settings.json`,
which in turn overrides [`settings.default.json`](settings.default.json). Use the
optional JSON file only for advanced nested customization; it may contain just
the fields you want to override. You can point to another file with
`SETTINGS_FILE=/path/to/settings.json`.

The legacy `TOKEN` and `CLIENT_ID` environment names are still accepted, though
`DISCORD_TOKEN` and `DISCORD_CLIENT_ID` are preferred. See [`.env.example`](.env.example)
for every common setting and the advanced JSON environment variables.

For Docker, pass secrets at runtime instead of copying `.env` into the image:

```bash
docker build -t vocard .
docker run --env-file .env vocard
```

To use an optional settings file in Docker, mount it at runtime and set its
container path, for example `-v ./settings.json:/run/vocard-settings.json:ro`
and `-e SETTINGS_FILE=/run/vocard-settings.json`.

When MongoDB or Lavalink runs in another container, set its container/service
name as the corresponding host and attach the containers to the same Docker
network.

The full upstream guide is also available on the [Setup Page](https://docs.vocard.xyz/latest/bot/setup).

## Need Help?
Join the [Vocard Support Discord](https://discord.gg/wRCgB7vBQv) for help or questions.
