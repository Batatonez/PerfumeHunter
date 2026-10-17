"""/atualizar pelo Discord: git pull + testes, sem nunca deixar código quebrado rodando."""

from scentinel.atualizar import atualizar


class Shell:
    """Finge o git/pip/pytest: cada comando devolve o que foi programado."""

    def __init__(self, antes="aaa", depois="bbb", testes_ok=True, mudou=(), pull_ok=True):
        self.heads = [antes, depois]
        self.testes_ok, self.mudou, self.pull_ok = testes_ok, set(mudou), pull_ok
        self.comandos = []

    def __call__(self, cmd, pasta, tempo_s=180):
        self.comandos.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        if cmd[:2] == ["git", "pull"]:
            return (0, "") if self.pull_ok else (1, "fatal: Not possible to fast-forward")
        if cmd[:2] == ["git", "log"]:
            return 0, "bbb Top da semana\nccc % OFF"
        if cmd[:2] == ["git", "diff"]:
            return (1 if cmd[-1] in self.mudou else 0), ""
        if "pytest" in cmd:
            return (0, "300 passed in 12s") if self.testes_ok else (1, "1 failed, 299 passed")
        return 0, ""


def test_nada_novo():
    res = atualizar(rodar=Shell(antes="aaa", depois="aaa"))
    assert res.ok and not res.reiniciar and "versão mais nova" in res.texto


def test_atualiza_testa_e_reinicia():
    sh = Shell(mudou={"requirements.txt"})
    res = atualizar(rodar=sh)
    assert res.ok and res.reiniciar
    assert "• Top da semana" in res.texto and "300 passed" in res.texto and "Dependências novas" in res.texto
    assert any("pip" in c for c in sh.comandos)


def test_testes_falharam_volta_o_codigo():
    sh = Shell(testes_ok=False)
    res = atualizar(rodar=sh)
    assert not res.ok and not res.reiniciar and "Voltei pro código de antes" in res.texto
    assert ["git", "reset", "-q", "--keep", "aaa"] in sh.comandos


def test_servico_mudou_avisa_pra_rodar_no_notebook():
    res = atualizar(rodar=Shell(mudou={"scentinel.service"}))
    assert res.reiniciar and "scentinel-update --reiniciar" in res.texto


def test_pull_falhou():
    res = atualizar(rodar=Shell(pull_ok=False))
    assert not res.ok and "git pull não deu" in res.texto


def test_comando_so_do_dono_e_reinicia():
    import asyncio
    from types import SimpleNamespace

    from scentinel import atualizar as mod
    from scentinel import config, db
    from scentinel.atualizar import ResultadoAtualizacao
    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 42), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    respostas, edicoes, fechou = [], [], []

    async def responder(texto, **kw):
        respostas.append(texto)

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw["content"])

    async def fechar():
        fechou.append(True)

    bot.close = fechar
    original = mod.atualizar
    mod.atualizar = lambda rodando=None: ResultadoAtualizacao(True, reiniciar=True, linhas=["📦 Novidades:"])
    try:
        def interacao(uid):
            return SimpleNamespace(client=bot, user=SimpleNamespace(id=uid), edit_original_response=editar,
                                   response=SimpleNamespace(send_message=responder, defer=defer))

        cmd = bot.tree.get_command("atualizar")
        asyncio.run(cmd.callback(interacao(7)))
        assert "Só o dono" in respostas[-1] and not fechou
        asyncio.run(cmd.callback(interacao(42)))
        assert "Reiniciando" in edicoes[-1] and fechou and bot.reiniciar
    finally:
        mod.atualizar = original


def test_codigo_baixado_mas_nao_reiniciado_reinicia_agora():
    """O /atualizar anterior baixou o código e o Discord caiu antes de reiniciar: o próximo termina o serviço."""
    sh = Shell(antes="bbb", depois="bbb")
    res = atualizar(rodar=sh, rodando="aaa")
    assert res.ok and res.reiniciar and "ainda rodava a versão antiga" in res.texto
    assert ["git", "diff", "--quiet", "aaa", "bbb", "--", "requirements.txt"] in sh.comandos
    # e com teste falhando: não reinicia, e o código parado no disco volta pro que está rodando
    sh = Shell(antes="bbb", depois="bbb", testes_ok=False)
    res = atualizar(rodar=sh, rodando="aaa")
    assert not res.reiniciar and ["git", "reset", "-q", "--keep", "aaa"] in sh.comandos


