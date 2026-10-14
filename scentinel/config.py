"""Lê o config.toml (ajustes) e o .env (segredos)."""

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from scentinel.promo import Nivel

PASTA_PROJETO = Path(__file__).resolve().parent.parent
ARQ_CONFIG = PASTA_PROJETO / "config.toml"
ARQ_ENV = PASTA_PROJETO / ".env"
ARQ_DB = PASTA_PROJETO / "dados" / "scentinel.db"

# Educação com as lojas: ninguém baixa disso, nem pelo config.
INTERVALO_MIN_HORAS = 3
DELAY_MIN_S = 5

log = logging.getLogger("scentinel")


def _publico(valor):
    valor = str(valor or "").strip().lower()
    if valor in ("todos", "tudo", "all", ""):
        return "todos"
    if valor.startswith("masc"):
        return "masculino"
    log.warning("descoberta.publico = %r não existe (use \"masculino\" ou \"todos\"); usando masculino", valor)
    return "masculino"


@dataclass
class ConfigDescoberta:
    ativo: bool = True
    intervalo_horas: float = 6  # cada perfume é buscado em cada loja no máximo a cada X horas
    nivel_minimo: int = 2  # só posta a partir desse nível (2 = 20% abaixo do mercado)
    max_por_busca: int = 6  # quantos produtos de uma busca entram (os mais relevantes)
    max_alertas_por_hora: int = 10
    so_menor_preco: bool = True  # mesmo perfume e tamanho mais barato em outra loja -> não posta
    top_diario: str = "19:00"  # horário do 🏆 Top do dia ("" desliga)
    top_n: int = 5
    vitrine_minutos: float = 15  # posta a melhor oferta conhecida a cada X min (0 desliga)
    vitrine_sem_repetir_horas: float = 72  # a vitrine evita repetir o mesmo perfume nesse período
    varredura: bool = True  # varre as ofertas de perfume das lojas (estilo canal de promos)
    varredura_minutos: float = 60
    ml_min: float = 30  # frascos menores que isso (miniaturas) ficam de fora
    preco_min: float = 40
    desconto_min_rastrear: float = 10  # na varredura, só guarda perfume fora do catálogo com desconto >= isso
    so_oficial: bool = True  # só posta loja oficial / vendido pela própria loja
    max_everyone_por_dia: int = 2
    publico: str = "masculino"  # "masculino" (masculino + unissex) ou "todos"
    fontes: list = field(default_factory=list)


@dataclass
class Config:
    intervalo_horas: float = 3
    delay_min_s: float = 5
    delay_max_s: float = 15
    tick_minutos: float = 10
    nivel_minimo_aviso: int = 1
    nivel_minimo_por_perfume: dict = field(default_factory=dict)
    locate_rechecar_apos_horas: float = 6
    checar_forcado_min_minutos: float = 30
    fuso: str = "America/Sao_Paulo"
    ref_dias: int = 30
    ref_min_leituras: int = 5
    sanidade_queda_pct: float = 60
    sanidade_rechecar_apos_s: float = 120
    sanidade_tolerancia_pct: float = 2
    backoff_horas: float = 6
    backoff_max_horas: float = 48
    niveis: list = field(default_factory=list)
    descoberta: ConfigDescoberta = field(default_factory=ConfigDescoberta)

    def nivel_minimo_para(self, nome_perfume):
        from scentinel.busca import normalizar

        alvo = normalizar(nome_perfume)
        for nome, nivel in self.nivel_minimo_por_perfume.items():
            if normalizar(nome) == alvo:
                return int(nivel)
        return self.nivel_minimo_aviso


def _cor(valor):
    if isinstance(valor, int):
        return valor
    return int(str(valor).lstrip("#"), 16)


