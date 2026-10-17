"""Gráfico de preço do /historico: o menor preço de cada dia, num PNG pequeno (Pillow, sem matplotlib).

Desenha em 2x e reduz no fim: as linhas ficam lisas sem precisar de biblioteca pesada.
Sem Pillow instalado, o /historico continua só com o texto.
"""

import io
import logging
from datetime import timedelta

from scentinel import db
from scentinel.busca import sem_acento
from scentinel.formato import formatar_reais, fuso

log = logging.getLogger("scentinel")

ESCALA = 2
LARGURA, ALTURA = 900, 420
MARGEM = {"esq": 92, "dir": 40, "topo": 58, "baixo": 46}
FUNDO = (43, 45, 49)  # cinza escuro do Discord
GRADE = (63, 65, 71)
TEXTO = (219, 222, 225)
TEXTO_FRACO = (148, 155, 164)
LINHA = (88, 141, 239)
MENOR = (87, 242, 135)


def serie_do_perfume(con, perfume_id, dias=30, agora=None, nome_fuso="America/Sao_Paulo"):
    """(ml, [(data, menor preço do dia)]) do tamanho com mais dias de leitura nos últimos `dias`.
    Junta todos os anúncios/vendedores do perfume: vale o mais barato de cada dia."""
    desde = (agora or db.agora_utc()) - timedelta(days=dias)
    linhas = con.execute(
        "SELECT a.ml, p.quando, p.preco FROM precos p JOIN anuncios a ON a.id = p.anuncio_id"
        " WHERE a.perfume_id = ? AND p.esgotado = 0 AND p.quando >= ?",
        (perfume_id, db.para_texto(desde)),
    ).fetchall()
    tz = fuso(nome_fuso)
    por_ml = {}
    for ml, quando, preco in linhas:
        dia = db.de_texto(quando).astimezone(tz).date()
        dias_do_ml = por_ml.setdefault(ml, {})
        dias_do_ml[dia] = min(preco, dias_do_ml.get(dia, preco))
    if not por_ml:
        return None, []
    ml = max(por_ml, key=lambda m: (len(por_ml[m]), m or 0))
    return ml, sorted(por_ml[ml].items())


# fontes com acento (a padrão do Pillow não tem "ç"); sem nenhuma, o texto vai sem acento
_FONTES = (
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)


def _fonte(tamanho, fontes=_FONTES):
    """(fonte, escreve acento?)"""
    from PIL import ImageFont

    for caminho in fontes:
        try:
            return ImageFont.truetype(caminho, tamanho * ESCALA), True
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tamanho * ESCALA), False
    except TypeError:  # Pillow antigo: fonte pequena de bitmap
        return ImageFont.load_default(), False


def _caber(texto, fonte, largura):
    """Corta com "…" o que não cabe na largura (nome de perfume muito comprido)."""
    try:
        if fonte.getlength(texto) <= largura:
            return texto
        while texto and fonte.getlength(texto + "…") > largura:
            texto = texto[:-1]
        return texto.rstrip() + "…"
    except AttributeError:  # fonte de bitmap antiga: corta pelo número de letras
        return texto if len(texto) <= 90 else texto[:89] + "…"


def _passo_bonito(intervalo, divisoes=4):
    bruto = intervalo / divisoes
    potencia = 10 ** len(str(int(bruto))) / 10 if bruto >= 1 else 1
    for mult in (1, 2, 2.5, 5, 10):
        if bruto <= mult * potencia:
            return mult * potencia
    return 10 * potencia


def _reais_curto(valor):
    return formatar_reais(valor).removesuffix(",00")