def test_reset_que_falha_avisa():
    class ShellResetRuim(Shell):
        def __call__(self, cmd, pasta, tempo_s=180):
            if cmd[:2] == ["git", "reset"]:
                return 1, "error: Entry 'x' not uptodate. Cannot merge."
            return super().__call__(cmd, pasta, tempo_s)

    res = atualizar(rodar=ShellResetRuim(testes_ok=False))
    assert not res.reiniciar and "não consegui voltar o código" in res.texto


def test_registra_a_versao_rodando(tmp_path):
    from scentinel.atualizar import registrar_versao

    arq = tmp_path / "versao"
    assert registrar_versao(rodar=Shell(antes="abc123"), arq=arq) == "abc123"
    assert arq.read_text().strip() == "abc123"


def test_reinicia_mesmo_se_o_discord_falhar_na_resposta():
    import asyncio
    from types import SimpleNamespace

    import discord

    from scentinel import atualizar as mod
    from scentinel import config, db
    from scentinel.atualizar import ResultadoAtualizacao
    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 42), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    fechou = []

    async def defer(**kw):
        pass

    async def editar_falha(**kw):
        raise discord.HTTPException(SimpleNamespace(status=401, reason="Unauthorized"), "Invalid Webhook Token")

    async def fechar():
        fechou.append(True)

    bot.close = fechar
    original = mod.atualizar
    mod.atualizar = lambda rodando=None: ResultadoAtualizacao(True, reiniciar=True, linhas=["📦"])
    try:
        interacao = SimpleNamespace(client=bot, user=SimpleNamespace(id=42), edit_original_response=editar_falha,
                                    response=SimpleNamespace(defer=defer))
        asyncio.run(bot.tree.get_command("atualizar").callback(interacao))
    finally:
        mod.atualizar = original
    assert fechou and bot.reiniciar


def test_atualizacao_interrompida_volta_o_codigo(tmp_path):
    """O bot reiniciou no meio do /atualizar (queda de luz, restart): ao ligar, volta pro commit de antes."""
    from scentinel.atualizar import retomar_atualizacao_interrompida

    pendente = tmp_path / "atualizacao_pendente"
    sh = Shell(antes="bbb")  # o HEAD agora é o código novo (não testado)
    assert not retomar_atualizacao_interrompida(rodar=sh, pendente=pendente)  # sem marcador: nada
    pendente.write_text("aaa\n")
    assert retomar_atualizacao_interrompida(rodar=sh, pendente=pendente)
    assert ["git", "reset", "-q", "--keep", "aaa"] in sh.comandos and not pendente.exists()


def test_marcador_some_quando_o_atualizar_termina(tmp_path):
    pendente = tmp_path / "atualizacao_pendente"
    vistos = []

    class ShellQueOlha(Shell):
        def __call__(self, cmd, pasta, tempo_s=180):
            if cmd[:2] == ["git", "pull"]:
                vistos.append(pendente.read_text().strip())  # durante o pull o marcador existe
            return super().__call__(cmd, pasta, tempo_s)

    atualizar(rodar=ShellQueOlha(testes_ok=False), pendente=pendente)
    assert vistos == ["aaa"] and not pendente.exists()


def test_teste_falhou_com_codigo_parado_volta_pro_que_esta_rodando():
    """Nada novo no pull, mas o código no disco (não rodando) falha nos testes: volta pro que o bot roda,
    senão ele liga no próximo restart."""
    sh = Shell(antes="bbb", depois="bbb", testes_ok=False)
    res = atualizar(rodar=sh, rodando="aaa")
    assert not res.reiniciar and ["git", "reset", "-q", "--keep", "aaa"] in sh.comandos


def test_pull_com_arquivo_mexido_explica():
    class ShellMexido(Shell):
        def __call__(self, cmd, pasta, tempo_s=180):
            if cmd[:2] == ["git", "pull"]:
                return 1, "error: Your local changes to the following files would be overwritten by merge:\n\tconfig.toml"
            return super().__call__(cmd, pasta, tempo_s)

    res = atualizar(rodar=ShellMexido())
    assert "config.local.toml" in res.texto


def test_config_local_por_cima_e_testes_nao_veem(tmp_path):
    from scentinel import config

    base = config.ARQ_CONFIG
    local = tmp_path / "config.local.toml"
    local.write_text('[descoberta]\nvitrine_minutos = 30\ntop_n = 3\n', encoding="utf-8")
    cfg = config.carregar_config(base, local)
    assert cfg.descoberta.vitrine_minutos == 30 and cfg.descoberta.top_n == 3
    assert cfg.descoberta.publico == "masculino" and len(cfg.niveis) == 5  # o resto continua do config.toml
    assert config.carregar_config().descoberta.vitrine_minutos == 15  # nos testes, o config.local.toml não existe
