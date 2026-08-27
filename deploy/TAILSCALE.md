# Tailscale as the transport for the price relay

The relay speaks plain HTTP and holds a market-data credential. Neither belongs
on the open internet. Tailscale puts it on a private WireGuard network instead:
the phone reaches the relay from anywhere, and nothing is exposed publicly — no
port forward, no domain, no certificate, no open listener for anyone to find.

This replaces the "expose port 8787" approach entirely. It is the tighter of the
two options in [README.md](README.md), and the reason is simple: with Caddy in
front you still have a public endpoint that must be secured correctly forever;
with Tailscale there is no public endpoint at all.

## What is already done on this PC

* Tailscale 1.102.3 installed (winget, installer hash verified)
* Service running, start type Automatic — it comes back after a reboot
* Machine registered with hostname `algo-pc`

## What only you can do

**Authenticating.** Tailscale login is an account credential; it is yours to
enter, not something to hand to a tool. The CLI prints a URL, you sign in, done:

```bash
tailscale up --hostname=algo-pc
```

**Installing on the phone.** Tailscale from the Play Store, signed into the same
account. Both devices then appear in the same tailnet and can address each other
by IP or MagicDNS name.

## Binding the relay

Once the phone is on the tailnet, the relay should listen on the Tailscale
interface rather than the LAN. Two reasonable positions:

* `--host 0.0.0.0` — reachable over both the LAN and Tailscale. Simplest, and on
  a home network behind NAT it is not much of an exposure. This is what
  `run_price_widget.bat` currently does.
* `--host <tailscale-ip>` — reachable *only* over Tailscale. Tighter: even a
  device that joins your WiFi cannot reach it.

Keep `--token` set either way. Tailscale already restricts who can connect, so
the token is defence in depth rather than the only lock — but it costs nothing.

## Pointing the phone at it

In the app: gear → Relay URL → `http://<tailscale-ip>:8787`, plus the token.

Use the **Tailscale IP** (100.x.y.z), not the LAN IP. The LAN address only works
at home; the Tailscale address works everywhere, which is the whole point. With
MagicDNS enabled you can use `http://algo-pc:8787` instead, which survives the
IP changing.

The widget reads the same setting, so it follows automatically — and leaving the
app broadcasts a refresh so the change takes effect immediately rather than at
the next 30-minute tick.

## What this does and does not solve

**Solves:** GOLDM on mobile data, on someone else's WiFi, anywhere. Previously
the relay was only reachable from your own LAN.

**Does not solve:** the PC being off. Tailscale is transport, not availability —
it makes the relay reachable, not running. For that the relay has to live on
something always on, which is what the VM in [README.md](README.md) is for.
Install Tailscale on that VM too and it joins the same tailnet, at which point
the VM never needs a public IP or an open port either.

That ordering is deliberate: get Tailscale working PC↔phone first, confirm the
app works over it, then move the same relay to the VM and change one URL.

## Cleartext note

The app's network security config permits cleartext to the relay because it is
plain HTTP. Over Tailscale that traffic is inside a WireGuard tunnel and
encrypted end to end regardless, so "cleartext" here describes the HTTP layer,
not what crosses the network. `gold-api.com` stays pinned HTTPS-only.
