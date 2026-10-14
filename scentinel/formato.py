"""Formatação: dinheiro, datas e os embeds do Discord (como dicts).

Os embeds são montados como dict puro (testável) e viram discord.Embed
com discord.Embed.from_dict() no bot.
"""

from datetime import UTC, timedelta, timezone

from scentinel.promo import FONTE_CATALOGO, FONTE_LOJA, FONTE_MERCADO

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None

COR_ALVO = 0x2024CC  # azul da foto do bot
COR_NEUTRA = 0x15155F  # azul-marinho da foto do bot
LIMITE_DESCRICAO = 4000


def fuso(nome="America/Sao_Paulo"):
    try:
        return ZoneInfo(nome)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return timezone(timedelta(hours=-3))


def formatar_reais(valor):
    texto = f"{valor:,.2f}"  # 1,299.90
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_pct(pct):
    return f"{pct:.0f}%"


def formatar_data(momento, nome_fuso="America/Sao_Paulo"):
    if momento is None:
        return "?"
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return momento.astimezone(fuso(nome_fuso)).strftime("%d/%m %H:%M")


def cortar(texto, limite=LIMITE_DESCRICAO):
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


# ============================================================
# LINHAS COMUNS (alerta e /locate)
# ============================================================

PCT_MINIMO = 1  # abaixo de 1% não é desconto: não escreve "0% OFF"


def tem_desconto(oferta):
    av = oferta.avaliacao
    return bool(av.referencia and av.pct and av.pct >= PCT_MINIMO)


def linha_preco(oferta):
    av = oferta.avaliacao
    partes = [f"💰 **{formatar_reais(oferta.preco)}**"]
    if oferta.preco_de:
        partes = [f"~~{formatar_reais(oferta.preco_de)}~~ →"] + partes
    if av.referencia:
        ref = f"(ref. {formatar_reais(av.referencia)})"
        partes.append(f"**{formatar_pct(av.pct)} OFF** {ref}" if tem_desconto(oferta) else ref)
    if oferta.por_ml:
        partes.append(f"· {formatar_reais(oferta.por_ml)}/ml")
    return "   ".join(partes)


def linhas_detalhe(oferta):
    linhas = [linha_preco(oferta)]
    if oferta.preco_pix:
        linhas.append(f"⚡ Pix: {formatar_reais(oferta.preco_pix)}")
    if oferta.menor_ja_visto:
        menor = f"📉 Menor já visto: {formatar_reais(oferta.menor_ja_visto)}"
        if oferta.recorde:
            menor += " 🏆 RECORDE"
        linhas.append(menor)
    if oferta.avaliacao.fonte == FONTE_LOJA:
        linhas.append("📢 desconto anunciado pela loja (sem histórico ainda)")
    elif oferta.avaliacao.fonte == FONTE_CATALOGO:
        linhas.append("📊 comparado com o preço normal de mercado")
    elif oferta.avaliacao.fonte == FONTE_MERCADO:
        linhas.append("📊 comparado com a 2ª loja mais barata")
    if oferta.bateu_alvo and oferta.alvo:
        if oferta.origem == "descoberta":
            linhas.append(f"💸 Abaixo de {formatar_reais(oferta.alvo)}, preço bom pra esse perfume")
        else:
            linhas.append(f"🎯 Bateu seu alvo de {formatar_reais(oferta.alvo)}")
    selos = [s for s in (oferta.categoria, "🛡️ Loja oficial" if oferta.oficial else None) if s]
    if selos:
        linhas.append(" · ".join(selos))
    if oferta.esgotado:
        linhas.append("⚪ ESGOTADO na última leitura")
    linhas.append(f"🔗 [link do anúncio]({oferta.url})")
    return linhas


def titulo_nivel(oferta):
    av = oferta.avaliacao
    if av.nivel:
        n = av.nivel
        return f"{n.emoji} NÍVEL {n.nivel} · {n.nome.upper()} — {formatar_pct(av.pct)} OFF"
    off = f" — {formatar_pct(av.pct)} OFF" if tem_desconto(oferta) else ""
    if oferta.bateu_alvo:
        return ("💸 PREÇO BOM" if oferta.origem == "descoberta" else "🎯 BATEU SEU ALVO") + off
    if off:
        return f"🏷️ {formatar_pct(av.pct)} OFF"
    return "Sem promo agora"