def desenhar(titulo, pontos, fontes=_FONTES):
    """pontos: [(data, preço)], 2 ou mais. Devolve o PNG (bytes) ou None sem Pillow."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.info("Pillow não instalado: /historico sem gráfico")
        return None
    if len(pontos) < 2:
        return None
    s = ESCALA
    img = Image.new("RGB", (LARGURA * s, ALTURA * s), FUNDO)
    d = ImageDraw.Draw(img)
    (f_titulo, acento), (f_eixo, _), (f_rotulo, _) = _fonte(18, fontes), _fonte(13, fontes), _fonte(14, fontes)
    txt = (lambda t: t) if acento else sem_acento

    x0, x1 = MARGEM["esq"] * s, (LARGURA - MARGEM["dir"]) * s
    y0, y1 = MARGEM["topo"] * s, (ALTURA - MARGEM["baixo"]) * s
    precos = [p for _, p in pontos]
    baixo, alto = min(precos), max(precos)
    folga = max((alto - baixo) * 0.15, alto * 0.03, 1)
    passo = _passo_bonito(alto - baixo + 2 * folga)
    ymin = (int((baixo - folga) / passo)) * passo
    ymax = ymin + passo * (int((alto + folga - ymin) / passo) + 1)
    dia0, dia1 = pontos[0][0], pontos[-1][0]
    total_dias = max((dia1 - dia0).days, 1)

    def px(dia):
        return x0 + (x1 - x0) * (dia - dia0).days / total_dias

    def py(preco):
        return y1 - (y1 - y0) * (preco - ymin) / (ymax - ymin)

    d.text((x0, 16 * s), _caber(txt(titulo), f_titulo, x1 - x0), font=f_titulo, fill=TEXTO)
    valor = ymin
    while valor <= ymax + 0.01:
        y = py(valor)
        d.line([(x0, y), (x1, y)], fill=GRADE, width=s)
        d.text((x0 - 10 * s, y), _reais_curto(valor), font=f_eixo, fill=TEXTO_FRACO, anchor="rm")
        valor += passo
    n_rotulos = min(6, len(pontos))
    for i in range(n_rotulos):
        dia = pontos[round(i * (len(pontos) - 1) / max(n_rotulos - 1, 1))][0]
        d.text((px(dia), y1 + 12 * s), dia.strftime("%d/%m"), font=f_eixo, fill=TEXTO_FRACO, anchor="mt")

    xy = [(px(dia), py(preco)) for dia, preco in pontos]
    d.line(xy, fill=LINHA, width=3 * s, joint="curve")
    r = 4 * s
    for x, y in xy:
        d.ellipse([x - r, y - r, x + r, y + r], fill=LINHA)

    i_menor = min(range(len(pontos)), key=lambda i: (pontos[i][1], -i))  # o mais recente, se empatar
    x, y = xy[i_menor]
    r2 = 7 * s
    d.ellipse([x - r2, y - r2, x + r2, y + r2], outline=MENOR, width=2 * s)
    # o menor fica embaixo da linha (ali não tem nada), do lado que cabe; colado no eixo, vai pra cima
    lado = "l" if x < (x0 + x1) / 2 else "r"
    embaixo = y + 30 * s < y1
    ancora = lado + ("t" if embaixo else "b")
    d.text((x + (8 if lado == "l" else -8) * s, y + (10 if embaixo else -10) * s),
           f"menor: {_reais_curto(pontos[i_menor][1])}", font=f_rotulo, fill=MENOR, anchor=ancora)
    if i_menor != len(pontos) - 1:
        x, y = xy[-1]
        d.text((x - 8 * s, y - 12 * s), txt(f"último: {_reais_curto(pontos[-1][1])}"), font=f_rotulo, fill=TEXTO,
               anchor="rb")

    img = img.resize((LARGURA, ALTURA), Image.LANCZOS)
    saida = io.BytesIO()
    img.save(saida, "PNG", optimize=True)
    return saida.getvalue()


def dados_do_grafico(con, perfume, dias=30, nome_fuso="America/Sao_Paulo", agora=None):
    """(título, pontos) do /historico, ou None se tem menos de 2 dias de leitura.
    Lê o banco (fica na thread do bot); o desenhar() pode ir pra outra thread."""
    ml, pontos = serie_do_perfume(con, perfume["id"], dias, agora, nome_fuso)
    if len(pontos) < 2:
        return None
    tamanho = f" {ml:g}ml" if ml else ""
    return f"{perfume['nome']}{tamanho} · menor preço por dia ({dias} dias)", pontos
