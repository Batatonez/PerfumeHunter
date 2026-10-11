"""Linha de comando.

  python -m scentinel                      -> liga o bot (é o que o systemd roda)
  python -m scentinel testar URL           -> mostra o preço que ele acha nessa página
  python -m scentinel testar URL --seletor ".preco"   -> testa com seletor CSS
  python -m scentinel testar URL --salvar  -> também salva o HTML em tests/fixtures/reais/
  python -m scentinel lojas              -> testa a busca em cada loja do config.toml
  python -m scentinel lojas --termo "lattafa khamrah"
  python -m scentinel ml-login           -> login do Mercado Livre (uma vez)
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


def testar_lojas(termo, so=None):
    """Testa a busca automática em cada loja do config.toml (inclusive as desligadas)."""
    import random
    import time

    from scentinel import catalogo
    from scentinel.fontes import montar_fontes

    cfg = config.carregar_config()
    itens = catalogo.carregar_catalogo()
    configs = [dict(c, ativo=True) for c in cfg.descoberta.fontes]
    fontes = montar_fontes(configs)
    if so:
        fontes = [f for f in fontes if so.lower() in f.id.lower() or so.lower() in f.nome.lower()]
    print(f"🔎 Buscando \"{termo}\" em {len(fontes)} loja(s)...\n")
    funcionam = []
    for i, fonte in enumerate(fontes):
        if i:
            time.sleep(random.uniform(cfg.delay_min_s, cfg.delay_max_s))
        res = fonte.buscar(termo)
        if res.erro:
            extra = " (bloqueou: não vamos forçar)" if res.bloqueado else ""
            print(f"❌ {fonte.nome:22} {res.erro}{extra}")
            continue
        casam = []
        for a in res.achados:
            item = catalogo.identificar(a.titulo, itens, a.marca)
            if item:
                casam.append((a, item))
        oficiais = sum(1 for a in res.achados if a.oficial)
        print(f"✅ {fonte.nome:22} {len(res.achados)} resultado(s), {oficiais} oficial(is), "
              f"{len(casam)} reconhecido(s) no catálogo")
        tarefas = fonte.tarefas_varredura() if hasattr(fonte, "tarefas_varredura") else []
        if tarefas:
            time.sleep(random.uniform(cfg.delay_min_s, cfg.delay_max_s))
            rotulo, tarefa = tarefas[0]
            v = tarefa()
            if v.erro:
                print(f"     varredura: ❌ {v.erro}")
            else:
                promos = [a for a in v.achados if catalogo.e_perfume(a.titulo) and a.preco_de
                          and a.preco_de >= a.preco * 1.2]
                print(f"     varredura ({rotulo}): {len(v.achados)} produto(s), {len(promos)} perfume(s) com 20%+ OFF")
                for a in sorted(promos, key=lambda a: a.preco / a.preco_de)[:3]:
                    print(f"       −{(1 - a.preco / a.preco_de) * 100:.0f}% {formatar_reais(a.preco)} · {a.titulo[:60]}")
        for a, item in casam[:3]:
            ml = a.ml or catalogo.extrair_ml(a.titulo, a.variacao or "")
            de = f" (de {formatar_reais(a.preco_de)})" if a.preco_de else ""
            print(f"     {formatar_reais(a.preco)}{de} · {item.nome_exibicao} {ml or '?'}ml · {a.titulo[:60]}")
        if not casam:
            for a in res.achados[:2]:
                print(f"     (não reconhecido) {formatar_reais(a.preco)} · {a.titulo[:70]}")
        funcionam.append(fonte.nome)
    print(f"\nFuncionaram: {', '.join(funcionam) or 'nenhuma'}")
    print("Nas que deram ❌, coloque `ativo = false` no config.toml (ou mande o resultado pro Claude).")
    return 0


def ml_login():
    """Login do Mercado Livre (uma vez). Depois o bot renova o token sozinho."""
    from scentinel.fontes import mercadolivre as ml

    cid, segredo, redirect = ml.credenciais()
    if not cid or not segredo:
        print("❌ Falta ML_CLIENT_ID e ML_CLIENT_SECRET no .env (veja o README, seção Mercado Livre).")
        return 1
    print("1) Abra este link no navegador, entre na sua conta do Mercado Livre e clique em Permitir:\n")
    print(f"   {ml.link_login()}\n")
    print(f"2) Ele vai abrir {redirect} com um ?code=TG-... no endereço.")
    print("   Copie o ENDEREÇO INTEIRO da barra do navegador e cole aqui:\n")
    codigo = input("   endereço ou código: ").strip()
    ok, msg = ml.trocar_codigo(codigo)
    if not ok:
        print(f"\n❌ Não deu: {msg}")
        print("   (o código vale poucos minutos e só uma vez: rode de novo e cole rapidinho)")
        return 1
    print("\n✅ Login feito! Testando a busca...")
    res = ml.FonteMercadoLivre().buscar("sauvage dior")
    if res.erro:
        print(f"⚠️  Login ok, mas a busca deu: {res.erro}")
        return 1
    print(f"✅ Busca ok: {len(res.achados)} anúncio(s) de loja oficial. Pode reiniciar o bot.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m scentinel", description="Scentinel 🐈‍⬛ o gato de guarda dos perfumes")
    sub = parser.add_subparsers(dest="comando")
    p_testar = sub.add_parser("testar", help="mostra o preço que ele acha numa URL")
    p_testar.add_argument("url")
    p_testar.add_argument("--seletor", help="seletor CSS do preço (plano B)")
    p_testar.add_argument("--salvar", action="store_true", help="salva o HTML em tests/fixtures/reais/")
    p_lojas = sub.add_parser("lojas", help="testa a busca automática em cada loja")
    p_lojas.add_argument("--termo", default="sauvage dior", help="o que buscar (padrão: sauvage dior)")
    p_lojas.add_argument("--so", help="testa só a loja com esse nome (ex.: sephora)")
    sub.add_parser("ml-login", help="login do Mercado Livre (uma vez)")
    sub.add_parser("bot", help="liga o bot (padrão)")

    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv(config.ARQ_ENV)  # ML_CLIENT_ID etc. também valem nos comandos de terminal
    if args.comando == "testar":
        return testar(args.url, args.seletor, args.salvar)
    if args.comando == "lojas":
        return testar_lojas(args.termo, args.so)
    if args.comando == "ml-login":
        return ml_login()

    from scentinel.bot import rodar

    rodar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
