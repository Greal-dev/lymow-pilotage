@echo off
chcp 65001 >nul
rem Lance l'interface locale de pilotage Lymow en SIMULATION (aucun ordre réel).
rem Pour émettre vraiment : ouvrir un terminal et lancer  .venv\Scripts\lymow.exe ui --reel
cd /d "%~dp0"
if not exist ".venv\Scripts\lymow.exe" (
  echo Environnement absent : suivez la section Installation du README.
  pause
  exit /b 1
)
echo Interface Lymow en SIMULATION. Fermez cette fenêtre ou faites Ctrl+C pour l'arrêter.
echo En mode réel (lymow ui --reel) : ne fermez jamais la fenêtre pendant un mouvement.
".venv\Scripts\lymow.exe" ui
if errorlevel 1 pause
