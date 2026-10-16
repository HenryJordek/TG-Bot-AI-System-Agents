@echo off
python main.py
echo.
echo Bot stopped, unloading model...
ollama stop qwen2.5:3b
echo Done.
pause