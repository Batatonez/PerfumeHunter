"""Linha de comando.

  python -m scentinel                      -> liga o bot (é o que o systemd roda)
  python -m scentinel testar URL           -> mostra o preço que ele acha nessa página
  python -m scentinel testar URL --seletor ".preco"   -> testa com seletor CSS
  python -m scentinel testar URL --salvar  -> também salva o HTML em tests/fixtures/reais/
"""

import argparse
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

from scentinel import coleta, config, lojas
from scentinel.formato import formatar_reais


def testar(url, seletor=None, salvar=False):
    print(f"🔎 Testando: {url}")
    print(f"   loja: {lojas.nome_da_loja(url)}\n")
    html, erro, bloqueado = coleta.baixar_pagina(url)
    if erro:
        print(f"❌ Não deu pra baixar a página: {erro}")
        if bloqueado:
            print("   (a loja bloqueou: não vamos tentar burlar isso)")
        return 1

    if salvar:
        pasta = config.PASTA_PROJETO / "tests" / "fixtures" / "reais"
        pasta.mkdir(parents=True, exist_ok=True)
        host = re.sub(r"[^a-z0-9]+", "-", (urlparse(url).hostname or "pagina").lower()).strip("-")
        arquivo = pasta / f"{host}-{datetime.now():%Y%m%d-%H%M%S}.html"
        arquivo.write_bytes(html)
        print(f"💾 HTML salvo em {arquivo}\n")

    adaptador = lojas.adaptador_para(url)
    leitura = adaptador(html, seletor) if adaptador else coleta.achar_preco(html, seletor)
    if not leitura or not leitura.preco:
        print("❌ Não achei o preço automaticamente nessa página.")
        print("   Tenta com --seletor, ou roda com --salvar e manda o HTML pro Claude.")
        return 1

    print(f"✅ Preço: {formatar_reais(leitura.preco)}  (via {leitura.metodo})")
    if leitura.preco_de:
        print(f"   Preço de (riscado): {formatar_reais(leitura.preco_de)}")
    if leitura.preco_pix:
        print(f"   Pix: {formatar_reais(leitura.preco_pix)}")
    if leitura.esgotado:
        print("⚠️  A página diz que está ESGOTADO")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m scentinel", description="Scentinel 🐈‍⬛ o gato de guarda dos perfumes")
    sub = parser.add_subparsers(dest="comando")
    p_testar = sub.add_parser("testar", help="mostra o preço que ele acha numa URL")
    p_testar.add_argument("url")
    p_testar.add_argument("--seletor", help="seletor CSS do preço (plano B)")
    p_testar.add_argument("--salvar", action="store_true", help="salva o HTML em tests/fixtures/reais/")
    sub.add_parser("bot", help="liga o bot (padrão)")

    args = parser.parse_args(argv)
    if args.comando == "testar":
        return testar(args.url, args.seletor, args.salvar)

    from scentinel.bot import rodar

    rodar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
