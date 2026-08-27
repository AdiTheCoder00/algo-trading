# Running the price relay on an always-on VM

The phone widget shows GOLDM only while something is polling Kotak for it. There
is no free public MCX quote API to fall back on — `mcxindia.com` returns 404 to
non-browser clients, `investing.com` 403s, Groww's endpoint 500s, and Angel One
and 5paisa serve HTML pages only. That is why a relay exists at all, and why
"works when the PC is off" means moving the relay, not removing it.

XAUUSD is unaffected either way: it comes from a public spot API the phone can
reach on its own.

## What actually goes on the VM

**Only `ALGO_KOTAK_CONSUMER_KEY`.** Kotak's quote endpoint authenticates on the
consumer key alone — no TOTP, no MPIN, no trade session (see the module docstring
in `algo/data/kotak_feed.py`). So the VM gets a read-only market-data credential
and nothing else.

Your TOTP seeds, MPINs, and the Angel One credentials **stay on your PC**. They
are order-placement credentials; they never belong on a machine exposed to the
internet. Do not copy `.env` wholesale — copy the one line.

Worst case if the VM is fully compromised: someone can read gold quotes and, at
most, exhaust that key's rate limit. Rotate it in the Kotak portal and move on.

## Before you start

Kotak validates API requests against whitelisted IPs. Take the VM's public IP and
add it to the app's Primary/Secondary IP fields in the developer portal
(api.kotaksecurities.com/openapi), or every quote call is rejected. A cloud VM
with a reserved/static IP is required for this reason — an ephemeral IP will
break the moment it changes.

## Provider

Any always-on Linux VM works. Oracle Cloud's Always Free tier is the usual choice
because it is genuinely free indefinitely and includes a reserved public IP;
fly.io, Hetzner, and a €4 VPS are all fine too. The relay needs almost nothing —
it is one Python process making one HTTP call every couple of seconds.

## Install

On a fresh Ubuntu/Debian VM:

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <your repo> /opt/algo && cd /opt/algo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # or: pip install -e .
```

Write the single credential, readable only by the service user:

```bash
sudo install -m 600 /dev/null /etc/gold-relay.env
echo 'ALGO_KOTAK_CONSUMER_KEY=your-key-here' | sudo tee /etc/gold-relay.env >/dev/null
```

Then install and start the unit:

```bash
sudo cp deploy/gold-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gold-relay
systemctl status gold-relay
```

## Exposing it

The relay speaks plain HTTP. On a public VM that is not acceptable on its own —
anyone can read it, and more importantly the token travels in clear. Two options:

1. **Caddy in front** (simplest real answer). Point a domain at the VM and let
   Caddy get a certificate automatically:

   ```
   gold.example.com {
       reverse_proxy 127.0.0.1:8787
   }
   ```

   Then set the app's relay URL to `https://gold.example.com` and remove the
   cleartext exception. Keep the relay bound to `127.0.0.1` so only Caddy reaches it.

2. **Tailscale** — no domain, no certificate, nothing public. Install it on the VM
   and the phone, and use the VM's Tailscale IP as the relay URL. The relay is
   then reachable only from your own devices, which is the tighter option.

Either way, **set `--token`**. It is in the unit file already; change it from the
placeholder.

## Firewall

If you expose port 8787 directly (option neither of the above), open it in both
the cloud provider's security list *and* the VM firewall — Oracle images ship with
iptables rules that drop everything, and forgetting the second one is the usual
reason a fresh VM appears unreachable.

## Checking it works

```bash
curl -s -H 'Authorization: Bearer <token>' http://127.0.0.1:8787/prices.json | head -c 300
```

`goldm.price` non-null and `goldm.stale` false means the whitelist is right. If
`goldm.error` mentions authentication or the payload is a dict rather than a list,
the VM's IP is not whitelisted yet.

## Then, on the phone

Settings gear → set the relay URL to the VM's address (and token). The widget
picks it up on its next refresh; leaving the app also nudges it immediately.
