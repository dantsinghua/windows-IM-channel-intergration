@echo off
rem Legacy official SFX extraction cannot provide a protected persistent stage.
rem Never move or merge unverified payloads into the live installation.
rem Production packages must use the QTrade SFX with exclusive staging.
echo QTrade: this legacy extraction mode is disabled.
echo Please obtain the complete installer with protected independent staging.
exit /b 41
