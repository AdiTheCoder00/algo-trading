"""``algo telegram-check`` - find the chat id and prove alerts can be delivered.

Setting alerting up has one genuinely awkward step: the chat id. It is not
shown anywhere in the Telegram UI, and the usual instructions send people to
paste their **bot token into a browser URL bar** - which puts a live credential
into history, and into any screenshot of it. This does the same lookup from the
token already in `.env`, so the secret stays where it belongs.

Nothing here writes the token anywhere, echoes it, or lets it reach a log line:
every failure message goes through `alerts.redact` for the reason D-128 records,
and the summary prints a length rather than a value.
"""

from __future__ import annotations

import typer

app = typer.Typer()

#: Telegram will not message someone who has never opened a chat with the bot,
#: and that is by far the most common reason this command finds nothing.
_NO_UPDATES_HELP = """
No chats found. Telegram only reports a chat once the *user* has spoken first,
so:

  1. Open your bot in Telegram (BotFather gives you a t.me/<name> link).
  2. Press Start, or send it any message.
  3. Run this again.

Note also that getUpdates only returns recent history - if you messaged the bot
days ago and something already consumed the updates, send it another message.
""".strip()


@app.command("telegram-check")
def telegram_check(
    send: bool = typer.Option(
        True,
        "--send/--no-send",
        help="Also send a test alert to the configured chat",
    ),
    chat_id: str = typer.Option(
        "",
        "--chat-id",
        help="Send to this chat instead of ALGO_TELEGRAM_CHAT_ID",
    ),
) -> None:
    """Report the Telegram chat id, and prove an alert can be delivered.

    Reads `ALGO_TELEGRAM_BOT_TOKEN` from the environment (`.env` is loaded by
    the CLI). The token is never printed - only its length, which is enough to
    tell "set" from "empty" without putting a credential on screen or into a
    terminal scrollback.
    """
    import requests

    from algo.core.tls import trust_the_os_certificate_store
    from algo.live.alerts import (
        Alert,
        Severity,
        TelegramCredentials,
        TelegramNotifier,
        redact,
        telegram_credentials_from_env,
    )

    # Same reason the transports do it (D-113): a TLS-scanning antivirus
    # swaps the certificate in transit, and the OS trusts that root while
    # Python's bundled list does not.
    trust_the_os_certificate_store()

    credentials = telegram_credentials_from_env()
    if not credentials.token:
        typer.echo("ALGO_TELEGRAM_BOT_TOKEN is not set.")
        typer.echo("")
        typer.echo("Add it to .env (gitignored), then run this again:")
        typer.echo("  ALGO_TELEGRAM_BOT_TOKEN=<token from @BotFather>")
        raise typer.Exit(code=1)

    typer.echo(f"token         set ({len(credentials.token)} chars)")
    typer.echo(
        f"chat id       {credentials.chat_id or '(not set)'}"
        + ("" if credentials.chat_id else "  <- this is what we are looking for")
    )
    typer.echo("")

    # ---- who has talked to this bot?
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{credentials.token}/getUpdates",
            timeout=15,
        )
        payload = response.json()
    except Exception as exc:  # report it, but never leak the URL it carries
        typer.echo(f"getUpdates failed: {redact(str(exc))}")
        raise typer.Exit(code=1) from exc

    if not payload.get("ok"):
        # A bad token answers here, and its description is safe to show.
        typer.echo(f"Telegram rejected the token: {payload.get('description', payload)}")
        typer.echo("If it was revoked, @BotFather issues a new one with /token.")
        raise typer.Exit(code=1)

    found: dict[str, str] = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is None:
            continue
        kind = chat.get("type", "?")
        who = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        found[str(chat["id"])] = f"{kind}{f' - {who}' if who else ''}"

    if found:
        typer.echo(f"chats that have messaged this bot ({len(found)}):")
        for cid, description in found.items():
            marker = "  <- currently configured" if cid == credentials.chat_id else ""
            typer.echo(f"  {cid:<16} {description}{marker}")
        if not credentials.chat_id:
            first = next(iter(found))
            typer.echo("")
            typer.echo("Add this to .env:")
            typer.echo(f"  ALGO_TELEGRAM_CHAT_ID={first}")
    else:
        typer.echo(_NO_UPDATES_HELP)

    # ---- can we actually deliver?
    target = chat_id or credentials.chat_id
    if not send:
        return
    if not target:
        typer.echo("")
        typer.echo("No chat id to send to yet - set one and run again.")
        raise typer.Exit(code=1)

    notifier = TelegramNotifier(
        TelegramCredentials(token=credentials.token, chat_id=target)
    )
    delivered = notifier.deliver(
        Alert(
            Severity.INFO,
            "alerting configured",
            "If you can read this, the trading engine can reach you.",
        )
    )
    typer.echo("")
    if delivered:
        typer.echo(f"test alert    delivered to {target}")
    else:
        # `deliver` already logged the redacted reason; the usual cause is that
        # the user has never opened a chat with this particular bot.
        typer.echo(f"test alert    NOT delivered to {target}")
        typer.echo("")
        typer.echo(
            "The most common cause is that this chat has never messaged this "
            "bot - Telegram will not let a bot open a conversation. Press Start "
            "in the bot's chat and try again."
        )
        raise typer.Exit(code=1)
