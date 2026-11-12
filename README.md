# IPTU de Franca · Diagnóstico do LiteLLM

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
automaticamente comprovantes duplicados, valores de meses diferentes ou
rendimentos de terceiros. O teto de R$ 3.242,00 é o publicado pela Prefeitura
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
