# VRChat Avatar Watch

A Discord bot for monitoring **your own VRChat uploads** from Railway. Target: around 10 avatars checked every 90 seconds, subject to API throttling and outages.

- Alerts when a previously accessible avatar becomes unavailable: **possible takedown**, not a confirmed ban.
- Alerts when a public avatar becomes private, enabled by default and configurable.
- One selected Discord person **or** role is mentioned for incident/monitor-health alerts.
- Recovery messages are posted without another ping. Returning from private to public is not a recovery incident; returning from unavailable is.
- Automatically discovers your uploads approximately every five minutes; accepts individual avatar URLs too.
- Saves observations, alert configuration, exclusions and queued alerts in SQLite on a persistent volume.
- Owner-only slash commands and a VRChat website device-approval login; no password collection.

## What has actually been verified

An unauthenticated call to VRChat's device-code creation endpoint returned HTTP 200 and a code/approval URL. Full account approval, authenticated avatar checks, Discord delivery and a Railway deployment **have not yet been live-tested**. Automated tests cover the local state transitions, failures and persistence. Actual takedown response semantics remain unverified.

Public avatar webpage previews continued to display after the supplied avatar was made private, so this bot checks the authenticated avatar-data endpoint instead. It does not scrape thumbnails or treat HTTP 200 on a webpage as proof the avatar is public.

VRChat's API is unofficially documented and may change. Its [Creator Guidelines](https://hello.vrchat.com/creator-guidelines#api-usage-bots) restrict credential/session handling and acting for other users. This is an owner-operated single-account tool, not a shared account-login service. Device approval creates a session with account access; it is **not** a documented read-only permission grant. Hosting on Railway is not claimed to be officially approved by VRChat. The code only issues authentication requests and reads account/avatar data; it never uploads, edits, selects or deletes an avatar.

## 1. Create the Discord bot

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications) and obtain its bot token privately.
2. Install it in your server with the `bot` and `applications.commands` scopes. Give it **View Channels**, **Send Messages** and **Embed Links** in the intended alert channel. Administrator permission and privileged intents are unnecessary.
3. Enable Discord Developer Mode and copy your server ID and your own Discord user ID. Only that configured user can run this bot's commands, even if another member is an administrator.
4. If pinging a role, make that role mentionable. The bot rejects @everyone as a target and suppresses all unrelated mentions.

## 2. Deploy this folder on Railway

Deploy the contents of **this folder** as a repository's root, or configure Railway's service root directory to point at this folder. Railway must see `Dockerfile`, `railway.json`, `requirements.txt` and `monitor/` at the service root. Use a private repository. This guide does not require publishing tokens or session files.

Configure a **persistent volume mounted at `/data`** before the first login. Run **one replica** and disable **Serverless/app sleeping**. This is a continuously running worker, not a cron job. No public domain or HTTP healthcheck path is required for Discord's outbound Gateway connection.

Set these Railway service variables:

| Variable | Value |
| --- | --- |
| `DISCORD_TOKEN` | Your Discord bot token (secret) |
| `DISCORD_GUILD_ID` | Your server ID |
| `DISCORD_OWNER_ID` | Your own Discord user ID |
| `VRCHAT_USER_AGENT` | `AvatarWatch/0.1 real-contact@example.com`, replacing the contact with yours |
| `DATA_DIR` | `/data` |
| `CHECK_INTERVAL_SECONDS` | `90` |
| `DISCOVERY_INTERVAL_SECONDS` | `300` |
| `VRCHAT_USER_ID` | Optional `usr_...` account ID to require during login |

The provided Dockerfile starts `python -m monitor`. Railway configuration requests restart-on-failure; it has a finite retry count, so inspect Railway if a service repeatedly crashes. Keep sufficient Railway usage allowance for continuous operation. Volume-backed redeployments can cause a brief monitoring gap.

## 3. Configure alerts and sign in

In your configured Discord server, using your configured owner account:

