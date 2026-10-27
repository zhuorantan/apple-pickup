# Two warm Proton VPN connections

`docker compose up -d --build` runs two Gluetun VPN containers and the pickup monitor on a private Docker bridge network. It requires `/dev/net/tun` on the Docker host and a Proton account allowing two simultaneous OpenVPN connections. No host ports or Docker socket are exposed.

Keep Apple and Telegram settings in `.env`. Set `GLUETUN_API_KEY` to a randomly generated secret. In `.vpn.env`, set Proton's **OpenVPN-specific** credentials and the same API key:

```dotenv
OPENVPN_USER=your-openvpn-username
OPENVPN_PASSWORD=your-openvpn-password
HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE={"auth":"apikey","apikey":"same-key-as-GLUETUN_API_KEY"}
```

Keep both files private (`chmod 600 .env .vpn.env`); Git and the Docker build context exclude them. Remove the old `GLUETUN_CONTROL_URL` setting when migrating. Compose supplies `GLUETUN_ENDPOINTS`, a JSON array containing exactly two distinct `name`, `control_url`, and `proxy_url` entries. A legacy single-VPN setting without endpoints fails explicitly.

Apple pickup requests, public-IP probes, and Apple hostname readiness checks use the selected VPN's HTTP CONNECT proxy. DNS for those destinations resolves inside Gluetun, using Cloudflare DNS over HTTPS. Each proxy is protected by Gluetun's VPN firewall. Control API requests use the private Docker network and an API key; that key is never attached to external requests. Telegram uses the bot's ordinary Internet connection, so VPN reconnects do not interrupt notifications. The proxies have no published ports and should only share a Docker network with trusted containers.

At startup, both VPN routes are prepared concurrently. The bot can start using the first ready route while the other prepares. Readiness requires a live public-IP check through the proxy, an Apple hostname TLS connection, an IP distinct from the other ready route, and no recent HTTP 541 block. Readiness does not guarantee that Apple accepts a pickup request from that IP.

Normal polling remains serial, waiting 10 seconds with 20% jitter (8–12 seconds) after a successful check. Every successful check logs its route, request duration, and model/store counts on one line. Unavailable models are summarized as a count; only available models list pickup locations. Models with pending availability and no confirmed pickup location are counted separately. Notifications remain change-only.

On HTTP 541 or a transport failure, the bot marks the route unavailable and immediately selects the ready standby. The failed route reconnects in an owned background task: stop/start VPN, restart DNS to discard stale upstream connections, then verify readiness. HTTP 541 IPs are avoided for 30 minutes. Failed repairs retry after 15 seconds, doubling to a maximum of 60 seconds. Duplicate or recently blocked IPs are rejected. Background tasks are cancelled and awaited when the monitor shuts down.

If both routes are unavailable, polling waits for a verified route and retains the last confirmed stock results; it never falls back to direct Apple requests. A request can still take up to its 10-second timeout before switching, and standby routes can themselves fail or receive HTTP 541. Two connections remove the need to wait for a reconnect when the other route works; they cannot guarantee uninterrupted checks.

HTTP 403 retains the existing 5/10/20/30-minute cooldown. HTTP 429 honors `Retry-After` without switching routes. Other request failures retain exponential backoff starting at 15 seconds. With no VPN environment settings, direct mode remains available.

The bot disables aiohttp's local DNS cache in VPN mode to avoid shielded resolver tasks surviving a timed-out request. Gluetun handles destination DNS caching. Fresh connections also avoid reusing sockets from a prior tunnel.

Run regression tests with `python -m unittest discover -s tests`. They cover standby switching, both-route failure, background repairs, IP rejection, cancellation, proxy/control separation, Retry-After, and single-line result logging.
