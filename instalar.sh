#!/usr/bin/env bash
# Instala o Scentinel na pasta atual. NÃO usa sudo: só mexe nesta pasta.
# Uso: ./instalar.sh
set -euo pipefail
cd "$(dirname "$0")"
PASTA="$(pwd)"
USUARIO="${USER:-$(id -un)}"

echo "🐈‍⬛ Instalando o Scentinel em $PASTA"

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "❌ Precisa de Python 3.11 ou mais novo (tem: $(python3 --version))"
    exit 1
fi

echo "1/4 criando o ambiente virtual (.venv)..."
if ! python3 -m venv .venv 2>/dev/null; then
    echo "❌ Faltou o pacote do venv. Rode:  sudo apt install python3-venv python3-pip"
    exit 1
fi

echo "2/4 instalando as dependências (demora uns minutos na primeira vez)..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "3/4 rodando os testes..."
.venv/bin/python -m pytest -q

echo "4/4 preparando arquivos..."
mkdir -p dados
if [ ! -f .env ]; then
    cp .env.exemplo .env
    echo "   criei o .env: abra e preencha (nano .env)"
fi
chmod 600 .env
sed -e "s|__USUARIO__|$USUARIO|g" -e "s|__PASTA__|$PASTA|g" scentinel.service > dados/scentinel.service
echo "   gerei dados/scentinel.service (usuário $USUARIO)"

echo
echo "✅ Pronto! Próximos passos no README: preencher o .env, testar e ligar o systemd."
