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
if ! SAIDA_PULL=$(git pull --ff-only -q 2>&1); then
    echo "$SAIDA_PULL" | tail -n 5
    echo "❌ O git pull não deu. Manda print disso pro Claude."
    if echo "$SAIDA_PULL" | grep -q -e "overwritten" -e "local changes"; then
        echo "   (algum arquivo do bot foi mudado aqui no notebook, tipo o config.toml: o que você quiser"
        echo "    mudar no config vai no config.local.toml)"
    fi
    exit 1
fi
DEPOIS=$(git rev-parse HEAD)
# versão que o bot está rodando (ele grava ao ligar): código baixado mas não reiniciado não fica esquecido
RODANDO=$(cat dados/versao_rodando 2>/dev/null || true)

BASE=""
if [ "$ANTES" != "$DEPOIS" ]; then
    BASE="$ANTES"
    echo "📦 Novidades:"
    git log --oneline "$ANTES..$DEPOIS" | sed 's/^/   /'
elif [ -n "$RODANDO" ] && [ "$RODANDO" != "$DEPOIS" ]; then
    BASE="$RODANDO"
    echo "📦 O código novo já estava baixado, mas o bot ainda roda a versão antiga: vou testar e reiniciar."
else
    echo "✅ Já está na versão mais nova."
    if [ "$REINICIAR" = 0 ]; then
        echo "   (pra reiniciar mesmo assim: scentinel-update --reiniciar)"
        exit 0
    fi
fi

if [ -n "$BASE" ]; then
    # commit que não existe mais dá erro no diff: conta como "mudou", por segurança
    mudou() { ! git diff --quiet "$BASE" "$DEPOIS" -- "$@" 2>/dev/null; }
    desfazer() {
        echo "$1"
        # volta pro que estava antes do pull; se nada veio no pull, pro que o bot está rodando (o código parado
        # no disco não passou: não pode ligar no próximo restart)
        ALVO="$ANTES"
        [ "$ANTES" = "$DEPOIS" ] && ALVO="$BASE"
        if [ "$ALVO" = "$DEPOIS" ]; then
            echo "   NÃO reiniciei o bot (ele segue na versão que estava rodando). Manda print disso pro Claude."
        elif git reset -q --keep "$ALVO"; then
            echo "   Voltei pro código de antes e NÃO reiniciei o bot. Manda print disso pro Claude."
        else
            echo "   ⚠️ E não consegui voltar o código. NÃO reiniciei o bot. Manda print disso pro Claude."
        fi
        exit 1
    }

    if mudou requirements.txt; then
        echo "📥 Instalando dependências novas..."
        .venv/bin/pip install -q -r requirements.txt || desfazer "❌ Não deu pra instalar as dependências novas."
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
    # SCENTINEL_TESTE_PELO_GIT: os testes usam o config/catálogo do git (configuração sua não trava a atualização)
    if ! SCENTINEL_TESTE_PELO_GIT=1 .venv/bin/python -m pytest -q -p no:cacheprovider >/tmp/scentinel-testes.log 2>&1; then
        tail -n 15 /tmp/scentinel-testes.log
        desfazer "❌ Os testes falharam."
    fi
    echo "   $(tail -n 1 /tmp/scentinel-testes.log)"
fi

# o serviço do systemd instalado é igual ao do código? (confere sempre, até no --reiniciar: o /atualizar não
# tem senha pra instalar, então uma mudança dele espera aqui)
USUARIO="${USER:-$(id -un)}"
mkdir -p dados
sed -e "s|__USUARIO__|$USUARIO|g" -e "s|__PASTA__|$(pwd)|g" scentinel.service > dados/scentinel.service
if ! cmp -s dados/scentinel.service /etc/systemd/system/scentinel.service; then
    echo "⚙️  O serviço do systemd mudou: atualizando (pede a senha)..."
    sudo cp dados/scentinel.service /etc/systemd/system/scentinel.service
    sudo systemctl daemon-reload
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
