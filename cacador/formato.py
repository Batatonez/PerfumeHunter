"""Formatação: dinheiro, datas e os embeds do Discord (como dicts).

Os embeds são montados como dict puro (testável) e viram discord.Embed
com discord.Embed.from_dict() no bot.
"""

from datetime import UTC, timedelta, timezone

from cacador.promo import FONTE_LOJA

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

def linha_preco(oferta):
    av = oferta.avaliacao
    partes = [f"💰 **{formatar_reais(oferta.preco)}**"]
    if av.referencia:
        partes.append(f"(ref. {formatar_reais(av.referencia)})")
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
    if oferta.bateu_alvo and oferta.alvo:
        linhas.append(f"🎯 Bateu seu alvo de {formatar_reais(oferta.alvo)}")
    if oferta.esgotado:
        linhas.append("⚪ ESGOTADO na última leitura")
    linhas.append(f"🔗 [link do anúncio]({oferta.url})")
    return linhas


def titulo_nivel(oferta):
    av = oferta.avaliacao
    if av.nivel:
        n = av.nivel
        return f"{n.emoji} NÍVEL {n.nivel} · {n.nome.upper()} — {formatar_pct(av.pct)} OFF"
    if oferta.bateu_alvo:
        return "🎯 BATEU SEU ALVO"
    if av.referencia and av.pct > 0:
        return f"−{formatar_pct(av.pct)} vs referência"
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
    return {
        "title": titulo_nivel(oferta),
        "url": oferta.url,
        "color": cor_da_oferta(oferta),
        "description": cortar("\n".join(descricao)),
        "footer": {"text": f"Checado em {formatar_data(oferta.quando, nome_fuso)}"},
    }


# ============================================================
# /locate
# ============================================================

def _resumo_curto(oferta):
    av = oferta.avaliacao
    if av.referencia and av.pct > 0:
        return f"{oferta.loja} −{formatar_pct(av.pct)}"
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
