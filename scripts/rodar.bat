@echo off
chcp 65001 > nul
title Franchise Intelligence — Import IBGE

echo.
echo  ============================================
echo   Franchise Intelligence - Importador IBGE
echo  ============================================
echo.

:: Verifica se Python está disponível
python --version > nul 2>&1
if errorlevel 1 (
    echo  [ERRO] Python nao encontrado.
    echo  Instale em https://python.org e marque "Add to PATH"
    pause
    exit /b 1
)

:: Verifica se o .env existe
if not exist "%~dp0..\.env" (
    echo  [AVISO] Arquivo .env nao encontrado.
    echo  Copie .env.example para .env e preencha as chaves do Supabase.
    pause
    exit /b 1
)

:: Instala dependências se necessário
echo  Verificando dependencias...
pip install -r "%~dp0..\requirements.txt" -q

echo.
echo  Escolha o modo de importacao:
echo.
echo   [1] Importacao completa (municipios + populacao + PIB) ~3-5 min
echo   [2] Apenas metadados (nomes e estados) ~30 segundos
echo   [3] Apenas um estado (ex: SP, MG, RJ)
echo   [4] Sair
echo.
set /p opcao="  Opcao: "

if "%opcao%"=="1" (
    python "%~dp0import_ibge.py"
)
if "%opcao%"=="2" (
    python "%~dp0import_ibge.py" --only-meta
)
if "%opcao%"=="3" (
    set /p estado="  Sigla do estado (ex: SP): "
    python "%~dp0import_ibge.py" --state %estado%
)
if "%opcao%"=="4" exit /b 0

echo.
pause