1. Run `/monitor setup`, choose a channel, and supply **either** `person` **or** `role`. Private alerts default to enabled.
2. Run `/monitor login`. The response is private/ephemeral and contains a temporary link to `https://vrchat.com/home/device` with an approval code.
3. Open that link yourself, sign into **your own VRChat account on VRChat's website**, and approve the device you just requested. Never approve an unsolicited device link. The server is the device being authorized.
4. The bot waits for approval, checks the authenticated user's ID, binds that account and saves the session. If `VRCHAT_USER_ID` is set, the approved account must match; otherwise the first successful login binds the account and later approvals must match it. Keep approval links private. No password or cookie should be pasted into Discord commands or this chat.
5. Check `/monitor health`. Automatic discovery adds your uploads, then staggered checks establish initial baselines. Alternatively run `/avatar watch` with your avatar URL to register it immediately.

The session is stored in `/data/session.json` with restrictive Linux filesystem permissions. It is a secret and remains accessible to trusted Railway project administrators and volume backups. SQLite does not contain the session. Protect access to your hosting project; do not publish/download/share this file. Logging out/revoking the session through VRChat invalidates the bot's access. It asks for a fresh approval when access expires; it does not keep trying your password.

Running `/monitor login` again deliberately pauses checks and replaces the old session. If approval expires, retry the command. A restart during approval requires a new command; pending device codes are not persisted. The bot does not offer a password or copied-cookie fallback. If the live approval flow fails, keep the error from `/monitor health` (not session data) for troubleshooting.

## Commands

| Command | Purpose |
| --- | --- |
| `/monitor setup` | Set channel, one mention target, and private-alert preference |
| `/monitor login` | Request your private VRChat approval link |
| `/monitor health` | Show authentication, checks, discovery and delivery status |
| `/monitor private-alerts enabled:false` | Disable public-to-private alerts |
| `/avatar watch url_or_id:...` | Add a URL/ID; remove any discovery exclusion |
| `/avatar unwatch url_or_id:...` | Stop tracking, cancel pending avatar alerts, and exclude it from automatic discovery |
| `/avatar list` | Show last-known state, observation time and any monitoring error |

Automatic discovery only adds avatars. It never treats absence from a list as a takedown. A new upload may take five minutes to be discovered plus its first check; an avatar that was removed before ever being observed cannot generate a newly-detected-takedown alert. A first-ever private observation establishes a baseline and does not ping.

## Alert behavior and limits

A change from public to private emits one private alert. A 404 with a working matching owner session, or an explicit `hidden` release status, becomes **unavailable — possible takedown** after the first observed change. Unknown status values, ownership mismatches, login/access failures, rate limits, malformed responses, timeouts and server errors do not change the last-known avatar state. A 404 could still be deletion or an unusual upstream issue. Exact moderation behavior needs live validation.

Repeated identical observations do not generate repeated incident alerts. Pending messages survive restarts and retry on Discord failures. A crash after Discord accepts a message but before the database records delivery can cause a duplicate; each message includes an event number. When a previously unavailable avatar becomes accessible again, a recovery message is queued without a ping. Incident messages report when the bot observed the change, not when VRChat actually removed the avatar.

Checks use jitter, spacing and shared exponential backoff. At 10 avatars and 90-second intervals there are approximately 6.7 direct avatar requests per minute, plus discovery/authentication and extra owner-session validation on 404s. That is arithmetic, not a guaranteed safe VRChat quota. Rate limits, large watchlists, login expiry, deployment gaps and outages extend the interval. Mentions cannot force a Discord push notification if the recipient has disabled them.

Before relying on alerts, approve the bot, verify your avatar appears with the expected owner and state, change a test avatar public → private → public, and confirm exactly one private alert. If you intentionally delete an expendable test upload later, you can check unavailable detection, but deletion or a moderation ban is not required just to set up the bot.

## Local development

Python 3.10+ is supported; the container uses Python 3.12. Install `requirements.txt`, set the environment variables above (use a local directory for `DATA_DIR`), then run:

```text
python -m monitor
```

`.env.example` documents variables; the program does not automatically read `.env` files. Never commit credentials or a `data` directory. Run the regression tests with:

```text
python -m unittest discover -s tests -v
```

References: [VRChat avatar endpoint](https://vrchat.community/reference/get-avatar), [device creation](https://vrchat.community/reference/create-device-code), [device approval polling](https://vrchat.community/reference/check-device-code), [own-avatar listing](https://vrchat.community/reference/search-avatars), [Railway volumes](https://docs.railway.com/volumes/reference), [Railway Serverless](https://docs.railway.com/deployments/serverless).
