@echo off
rem Compatibility launcher for installations made before the rename.
call "%~dp0hydraweave.cmd" %*
exit /b %errorlevel%
