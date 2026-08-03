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
2. Create your local environment file and fill in the Discord token and the
   password used by your Lavalink server:

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

## Railway

This repository is ready to deploy as a Railway worker with the included
[`railway.json`](railway.json) and `Dockerfile`. The bot service still needs a
Discord token, MongoDB, and a Lavalink 4 service; `railway.json` configures the
bot service itself and does not create those external services.

1. Create an **Empty Project** in Railway first; do not deploy the bot service
   until its dependencies and variables below are ready.
2. Add [Railway's MongoDB database](https://docs.railway.com/databases/mongodb)
   to the same project.
3. Add an empty service named `Lavalink`, enter the variables below, then
   connect this same GitHub repository as its source. Set its **Root Directory**
   to `/lavalink` and its **Railway Config File** to
   `/lavalink/railway.json` before the first deployment:

   ```dotenv
   PORT=2333
   LAVALINK_SERVER_PASSWORD=replace-with-a-strong-random-password
   ```

   The included [`lavalink/application.yml`](lavalink/application.yml) pins
   [Lavalink 4.2.2](https://github.com/lavalink-devs/Lavalink/releases/tag/4.2.2),
   the official [YouTube source plugin 1.18.2](https://github.com/lavalink-devs/youtube-source/releases/tag/1.18.2),
   and [LavaSrc 4.8.3](https://github.com/topi314/LavaSrc/releases/tag/4.8.3).
   The initial YouTube and built-in-source configuration does not require
   provider credentials. However, YouTube can block datacenter IPs or require
   bot verification, so deployed playback must still be tested and may require
   the plugin's [OAuth or poToken setup](https://github.com/lavalink-devs/youtube-source/blob/1.18.2/README.md).
   LavaSrc providers are disabled by default; enable the matching flag **and**
   supply all required credentials. For example, enable Spotify search on the
   Lavalink service with:

   ```dotenv
   LAVASRC_SPOTIFY_ENABLED=true
   SPOTIFY_CLIENT_ID=replace-with-spotify-client-id
   SPOTIFY_CLIENT_SECRET=replace-with-spotify-client-secret
   ```

   Apple Music, Deezer, Yandex Music, VK Music, Tidal, Qobuz, and JioSaavn have
   matching `LAVASRC_*_ENABLED` flags and secret placeholders documented in
   `lavalink/application.yml`. Keep provider secrets on the Lavalink service,
   not the bot service. Wait until the Lavalink `/metrics` health check and
   MongoDB deployment both pass.

4. Add an empty bot service, open its **Variables** tab, switch to the Raw
   Editor, and add the following values before connecting the GitHub repository
   as its source:

   ```dotenv
   DISCORD_TOKEN=replace-with-your-discord-bot-token
   MONGODB_URL=${{MongoDB.MONGO_URL}}
   MONGODB_NAME=vocard
   LAVALINK_HOST=${{Lavalink.RAILWAY_PRIVATE_DOMAIN}}
   LAVALINK_PORT=${{Lavalink.PORT}}
   LAVALINK_PASSWORD=${{Lavalink.LAVALINK_SERVER_PASSWORD}}
   LAVALINK_SECURE=false
   LAVALINK_IDENTIFIER=DEFAULT
   BOT_PREFIX=null
   LOGGING_JSON={"file":{"enable":false}}
   ```

   `MongoDB` and `Lavalink` are [reference-variable service names](https://docs.railway.com/variables#referencing-another-services-variable)
   and are case-sensitive. Change
   those two reference namespaces if your services have different names. The
   Lavalink password must exactly match the password configured on that
   service. Never use `localhost` to connect separate Railway services. Do not
   set `PORT` on the bot service; Railway injects it for the health endpoint.

5. Deploy the bot service. GitHub-connected services deploy automatically, or
   from a linked Railway CLI project run:

   ```bash
   railway up --service <bot-service-name> -m "Deploy Vocard"
   ```

6. Confirm the deployment reaches `SUCCESS`, then check the logs for
   `MongoDB databases initialized`, `Node [DEFAULT] is connected!`, and
   `Logging As`. Finally, run `/help` and play a track in a Discord voice
   channel; a successful health check alone cannot test Discord audio.

`BOT_PREFIX=null` enables slash-command-only mode, so Discord's privileged
Message Content intent is not required. If you set a message prefix instead,
enable **Message Content Intent** in the Discord Developer Portal. Enabling the
optional dashboard IPC also requires **Server Members Intent**.

The `/health` endpoint returns success only after Discord, a live MongoDB ping,
and at least one Lavalink node are ready. Railway uses it during deployment; the
bot does not need a public domain. Startup has a 540-second global deadline below
Railway's 600-second health-check timeout. Dependency connections use bounded
retries, failed required cogs stop startup, and Railway restarts failed processes
automatically.
Keep `CHECK_FOR_UPDATES_ON_STARTUP=false` on immutable deployments and deploy
new code through Git/Railway instead of running `update.py` inside the service.

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
