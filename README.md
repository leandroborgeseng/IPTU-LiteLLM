# IPTU de Franca · Conferência automática de isenção

## Uso

Abra o painel, clique em **Selecionar pasta** (ou arraste a pasta) e aguarde.
A pasta pode ser de um processo ou conter várias subpastas, uma por processo.
O navegador monta um ZIP sem compressão sob demanda (sem carregar os arquivos
na memória) e o envia em partes retomáveis. O servidor separa os processos
pelas pastas (o número SEI no nome da pasta identifica o processo), lê todas
as páginas de todos os processos numa única fila paralela
(`IPTU_PAGE_WORKERS`, padrão 16) e publica o parecer de cada processo assim
que suas páginas terminam. A tabela mostra o resultado de cada processo e
exporta CSV; o detalhe mostra requisitos, páginas de origem e o texto do
parecer de deferimento ou indeferimento.

Resultados: **Enquadra**, **Enquadra · conferir cadastro** (documentos
atendem; falta o que só o cadastro confirma), **Não enquadra** (condição de
indeferimento da LC 456/2025 comprovada) e **Em análise** (os documentos não
permitem decidir). Cada critério cita as páginas usadas.

Cruzamentos automáticos: CPF/nome do requerimento × documento de identidade;
benefícios declarados × extratos (número do benefício); soma da renda do
requerente e do cônjuge na competência mais recente, sem duplicar
comprovantes e sem rendas de terceiros, com teto inclusivo de 2 salários
mínimos; residência e edícula pelas respostas do requerimento; testamento,
escritura ou matrícula citando o requerente; e, com a SIL configurada,
titularidade e endereço do imóvel pela inscrição e inscrição mobiliária
pelo CPF. A API da SIL ainda não consulta imóveis por CPF, por isso a
unicidade do imóvel continua como conferência cadastral.

### Histórico, busca e exportação

Tudo fica persistido no volume `/data` (faça backup dele):

- `/data/<lote>/files/` — documentos originais extraídos; `/data/<lote>/<processo>/` — leitura de cada página,
  conferência manual (`review.json`) e consulta à SIL (`sil.json`).
- `/data/iptu.db` (SQLite) — um registro por processo de todos os lotes com o parecer atual completo, as
  versões anteriores (cada mudança de parecer, com data e motivo: leitura, reanálise, conferência manual) e a
  auditoria de exclusões (número do processo, data e usuário, sem dados pessoais).

O painel sincroniza arquivos → banco a cada 5 s. A tabela mostra os processos de todos os lotes, com busca
textual (processo, requerente, CPF, inscrição, síntese, motivos), ordenação pelos cabeçalhos e filtros
**Passaram** (enquadra), **Não passaram** (não enquadra ou em análise), Fora do fluxo e Processando. Cada
linha tem **Reanalisar** e **Excluir** (dupla confirmação: aviso e digitação de EXCLUIR; apaga documentos,
leituras e versões do processo).

Exportação para outros sistemas (JSON, `versao_formato` 1):

- `GET /api/lotes/<lote>/processos/<processo>/exportar` — um processo, com `historico` das versões.
- `GET /api/processos/exportar?q=&filtro=&lote=` — todos os processos do filtro (`{"processos": [...]}`).
- `GET /api/processos/exportar.csv?...` — planilha.
- `GET /api/lotes/<lote>/processos/<processo>/historico[/<versao>]` — versões do parecer.

Cada documento traz `lote`, `processo` (número SEI e arquivos), `resultado`
(`enquadra`, `enquadra_conferir_cadastro`, `nao_enquadra`, `em_analise`, `fora_do_fluxo`), `parecer`
(critérios com páginas de origem, fatos lidos, renda, síntese e texto do parecer), `conferencia_manual`,
`cadastro_sil`, `versao_regras` e `gerado_em`. Use Basic Auth com o usuário do painel.

### Parâmetros do exercício

