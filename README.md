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