def cor_da_oferta(oferta):
    if oferta.avaliacao.nivel:
        return oferta.avaliacao.nivel.cor
    if oferta.bateu_alvo:
        return COR_ALVO
    return COR_NEUTRA


# ============================================================
# ALERTA
# ============================================================

def embed_alerta(oferta, nome_fuso="America/Sao_Paulo"):
    descricao = [f"**{oferta.titulo}** · {oferta.loja}"] + linhas_detalhe(oferta)
    embed = {
        "title": titulo_nivel(oferta),
        "url": oferta.url,
        "color": cor_da_oferta(oferta),
        "description": cortar("\n".join(descricao)),
        "footer": {"text": f"Checado em {formatar_data(oferta.quando, nome_fuso)}"},
    }
    if oferta.imagem:
        embed["thumbnail"] = {"url": oferta.imagem}
    return embed


def embed_vitrine(oferta, nome_fuso="America/Sao_Paulo"):
    """Post da vitrine: igual ao alerta (com o % OFF no título), mas não diz "sem promo" quando não tem
    desconto."""
    embed = embed_alerta(oferta, nome_fuso)
    av = oferta.avaliacao
    if not av.nivel:
        if tem_desconto(oferta):
            embed["title"] = f"🛍️ VITRINE — {formatar_pct(av.pct)} OFF"
        else:
            embed["title"] = "🛍️ VITRINE — achado do Scentinel"
    embed["footer"] = {"text": f"🛍️ Vitrine · checado em {formatar_data(oferta.quando, nome_fuso)}"}
    return embed


# ============================================================
# /locate
# ============================================================

def _resumo_curto(oferta):
    av = oferta.avaliacao
    if av.referencia and av.pct > 0:
        return f"{oferta.loja} {formatar_pct(av.pct)} OFF"
    return f"{oferta.loja} {formatar_reais(oferta.preco)}"


def embed_locate(melhor, em_promo, outros, menor_por_ml, outras_variacoes, nome_fuso="America/Sao_Paulo"):
    """melhor: Oferta vencedora. em_promo: se ela tem nível.
    outros: próximas Ofertas. menor_por_ml: Oferta com menor R$/ml (ou None).
    outras_variacoes: nomes de outros perfumes que também bateram."""
    if em_promo:
        titulo = f"🏆 Maior promo: {melhor.titulo} — {melhor.loja}"
        descricao = [titulo_nivel(melhor)]
    else:
        titulo = f"😴 Nenhum em promo agora · mais barato: {melhor.titulo} — {melhor.loja}"
        descricao = []
    descricao += linhas_detalhe(melhor)

    if outros:
        descricao.append("")
        descricao.append("**Outros:** " + " · ".join(_resumo_curto(o) for o in outros))
    if menor_por_ml and menor_por_ml.anuncio_id != melhor.anuncio_id:
        descricao.append(
            f"💰 **Menor R$/ml:** {menor_por_ml.titulo} — {menor_por_ml.loja} "
            f"({formatar_reais(menor_por_ml.por_ml)}/ml)"
        )

    embed = {
        "title": cortar(titulo, 256),
        "url": melhor.url,
        "color": cor_da_oferta(melhor),
        "description": cortar("\n".join(descricao)),
    }
    rodape = f"Leitura de {formatar_data(melhor.quando, nome_fuso)}"
    if outras_variacoes:
        rodape = (
            "também achei: " + ", ".join(outras_variacoes)
            + ". Use o nome completo pra filtrar.\n" + rodape
        )
    embed["footer"] = {"text": cortar(rodape, 2000)}
    return embed


# ============================================================
# 🏆 Top do dia
# ============================================================

def embed_top(ofertas, titulo="🏆 Top do dia", nome_fuso="America/Sao_Paulo"):
    if not ofertas:
        return {
            "title": titulo,
            "color": COR_NEUTRA,
            "description": "Nenhuma promo boa nas últimas 24 h. O gato segue de olho 👀",
        }
    linhas = []
    for i, o in enumerate(ofertas, 1):
        n = o.avaliacao.nivel
        selo = f"{n.emoji} **{formatar_pct(o.avaliacao.pct)} OFF**" if n else ""
        extra = f" · {o.categoria}" if o.categoria else ""
        pix = f" · ⚡ Pix {formatar_reais(o.preco_pix)}" if o.preco_pix else ""
        linhas.append(
            f"**{i}. [{o.titulo}]({o.url})**\n"
            f"{selo} · **{formatar_reais(o.preco)}** em {o.loja}{pix}{extra}"
        )
    return {
        "title": titulo,
        "color": ofertas[0].avaliacao.nivel.cor if ofertas[0].avaliacao.nivel else COR_NEUTRA,
        "description": cortar("\n\n".join(linhas)),
        "footer": {"text": f"Preços de {formatar_data(max(o.quando for o in ofertas), nome_fuso)} · podem mudar a qualquer momento"},
    }


