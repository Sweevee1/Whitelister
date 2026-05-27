# Twitch OAuth Setup — How It Was Made to Work

This documents exactly what was done to get Twitch OAuth connecting successfully, including the non-obvious discoveries. Reference this for any future Twitch integration work.

## What's registered in the Twitch developer console

At [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps), the app has these OAuth Redirect URLs registered:

- `http://localhost:8765/twitch/auth` — used for local dev testing
- `https://<Unraid-IP>:8765/twitch/callback` — used for production

The production URI must match the exact scheme, host, port, and path that the app sends to Twitch. Since the container serves HTTPS on port 8765, the URI must use `https://`.

## The `/twitch/auth` path quirk

The original locally-registered URI was `/twitch/auth` (wrong path — should have been `/twitch/callback`). Rather than forcing a console change, `GET /twitch/auth` was added to the app to transparently redirect to `/twitch/callback` preserving all query params:

```python
@app.route("/twitch/auth", methods=["GET", "POST"])
def twitch_auth():
    if request.method == "GET":
        return redirect(url_for("twitch_callback", **request.args))
    # ... existing POST logic
```

This means both `/twitch/auth` and `/twitch/callback` work as redirect targets. When Twitch sends `?code=...` back, either path lands correctly at the callback handler.

## How to discover the registered redirect URI

If you get `redirect_mismatch`, Twitch redirects the error to the **registered** URI (not the one you sent). Watch where the browser ends up — that URL (minus the `?error=...` params) is what's registered. Then either match it in your request or register the correct one in the console.

## What the dashboard sends to Twitch

`POST /twitch/auth` → `GET /twitch/callback` flow:

1. Dashboard POSTs `{client_id, client_secret, channel_name, redirect_uri}` to `/twitch/auth`
2. App saves credentials to `config.yaml` and returns `{"auth_url": "https://id.twitch.tv/oauth2/authorize?..."}`
3. Dashboard opens a popup to that URL
4. Twitch shows the login/authorise screen
5. After authorisation, Twitch redirects to the `redirect_uri` with `?code=...`
6. `/twitch/callback` calls `exchange_code()` to swap the code for tokens, then `get_broadcaster_id()`, saves both to `config.yaml`, and starts the EventSub WebSocket client

**Leave the Redirect URI field blank in the dashboard.** The app auto-detects it as `request.url_root + "/twitch/callback"`. Since production runs HTTPS, this correctly produces `https://<IP>:8765/twitch/callback`.

## Confirming a successful connection

In the container logs you should see, in order:

```
GET /twitch/auth?code=<...> → 302   (redirect to callback)
GET /twitch/callback?code=<...> → 200
Twitch EventSub client started
Websocket connected
Subscribed to channel points redemptions (broadcaster <ID>)
```

The browser popup shows "Authorised — closing…" and closes itself. The dashboard Twitch status updates to connected.

## What breaks and why

| Symptom | Cause | Fix |
|---------|-------|-----|
| `redirect_mismatch` | URI sent to Twitch doesn't match any registered URI | Check where Twitch redirects the error — that's the registered URI. Register the correct one or match it in your request. |
| `405 Method Not Allowed` on `/twitch/auth` | Twitch redirected to `/twitch/auth` but GET wasn't handled | Already fixed — GET now redirects to `/twitch/callback` |
| "Authorised" but no EventSub connection | `exchange_code` or `get_broadcaster_id` failed silently | Check logs for the callback request; look for exceptions in the token exchange |
| Works locally but not in production | `http://` vs `https://` mismatch in redirect URI | Production uses HTTPS — register `https://` URI in Twitch console |

## Local dev testing

For local testing, register `http://localhost:8765/twitch/auth` in the Twitch console (HTTP is allowed for localhost). Start the Flask dev server and POST credentials with `redirect_uri: http://localhost:8765/twitch/auth`. Twitch will show the login screen and redirect back to localhost.