`IPTU_EXERCICIO` (2027), `IPTU_SALARIO_MINIMO_CENTAVOS` (162100; o teto é o dobro) e, opcionalmente,
`IPTU_PRAZO_INICIO`/`IPTU_PRAZO_FIM` (AAAA-MM-DD) para comparar com a data de assinatura do requerimento.
Atualize-os a cada exercício; a mudança gera nova versão dos pareceres afetados.

### Desempenho (H200)

- Imagens em JPEG: a compressão PNG custava ~1,4 s de CPU por página digitalizada (JPEG: ~0,05 s).
- `IPTU_PAGE_WORKERS` (padrão 32, máximo 128) define as páginas em leitura simultânea. Suba enquanto o
  Diagnóstico do lote não mostrar 429/5xx/tempo esgotado e o tempo total cair.
- Páginas de texto digital irrelevante (andamentos, despachos) não vão ao modelo; a 1ª leitura transcreve só
  o início da página; leitura detalhada e verificação do requerimento correm em paralelo.
- A renderização usa os núcleos do container (`IPTU_RENDER_WORKERS` ajusta). Para lotes grandes, rode o
  worker no servidor da H200 (`compose.gpu-worker.yaml`), que tem mais CPU, e o painel separado.

### SIL

Configure `SIL_BASE_URL`, `SIL_CLIENT_ID`, `SIL_CLIENT_SECRET` e `SIL_SCOPE`
apenas no ambiente (Coolify), nunca no repositório. Teste com
`python app.py sil` no terminal do container. O resultado da consulta fica
em cache por processo por 24 h.

### Como a leitura funciona

1. Cada página: classificação do documento e transcrição (imagem 1800 px, cinza).
2. Requerimento, extrato e documento de identidade: segunda leitura com
   instruções específicas do documento sobre imagem de 2600 px colorida.
3. Requerimento: leitura independente em perguntas e respostas
   (`IPTU_VERIFY_MODEL`, ou o modelo reserva, ou o principal). Campo decisivo
   lido de forma diferente não é usado: vira pendência com a página.
4. O parecer cruza os documentos e redige o texto; a tabela mostra uma
   síntese de uma linha por processo.

Custo: 1 chamada por página comum, 2 por extrato/identidade e 3 por página
de requerimento.

### Medir a assertividade

Monte um gabarito JSON (formato em `avaliacao.py`) com o resultado conferido
por servidor para alguns processos, processe o mesmo lote e clique em
**Comparar com gabarito** (ou `python app.py avaliar <lote> <gabarito.json>`).
O relatório mostra acerto do resultado, da renda e de cada campo lido. Use-o
para escolher o modelo e para achar novos padrões de documento.

### Modelo

Configuração sugerida para o LiteLLM da Prefeitura (confirme com o gabarito):

| Variável | Modelo | Uso |
|---|---|---|
| `IPTU_BATCH_MODEL` | `qwen3.8-27b-fast` | 1ª leitura de todas as páginas |
| `IPTU_DETAIL_MODEL` | `qwen3.8-27b` | leitura detalhada de requerimento, extrato e identidade |
| `IPTU_VERIFY_MODEL` | `gemma-4-12b-it` | leitura independente do requerimento (outra família de modelo) |
| `IPTU_BATCH_MODEL_FALLBACK` | `qwen3.8-27b` | resposta fora do formato |

Se a verificação gerar muitas pendências de "leitura divergente" que o
servidor confirma como erro do verificador, troque `IPTU_VERIFY_MODEL` por
`qwen3.8-27b-fast`. O log do worker mostra na inicialização os modelos em uso.

`IPTU_BATCH_MODEL` define o modelo multimodal. `IPTU_BATCH_MODEL_FALLBACK`
(opcional) é usado uma vez quando a resposta do principal vem fora do
esquema. Após trocar o modelo ou atualizar o sistema, use **Reanalisar lote**.

### Testes

`python -m unittest discover -s tests` cobre as regras do parecer.

## Diagnóstico do LiteLLM

