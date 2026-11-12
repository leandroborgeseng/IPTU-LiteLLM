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
