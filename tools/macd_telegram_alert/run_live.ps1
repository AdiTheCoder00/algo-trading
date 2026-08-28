# Entry point for the "MACD Telegram Alert" scheduled task.
#
# Exists only to set REQUESTS_CA_BUNDLE before launching macd_alert.py -
# Task Scheduler starts a fresh environment with none of this shell's
# variables, and ccxt otherwise fails every request with
# CERTIFICATE_VERIFY_FAILED on a machine where antivirus intercepts TLS
# (see README.md, "CERTIFICATE_VERIFY_FAILED").
#
# Regenerate ca-bundle.pem if this ever goes stale (the antivirus rotates its
# interception certificate, or macd_alert.py moves to a machine without one):
#
#   python -c "import certifi,pathlib; ..." (see README.md)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -Path $root

$bundle = Join-Path $root "ca-bundle.pem"
if (Test-Path $bundle) {
    $env:REQUESTS_CA_BUNDLE = $bundle
}

& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "macd_alert.py") *>> (Join-Path $root "macd_alert.log")
