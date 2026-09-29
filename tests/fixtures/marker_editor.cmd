@echo off
rem Test double for the "open external editor" step in tests/verify_exe.py.
rem Appends a marker line to the last argument it is given, so the test can tell
rem whether suspend -> external process -> resume -> load back actually happened.
rem ASCII only: cmd.exe reads this file with the OEM code page.
setlocal
set "target="
:collect
if "%~1"=="" goto done
set "target=%~1"
shift
goto collect
:done
if not defined target exit /b 1
echo [marker-editor] >> "%target%"
