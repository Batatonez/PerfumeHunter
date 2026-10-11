"""O caçador: checa anúncios, decide avisos, monta o /locate e o /add.

Não conhece o Discord. O bot passa duas funções:
  enviar_alerta(oferta, mencao) -> bool (manda o embed no canal; mencao: None/'dono'/'everyone')
  enviar_aviso(texto) -> None           (mensagem simples, ex.: bloqueio)
Isso deixa testar tudo com funções falsas.
"""

import asyncio
import logging
import random
import re
import statistics
import time
from dataclasses import dataclass
from datetime import timedelta

from scentinel import busca, coleta, db, lojas, promo
from scentinel.formato import formatar_pct, formatar_reais

log = logging.getLogger("scentinel")


@dataclass
class Checagem:
    oferta: promo.Oferta | None
    linha: str  # uma linha pro log
    erro: str | None = None
    avisou: bool = False


@dataclass
class RespostaLocate:
    melhor: promo.Oferta | None = None
    em_promo: bool = False
    outros: list | None = None
    menor_por_ml: promo.Oferta | None = None
    outras_variacoes: list | None = None
    texto: str | None = None  # quando não tem embed pra mostrar


def limpar_nome(nome):
    """Tira o tamanho do nome ('Sauvage EDP 100ml' -> 'Sauvage EDP'): o ml fica no anúncio."""
    nome = re.sub(r"\s*\d+([.,]\d+)?\s*ml\b", "", nome, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", nome).strip(" -·")


class Cacador:
    def __init__(self, con, cfg, enviar_alerta=None, enviar_aviso=None,
                 baixar=coleta.checar_url, dormir=asyncio.sleep, relogio=db.agora_utc):
        self.con = con
        self.cfg = cfg
        self.enviar_alerta = enviar_alerta
        self.enviar_aviso = enviar_aviso
        self.baixar = baixar
        self.dormir = dormir
        self.relogio = relogio
        self.lock = asyncio.Lock()  # uma requisição por vez, sempre
        self._ultimo_request = None

    # ------------------------------------------------------------
    # rede
    # ------------------------------------------------------------

    async def _baixar(self, anuncio):
        """Baixa numa thread, respeitando o delay aleatório entre requisições."""
        if self._ultimo_request is not None:
            alvo = random.uniform(self.cfg.delay_min_s, self.cfg.delay_max_s)
            espera = alvo - (time.monotonic() - self._ultimo_request)
            if espera > 0:
                await self.dormir(espera)
        try:
            return await asyncio.to_thread(self.baixar, anuncio["url"], anuncio["seletor"])
        finally:
            self._ultimo_request = time.monotonic()

    # ------------------------------------------------------------
    # avaliação
    # ------------------------------------------------------------

    def _historico(self, anuncio_id, quando):
        desde = quando - timedelta(days=self.cfg.ref_dias)
        return db.precos_validos(self.con, anuncio_id, desde, ate=quando)

    def _oferta(self, anuncio, leitura, quando, avaliacao, bateu_alvo=False):
        return promo.Oferta(
            anuncio_id=anuncio["id"],
            perfume=anuncio["perfume_nome"],
            loja=anuncio["loja"],
            url=anuncio["url"],
            ml=anuncio["ml"],
            preco=leitura.preco,
            avaliacao=avaliacao,
            preco_pix=leitura.preco_pix,
            esgotado=leitura.esgotado,
            menor_ja_visto=db.menor_preco(self.con, anuncio["id"]),
            alvo=anuncio["alvo"],
            bateu_alvo=bateu_alvo,
            quando=quando,
        )

    def _avaliar(self, anuncio, leitura, quando):
        avaliacao = promo.avaliar(
            leitura.preco, leitura.preco_de, self._historico(anuncio["id"], quando),
            self.cfg.niveis, self.cfg.ref_min_leituras,
        )
        estado = promo.Estado(anuncio["nivel_avisado"], bool(anuncio["alvo_avisado"]))
        decisao = promo.decidir(
            leitura.preco, leitura.esgotado, avaliacao.nivel, anuncio["alvo"], estado,
            self.cfg.nivel_minimo_para(anuncio["perfume_nome"]),
        )
        return avaliacao, decisao

    @staticmethod
    def _linha(nome, oferta):
        av = oferta.avaliacao
        texto = f"{nome}: {formatar_reais(oferta.preco)}"
        if av.referencia:
            texto += f" (ref {formatar_reais(av.referencia)} −{formatar_pct(max(av.pct, 0))}"
            texto += f" · nível {av.nivel.nivel})" if av.nivel else ")"
        if oferta.esgotado:
            texto += " ESGOTADO"
        return texto

    # ------------------------------------------------------------
    # checar um anúncio
    # ------------------------------------------------------------

    async def checar_anuncio(self, anuncio, avisar=True):
        """Baixa, registra e (se avisar=True) decide e manda o alerta.
        Quem chama tem que estar segurando self.lock."""
        anuncio = db.anuncio_por_id(self.con, anuncio["id"])  # estado fresco
        if not anuncio:
            return Checagem(None, "anúncio removido", "removido")
        nome = f"{anuncio['perfume_nome']} @ {anuncio['loja']}"

        res = await self._baixar(anuncio)
        agora = self.relogio()

        if res.bloqueado:
            primeiro = db.marcar_bloqueado(
                self.con, anuncio["id"], self.cfg.backoff_horas, self.cfg.backoff_max_horas, agora
            )
            if primeiro and self.enviar_aviso:
                await self.enviar_aviso(
                    f"🚫 **{nome}**: a loja bloqueou ({res.erro}). Vou dar um tempo e tentar "
                    "de novo mais tarde, sem forçar. Se continuar, talvez seja melhor remover."
                )
            return Checagem(None, f"🚫 {nome}: {res.erro}", res.erro)

        if res.erro:
            db.marcar_checado(self.con, anuncio["id"], erro=res.erro, quando=agora)
            return Checagem(None, f"❌ {nome}: {res.erro}", res.erro)

        if anuncio["bloqueado"]:
            db.limpar_bloqueio(self.con, anuncio["id"])

        leitura = res.leitura
        avaliacao, decisao = self._avaliar(anuncio, leitura, agora)

        # Sanidade: queda grande demais -> baixa de novo antes de avisar
        if avisar and promo.precisa_rechecar(decisao, avaliacao.pct, self.cfg.sanidade_queda_pct):
            log.info("⏳ %s: queda de %s, rechecando em %ss", nome, formatar_pct(avaliacao.pct),
                     int(self.cfg.sanidade_rechecar_apos_s))
            await self.dormir(self.cfg.sanidade_rechecar_apos_s)
            res2 = await self._baixar(anuncio)
            preco2 = res2.leitura.preco if res2.leitura else None
            if not promo.confirmou(leitura.preco, preco2, self.cfg.sanidade_tolerancia_pct):
                agora2 = self.relogio()
                if res2.leitura:
                    db.registrar_preco(self.con, anuncio["id"], res2.leitura, agora2)
                db.marcar_checado(self.con, anuncio["id"], quando=agora2)
                segundo = formatar_reais(preco2) if preco2 else "sem preço"
                return Checagem(
                    None,
                    f"⚠️ {nome}: queda de {formatar_pct(avaliacao.pct)} não confirmou "
                    f"({formatar_reais(leitura.preco)} → {segundo}), sem aviso",
                )

        menor_antes = db.menor_preco(self.con, anuncio["id"])
        db.registrar_preco(self.con, anuncio["id"], leitura, agora)
        oferta = self._oferta(anuncio, leitura, agora, avaliacao, bateu_alvo=decisao.alvo)
        oferta.recorde = bool(menor_antes and leitura.preco < menor_antes and not leitura.esgotado)

        emoji = "🔵"
        avisou = False
        if avisar and decisao.avisar:
            quem = promo.mencao(decisao.nivel)
            ok = await self.enviar_alerta(oferta, quem) if self.enviar_alerta else False
            if ok:
                novo = decisao.novo_estado
                db.salvar_estado(self.con, anuncio["id"], novo.nivel_avisado, novo.alvo_avisado)
                tipo = "+".join(t for t, sim in (("nivel", decisao.nivel), ("alvo", decisao.alvo)) if sim)
                db.registrar_alerta(
                    self.con, anuncio["id"], tipo,
                    decisao.nivel.nivel if decisao.nivel else None, leitura.preco, agora,
                )
                emoji, avisou = "🔥", True
            else:
                emoji = "⚠️ (aviso falhou)"
        elif avisar:
            novo = decisao.novo_estado
            db.salvar_estado(self.con, anuncio["id"], novo.nivel_avisado, novo.alvo_avisado)
            if avaliacao.nivel:
                emoji = "🟢"
        if leitura.esgotado:
            emoji = "⚪"

        db.marcar_checado(self.con, anuncio["id"], quando=agora)
        return Checagem(oferta, f"{emoji} {self._linha(nome, oferta)}", avisou=avisou)

    # ------------------------------------------------------------
    # rodada
    # ------------------------------------------------------------

    async def rodada(self, forcar=False):
        """Checa tudo que está vencido. forcar=True ignora o intervalo de 3 h,
        mas ainda pula quem foi checado nos últimos `checar_forcado_min_minutos`."""
        async with self.lock:
            if forcar:
                intervalo = timedelta(minutes=self.cfg.checar_forcado_min_minutos)
            else:
                intervalo = timedelta(hours=self.cfg.intervalo_horas)
            anuncios = db.anuncios_para_checar(self.con, self.relogio(), intervalo)
            if anuncios:
                log.info("🧴 Rodada: %d anúncio(s)", len(anuncios))
            checagens = []
            for anuncio in anuncios:
                try:
                    chk = await self.checar_anuncio(anuncio)
                except Exception:
                    log.exception("erro checando anúncio %s", anuncio["id"])
                    continue
                log.info(chk.linha)
                checagens.append(chk)
            return checagens

    # ------------------------------------------------------------
    # /add
    # ------------------------------------------------------------

    async def adicionar(self, url, nome, ml=None, alvo=None, marca=None, seletor=None):
        """Cadastra e testa na hora. Devolve (anuncio, checagem).
        Levanta ValueError se a URL já existe."""
        if db.anuncio_por_url(self.con, url):
            raise ValueError("esse link já está cadastrado")
        nome = limpar_nome(nome)
        nome_norm = busca.normalizar(nome)
        if not nome_norm:
            raise ValueError("nome vazio")
        perfume = db.perfume_por_nome_norm(self.con, nome_norm) or db.criar_perfume(
            self.con, nome, nome_norm, marca=marca, concentracao=busca.detectar_concentracao(nome)
        )
        anuncio = db.criar_anuncio(
            self.con, perfume["id"], lojas.nome_da_loja(url), url, ml=ml, seletor=seletor, alvo=alvo
        )
        async with self.lock:
            chk = await self.checar_anuncio(anuncio, avisar=False)
        log.info("➕ %s", chk.linha)
        return db.anuncio_por_id(self.con, anuncio["id"]), chk

    # ------------------------------------------------------------
    # /locate
    # ------------------------------------------------------------

    def oferta_atual(self, anuncio):
        """Oferta montada a partir da última leitura salva (sem baixar nada)."""
        linha = db.ultima_leitura(self.con, anuncio["id"])
        if not linha:
            return None
        quando = db.de_texto(linha["quando"])
        leitura = coleta.Leitura(
            linha["preco"], linha["metodo"], bool(linha["esgotado"]), linha["preco_de"], linha["preco_pix"]
        )
        avaliacao = promo.avaliar(
            leitura.preco, leitura.preco_de, self._historico(anuncio["id"], quando),
            self.cfg.niveis, self.cfg.ref_min_leituras,
        )
        bateu = bool(anuncio["alvo"] and leitura.preco <= anuncio["alvo"])
        return self._oferta(anuncio, leitura, quando, avaliacao, bateu_alvo=bateu)

    def _precisa_atualizar(self, anuncio, agora):
        if anuncio["bloqueado_ate"] and db.de_texto(anuncio["bloqueado_ate"]) > agora:
            return False
        ultima = db.de_texto(anuncio["ultima_checagem"])
        limite = timedelta(hours=self.cfg.locate_rechecar_apos_horas)
        return ultima is None or agora - ultima >= limite

    async def locate(self, consulta, ao_rechecar=None, marca=None):
        achados = busca.buscar(consulta, db.listar_perfumes(self.con), marca=marca)
        if not achados:
            alvo = f"**{consulta}**" + (f" da marca **{marca}**" if marca else "")
            return RespostaLocate(texto=f"Não achei {alvo} nos perfumes vigiados. Use `/add` pra começar a vigiar.")

        perfumes = [p for p, _ in achados]
        anuncios = [a for p in perfumes for a in db.listar_anuncios(self.con, p["id"], so_ativos=True)]
        agora = self.relogio()
        velhos = [a for a in anuncios if self._precisa_atualizar(a, agora)]

        if velhos and not self.lock.locked():
            if ao_rechecar:
                await ao_rechecar(len(velhos))
            async with self.lock:
                for anuncio in velhos:
                    try:
                        chk = await self.checar_anuncio(anuncio)
                        log.info("🔎 %s", chk.linha)
                    except Exception:
                        log.exception("erro rechecando anúncio %s", anuncio["id"])

        ofertas = [o for o in (self.oferta_atual(a) for a in anuncios) if o]
        if not ofertas:
            nomes = ", ".join(p["nome"] for p in perfumes)
            return RespostaLocate(
                texto=f"Achei **{nomes}**, mas ainda não tenho nenhuma leitura de preço. "
                "Tenta de novo mais tarde (ou `/checar`)."
            )

        melhor, em_promo, outros, menor_ml = ranquear(ofertas)
        variacoes = [p["nome"] for p in perfumes if p["nome"] != melhor.perfume]
        return RespostaLocate(melhor, em_promo, outros, menor_ml, variacoes)

    # ------------------------------------------------------------
    # /historico
    # ------------------------------------------------------------

    def historico(self, perfume_id):
        """Por anúncio: mín, máx, mediana dos últimos N dias e último preço."""
        agora = self.relogio()
        saida = []
        for anuncio in db.listar_anuncios(self.con, perfume_id):
            stats = db.estatisticas(self.con, anuncio["id"])
            recentes = db.precos_validos(self.con, anuncio["id"], agora - timedelta(days=self.cfg.ref_dias))
            saida.append({
                "anuncio": anuncio,
                "min": stats["min"],
                "max": stats["max"],
                "leituras": stats["leituras"],
                "mediana": statistics.median(recentes) if recentes else None,
                "ultima": db.ultima_leitura(self.con, anuncio["id"]),
            })
        return saida


def ranquear(ofertas, n_outros=3):
    """Devolve (melhor, em_promo, outros, menor_por_ml).
    Maior % de desconto vence; empate -> menor preço. Sem promo -> o mais barato."""
    disponiveis = [o for o in ofertas if not o.esgotado] or list(ofertas)

    def chave(o):
        pct = o.avaliacao.pct if o.avaliacao.referencia else -1000
        return promo.chave_ranking(pct, o.preco)

    em_promo = sorted((o for o in disponiveis if o.avaliacao.nivel), key=chave)
    if em_promo:
        melhor, tem_promo = em_promo[0], True
    else:
        melhor, tem_promo = min(disponiveis, key=lambda o: o.preco), False

    outros = sorted((o for o in disponiveis if o is not melhor), key=chave)[:n_outros]
    com_ml = [o for o in disponiveis if o.por_ml]
    menor_ml = min(com_ml, key=lambda o: o.por_ml) if com_ml else None
    return melhor, tem_promo, outros, menor_ml