def carregar_config(caminho=ARQ_CONFIG):
    with open(caminho, "rb") as f:
        bruto = tomllib.load(f)

    geral = bruto.get("geral", {})
    ref = bruto.get("referencia", {})
    sanidade = bruto.get("sanidade", {})
    bloqueio = bruto.get("bloqueio", {})

    cfg = Config(
        intervalo_horas=geral.get("intervalo_horas", 3),
        delay_min_s=geral.get("delay_min_s", 5),
        delay_max_s=geral.get("delay_max_s", 15),
        tick_minutos=geral.get("tick_minutos", 10),
        nivel_minimo_aviso=geral.get("nivel_minimo_aviso", 1),
        locate_rechecar_apos_horas=geral.get("locate_rechecar_apos_horas", 6),
        checar_forcado_min_minutos=geral.get("checar_forcado_min_minutos", 30),
        fuso=geral.get("fuso", "America/Sao_Paulo"),
        nivel_minimo_por_perfume=bruto.get("nivel_minimo_por_perfume", {}),
        ref_dias=ref.get("dias", 30),
        ref_min_leituras=ref.get("min_leituras", 5),
        sanidade_queda_pct=sanidade.get("queda_pct", 60),
        sanidade_rechecar_apos_s=sanidade.get("rechecar_apos_s", 120),
        sanidade_tolerancia_pct=sanidade.get("tolerancia_pct", 2),
        backoff_horas=bloqueio.get("backoff_horas", 6),
        backoff_max_horas=bloqueio.get("backoff_max_horas", 48),
        niveis=[
            Nivel(
                nivel=int(n["nivel"]),
                nome=n["nome"],
                emoji=n.get("emoji", ""),
                min_pct=float(n["min_pct"]),
                cor=_cor(n.get("cor", "#95A5A6")),
                ping=bool(n.get("ping", False)),
                rechecar=bool(n.get("rechecar", False)),
                everyone=bool(n.get("everyone", False)),
            )
            for n in bruto.get("niveis", [])
        ],
    )

    d = bruto.get("descoberta", {})
    cfg.descoberta = ConfigDescoberta(
        ativo=bool(d.get("ativo", True)),
        intervalo_horas=max(float(d.get("intervalo_horas", 6)), INTERVALO_MIN_HORAS),
        nivel_minimo=int(d.get("nivel_minimo", 2)),
        max_por_busca=int(d.get("max_por_busca", 6)),
        max_alertas_por_hora=int(d.get("max_alertas_por_hora", 10)),
        so_menor_preco=bool(d.get("so_menor_preco", True)),
        top_diario=str(d.get("top_diario", "19:00")),
        top_n=int(d.get("top_n", 5)),
        vitrine_minutos=max(float(d.get("vitrine_minutos", 15)), 0),
        vitrine_sem_repetir_horas=max(float(d.get("vitrine_sem_repetir_horas", 72)), 1),
        varredura=bool(d.get("varredura", True)),
        varredura_minutos=max(float(d.get("varredura_minutos", 60)), 30),
        ml_min=float(d.get("ml_min", 30)),
        preco_min=float(d.get("preco_min", 40)),
        desconto_min_rastrear=float(d.get("desconto_min_rastrear", 10)),
        so_oficial=bool(d.get("so_oficial", True)),
        max_everyone_por_dia=int(d.get("max_everyone_por_dia", 2)),
        publico=_publico(d.get("publico", "masculino")),
        fontes=list(d.get("fontes", [])),
    )

    if cfg.intervalo_horas < INTERVALO_MIN_HORAS:
        log.warning("intervalo_horas < %s no config; usando %s", INTERVALO_MIN_HORAS, INTERVALO_MIN_HORAS)
        cfg.intervalo_horas = INTERVALO_MIN_HORAS
    if cfg.delay_min_s < DELAY_MIN_S:
        cfg.delay_min_s = DELAY_MIN_S
    cfg.delay_max_s = max(cfg.delay_max_s, cfg.delay_min_s)
    cfg.locate_rechecar_apos_horas = max(cfg.locate_rechecar_apos_horas, cfg.intervalo_horas)
    if not cfg.niveis:
        raise ValueError("config.toml sem [[niveis]]")
    return cfg


@dataclass
class Segredos:
    token: str
    guild_id: int
    canal_alertas_id: int
    dono_id: int


def carregar_segredos(caminho=ARQ_ENV):
    load_dotenv(caminho)
    faltando = [
        nome
        for nome in ("DISCORD_TOKEN", "GUILD_ID", "CANAL_ALERTAS_ID", "DONO_ID")
        if not os.environ.get(nome)
    ]
    if faltando:
        raise SystemExit(f"❌ Falta no .env: {', '.join(faltando)} (veja o .env.exemplo)")
    try:
        return Segredos(
            token=os.environ["DISCORD_TOKEN"].strip(),
            guild_id=int(os.environ["GUILD_ID"]),
            canal_alertas_id=int(os.environ["CANAL_ALERTAS_ID"]),
            dono_id=int(os.environ["DONO_ID"]),
        )
    except ValueError:
        raise SystemExit("❌ GUILD_ID, CANAL_ALERTAS_ID e DONO_ID no .env têm que ser números")
