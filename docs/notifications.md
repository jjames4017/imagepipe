# Notifications

ImagePipe sends notifications to an **Apprise API** server. Apprise is the hub:
it forwards each message to whatever services you configure in it - ntfy,
email, Telegram, Discord and many more - so ImagePipe never needs to know which
services you use. Other scripts in your lab can send through the same Apprise
server.

```text
ImagePipe ──► Apprise API ──► ntfy ──► phone
                          └─► email (optional, and anything else)
```

## Setting it up

1. Run an Apprise API server (the `caronc/apprise` container).
2. In Apprise, create a **config key** whose configuration lists your services
   (for ntfy, an `ntfy://` or `ntfys://` URL for your topic).
3. Set `notify.apprise_url` and `notify.apprise_key` in ImagePipe's config.
4. Send a test:

```bash
imagepipe notify-test
```

## Events

| Event | Sent when | Apprise type |
|---|---|---|
| `finished` | A run completes | success |
| `error` | A run stops on an error, or is interrupted | failure / warning |

Later stages add `waiting` (the pipeline is paused at the digiKam stage and
needs you).

Choose which events notify with `notify.events`. A failed notification is logged
and never stops a run.

## What ImagePipe sends

`POST <apprise_url>/notify/<apprise_key>` with JSON `{title, body, type, tag}`.
