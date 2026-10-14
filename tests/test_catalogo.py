import pytest

from scentinel.catalogo import ItemCatalogo, casar, e_lixo, extrair_ml, identificar

CAT = [
    ItemCatalogo("dior-sauvage-edt", "Sauvage", "Dior", "EDT", [60, 100, 200], "masc", "designer", 700, 100, 520),
    ItemCatalogo("dior-sauvage-edp", "Sauvage", "Dior", "EDP", [60, 100, 200], "masc", "designer", 850, 100, 640),
    ItemCatalogo("dior-sauvage-elixir", "Sauvage", "Dior", "Elixir", [60, 100], "masc", "designer", 1100, 60, 850),
    ItemCatalogo("ch-212-vip-men", "212 VIP Men", "Carolina Herrera", "EDT", [50, 100], "masc", "designer",
                 600, 100, 450, evitar=["black", "rose", "rosé", "party"]),
    ItemCatalogo("ch-212-vip-black", "212 VIP Black", "Carolina Herrera", "EDP", [50, 100], "masc", "designer",
                 650, 100, 480),
    ItemCatalogo("vr-spicebomb", "Spicebomb", "Viktor&Rolf", "EDT", [50, 90], "masc", "designer",
                 650, 90, 480, evitar=["extreme", "infrared", "night vision"]),
    ItemCatalogo("bot-malbec", "Malbec", "O Boticário", "Colônia", [100], "masc", "nacional",
                 199, 100, 140, evitar=["gold", "black", "magnetic", "club", "ultra bleu", "x"]),
    ItemCatalogo("lattafa-khamrah", "Khamrah", "Lattafa", "EDP", [100], "unissex", "arabe", 350, 100, 250,
                 evitar=["qahwa"]),
    ItemCatalogo("ch-good-girl", "Good Girl", "Carolina Herrera", "EDP", [30, 50, 80], "fem", "designer",
                 750, 80, 560, evitar=["supreme", "blush", "legere", "very good girl"]),
]
POR_ID = {i.id: i for i in CAT}


def ident(titulo, marca=None):
    item = identificar(titulo, CAT, marca)
    return item.id if item else None


@pytest.mark.parametrize(
    "titulo, esperado",
    [
        ("Sauvage Dior Eau de Parfum - Perfume Masculino 100ml", "dior-sauvage-edp"),
        ("Perfume Dior Sauvage Eau de Toilette 200 ml", "dior-sauvage-edt"),
        ("Dior Sauvage Elixir 60ml", "dior-sauvage-elixir"),
        ("Dior Sauvage 100ml", None),  # sem concentração e existem 3 Sauvage: ambíguo
        ("212 VIP Men Carolina Herrera Eau de Toilette 100ml", "ch-212-vip-men"),
        ("212 VIP Black Carolina Herrera EDP Masculino 100ml", "ch-212-vip-black"),
        ("212 VIP Rosé Carolina Herrera Feminino 80ml", None),
        ("Spicebomb Viktor&Rolf Eau de Toilette 90ml", "vr-spicebomb"),
        ("Spicebomb Extreme Viktor&Rolf EDP 90ml", None),
        ("Malbec Desodorante Colônia 100ml - O Boticário", "bot-malbec"),  # deo colônia é o perfume!
        ("Malbec Gold Desodorante Colônia 100ml", None),
        ("Lattafa Khamrah Eau de Parfum 100ml Unissex", "lattafa-khamrah"),
        ("Good Girl Carolina Herrera Eau de Parfum Feminino 80ml", "ch-good-girl"),
        ("Very Good Girl Carolina Herrera EDP 80ml", None),
    ],
)
def test_identificar(titulo, esperado):
    assert ident(titulo) == esperado


@pytest.mark.parametrize(
    "titulo",
    [
        "Kit Dior Sauvage EDP 100ml + Travel 10ml",
        "Decant Sauvage Elixir 5ml",
        "Amostra Dior Sauvage EDT",
        "Contratipo Sauvage 100ml",
        "Perfume Inspirado no Sauvage Dior 100ml",
        "Malbec Desodorante Body Spray 100ml",
        "Malbec Desodorante Antitranspirante Aerosol",
        "Dior Sauvage Gel de Banho 250ml",
        "Sauvage Dior Tester EDP 100ml",
    ],
)
def test_lixo_e_descartado(titulo):
    assert e_lixo(titulo)
    assert ident(titulo) is None


def test_desodorante_colonia_nao_e_lixo():
    assert not e_lixo("Malbec Desodorante Colônia 100ml")
    assert not e_lixo("Kaiak Desodorante Colônia Masculino 100 ml")


def test_marca_pode_vir_do_campo_da_loja():
    # VTEX manda a marca separada; o título às vezes não tem
    assert identificar("Khamrah Eau de Parfum 100ml", CAT, marca_loja="Lattafa").id == "lattafa-khamrah"
    assert identificar("Khamrah Eau de Parfum 100ml", CAT) is None


def test_genero_oposto_descarta():
    ok, motivo = casar(POR_ID["dior-sauvage-edp"], "Sauvage Dior EDP Feminino 100ml")
    assert not ok and motivo == "feminino"


@pytest.mark.parametrize(
    "texto, ml",
    [("Sauvage 100ml", 100), ("Malbec 100 ML", 100), ("Khamrah EDP 100 ml", 100), ("Good Girl 80ML", 80),
     ("Elixir 7,5ml", 7.5), ("sem tamanho", None)],
)
def test_extrair_ml(texto, ml):
    assert extrair_ml(texto) == ml


def test_referencia_escala_com_tamanho():
    edp = POR_ID["dior-sauvage-edp"]
    assert edp.referencia_para(100) == 850
    assert 850 * 0.6 < edp.referencia_para(60) < 850  # 60 ml custa menos, mas não proporcional
    assert edp.referencia_para(200) < 850 * 2  # frasco grande sai mais barato por ml
    assert edp.preco_bom_para(100) == 640
    assert edp.nome_exibicao == "Sauvage EDP"


def test_estilo_pela_marca():
    from scentinel.catalogo import estilo_da_marca

    assert estilo_da_marca("Lattafa") == "arabe"
    assert estilo_da_marca(None, "Perfume Maison Alhambra Kismet Masculino EDP 100ml") == "arabe"
    assert estilo_da_marca("O Boticário") == "nacional"
    assert estilo_da_marca("Christian Dior") == "designer"
    assert estilo_da_marca("Parfums de Marly") == "nicho"
    assert estilo_da_marca("Viktor&Rolf") == "designer"
    assert estilo_da_marca("Marca Desconhecida", "Perfume Xyz Masculino 100ml") is None
    assert estilo_da_marca(None, "Perfume Natural de Lavanda 100ml") is None  # 'natura' não casa com 'natural'
    # marca de várias palavras não vira uma palavra solta ('my perfumes' não é 'my'; 'le labo' não é 'labo')
    assert estilo_da_marca("Xpto", "Perfume My Way Xpto Masculino 100ml") is None
    assert estilo_da_marca("My Perfumes") == "arabe" and estilo_da_marca("Le Labo") == "nicho"
    assert estilo_da_marca(None, "Perfume Labo Xyz 100ml") is None
    assert estilo_da_marca("Bond No. 9") == "nicho" and estilo_da_marca("Penhaligon's") == "nicho"
