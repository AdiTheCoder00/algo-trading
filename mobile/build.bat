@echo off
REM Build the Gold APKs from the repository copy.
REM
REM This is the only copy: the app used to live at D:\dev\gold_app, built there
REM because Gradle has a long history of breaking on paths with spaces. It turns
REM out to build fine from "D:\algo trading", so the split -- and the drift it
REM invited -- is gone.
REM
REM Requires lib\secrets.dart and Secrets.kt, both gitignored. Copy the .example
REM files beside them and fill in your relay address and token.

set JAVA_HOME=D:\dev\jdk17
set ANDROID_HOME=D:\dev\android-sdk
set ANDROID_SDK_ROOT=D:\dev\android-sdk

REM Load-bearing: without it Gradle cannot resolve dependencies on this machine.
REM The JDK validates TLS against its own cacerts rather than the Windows store,
REM and the chain served here does not satisfy it. Same class of problem that
REM truststore.inject_into_ssl() solves for Python in scripts\mcx_live_to_excel.py.
set JAVA_TOOL_OPTIONS=-Djavax.net.ssl.trustStoreType=WINDOWS-ROOT

set PATH=%JAVA_HOME%\bin;D:\dev\flutter\bin;%ANDROID_HOME%\platform-tools;%PATH%

cd /d "%~dp0gold"
if not exist "lib\secrets.dart" (
  echo Missing lib\secrets.dart -- copy lib\secrets.example.dart and fill it in.
  exit /b 1
)
flutter build apk --release --split-per-abi %*
