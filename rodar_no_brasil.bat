@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

title TV Online HD - Gerar lista M3U
color 0A

echo ============================================================
echo   TV ONLINE HD  -  Gerador de lista M3U para SS IPTV
echo ============================================================
echo.

REM --- Procura o Python de verdade (evita o atalho falso da Microsoft Store)
set PY=
for %%P in (python.exe) do (
  for /f "delims=" %%i in ('where %%P 2^>nul') do (
    echo %%i | find /i "WindowsApps" >nul
    if errorlevel 1 if not defined PY set PY=%%i
  )
)
if not defined PY (
  py -3 -c "import sys" >nul 2>&1 && set PY=py -3
)

if not defined PY (
  echo [ERRO] Python nao encontrado.
  echo Instale em https://www.python.org/downloads/
  echo e marque a opcao "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo Python: %PY%
%PY% -V
echo.

REM --- Verifica se o ffprobe (ffmpeg) esta disponivel
where ffprobe >nul 2>&1
if errorlevel 1 (
  echo [AVISO] ffprobe nao encontrado.
  echo         A validacao sera feita apenas via HTTP ^(menos precisa^).
  echo         Para melhor resultado instale o ffmpeg: https://www.gyan.dev/ffmpeg/builds/
echo.
) else (
  echo ffprobe: OK
)
echo.

echo Gerando e testando a lista... isso leva cerca de 1-2 minutos.
echo.
%PY% atualizar_m3u.py
echo.

if exist tvonlinehd.m3u (
  echo ============================================================
  echo   PRONTO! Arquivo gerado: tvonlinehd.m3u
  echo ============================================================
  echo.
  echo Para usar no SS IPTV:
  echo   1. Copie a pasta do projeto para um pendrive OU
  echo   2. Suba a lista no seu GitHub ^(Actions ja atualiza sozinho^)
  echo   3. No SS IPTV: Configuracoes ^> Playlist externa
  echo      e informe o endereco do arquivo no GitHub, ex.:
  echo      https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPO/main/tvonlinehd.m3u
  echo.
  start "" explorer "%~dp0"
) else (
  echo [ERRO] A lista nao foi gerada. Veja as mensagens acima.
)

echo.
pause
endlocal