Painel de teste com login, pronto para publicar via Docker Compose no Coolify. Consulta `GET /v1/models` de dentro do container. A chave do LiteLLM fica no backend. O painel ainda não recebe processos nem faz OCR.

## Publicar via GitHub e Coolify

1. Descompacte o projeto e suba o conteúdo na raiz de um repositório GitHub privado. O repositório contém somente código e arquivos de configuração de exemplo.
2. No Coolify, crie uma aplicação a partir do repositório usando o build pack **Docker Compose**. Escolha como destino o servidor com acesso à rede da Prefeitura. Use **Base Directory** `/` e **Docker Compose Location** `/compose.yaml`.
3. Em **Environment Variables**, configure estas variáveis apenas em runtime. Desmarque a opção de disponibilizá-las no build para as credenciais.

   - `LITELLM_BASE_URL` — URL interna autorizada, sem o sufixo `/models`.
   - `LITELLM_API_KEY` — chave substituta do LiteLLM.
   - `IPTU_ADMIN_USER` — login do painel. O padrão é `iptu`.
   - `IPTU_ADMIN_PASSWORD` — senha exclusiva do painel, com no mínimo 16 caracteres. Gere e guarde a senha no gerenciador da equipe. Exemplo de geração local: `openssl rand -hex 24`.

4. Em **Domains**, adicione um domínio HTTPS ao serviço `worker` com porta interna `8080`. Exemplo no Coolify: `https://iptu-teste.seu-dominio:8080`. Ative o redirecionamento HTTP para HTTPS. Abra o endereço HTTPS no navegador sem o sufixo de porta.
5. Clique em **Deploy**. Abra o domínio, entre com o usuário e a senha do painel e clique em **Testar conexão**.

A porta do container é acessada pelo proxy. `GET /healthz` responde apenas com o status 204 e serve ao health check. Para os testes com documentos reais, confirme que o gateway também utiliza transporte criptografado e que os modelos autorizados rodam no ambiente aprovado.

Documentação do Coolify: https://coolify.io/docs/applications/builds/docker-compose

## Interpretar o diagnóstico

- `conectado`: o gateway respondeu. A lista informa os aliases visíveis à chave.
- HTTP 401: confira a chave. HTTP 403: confira as permissões. HTTP 404: confira a URL e a rota.
- `falha_de_conexao`: confira acesso do container à rede interna, firewall, TLS e rota. Se o host acessa o endereço e o container não, peça à infraestrutura para verificar se uma rede Docker se sobrepõe à sub-rede do LiteLLM.
- O diagnóstico também pode ser executado no terminal do container com `python app.py check`.

O próximo teste é usar uma página sintética para verificar um alias multimodal e confirmar a rota do OCR dedicado. A listagem por si só não comprova suporte a imagem ou execução nas H200.

## Diagnosticar com curl no terminal do worker

No Coolify, abra **Terminal** no serviço `worker`. Em ambos os comandos, a expansão abaixo aceita a URL base com ou sem `/v1`.

```sh
BASE="${LITELLM_BASE_URL%/}"
MODELS_URL="${BASE%/v1}/v1/models"
curl --noproxy '*' --connect-timeout 5 --max-time 15 -sS \
  -o /dev/null -w 'HTTP %{http_code}\n' "$MODELS_URL"
```

Um HTTP 401 ou 403 sem chave confirma que houve resposta HTTP. HTTP 000 e a mensagem do curl ajudam a identificar timeout, conexão recusada ou falha TLS.

Se o gateway respondeu, repita usando a chave que já está no ambiente. Evite executar com rastreamento de shell ativo.

```sh
set +x
printf 'Authorization: Bearer %s\n' "$LITELLM_API_KEY" |
  curl --noproxy '*' --connect-timeout 5 --max-time 15 -sS \
    -H @- -w '\nHTTP %{http_code}\n' "$MODELS_URL"
```

Compartilhe apenas o status HTTP, a mensagem de erro ou os nomes dos modelos. O comando não imprime o cabeçalho de autenticação.

## Testar leitura de imagens