def embed_top_semana(itens, nome_fuso="America/Sao_Paulo"):
    """itens: Descoberta.top_semana() -> maiores % OFF que passaram no canal nos últimos 7 dias."""
    linhas = []
    for i, it in enumerate(itens, 1):
        a, atual = it["anuncio"], it["agora"]
        nome = a["perfume_nome"] + (f" {a['ml']:g}ml" if a.get("ml") else "")
        linha = (f"**{i}. [{nome}]({a['url']})**\n"
                 f"🏷️ **{formatar_pct(it['pct'])} OFF** · {formatar_reais(it['preco'])} em "
                 f"{formatar_data(it['quando'], nome_fuso)[:5]}")
        if atual and not atual.esgotado:
            if atual.preco <= it["preco"] * 1.02:
                linha += " · ✅ ainda nesse preço"
            else:
                linha += f" · agora {formatar_reais(atual.preco)}"
        elif atual:
            linha += " · ⚪ esgotou"
        linhas.append(linha)
    return {
        "title": "🏆 Top da semana",
        "color": 0xF1C40F,
        "description": cortar("\n\n".join(linhas) or "Semana fraca: nenhuma promo boa passou no canal."),
        "footer": {"text": "Os maiores descontos que passaram no canal nos últimos 7 dias · preços podem mudar"},
    }


# ============================================================
# /locate ao vivo
# ============================================================

def _linha_oferta_viva(achado, ml, pct):
    partes = [f"**{formatar_reais(achado.preco)}**"]
    if achado.preco_de:
        partes.insert(0, f"~~{formatar_reais(achado.preco_de)}~~")
    if pct >= 1:
        partes.append(f"**{formatar_pct(pct)} OFF**")
    partes.append(achado.loja + (" 🛡️" if achado.oficial else ""))
    if ml:
        partes.append(f"{ml:g}ml · {formatar_reais(achado.preco / ml)}/ml")
    return " · ".join(partes)


def _porque_nao_achou(res):
    """(manchete, explicação) em português simples, a partir do que as lojas responderam."""
    motivos = " ".join(m or "" for _, m in getattr(res, "detalhes", None) or [])
    if res.lojas_falha and not res.lojas_ok:
        motivo = next((m for _, m in res.detalhes if m), "")
        if "429" in motivo or "devagar" in motivo:
            explica = "O ML pediu pro bot ir mais devagar. Tenta de novo em uns minutinhos."
        elif "401" in motivo or "login" in motivo:
            explica = "O login do bot no ML venceu. Avisa o dono pra rodar o ml-login no notebook."
        else:
            explica = "Pode ser instabilidade do ML. Tenta de novo daqui a pouco."
        return f"⚠️ {', '.join(res.lojas_falha)} não respondeu agora", explica
    if "com outro nome" in motivos and "nenhum produto com esse nome" not in motivos:
        return ("🤔 Achei anúncios, mas nenhum com esse nome",
                "Confere a escrita ou tenta com a marca (ex.: `/locate nome:sauvage marca:dior`).")
    if "sem ninguém vendendo" in motivos or "escondidos" in motivos:
        if "com vendedor" not in motivos:
            return ("📭 Tá no catálogo do ML, mas ninguém tá vendendo agora",
                    "Pode voltar a qualquer hora. Dá uma olhada nos anúncios avulsos no link abaixo.")
    if "kit/decant" in motivos and "com vendedor" not in motivos:
        return ("🧪 Só achei kit, decant ou amostra",
                "O bot mostra só o frasco inteiro. Os outros anúncios estão no link abaixo.")
    return ("🔍 Não achei esse perfume no catálogo do Mercado Livre",
            "Ele deve estar só em anúncios avulsos, que o ML não mostra pra bots.")


