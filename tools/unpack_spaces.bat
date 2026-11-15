@echo off
rem Pull map data (minimap, bounds, terrain) out of a WoWS install with WG's wowsunpack.exe.
rem Usage: put this file and wowsunpack.exe (from ModSDK) in one folder, then
rem        unpack_spaces.bat "C:\Games\World_of_Warships"
rem Reads from the game only. Writes everything to .\wows_spaces next to this script.
setlocal

if "%~1"=="" (
    echo usage: %~nx0 "path\to\World_of_Warships"
    exit /b 1
)
set "GAME=%~1"
set "HERE=%~dp0"
set "UNPACK=%HERE%wowsunpack.exe"
set "OUT=%HERE%wows_spaces"

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
echo build %BUILD%

if not exist "%OUT%" mkdir "%OUT%"
"%UNPACK%" --help > "%OUT%\wowsunpack_help.txt" 2>&1

echo listing spaces...
"%UNPACK%" -l "%IDX%" -p "%PKG%" -I "spaces/*" > "%OUT%\spaces_list.txt" 2>&1

for %%m in (38_Canada 44_Path_warrior 56_AngelWings) do (
    echo extracting %%m...
    "%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%" -I "spaces/%%m/*" >> "%OUT%\extract_log.txt" 2>&1
)

echo done: %OUT%
endlocal
