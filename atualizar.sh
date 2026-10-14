#!/usr/bin/env bash
# Atualiza o Scentinel: baixa o código novo, instala dependência nova, roda os testes e reinicia o bot.
# Uso: scentinel-update            (ou ./atualizar.sh dentro da pasta)
#      scentinel-update --reiniciar   (reinicia mesmo sem nada novo)
# Se os testes falharem, volta pro código de antes e NÃO reinicia (o bot segue rodando como estava).
set -euo pipefail

# Tudo dentro de uma função: o bash lê o script inteiro antes de rodar, então o git pull pode
# trocar este próprio arquivo sem bagunçar a execução.
main() {
if [ "$(id -u)" = 0 ]; then
    echo "❌ Rode sem sudo: scentinel-update (ele pede a senha só na hora de reiniciar o bot)"
    exit 1
fi
cd "$(dirname "$(readlink -f "$0")")"
REINICIAR=0
[ "${1:-}" = "--reiniciar" ] && REINICIAR=1

echo "🐈‍⬛ Atualizando o Scentinel em $(pwd)"
ANTES=$(git rev-parse HEAD)
if ! git pull --ff-only -q; then
    echo "❌ O git pull não deu. Manda print disso pro Claude."
    exit 1
fi
DEPOIS=$(git rev-parse HEAD)

if [ "$ANTES" = "$DEPOIS" ]; then
    echo "✅ Já está na versão mais nova."
    if [ "$REINICIAR" = 0 ]; then
        echo "   (pra reiniciar mesmo assim: scentinel-update --reiniciar)"
        exit 0
    fi
else
    echo "📦 Novidades:"
    git log --oneline "$ANTES..$DEPOIS" | sed 's/^/   /'
    mudou() { ! git diff --quiet "$ANTES" "$DEPOIS" -- "$@"; }

    if mudou requirements.txt; then
        echo "📥 Instalando dependências novas..."
        .venv/bin/pip install -q -r requirements.txt
    fi

    if grep -q "^playwright" requirements.txt; then
        echo "🌐 Conferindo o Chromium do /locate (na 1ª vez baixa uns 100 MB)..."
        if .venv/bin/python -m playwright install --only-shell chromium >/tmp/scentinel-chromium.log 2>&1; then
            .venv/bin/python -c "from scentinel import navegador; _, e, _ = navegador.baixar_html('about:blank', tempo_s=60); print('   ✅ Chromium ok' if not e else '   ⚠️  Chromium: ' + e)" || true
        else
            tail -n 5 /tmp/scentinel-chromium.log
            echo "   ⚠️  Não deu pra instalar o Chromium: o /locate segue só com a API do ML."
        fi
    fi

    echo "🧪 Rodando os testes..."
    if ! .venv/bin/python -m pytest -q -p no:cacheprovider >/tmp/scentinel-testes.log 2>&1; then
        tail -n 15 /tmp/scentinel-testes.log
        git reset -q --keep "$ANTES"
        echo "❌ Os testes falharam: voltei pro código de antes e NÃO reiniciei o bot."
        echo "   Manda print disso pro Claude."
        exit 1
    fi
    echo "   $(tail -n 1 /tmp/scentinel-testes.log)"

    if mudou scentinel.service; then
        echo "⚙️  O serviço do systemd mudou: atualizando (pede a senha)..."
        USUARIO="${USER:-$(id -un)}"
        sed -e "s|__USUARIO__|$USUARIO|g" -e "s|__PASTA__|$(pwd)|g" scentinel.service > dados/scentinel.service
        sudo cp dados/scentinel.service /etc/systemd/system/scentinel.service
        sudo systemctl daemon-reload
    fi
fi

echo "🔄 Reiniciando o bot (pede a senha do notebook)..."
sudo systemctl restart scentinel
sleep 5
if systemctl is-active --quiet scentinel; then
    echo "✅ Scentinel atualizado e rodando!"
else
    echo "❌ O bot não ligou depois de atualizar. Últimas linhas do log:"
    sudo journalctl -u scentinel -n 25 --no-pager
    exit 1
fi
}

main "$@"
