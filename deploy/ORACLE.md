# Oracle Always Free VM for the price relay

This is the answer to "it doesn't work when my PC is off". Tailscale made the
relay *reachable* from anywhere; this makes it *running* all the time.

Everything after account creation is automated by
[oracle-cloud-init.yaml](oracle-cloud-init.yaml) — the VM configures itself on
first boot and joins your tailnet. You should not need to SSH in at all.

## What only you can do

**Creating the Oracle account.** Signing up and entering a card are yours, not
something to hand to a tool. Oracle asks for a card for identity verification
even on Always Free; it is not charged while you stay within free-tier limits,
but be aware the account can be rejected during their review — it happens, and
Fly.io or a €4/month Hetzner box are fine substitutes if it does. The same
cloud-init works on any Ubuntu VM.

**Generating a Tailscale auth key.** login.tailscale.com/admin/settings/keys →
Generate auth key. Single-use is fine, non-ephemeral so the node persists.

## Creating the instance

Compute → Instances → Create instance.

| Field | Value |
|---|---|
| Image | Canonical Ubuntu 24.04 (or 22.04) |
| Shape | `VM.Standard.A1.Flex` — Always Free covers 4 OCPU / 24 GB total |
| OCPU / memory | 1 OCPU, 6 GB is plenty; the relay is one Python process |
| Networking | Default VCN, **assign a public IPv4** |
| SSH key | Paste the public key below |

Public key (private half is in `state/oracle_relay_key`, gitignored):

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINZQsjmoLw0DrkJE/pGtogdsdul6qb3pFJXb2jiudHBM gold-relay@algo-pc
```

Then **Advanced options → Management → Initialization script**: paste the whole
of `oracle-cloud-init.yaml`, with the three `REPLACE_ME` values filled in.

If A1 capacity is unavailable in your region — very common, Oracle's free ARM
shapes are heavily contended — either retry over a few hours or use the
`VM.Standard.E2.1.Micro` AMD shape, which is also Always Free and adequate here.

## Kotak IP whitelist

**This is the step people miss.** Kotak validates API requests against
whitelisted IPs. Take the instance's public IP and add it to the app's
Primary/Secondary IP fields at api.kotaksecurities.com/openapi.

Without it every quote call is rejected and `goldm.error` will mention
authentication, while XAUUSD keeps working — a confusing half-failure if you are
not expecting it.

Use the **public** IP for the whitelist even though traffic to *you* goes over
Tailscale: the whitelist governs the VM's outbound calls to Kotak.

## Do not open port 8787

Leave Oracle's security list closed. The cloud-init accepts 8787 on `tailscale0`
only, so the relay is reachable from your devices and invisible to the internet.
There is no public endpoint to secure, which is the entire point of routing it
over Tailscale rather than putting Caddy in front.

## Checking it worked

The VM appears in your Tailscale admin console as `gold-relay` within a couple of
minutes. From the PC:

```bash
tailscale status | grep gold-relay
curl -s "http://gold-relay:8787/prices.json?k=<token>" | head -c 300
```

`goldm.price` non-null with `goldm.stale` false means the whitelist is right.

Bootstrap logs, if something went wrong:

```bash
tailscale ssh ubuntu@gold-relay
sudo cat /var/log/cloud-init-output.log
sudo systemctl status gold-relay
sudo journalctl -u gold-relay -n 50
```

## Switching the phone over

App → gear → Relay URL → `http://gold-relay:8787`, same token.

That is the only change. The widget reads the same setting, and leaving the app
broadcasts a refresh so it takes effect immediately.

At that point the PC is out of the path entirely: the relay runs on the VM, the
phone reaches it over Tailscale from anywhere, and GOLDM stays live whether your
PC is on, asleep, or unplugged.

## What is now where

| | PC | VM |
|---|---|---|
| Kotak consumer key (quotes) | yes | yes |
| Kotak TOTP seed / MPIN | yes | **no** |
| Angel One credentials | yes | **no** |
| Order placement possible | yes | **no** |

The VM holds one read-only market-data credential. If it were fully compromised
the worst outcome is that someone reads gold prices and burns that key's rate
limit — rotate it in the Kotak portal and redeploy.