No painel, selecione um modelo e uma marcação e clique em **Testar imagem**.
Para rodar os seis testes em sequência, use `python app.py vision` no terminal
do worker. Cada linha contém modelo, caso, tempo, resultado esperado e resultado
obtido quando o JSON é válido. A bateria retorna código 1 se houver alguma falha.

As imagens sintéticas mostram exercício 2027 com “Sim” marcado ou as duas caixas
em branco. A resposta precisa conter exatamente os campos `exercicio` e
`reside_no_imovel`. A caixa em branco exige `null`. O diagnóstico valida o JSON
que o modelo retornou por instrução de texto. Ele não exige suporte a JSON Schema
no gateway. Os testes exercitam a leitura multimodal pelo LiteLLM. O serviço de
OCR dedicado da NVIDIA continua sendo uma integração separada.

O diagnóstico distingue JSON direto, JSON em bloco Markdown e resposta sem
conteúdo. Para texto que não pôde ser interpretado como JSON, mostra uma prévia
de até 240 caracteres. Essa prévia é usada apenas no teste de imagens sintéticas.

## Piloto com upload de PDF

No painel, em **Ler uma página de PDF**, envie um PDF sintético ou anonimizado
de até 10 MB e informe a página (a partir de 1). O worker renderiza a página
com Poppler, remove os temporários do volume em memória e envia apenas a
imagem ao LiteLLM. A resposta informa a página, o modelo e os valores extraídos.
O resultado preserva `null` quando a caixa não pode ser determinada. Esse teste
não faz validação cadastral, não classifica o processo e não grava parecer.
A inferência usa o alias Gemma já verificado no teste sintético.

## Piloto de lotes e conferência

A página inicial recebe um PDF ou ZIP sintético ou anonimizado. Envie arquivos
pequenos primeiro. O upload é dividido em partes de até 8 MiB e pode ser
retomado selecionando o mesmo arquivo no mesmo navegador. O servidor persiste
o original, os PDFs extraídos e os resultados no volume Docker `iptu_data`.
Não remova esse volume entre implantações. Os limites atuais são 48 GiB
compactados e 150 GiB de extração acumulada. A capacidade para um lote de
40 GB depende de teste de disco, proxy e carga no ambiente de destino.

O processamento continua em segundo plano. O importador agrupa os documentos
pela pasta de processo, lê ZIPs internos até o limite configurado no código
e registra formatos que não processa, incluindo HTML. O worker percorre as
páginas com o modelo padrão `qwen3.8-27b-fast` via LiteLLM e salva um resultado por página.
Ao reiniciar, retoma as páginas que ainda não têm resultado salvo. Neste
piloto, resultados de falha ficam visíveis para revisão após uma tentativa
adicional nas falhas transitórias de transporte. Use uma única réplica.

Na tela de conferência, selecione o lote, o processo e uma página. A imagem
da página aparece ao lado dos campos extraídos. A nota de conferência é
persistida. Este piloto não consulta a SIL, não verifica elegibilidade e
não grava no F4. O diagnóstico anterior continua disponível no menu.

## Parecer preliminar e número SEI

Envie a pasta do processo em um ZIP. A pasta com o número SEI agrupa os PDFs,
subpastas e ZIPs internos em um único processo. Para pastas sem número SEI,
os PDFs são agrupados pela pasta que os contém. Os PDFs na raiz do ZIP formam
um grupo próprio. A tela e o JSON guardam as origens encontradas. Conflitos
entre arquivo e pasta exigem conferência no mesmo processo. Use **Reanalisar lote**
para atualizar extrações antigas. Para aplicar o agrupamento a um lote antigo, importe o ZIP novamente.

