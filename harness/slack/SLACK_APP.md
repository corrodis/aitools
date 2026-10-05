# Slack app setup: scopes and events by use case

One Slack app (a "bot user") can serve several of our tools: the chat bot
(`mu2e-slack-bot`, or daqpy's `daqpy-slack` on the same adapter), the
read-only channel server (`mcp/slack`, `slack-mcp`) and, later, publishers
such as the daqpy watchdog. Each needs a different, small set of
**bot token scopes** (OAuth & Permissions) and, for anything that reacts
to messages, **event subscriptions** (Event Subscriptions → bot events).
Scopes are additive; after changing them, reinstall the app to the
workspace (the `xoxb-` token keeps working).

All our bots use **Socket Mode** (the host has no inbound path from Slack):
enable it and create an **app-level token** with `connections:write`
(`xapp-…`, `SLACK_APP_TOKEN`). That token is only for the event stream;
the API calls use the bot token (`SLACK_BOT_TOKEN`).

## Where to click

Everything below is configured at <https://api.slack.com/apps> → *Your
Apps* → the app (e.g. `mu2eshifterbot`). In the left sidebar:

- **Socket Mode** — enable; the app-level token (`xapp-…`) is created here
  or under *Basic Information → App-Level Tokens* (scope `connections:write`).
- **OAuth & Permissions → Scopes → Bot Token Scopes** — add scopes from the
  table. After adding any, the yellow banner at the top of the page asks you
  to **reinstall the app** to the workspace; do that. The Bot User OAuth
  Token (`xoxb-…`, `SLACK_BOT_TOKEN`) is on the same page and stays valid.
- **Event Subscriptions** — *Enable Events*, then *Subscribe to bot events*
  and add the events from the table (`app_mention`, `message.channels`,
  `message.groups`, `message.im`). With Socket Mode there is no Request URL
  to fill in. Save changes.
- **App Home → Show Tabs** — for DMs, tick *Messages Tab* and *Allow users to
  send Slash commands and messages from the messages tab*.
- In Slack itself: `/invite @<bot>` in every channel it should answer in or
  read from.

## By use case

| Use case | Bot token scopes | Bot events | Notes |
|---|---|---|---|
| **Answer @mentions** in channels the bot is invited to | `app_mentions:read`, `chat:write` | `app_mention` | Minimum for the chat bot. The bot must be invited (`/invite @bot`) to each channel. |
| **Thread follow-ups without a mention** (home channel) | + `channels:history` | + `message.channels` | Slack then delivers *every* message in subscribed public channels; the adapter drops what isn't addressed to it before logging anything. |
| **Private home channel** | + `groups:history`, `groups:read` | + `message.groups` | `groups:read` is also what lets the bot resolve a private channel's *name* (`conversations.info`); without it the id is used. |
| **Direct messages** with the bot | + `im:history` (and `im:read`, `im:write` to let the app open DMs itself) | + `message.im` | A top-level DM starts a thread = conversation; replies continue it. Enable "Allow users to send Slash commands and messages from the messages tab" under App Home. |
| **Status/progress feedback** (the per-tool status line edited in place, "working" reactions) | `chat:write` (edit own messages), `reactions:write` | — | Both best-effort; a missing scope only loses the feedback. |
| **Show author names** (resolve `<@U…>` to people) | `users:read` | — | Used by the chat bot's context and by `slack-mcp`'s message formatting. |
| **Read channel history** (`slack-mcp`: `slack_read_channel`, `slack_read_thread`) | `channels:history`, `channels:read`, `users:read`; private channels: `groups:history`, `groups:read` | — (polling API, no events) | The bot must be a **member** of each allowlisted channel — `conversations.list` shows public channels it is not in, but `conversations.history` then fails with `not_in_channel`. |
| **Post reports** (e.g. watchdog → channel) | `chat:write` | — | Membership required. `chat:write.customize` only if posting under a different name/icon. |
| **Attach/read files** | `files:read` / `files:write` | — | Not used by anything yet. |
| **Workspace search** | — | — | Needs a *user* token (`search:read`); deliberately not supported anywhere here. |

## Our deployments

**`mu2eshifterbot`** (DAQ bot, daqpy) — observed scopes on 2026-10-05:
`app_mentions:read, chat:write, channels:history, channels:read,
groups:history, users:read, reactions:write` (+ `assistant:write`,
`links:write`, `emoji:read`, `incoming-webhook`, `metadata.message:read`,
which nothing here uses). To cover every row above it still needs:

- `groups:read` — private channel names (its home channel is private);
- `im:history`, `im:read`, `im:write` + event `message.im` — direct messages;
- an invite to `#mu2e-shift` — for `slack-mcp` to read it.

Its token serves both `daqpy-slack` and `slack-mcp` on the DAQ cluster.
That is convenient but means every channel the reader is invited to is
also a channel where the bot can be @mentioned. If reading should not
imply presence, give `slack-mcp` its own app with only the read scopes
(`channels:history, channels:read, users:read` + `groups:*` as needed) and
no `chat:write`; nothing in `slack-mcp` posts, so the smaller app is the
honest one.

**`mu2e-ai`** (collaboration bot, `mu2e-slack-bot` on the registry) —
see the README's "Slack app setup": mention + home-channel follow-ups +
reactions; add the DM row if desired.

## Checking what a token has

Any failing API call reports the missing scope, and `daqpy-slack --check`
/ `slack-mcp --check` surface it:

```
missing_scope (needs scope groups:read) — provided: chat:write,app_mentions:read,…
not_in_channel — invite the bot to the channel
```

`auth.test` (what both `--check`s call first) needs no particular scope and
tells you which bot user and workspace a token belongs to.
