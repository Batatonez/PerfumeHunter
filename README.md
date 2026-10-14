![Scentinel](assets/banner-gato.png)

# 🐈‍⬛ Scentinel

*O gato de guarda dos perfumes.*

Bot de Discord que funciona como um **canal de promos de perfume** (estilo MDLinks, só perfume): de hora em hora varre as ofertas do **Mercado Livre (só lojas oficiais)** e posta **qualquer perfume masculino (ou unissex) em promo boa**, com foto, ~~preço antigo~~ → preço novo e nível de desconto. Manda um **🏆 Top do dia**, e o `/locate nome [marca]` pesquisa qualquer perfume nas lojas na hora. Também vigia links específicos que você cadastrar com `/add`.

Feito pra rodar 24/7 num notebook velho (3 GB de RAM): sem navegador headless, uma requisição por vez e uns 60 MB de RAM.

---

## Sumário

0. [Acessar o notebook pelo PC (SSH)](#0-acessar-o-notebook-pelo-pc-ssh)
1. [Instalação do zero](#1-instalação-do-zero)
2. [Criar o bot no Discord](#2-criar-o-bot-no-discord)
3. [Preencher o .env](#3-preencher-o-env)
4. [Testar](#4-testar)
5. [Ligar 24/7 com systemd](#5-ligar-247-com-systemd)
6. [Notebook: não dormir, tampa e Wi-Fi](#6-notebook-não-dormir-tampa-e-wi-fi)
7. [Comandos](#7-comandos)
8. [Níveis e regras de aviso](#8-níveis-e-regras-de-aviso)
8.5. [Canal de promos (busca automática)](#85-canal-de-promos-busca-automática)
9. [Quando uma loja não funciona](#9-quando-uma-loja-não-funciona)
10. [Dia a dia](#10-dia-a-dia)
11. [Migrar pro Ubuntu Server](#11-migrar-pro-ubuntu-server)

---

## 0. Acessar o notebook pelo PC (SSH)

Assim você controla o notebook do PC Windows, e o notebook pode ficar fechado num canto.

**No notebook** (uma vez, com teclado e tela). O `sudo` pede sua senha. Esses comandos instalam e ligam o servidor SSH, que é o programa que aceita a conexão do PC:

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
whoami          # seu usuário no notebook
hostname -I     # IP do notebook na rede (o primeiro número, tipo 192.168.0.15)
```

**No PC Windows** (PowerShell ou Terminal; o Windows 10/11 já vem com `ssh`):

```powershell
ssh SEU_USUARIO@IP_DO_NOTEBOOK
```

Na primeira vez ele pergunta se confia no notebook: digite `yes` e depois a senha do notebook. Pra sair, digite `exit`.

Opcional: entrar sem digitar senha. No PowerShell do PC:

```powershell
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh SEU_USUARIO@IP_DO_NOTEBOOK "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

> O IP pode mudar se o roteador reiniciar. Se o `ssh` parar de conectar, rode `hostname -I` no notebook de novo, ou reserve um IP fixo pro notebook nas configurações do roteador.

---

## 1. Instalação do zero

Funciona igual no Linux Mint e no Ubuntu Server. Precisa de Python 3.11 ou mais novo.

Primeiro, confira a máquina (nenhum desses comandos muda nada, só mostra informação):

```bash
cat /etc/os-release     # qual Linux é
python3 --version       # versão do Python (tem que ser 3.11+)
free -h                 # RAM livre
df -h                   # espaço em disco
```

Agora instale os pacotes do sistema. O `sudo` roda como administrador e vai pedir sua senha. Esse comando só **instala** o git, o venv do Python e o pip:

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip
```

O repo é **privado**, então o notebook precisa de uma chave SSH pra conseguir baixar. Crie uma chave **só pro Scentinel** (arquivo `~/.ssh/scentinel`, sem senha, pra o `git pull` funcionar sozinho). Ter um arquivo próprio evita sobrescrever outra chave que já exista no notebook, como a de outro projeto:

```bash
ssh-keygen -t ed25519 -C scentinel -f ~/.ssh/scentinel -N ""
cat ~/.ssh/scentinel.pub
```

Copie a linha que aparecer (começa com `ssh-ed25519`). No GitHub, vá no repo → **Settings → Deploy keys → Add deploy key**, cole a linha, dê o nome "notebook" e **deixe "Allow write access" desmarcado**. Assim essa chave só consegue *ler* este repo, e mais nada da sua conta.

Agora ensine o SSH a usar essa chave com um apelido (`github-scentinel`). Isso só **acrescenta** um bloco no fim do `~/.ssh/config`, sem mexer no que já existe lá:

```bash
printf '\nHost github-scentinel\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/scentinel\n  IdentitiesOnly yes\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
ssh -T github-scentinel     # 1ª vez: digite yes. Tem que responder "Hi Batatonez/Scentinel! You've successfully authenticated"
```

Baixe o projeto e rode o instalador. O instalador **não usa sudo**: ele só mexe dentro da pasta `~/scentinel`.

```bash
cd ~
git clone -b claude/new-session-uawklz github-scentinel:Batatonez/Scentinel.git scentinel
cd scentinel
./instalar.sh
```

O `instalar.sh` faz quatro coisas:
- cria o `.venv` (ambiente Python isolado);
- instala as dependências;
- roda os testes;
- cria o `.env` e gera o `dados/scentinel.service` com o seu usuário.

---

## 2. Criar o bot no Discord

1. Entre em <https://discord.com/developers/applications> e clique em **New Application**. Dê o nome de "Scentinel".
2. No menu **Bot**:
   - clique em **Reset Token** e copie o token (ele vai pro `.env`; **não mostre pra ninguém**);
   - **Privileged Gateway Intents** podem ficar todas desligadas (o bot não precisa delas);
   - em **Icon**, suba `assets/avatar.png`; em **Banner**, suba `assets/banner-gato.png` (ou `assets/banner.png`, sem o gato).
3. No menu **OAuth2 → General**, copie o **Client ID**.
4. Monte o link de convite trocando `CLIENT_ID` pelo Client ID que você copiou:

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=150528
   ```

   O `150528` dá ao bot só quatro permissões: ver canais, mandar mensagem, mandar embed (o card bonito) e mencionar @everyone (usada só no 💎 Bug de preço). Se o bot já estiver no servidor, abrir o link de novo e autorizar atualiza as permissões.
5. Abra o link, escolha o seu servidor e autorize.
6. (Opcional) Em **General Information**, coloque a descrição e as tags:

   ```
   🐈‍⬛ Scentinel, o gato de guarda dos perfumes.

   Vigio preços no Mercado Livre, Boticário, Natura e lojas de perfume 24h por dia e aviso aqui quando aparece promo de verdade, do 🟢 Cheirinho ao 💎 Bug de preço.

   🔎 /locate: acha a maior promo de um perfume
   📈 /historico: menor, maior e preço médio
   📋 /lista: tudo que tô vigiando
   ```

   Tags: `perfume`, `promoções`, `alerta de preço`, `ofertas`, `compras`.

---

## 3. Preencher o .env

Primeiro, ligue o **Modo Desenvolvedor** no Discord: Configurações → Avançado → Modo Desenvolvedor. É ele que faz aparecer a opção "Copiar ID" no botão direito.

```bash
nano ~/scentinel/.env
```

| Variável | Onde pegar |
|---|---|
| `DISCORD_TOKEN` | Developer Portal → Bot → Reset Token |
| `GUILD_ID` | botão direito no servidor → Copiar ID do servidor |
| `CANAL_ALERTAS_ID` | botão direito no canal dos alertas → Copiar ID do canal |
| `DONO_ID` | botão direito no seu nome → Copiar ID do usuário |

Sem aspas e sem espaço. Pra salvar no nano: `Ctrl+O`, `Enter` e depois `Ctrl+X` pra sair.

---

## 4. Testar

**Teste de leitura de preço** (não precisa do Discord):

```bash
cd ~/scentinel
.venv/bin/python -m scentinel testar "https://link-do-perfume"
```

Ele mostra o preço, o "de" riscado e o Pix. Se não achar o preço, veja a [seção 9](#9-quando-uma-loja-não-funciona).

**Bot ligado na mão** (pra ver funcionando antes do systemd):

```bash
.venv/bin/python -m scentinel
```

Quando aparecer `✅ Bot online`, teste no Discord:
- `/teste_alerta`: manda um alerta de mentira no canal;
- `/add`: cadastra um perfume de verdade.

Pra desligar, aperte `Ctrl+C`.

---

## 5. Ligar 24/7 com systemd

O systemd é quem liga programas no boot e religa se eles caírem. Os comandos abaixo usam `sudo` porque mexem na pasta de serviços do sistema, `/etc/systemd/system`.

```bash
cd ~/scentinel
sudo cp dados/scentinel.service /etc/systemd/system/scentinel.service   # registra o serviço
sudo systemctl daemon-reload                                       # avisa o systemd que tem serviço novo
sudo systemctl enable --now scentinel                                # liga agora E em todo boot
```

Pra conferir:

```bash
systemctl status scentinel        # tá rodando? (q pra sair)
journalctl -u scentinel -f        # log ao vivo, uma linha por anúncio checado (Ctrl+C sai)
```

Se você mexer no `config.toml` ou no `.env`, reinicie o bot:

```bash
sudo systemctl restart scentinel
```

---

## 6. Notebook: não dormir, tampa e Wi-Fi

Sem isso, o notebook dorme e o bot para.

### Fechar a tampa não suspende

Esse comando cria um arquivo de configuração **novo** (não edita nenhum que já existe) dizendo pro sistema ignorar a tampa:

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
  | sudo tee /etc/systemd/logind.conf.d/tampa.conf
```

No Mint (Cinnamon), também vá em **Configurações do sistema → Gerenciamento de energia** e coloque **"Ao fechar a tampa" → "Não fazer nada"** (na tomada e na bateria).

### Nunca suspender nem hibernar

O comando abaixo desliga a suspensão de vez. Pra um servidor, é isso mesmo que a gente quer:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Pra desfazer um dia, é só trocar `mask` por `unmask`.

### Wi-Fi conectando antes do login

No Mint: clique no ícone de rede → **Configurações de rede** → engrenagem da sua rede → aba **Geral**. Marque **"Conectar automaticamente"** e **"Disponível para todos os usuários"**.

Se a senha do Wi-Fi tiver ficado guardada só no seu usuário, ele não conecta antes do login. Pra resolver, rode isto (troque `NOME_DA_REDE` e `SENHA`):

```bash
nmcli connection show                        # lista as redes salvas (só leitura)
sudo nmcli connection modify "NOME_DA_REDE" connection.permissions "" wifi-sec.psk-flags 0 wifi-sec.psk "SENHA"
```

### Prova final ✅

Reinicie com a tampa fechada (`sudo reboot`). Espere uns 2 minutos e veja se o bot aparece online no Discord.

---

## 7. Comandos

| Comando | Quem | O que faz |
|---|---|---|
| `/locate nome [marca]` | todo mundo | **pesquisa nas lojas na hora** (qualquer perfume, não precisa cadastrar) e mostra o mais barato, as outras ofertas, desconto e R$/ml. Ex.: `/locate nome:khamrah marca:lattafa` |
| `/varrer` | dono | força a varredura de ofertas agora (normalmente é de hora em hora); tira a pausa das lojas |
| `/status` | todos | mostra se cada loja está funcionando ou pausada (até quando e por quê) |
| `/despausar` | dono | tira a pausa das lojas na hora |
| `/add url nome ml [alvo] [marca] [seletor]` | dono | cadastra um anúncio, **testa o preço na hora** e mostra o que leu |
| `/remove anuncio` | dono | para de vigiar (tem autocomplete) |
| `/top` | todo mundo | as melhores promos das últimas 24 h, uma por perfume |
| `/lista` | todo mundo | tudo que está sendo vigiado, com o último preço |
| `/historico nome` | todo mundo | mínimo, máximo, mediana de 30 dias e último preço de cada loja |
| `/checar` | dono | força uma rodada agora (pula o que foi checado há menos de 30 min) |
| `/teste_alerta` | dono | manda um alerta de mentira no canal |

Dicas pro `/add`:
- No `nome`, coloque o perfume com a concentração (`Sauvage EDP`, `Malbec Deo Colônia`). O tamanho vai no campo `ml`.
- Mesmo perfume em lojas ou tamanhos diferentes: use o **mesmo nome**. Aí o `/locate` compara todos juntos.

---

## 8. Níveis e regras de aviso

O desconto é calculado contra um **preço de referência honesto**:
1. a mediana do próprio histórico do anúncio nos últimos 30 dias (mínimo de 5 leituras);
2. se ainda não tem histórico, o "de R$ X" riscado da loja (o alerta avisa: "📢 desconto anunciado pela loja");
3. se tem os dois, o **menor** deles (proteção contra Black Fraude);
4. se não tem nenhum dos dois, não tem nível. Nesse caso só o `alvo` funciona.

| Nível | Nome | Desconto | Ping |
|---|---|---|---|
| 1 | 🟢 Cheirinho | 10–19% | – |
| 2 | 🟡 Elogio | 20–29% | – |
| 3 | 🟠 Rastro | 30–39% | – |
| 4 | 🔴 Fera | 40–49% | @você |
| 5 | 💎 Bug de preço | 50%+ | **@everyone** + @você + rechecagem |

Anti-spam:
- Só avisa quando o anúncio **sobe de nível**. Mesmo nível ou nível menor não repete.
- Quando o desconto volta pra baixo de 10%, o estado zera e a próxima queda avisa de novo.
- `alvo`: avisa uma vez quando o preço fica igual ou abaixo, e libera de novo quando volta pra cima.
- Esgotado nunca avisa.
- **Sanidade:** se a queda passar de 60% (ou se for nível 5), ele baixa a página de novo 2 minutos depois e só avisa se o preço confirmar.
- O preço Pix é guardado e mostrado, mas o nível usa o preço normal. Preço parcelado nunca vira preço.

Tudo isso (faixas, nomes, cores, pings e nível mínimo, inclusive por perfume) fica no `config.toml`.

Educação com as lojas (não negociável):
- cada anúncio é checado no máximo a cada 3 h;
- uma requisição por vez, com 5 a 15 s de espera entre elas;
- se a loja bloquear (403/429/captcha), ele avisa no canal uma vez e espera 6 h, 12 h, 24 h... (máximo 48 h);
- nada de proxy, anti-captcha ou login falso.

---

## 8.5. Canal de promos (busca automática)

São dois jeitos, rodando juntos:

1. **Varredura (o principal):** de hora em hora o bot percorre as ofertas de perfume das lojas: no Mercado Livre, os **mais vendidos** das categorias de perfume e vários termos ("perfume masculino", "perfume árabe", "eau de parfum"...), só **loja oficial**; nas lojas VTEX, a lista de perfumes **ordenada por maior desconto**. Posta **qualquer perfume** em promo boa, os maiores descontos primeiro. Perfume que aparece em várias lojas é reconhecido como o mesmo (pelo nome e tamanho) e só o mais barato é postado.
2. **Catálogo:** o `catalogo.toml` tem ~80 perfumes famosos com preço normal de mercado. Eles são buscados um a um (a cada 6 h por loja) e têm comparação de preço mais precisa.

Uma busca por vez, com pausa educada entre elas.

**Lojas:** hoje só o **Mercado Livre** está ligado (o mais seguro). As outras (Época, Sephora, Boticário, Natura...) continuam no `config.toml` com `ativo = false`; troque pra `true` pra religar.

**Só masculino:** com `publico = "masculino"` no `config.toml` (o padrão), o canal posta só perfume masculino e unissex. O que é feminino (pelo catálogo, pelo que a loja diz ou pelo título) fica de fora. O `/locate` mostra tudo o que você pesquisar. Pra postar tudo: `publico = "todos"`.

O que ele faz com o que acha:
- **Descarta o que não é o frasco:** kit, decant, amostra, miniatura, tester, contratipo, "inspirado em", body splash, hidratante. ("Desodorante Colônia" do Boticário/Natura é perfume e **entra**.)
- **Confere se é o perfume certo:** marca, nome, concentração (Sauvage EDT ≠ EDP ≠ Elixir), público (212 VIP masculino ≠ feminino) e tamanho. Anúncio sem ml no título fica de fora (não dá pra comparar preço).
- **Compara com o preço normal de mercado** do catálogo, com o histórico real de 30 dias e com o "de R$" da loja, e fica sempre com o **menor** dos três (assim promo falsa de "de R$ 999 por R$ 699" não passa).
- **Posta só o que vale:** a partir do nível 2 (20% abaixo da referência), ou abaixo do "preço bom" do perfume. Se o mesmo perfume e tamanho estiver mais barato em outra loja, posta só o mais barato. No máximo 10 posts por hora.
- **🏆 Top do dia** às 19h com os 5 melhores (um por perfume). `/top` mostra na hora.
- Loja que bloquear é pausada (6 h, 12 h, 24 h...) e avisada uma vez no canal. Loja que só dá erro (provavelmente não tem essa busca) é pausada por 24 h, sem avisar.

**Testar quais lojas funcionam** (no notebook; daqui do Claude as lojas são bloqueadas):

```bash
.venv/bin/python -m scentinel lojas                          # busca "sauvage dior" em todas
.venv/bin/python -m scentinel lojas --termo "lattafa khamrah"
.venv/bin/python -m scentinel lojas --so boticario --termo "malbec"
```

Nas que derem ❌, coloque `ativo = false` no `config.toml`.

### Mercado Livre (precisa de login, uma vez)

A busca do ML só funciona com um app (grátis) logado; sem isso ele pede captcha. Como tem muito perfume falsificado lá, o canal só posta **loja oficial** e **vendedor bem avaliado** (MercadoLíder Gold/Platinum com termômetro verde; `vendedor_bem_avaliado = false` no `config.toml` deixa só loja oficial). O `/locate` mostra todo mundo.

Desde 2025 o ML fechou a busca de anúncios da API (`/sites/MLB/search` dá 403 pra quase todo app). Por isso o bot usa o **catálogo** do ML: acha a página do produto (`/products/search`), vê todos os vendedores dele (`/products/{id}/items`) e fica com a **loja oficial mais barata**. O link do post é a página do produto no ML.

1. Entre em <https://developers.mercadolivre.com.br/devcenter> com sua conta do ML e clique em **Criar uma aplicação**.
2. Preencha: nome `Scentinel`, finalidade **Pessoal**, e em **URIs de redirect** coloque `https://www.google.com/` (com a barra no final). Permissões: **Publicação e sincronização = só leitura**, o resto sem acesso; tópicos de notificação todos desmarcados. Em **fluxos de autenticação**, marque **Authorization Code** e **Refresh Token** (sem o Refresh Token o acesso vence em ~6 h e não renova); **PKCE desmarcado**. Salve.
3. Copie o **App ID** e a **Secret Key** que aparecem e coloque no `.env`:
   ```
   ML_CLIENT_ID=o_app_id
   ML_CLIENT_SECRET=a_secret_key
   ML_REDIRECT_URI=https://www.google.com/
   ```
4. Rode o login e siga o que aparecer (abre um link, você clica em Permitir, cola o endereço do Google que abriu):
   ```bash
   .venv/bin/python -m scentinel ml-login
   ```
5. Reinicie o bot. O token é renovado sozinho daí pra frente (fica em `dados/ml_token.json`, fora do git).
6. Pra conferir o que a API responde (mostra só status e nomes de campos, pode mandar print):
   ```bash
   .venv/bin/python -m scentinel ml-teste
   ```
   Se o `/products/search` der ❌ 403: no app do devcenter, mude **Publicação e sincronização** pra **leitura e escrita**, salve, rode o `ml-login` de novo e reinicie o bot. (O bot só lê; nunca publica nada.)

**Mexer no catálogo:** abra o `catalogo.toml`. Cada perfume tem preço normal, "preço bom", termos de busca e palavras a evitar (flankers). Os preços vêm estimados; ajuste à vontade e reinicie o bot.

---

## 9. Quando uma loja não funciona

1. Rode `.venv/bin/python -m scentinel testar "URL" --salvar`. Isso salva o HTML em `tests/fixtures/reais/`.
2. Se não achou o preço, abra a página no navegador, clique com o botão direito no preço e escolha **Inspecionar**. Veja a classe do elemento, por exemplo `<span class="preco-final">`, e teste:
   ```bash
   .venv/bin/python -m scentinel testar "URL" --seletor ".preco-final"
   ```
3. Funcionou? Cadastre com `/add ... seletor:.preco-final`.
4. Não funcionou? Manda o HTML salvo pro Claude. Pode ser que precise de um adaptador em `scentinel/lojas/`.

---

## 10. Dia a dia

```bash
scentinel-update                             # atualizar (baixa, testa e reinicia)
journalctl -u scentinel -f                   # ver o log
sudo systemctl restart scentinel             # reiniciar
```

**`scentinel-update`** baixa o código novo, instala dependência nova se tiver, roda os testes e reinicia o bot (pede a senha do notebook só pra reiniciar). Se os testes falharem, ele volta pro código de antes e não reinicia. Já está na versão mais nova? Ele avisa e não faz nada (`scentinel-update --reiniciar` reinicia mesmo assim).

Pra criar o comando (uma vez só):
```bash
sudo ln -sf ~/scentinel/atualizar.sh /usr/local/bin/scentinel-update
```
(cria um atalho na pasta de comandos do sistema, pra funcionar de qualquer pasta.)

O banco fica em `dados/scentinel.db`. Pra fazer backup, é só copiar esse arquivo. ⚠️ O HD de 500 GB (`/dev/sdb`) tem dados antigos: **não formate nem reparticione**.

---

## 11. Migrar pro Ubuntu Server

Nada aqui depende do Mint:

1. No Mint, copie o `.env` e o `dados/scentinel.db` pra fora (pendrive ou `scp`).
2. Instale o Ubuntu Server e siga as seções 1, 3 e 5. Antes da seção 5, coloque o `.env` e o `scentinel.db` de volta no lugar.
3. Na seção 6, a parte da tampa e da suspensão é igual. O Wi-Fi no Ubuntu Server é configurado no `netplan` durante a instalação.

---

## Desenvolvimento

```bash
.venv/bin/python -m pytest -q
```

| Arquivo | O que faz |
|---|---|
| `scentinel/coleta.py` | baixa a página e acha o preço (seletor → JSON-LD → meta), o riscado e o Pix |
| `scentinel/promo.py` | referência, níveis, anti-spam e sanidade (funções puras) |
| `scentinel/caca.py` | junta tudo: rodada, `/add`, `/locate`, `/historico` |
| `scentinel/busca.py` | normalização de nome e fuzzy match |
| `scentinel/db.py` | SQLite (datas em UTC) |
| `scentinel/formato.py` | R$, datas em São Paulo e embeds |
| `scentinel/bot.py` | Discord: loop de caça e slash commands |
| `scentinel/lojas/` | nome das lojas e adaptadores específicos (vazio até alguma loja precisar) |
