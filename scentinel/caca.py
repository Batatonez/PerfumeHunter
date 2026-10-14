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

from scentinel import busca, catalogo, coleta, db, lojas, promo
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
        self.lock = asyncio.Lock()  # estado dos anúncios: um processamento por vez
        self._rede = asyncio.Lock()  # rede: uma requisição por vez, sempre (todos os loops)
        self._ultimo_request = None
        self.catalogo = {}  # id -> catalogo.ItemCatalogo (preenchido pela busca automática)

    # ------------------------------------------------------------
    # rede
    # ------------------------------------------------------------

    async def na_vez(self, funcao, *args):
        """Roda uma requisição (função síncrona) numa thread, respeitando o delay
        aleatório desde a requisição anterior. Vale pra página, busca e API, de todos os loops."""
        async with self._rede:
            if self._ultimo_request is not None:
                alvo = random.uniform(self.cfg.delay_min_s, self.cfg.delay_max_s)
                espera = alvo - (time.monotonic() - self._ultimo_request)
                if espera > 0:
                    await self.dormir(espera)
            try:
                return await asyncio.to_thread(funcao, *args)
            finally:
                self._ultimo_request = time.monotonic()

    async def _baixar(self, anuncio):
        return await self.na_vez(self.baixar, anuncio["url"], anuncio["seletor"])

    # ------------------------------------------------------------
    # avaliação
    # ------------------------------------------------------------

    def _historico(self, anuncio_id, quando):
        desde = quando - timedelta(days=self.cfg.ref_dias)
        return db.precos_validos(self.con, anuncio_id, desde, ate=quando)

    def _item(self, anuncio):
        return self.catalogo.get(anuncio.get("catalogo_id")) if anuncio.get("catalogo_id") else None

    def _refs_descoberta(self, anuncio, preco, quando):
        """(ref_catalogo, ref_mercado, n_lojas) pra um anúncio da busca automática.
        Mercado = a OUTRA loja oficial mais barata pro mesmo perfume e tamanho (últimas 48 h),
        só quando pelo menos 2 outras lojas têm o produto: com uma só, um vendedor caro
        bastaria pra inventar desconto. Vários vendedores da mesma loja contam como uma loja.
        O preço do catálogo é estimativa: se o mercado confirma que ele está baixo, sobe até x1,25."""
        if (anuncio.get("origem") or "manual") != "descoberta" or not anuncio.get("catalogo_id") or not anuncio.get("ml"):
            return None, None, 0
        item = self._item(anuncio)
        lojas = db.menores_por_loja(
            self.con, anuncio["catalogo_id"], anuncio["ml"], quando - timedelta(hours=48),
            exceto_id=anuncio["id"], exceto_loja=anuncio.get("loja"), so_oficial=True,
        )
        ref_mercado = min(lojas.values()) if len(lojas) >= 2 else None
        ref_catalogo = item.referencia_para(anuncio["ml"]) if item else None
        if ref_catalogo and ref_mercado and ref_mercado > ref_catalogo:
            ref_catalogo = min(round(ref_catalogo * 1.25, 2), ref_mercado)
        return ref_catalogo, ref_mercado, len(lojas)

    def _limitar_nivel(self, avaliacao, nivel_max):
        """Referência fraca não pode virar nível alto (nem marcar ninguém)."""
        if avaliacao.nivel and avaliacao.nivel.nivel > nivel_max:
            permitidos = [n for n in self.cfg.niveis if n.nivel <= nivel_max]
            avaliacao.nivel = max(permitidos, key=lambda n: n.nivel) if permitidos else None
        return avaliacao

    def _avaliar_preco(self, anuncio, leitura, quando):
        ref_catalogo, ref_mercado, _n = self._refs_descoberta(anuncio, leitura.preco, quando)
        historico = self._historico(anuncio["id"], quando)
        avaliacao = promo.avaliar(
            leitura.preco, leitura.preco_de, historico, self.cfg.niveis, self.cfg.ref_min_leituras,
            ref_catalogo, ref_mercado,
        )
        if (anuncio.get("origem") or "manual") == "descoberta":
            tem_historico = len(historico) >= self.cfg.ref_min_leituras
            if avaliacao.fonte == promo.FONTE_LOJA:
                # só o "de R$" da própria loja: ninguém confirmou -> no máximo nível 3, sem marcar
                avaliacao = self._limitar_nivel(avaliacao, 3)
            elif not tem_historico:
                # mercado/catálogo sem histórico do anúncio: pode marcar o dono, @everyone não
                nivel_everyone = min((n.nivel for n in self.cfg.niveis if n.everyone), default=None)
                if nivel_everyone:
                    avaliacao = self._limitar_nivel(avaliacao, nivel_everyone - 1)
        return avaliacao

    def estilo(self, anuncio):
        """Estilo do perfume: o do catálogo; fora dele, adivinhado pela marca (None se a marca é desconhecida)."""
        item = self._item(anuncio)
        if item:
            return item.categoria
        return catalogo.estilo_da_marca(anuncio.get("perfume_marca"), anuncio.get("titulo") or anuncio.get("perfume_nome"))

    def _oferta(self, anuncio, leitura, quando, avaliacao, bateu_alvo=False):
        estilo = self.estilo(anuncio)
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
            origem=anuncio.get("origem") or "manual",
            categoria=catalogo.CATEGORIAS.get(estilo, estilo),
            estilo=estilo,
            oficial=bool(anuncio.get("oficial")),
            preco_de=leitura.preco_de if leitura.preco_de and leitura.preco_de > leitura.preco else None,
            imagem=anuncio.get("imagem"),
        )

    def _avaliar(self, anuncio, leitura, quando):
        avaliacao = self._avaliar_preco(anuncio, leitura, quando)
        estado = promo.Estado(anuncio["nivel_avisado"], bool(anuncio["alvo_avisado"]))
        nivel_minimo = self.cfg.nivel_minimo_para(anuncio["perfume_nome"])
        if (anuncio.get("origem") or "manual") == "descoberta":
            nivel_minimo = max(nivel_minimo, self.cfg.descoberta.nivel_minimo)
        decisao = promo.decidir(
            leitura.preco, leitura.esgotado, avaliacao.nivel, anuncio["alvo"], estado, nivel_minimo,
        )
        return avaliacao, decisao

    @staticmethod
    def _linha(nome, oferta):
        av = oferta.avaliacao
        texto = f"{nome}: {formatar_reais(oferta.preco)}"
        if av.referencia:
            if av.pct >= 0.5:
                texto += f" (ref {formatar_reais(av.referencia)} −{formatar_pct(av.pct)}"
                texto += f" · nível {av.nivel.nivel})" if av.nivel else ")"
            else:
                texto += f" (ref {formatar_reais(av.referencia)}, sem desconto)"
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

        return await self.processar_leitura(
            anuncio, res.leitura, agora, avisar=avisar, rebaixar=lambda: self._baixar(anuncio)
        )

    def _everyone_esgotado(self, agora):
        nivel_everyone = min((n.nivel for n in self.cfg.niveis if n.everyone), default=None)
        if not nivel_everyone:
            return False
        feitos = db.alertas_de_nivel_desde(self.con, nivel_everyone, agora - timedelta(hours=24))
        return feitos >= self.cfg.descoberta.max_everyone_por_dia

    async def processar_leitura(self, anuncio, leitura, agora, avisar=True, rebaixar=None, filtro=None,
                                registrar=True):
        """Registra uma leitura e (se avisar=True) decide e manda o alerta.
        Serve pros anúncios cadastrados (página baixada) e pros achados na busca automática.
        rebaixar: função async que lê o preço de novo (sanidade); devolve um coleta.Resultado.
        filtro: função async (anuncio, oferta) -> motivo pra NÃO avisar agora (ou None).
        Se o filtro segurar, o estado não muda: o aviso pode sair numa rodada seguinte.
        registrar=False: reavalia sem gravar outra leitura igual (o preço não mudou)."""
        nome = f"{anuncio['perfume_nome']} @ {anuncio['loja']}"
        avaliacao, decisao = self._avaliar(anuncio, leitura, agora)

        # Sanidade: queda grande demais -> lê de novo antes de avisar
        if avisar and rebaixar and promo.precisa_rechecar(decisao, avaliacao.pct, self.cfg.sanidade_queda_pct):
            log.info("⏳ %s: queda de %s, rechecando em %ss", nome, formatar_pct(avaliacao.pct),
                     int(self.cfg.sanidade_rechecar_apos_s))
            await self.dormir(self.cfg.sanidade_rechecar_apos_s)
            res2 = await rebaixar()
            preco2 = res2.leitura.preco if res2 and res2.leitura else None
            if not promo.confirmou(leitura.preco, preco2, self.cfg.sanidade_tolerancia_pct):
                agora2 = self.relogio()
                if res2 and res2.leitura:
                    db.registrar_preco(self.con, anuncio["id"], res2.leitura, agora2)
                db.marcar_checado(self.con, anuncio["id"], quando=agora2)
                segundo = formatar_reais(preco2) if preco2 else "sem preço"
                return Checagem(
                    None,
                    f"⚠️ {nome}: queda de {formatar_pct(avaliacao.pct)} não confirmou "
                    f"({formatar_reais(leitura.preco)} → {segundo}), sem aviso",
                )

        menor_antes = db.menor_preco(self.con, anuncio["id"])
        if registrar:
            db.registrar_preco(self.con, anuncio["id"], leitura, agora)
        oferta = self._oferta(anuncio, leitura, agora, avaliacao, bateu_alvo=decisao.alvo)
        oferta.recorde = bool(menor_antes and leitura.preco < menor_antes and not leitura.esgotado)

        emoji = "🔵"
        avisou = False
        segurado = await filtro(anuncio, oferta) if (avisar and decisao.avisar and filtro) else None
        if segurado:
            emoji = f"⏭️ ({segurado})"
        elif avisar and decisao.avisar:
            quem = promo.mencao(decisao.nivel)
            if quem == promo.MENCAO_EVERYONE and self._everyone_esgotado(agora):
                quem = promo.MENCAO_DONO  # teto diário de @everyone: o resto do dia marca só o dono
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
        avaliacao = self._avaliar_preco(anuncio, leitura, quando)
        bateu = bool(anuncio["alvo"] and leitura.preco <= anuncio["alvo"])
        return self._oferta(anuncio, leitura, quando, avaliacao, bateu_alvo=bateu)

    def _precisa_atualizar(self, anuncio, agora):
        if (anuncio.get("origem") or "manual") != "manual":
            return False
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
