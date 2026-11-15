@echo off
rem Pull game data for the replay viewer out of a WoWS install with WG's wowsunpack.exe.
rem Usage: put this file and wowsunpack.exe (from ModSDK) in one folder, then
rem        unpack_gamedata.bat "C:\Games\World_of_Warships"
rem Reads from the game only. Writes to .\wows_gamedata and zips it to .\wows_gamedata.zip.
rem
rem What it takes:
rem   all_files.txt          every file in the game's archives, with sizes (to find model/texture paths)
rem   content/GameParams.data ship/weapon/consumable stats
rem   scripts/               entity definitions for this exact game version (15.10 support)
rem   *<ship id>*            every file whose path contains these ship ids: sample models + textures
setlocal

rem ship ids to pull models for (the own ships of the replays so far)
set "SHIPS=PGSB310 PRSC208 PBSB510"

if "%~1"=="" (
    echo usage: %~nx0 "path\to\World_of_Warships"
    exit /b 1
)
set "GAME=%~1"
set "HERE=%~dp0"
set "UNPACK=%HERE%wowsunpack.exe"
set "OUT=%HERE%wows_gamedata"

if not exist "%UNPACK%" (
    echo wowsunpack.exe not found next to this script
    exit /b 1
)
if not exist "%GAME%\bin" (
    echo no bin folder in "%GAME%"
    exit /b 1
)

rem newest build folder that has an idx folder
set "BUILD="
for /f "delims=" %%d in ('dir /b /ad /o-n "%GAME%\bin"') do (
    if not defined BUILD if exist "%GAME%\bin\%%d\idx" set "BUILD=%%d"
)
if not defined BUILD (
    echo no bin\^<build^>\idx folder found
    exit /b 1
)
set "IDX=%GAME%\bin\%BUILD%\idx"
set "PKG=..\..\..\res_packages"
set "LOG=%OUT%\extract_log.txt"
echo build %BUILD%

if not exist "%OUT%" mkdir "%OUT%"
echo build %BUILD% > "%LOG%"
if exist "%GAME%\bin\%BUILD%\res\scripts\entities.xml" echo note: loose res\scripts folder exists >> "%LOG%"

echo [1/4] listing all files with sizes...
"%UNPACK%" -s "%IDX%" -p "%PKG%" > "%OUT%\all_files.txt" 2>&1

echo [2/4] GameParams...
echo --- GameParams >> "%LOG%"
"%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%" -I "content/GameParams.data" >> "%LOG%" 2>&1

echo [3/4] entity scripts...
echo --- scripts >> "%LOG%"
"%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%" -I "scripts/*" >> "%LOG%" 2>&1

echo [4/4] ship models...
for %%s in (%SHIPS%) do (
    echo     %%s
    echo --- %%s >> "%LOG%"
    "%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%\ships" -I "*%%s*" >> "%LOG%" 2>&1
)

echo zipping...
if exist "%HERE%wows_gamedata.zip" del "%HERE%wows_gamedata.zip"
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\*' -DestinationPath '%HERE%wows_gamedata.zip'"

echo done: %HERE%wows_gamedata.zip
endlocal