def embed_nao_achou(res):
    manchete, explica = _porque_nao_achou(res)
    linhas = [f"**{manchete}**", explica]
    links = getattr(res, "links", None) or []
    if links:
        linhas.append("")
        for loja, link in links:
            linhas.append(f"👉 **[Ver todos os anúncios de \"{res.consulta}\" no {loja}]({link})**")
    embed = {
        "title": f"🔎 {res.consulta}",
        "color": COR_NEUTRA,
        "description": cortar("\n".join(linhas)),
    }
    if links:
        embed["url"] = links[0][1]
    detalhes = " · ".join(f"{loja}: {m}" for loja, m in getattr(res, "detalhes", None) or [] if m)
    if detalhes:  # o técnico fica pequeno no rodapé (ajuda a entender o que houve, sem poluir)
        embed["footer"] = {"text": cortar(detalhes, 2000)}
    return embed


def embed_ao_vivo(res, max_linhas=8):
    if not res.ofertas:
        return embed_nao_achou(res)
    primeiro, ml, pct = res.ofertas[0]
    linhas = [
        f"🏆 **Mais barato:** [{primeiro.titulo[:90]}]({primeiro.url})",
        "💰 " + _linha_oferta_viva(primeiro, ml, pct),
    ]
    if primeiro.preco_pix:
        linhas.append(f"⚡ Pix: {formatar_reais(primeiro.preco_pix)}")
    if len(res.ofertas) > 1:
        linhas += ["", "**Outras ofertas:**"]
        for achado, ml2, pct2 in res.ofertas[1:max_linhas]:
            linhas.append(f"• [{achado.titulo[:55]}]({achado.url})\n  " + _linha_oferta_viva(achado, ml2, pct2))
    for loja, link in getattr(res, "links", None) or []:
        linhas += ["", f"🔗 [Ver todos os anúncios no {loja}]({link})"]
    rodape = f"Procurei em: {', '.join(res.lojas_ok)}"
    if res.lojas_falha:
        rodape += f" · fora do ar agora: {', '.join(res.lojas_falha)}"
    embed = {
        "title": f"🔎 {res.consulta} — {len(res.ofertas)} oferta(s)",
        "url": primeiro.url,
        "color": 0x2024CC,
        "description": cortar("\n".join(linhas)),
        "footer": {"text": cortar(rodape + " · 🛡️ = loja oficial", 2000)},
    }
    if primeiro.imagem:
        embed["thumbnail"] = {"url": primeiro.imagem}
    return embed


# ============================================================
# 🔔 /alerta (alerta pessoal)
# ============================================================

def embed_alerta_usuario(alerta, achado, ml, pct):
    linhas = [
        f"**{achado.titulo[:150]}**",
        "💰 " + _linha_oferta_viva(achado, ml, pct),
        f"🎯 Você pediu até {formatar_reais(alerta['preco_max'])}",
        f"🔗 [abrir o anúncio]({achado.url})",
    ]
    tamanho = f" {alerta['ml']:g}ml" if alerta.get("ml") else ""
    embed = {
        "title": cortar(f"🔔 {alerta['consulta']}{tamanho} chegou no seu preço", 256),
        "url": achado.url,
        "color": COR_ALVO,
        "description": cortar("\n".join(linhas)),
        "footer": {"text": f"Alerta #{alerta['id']} · /alerta lista pra ver os seus · /alerta remover pra parar"},
    }
    if achado.imagem:
        embed["thumbnail"] = {"url": achado.imagem}
    return embed


# ============================================================
# 📚 catálogo automático
# ============================================================

def embed_catalogo_novo(itens):
    linhas = []
    for i in itens:
        linhas.append(f"• **{i.nome_exibicao}** ({i.marca}) · {i.rotulo_categoria} · normal ~"
                      f"{formatar_reais(i.preco_normal)} ({i.ml_ref:g}ml)")
    return {
        "title": f"📚 {len(itens)} perfume(s) novo(s) no catálogo",
        "color": COR_NEUTRA,
        "description": cortar("Vi esses masculinos/unissex no Mercado Livre essa semana. Agora eu caço promo deles "
                              "também:\n\n" + "\n".join(linhas)),
        "footer": {"text": "O preço normal é a mediana da semana · o dono tira algum com /catalogo remover"},
    }
