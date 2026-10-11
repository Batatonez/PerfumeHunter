"""Busca automática: o "canal de promos".

Pra cada perfume do catálogo e cada fonte (loja), busca, separa os anúncios que
são mesmo aquele perfume (catalogo.casar), salva como anúncio 'descoberta' e passa
a leitura pelo mesmo motor dos alertas (níveis, anti-spam, sanidade).
"""

import logging
from datetime import timedelta

from scentinel import busca, catalogo, db, promo
from scentinel.fontes import fonte_busca_item
from scentinel.coleta import Leitura, Resultado
from scentinel.formato import formatar_reais

log = logging.getLogger("scentinel")

ERROS_PRA_PAUSAR = 3  # erros seguidos numa loja -> pausa 24 h (ela provavelmente não tem essa busca)


class Descoberta:
    def __init__(self, cacador, itens, fontes, enviar_aviso=None):
        self.cacador = cacador
        self.con = cacador.con
        self.cfg = cacador.cfg.descoberta
        self.itens = list(itens)
        self.fontes = list(fontes)
        self.enviar_aviso = enviar_aviso
        self._fonte_pausada_ate = {}  # fonte.id -> datetime
        self._fonte_falhas = {}  # bloqueios seguidos
        self._fonte_erros = {}  # erros seguidos (loja fora do ar, endpoint que não existe...)
        cacador.catalogo = {i.id: i for i in self.itens}

    # ------------------------------------------------------------
    # o que buscar agora
    # ------------------------------------------------------------

    def pendentes(self, agora):
        """Pares (item, fonte) vencidos, os mais atrasados primeiro."""
        intervalo = timedelta(hours=self.cfg.intervalo_horas)
        fila = []
        for fonte in self.fontes:
            pausa = self._fonte_pausada_ate.get(fonte.id)
            if pausa and pausa > agora:
                continue
            for item in self.itens:
                if not fonte_busca_item(fonte, item):
                    continue
                ultima = db.ultima_busca(self.con, item.id, fonte.id)
                if ultima is None or agora - ultima >= intervalo:
                    fila.append((ultima.timestamp() if ultima else 0, item.id, item, fonte))
        fila.sort(key=lambda t: (t[0], t[1]))
        return [(item, fonte) for _, _, item, fonte in fila]

    # ------------------------------------------------------------
    # filtro: só posta o melhor e sem enxurrada
    # ------------------------------------------------------------

    async def _filtro(self, anuncio, oferta):
        agora = self.cacador.relogio()
        if db.alertas_desde(self.con, agora - timedelta(hours=1)) >= self.cfg.max_alertas_por_hora:
            return "limite de posts por hora"
        if self.cfg.so_menor_preco and anuncio.get("catalogo_id") and anuncio.get("ml"):
            outros = db.ofertas_recentes_do_item(
                self.con, anuncio["catalogo_id"], anuncio["ml"], agora - timedelta(hours=24), exceto_id=anuncio["id"]
            )
            mais_barato = min(outros, key=lambda o: o["preco"], default=None)
            if mais_barato and mais_barato["preco"] < oferta.preco:
                return f"mais barato em {mais_barato['loja']} ({formatar_reais(mais_barato['preco'])})"
        return None

    # ------------------------------------------------------------
    # uma busca
    # ------------------------------------------------------------

    def _perfume_do_item(self, item):
        nome = item.nome_exibicao
        nome_norm = busca.normalizar(nome)
        perfume = db.perfume_por_nome_norm(self.con, nome_norm)
        if perfume:
            return perfume
        return db.criar_perfume(
            self.con, nome, nome_norm, marca=item.marca, concentracao=item.concentracao,
            apelidos=";".join(item.busca),
        )

    def selecionar(self, item, achados):
        """Achados que são mesmo esse item, com tamanho conhecido. Devolve [(achado, ml)]."""
        escolhidos, vistos = [], set()
        for a in achados:
            if not a.url or a.url in vistos or not a.preco:
                continue
            ok, _motivo = catalogo.casar(item, a.titulo, a.marca, self.itens)
            if not ok:
                continue
            ml = a.ml or catalogo.extrair_ml(a.titulo, a.variacao or "")
            if not ml:
                continue  # sem tamanho não dá pra comparar preço
            if item.ml and ml not in item.ml:
                continue
            vistos.add(a.url)
            escolhidos.append((a, ml))
            if len(escolhidos) >= self.cfg.max_por_busca:
                break
        return escolhidos

    async def _buscar(self, fonte, termo):
        return await self.cacador.na_vez(fonte.buscar, termo)

    async def buscar_item(self, item, fonte):
        """Busca um item numa fonte e processa o que achar. Devolve linhas de log."""
        termo = item.termos_busca[0]
        res = await self._buscar(fonte, termo)
        agora = self.cacador.relogio()

        if res.bloqueado:
            falhas = self._fonte_falhas.get(fonte.id, 0) + 1
            self._fonte_falhas[fonte.id] = falhas
            horas = min(self.cacador.cfg.backoff_horas * 2 ** (falhas - 1), self.cacador.cfg.backoff_max_horas)
            self._fonte_pausada_ate[fonte.id] = agora + timedelta(hours=horas)
            db.registrar_busca(self.con, item.id, fonte.id, agora, erro=res.erro)
            if falhas == 1 and self.enviar_aviso:
                await self.enviar_aviso(
                    f"🚫 **{fonte.nome}** bloqueou a busca ({res.erro}). Pausei essa loja por {horas:g} h, sem forçar."
                )
            return [f"🚫 {fonte.nome}: {res.erro} (pausada {horas:g} h)"]
        self._fonte_falhas.pop(fonte.id, None)

        if res.erro:
            db.registrar_busca(self.con, item.id, fonte.id, agora, erro=res.erro)
            erros = self._fonte_erros.get(fonte.id, 0) + 1
            self._fonte_erros[fonte.id] = erros
            if erros >= ERROS_PRA_PAUSAR:
                self._fonte_pausada_ate[fonte.id] = agora + timedelta(hours=24)
                self._fonte_erros[fonte.id] = 0
                return [f"⏸️ {fonte.nome}: {erros} erros seguidos ({res.erro}), pausada por 24 h"]
            return [f"❌ {item.nome_exibicao} @ {fonte.nome}: {res.erro}"]
        self._fonte_erros.pop(fonte.id, None)

        escolhidos = self.selecionar(item, res.achados)
        db.registrar_busca(self.con, item.id, fonte.id, agora, achados=len(escolhidos))
        if not escolhidos:
            return [f"· {item.nome_exibicao} @ {fonte.nome}: nada ({len(res.achados)} resultados)"]

        perfume = self._perfume_do_item(item)
        linhas = []
        for achado, ml in escolhidos:
            anuncio = db.salvar_anuncio_descoberto(
                self.con, perfume["id"], achado.loja, achado.url, ml, item.preco_bom_para(ml),
                item.id, fonte.id, achado.titulo, achado.oficial,
            )
            leitura = Leitura(achado.preco, f"busca:{fonte.id}", not achado.disponivel,
                              achado.preco_de, achado.preco_pix)

            async def rebuscar(url=achado.url):
                r = await self._buscar(fonte, termo)
                for a in r.achados:
                    if a.url == url:
                        return Resultado(leitura=Leitura(a.preco, f"busca:{fonte.id}", not a.disponivel,
                                                         a.preco_de, a.preco_pix))
                return Resultado(erro=r.erro or "sumiu da busca")

            chk = await self.cacador.processar_leitura(
                anuncio, leitura, agora, avisar=True, rebaixar=rebuscar, filtro=self._filtro
            )
            linhas.append(chk.linha)
        return linhas

    # ------------------------------------------------------------
    # rodada
    # ------------------------------------------------------------

    async def rodada(self, max_buscas=None):
        if not self.cfg.ativo or not self.fontes or not self.itens:
            return []
        pendentes = self.pendentes(self.cacador.relogio())
        if max_buscas:
            pendentes = pendentes[:max_buscas]
        if pendentes:
            log.info("🔎 Busca automática: %d busca(s) pendente(s)", len(pendentes))
        linhas = []
        for item, fonte in pendentes:
            pausa = self._fonte_pausada_ate.get(fonte.id)
            if pausa and pausa > self.cacador.relogio():
                continue
            try:
                async with self.cacador.lock:  # uma busca por vez; /add e /locate entram entre elas
                    novas = await self.buscar_item(item, fonte)
            except Exception:
                log.exception("erro buscando %s em %s", item.id, fonte.id)
                continue
            for linha in novas:
                log.info(linha)
            linhas += novas
        return linhas

    # ------------------------------------------------------------
    # 🏆 Top do dia
    # ------------------------------------------------------------

    def top(self, n=None, horas=24):
        """Melhores ofertas atuais (uma por perfume), maior desconto primeiro."""
        n = n or self.cfg.top_n
        agora = self.cacador.relogio()
        melhores = {}
        for anuncio in db.anuncios_descobertos_recentes(self.con, agora - timedelta(hours=horas)):
            oferta = self.cacador.oferta_atual(anuncio)
            if not oferta or oferta.esgotado or not oferta.avaliacao.nivel:
                continue
            chave = anuncio.get("catalogo_id") or anuncio["perfume_id"]
            atual = melhores.get(chave)
            if atual is None or promo.chave_ranking(oferta.avaliacao.pct, oferta.preco) < promo.chave_ranking(
                atual.avaliacao.pct, atual.preco
            ):
                melhores[chave] = oferta
        return sorted(melhores.values(), key=lambda o: promo.chave_ranking(o.avaliacao.pct, o.preco))[:n]