O parecer é calculado para o fluxo de aposentadoria, pensão, BPC e renda mensal
vitalícia de Franca, exercício 2027. A IA lê os campos e comprovantes por página.
A tela permite registrar evidências humanas e exportar o JSON com critérios,
motivos e páginas. Use o total bruto familiar mensal conferido, sem somar
comprovantes duplicados, valores de meses diferentes ou rendimentos de terceiros.
Como apoio, o parecer mostra a soma dos benefícios distintos do requerente na
competência mais recente (deduplicados pelo número do benefício) em
`renda_calculada_comprovantes_centavos`. Essa soma não substitui a conferência:
a renda do cônjuge e outras rendas entram no total registrado pelo operador.
O teto é inclusivo (renda menor ou igual a 2 salários mínimos), conforme a
especificação do motor de regras da LC 456/2025.

Residência e edícula usam as respostas do requerimento, como prevê a
especificação: "Reside no imóvel: Sim" atende a residência; edícula ausente,
sem moradores ou habitada por ascendente/descendente sem aluguel atende a
edícula; edícula alugada ou ponto comercial gera proposta de indeferimento.
A conferência de endereço e de unicidade do imóvel no cadastro continua com o
operador. Pedidos de aposentada e pensionista ao mesmo tempo são aceitos.
Os campos novos (edícula habitada, ocupante familiar, usufrutuário, ponto
comercial e número do benefício) exigem **Reanalisar lote** nos lotes antigos. O teto de R$ 3.242,00 é o publicado pela Prefeitura
para o período de solicitação em 2026. Casos sociais ficam em análise.
As confirmações cadastrais são registradas pelo operador. A consulta automática
à SIL e a decisão oficial no SEI/F4 ainda estão fora desta entrega.


O worker registra a transcrição do modelo de visão e o texto nativo de cada página. O
parecer usa campos do requerimento quando a classificação é sustentada por
um trecho do próprio pedido. O inventário indica possíveis comprovantes com
a página de origem para conferência. Use `IPTU_BATCH_MODEL` para trocar o
modelo do processamento em lote no Coolify. Para aplicar o OCR e a nova
classificação aos lotes antigos, use **Reanalisar lote**.

O worker processa até 8 páginas do mesmo PDF em paralelo por padrão. Ajuste
`IPTU_PAGE_WORKERS` no Coolify para testar outros limites, de 1 a 16. Meça o
tempo e os erros do LiteLLM com o mesmo PDF antes de subir o limite. A barra
de status exibe quantas páginas do PDF atual já têm resultado salvo.

## Processamento no servidor do LiteLLM

Para retirar o processamento de PDFs do servidor do painel, monte o mesmo
armazenamento compartilhado nos dois hosts, com leitura e escrita para o UID
10001. Em cada host, `IPTU_SHARED_DATA_DIR` deve apontar para a montagem desse
mesmo armazenamento. Preserve os lotes existentes copiando o conteúdo de
`/data` com os serviços parados, se desejar migrá-los.

- Host do painel: suba com `docker compose -f compose.yaml -f compose.gpu-panel.yaml up -d --build`.
  No Coolify, a configuração equivalente é `IPTU_RUN_WORKER=0` e uma única
  montagem do armazenamento compartilhado em `/data`.
- Host do LiteLLM: suba uma réplica com `docker compose -f compose.gpu-worker.yaml up -d --build`.
  Configure a URL privada, a chave e o alias que realmente aponta para a H200.
  Se o LiteLLM estiver no mesmo host, use um endereço acessível a partir do container.

O worker remoto abre e renderiza os PDFs com Poppler no servidor do LiteLLM.
O alias multimodal recebe a imagem e devolve a transcrição OCR e a classificação
em uma chamada por página. O painel exibe as imagens de prévia gravadas pelo
worker. Use a tela de lotes para testar PDFs nessa configuração. Depois da
migração, reanalise os lotes antigos para atualizar os resultados e as prévias.
O tempo exibido para LiteLLM inclui rede, fila e geração da resposta.


Fotos JPG, JPEG, PNG e WebP podem ser enviadas diretamente ou dentro da pasta
de um processo no ZIP. Cada foto é tratada como uma página, com o original
disponível para download. Para incluir fotos de um ZIP já importado, envie
o ZIP novamente.
