"""``algo credentials`` — inspect broker API keys configuration."""

from __future__ import annotations

import typer

app = typer.Typer()


@app.command("credentials")
def credentials() -> None:
    """Report which broker credentials are loaded - without printing any of them.

    Brief section 2.7. This shows presence and length only, which is enough to diagnose a
    truncated key or a stray newline and reveals nothing useful to anyone else. A
    trading system's output gets pasted into chat windows and issue trackers, so
    the safest secret is one that was never rendered.
    """
    import os

    from algo.api.app import TOKEN_ENV
    from algo.data.smartapi_feed import (
        credentials_from_env as smart_credentials_from_env,
    )
    from algo.execution.kotak import credentials_from_env as kotak_credentials_from_env

    def status(prefix: str, fields: list[str]) -> None:
        for name in fields:
            raw = os.environ.get(f"{prefix}_{name}", "")
            if raw:
                mark = "set"
                detail = f"{len(raw)} chars"
                if raw != raw.strip():
                    detail += "  ! has leading/trailing whitespace"
            else:
                mark, detail = "MISSING", ""
            typer.echo(f"  {prefix}_{name:<28} {mark:<8} {detail}")

    smart = smart_credentials_from_env()
    typer.echo("SmartAPI (historical bars - from .env or the environment):")
    status(
        "ALGO_SMARTAPI",
        ["API_KEY", "CLIENT_ID", "PASSWORD", "TOTP_SEED"],
    )
    typer.echo("")
    if smart.has_all():
        typer.echo("  all four SmartAPI credentials are present.")
    else:
        typer.echo("  MISSING: " + ", ".join(smart.missing()))

    typer.echo("")
    kotak = kotak_credentials_from_env()
    typer.echo("Kotak Neo (live broker - from .env or the environment):")
    status(
        "ALGO_KOTAK",
        ["CONSUMER_KEY", "MOBILE_NUMBER", "UCC", "TOTP_SEED", "MPIN", "MARKET_DATA_KEY"],
    )
    typer.echo("")
    if kotak.has_all():
        typer.echo("  all five Kotak credentials are present.")
    else:
        typer.echo("  MISSING: " + ", ".join(kotak.missing()))
        typer.echo("  Copy .env.example to .env and fill it in. An API key alone")
        typer.echo("  authenticates nothing - login needs the client code, the MPIN")
        typer.echo("  and the TOTP secret as well.")

    typer.echo("")
    token = os.environ.get(TOKEN_ENV, "")
    if token:
        typer.echo(f"  {TOKEN_ENV:<26} set      {len(token)} chars")
    else:
        typer.echo(f"  {TOKEN_ENV:<26} MISSING  (the monitoring API will not start)")
