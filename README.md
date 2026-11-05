# CTI Escuta

Plataforma acadêmica de análise e monitoramento de feedbacks. Projeto integrador do Técnico em Desenvolvimento de Sistemas do SENAI. Um único site, quatro áreas exclusivas, banco SQLite persistente e regras de autorização no backend.

Implementação em **Python 3.12+, Flask, SQLite e JavaScript sem framework**. O repositório de referência contém documentação, sem stack anterior. Essa escolha mantém a instalação pequena, o banco inspecionável e as permissões centralizadas. FFmpeg valida o conteúdo, formato e duração dos áudios. Não há integração com tickets ou telefonia externos.

## Executar localmente

Pré-requisitos: Python 3.12+, FFmpeg/ffprobe no PATH. Em Debian/Ubuntu, instale `python3-venv` e `ffmpeg` pelo gerenciador do sistema. Use o checkout existente; não é necessário criar worktree.

```bash
bash scripts/install.sh
# Apenas em um banco vazio de demonstração:
DEMO_PASSWORD='LocalDemo!2026' .venv/bin/flask --app run seed-demo
bash scripts/dev.sh
```

O servidor de desenvolvimento usa a porta 5000. Abra essa porta localmente no navegador; use HTTPS para gravação quando fora da máquina local. O login não tem seletor de perfil: o servidor consulta o perfil registrado no banco e redireciona.

Contas **exclusivamente locais de demonstração**, todas com a senha `LocalDemo!2026` se usar o comando acima:

| Perfil | E-mail |
| --- | --- |
| Cliente | cliente@demo.cti.local |
| Atendente | atendente@demo.cti.local |
| Supervisor | supervisor@demo.cti.local |
| Gestor | gestor@demo.cti.local |

Há uma segunda equipe com `cliente2`, `atendente2`, `supervisor2` e outro par de cliente/atendente na primeira equipe (`cliente3`, `atendente3`), no mesmo domínio fictício. O seed gera 30 atendimentos e 30 feedbacks fictícios, com análises de demonstração e pendências. Ele **recusa sobrescrever banco com usuários**. Não execute seed em produção e não reutilize essas senhas fora do ambiente local.

## Organização e persistência

- `app/__init__.py`: aplicação, sessões, autorização, APIs e comandos administrativos.
- `app/services.py`: adaptadores de análise e transcrição.
- `app/templates/` e `app/static/`: interface responsiva em português.
- `migrations/001_initial.sql`: esquema versionado, reaplicável sem apagar dados.
- `instance/cti.sqlite`: banco local ignorado pelo Git; usuários, equipes, sessões, atendimentos, feedbacks, análises e revisões.
- `instance/audio-tmp/`: arquivos temporários privados, jamais servidos por HTTP.
- `tests/`: testes de backend e de navegador com bases isoladas.

As análises originais não são alteradas por revisão: cada avaliação humana adiciona um evento com autor, data, sentimento, criticidade, justificativa, responsável e providência. Os indicadores usam a revisão mais recente, conservando o histórico. Falhas do provedor mantêm o feedback como **pendente**, sem inventar resultado; o supervisor pode tentar novamente nos detalhes. Métricas distinguem recebidos, pendentes e analisados. Não existem indicadores fixos no frontend.

## Permissões

| Perfil | Abas | Escopo e operações |
| --- | --- | --- |
| Cliente | Meus atendimentos, Enviar feedback, Meus feedbacks | Somente registros próprios; envio de texto confirmado; sem análises internas. |
| Atendente | Meus atendimentos, Registrar atendimento, Feedbacks dos meus atendimentos | Somente atendimentos atribuídos a ele; cria na própria equipe para clientes vinculados; altera apenas status operacional; consulta feedbacks associados. |
| Supervisor | Dashboard da equipe, Atendimentos da equipe, Feedbacks da equipe, Casos críticos | Somente sua equipe; filtros, revisão humana e atribuição a atendentes da mesma equipe. Não entra nas áreas pessoais. |
| Gestor | Visão geral, Indicadores e tendências, Comparação entre equipes, Relatórios | Agregados organizacionais e CSV. Sem endpoints de registros individuais, transcrições ou revisão operacional. |

Toda API valida sessão e perfil. Consultas operacionais aplicam o escopo antes de retornar registros, inclusive busca por ID; IDs fora do escopo retornam 404. Campos extras são rejeitados nas mutações. Não há cadastro público nem endpoint de alteração de perfil. O menu e as rotas também respeitam as quatro áreas.

### Criar contas internas com segurança

Somente o operador com acesso administrativo ao servidor e ao banco pode executar:

```bash
.venv/bin/flask --app run create-user --name 'Nome da pessoa' --email 'pessoa@empresa.com.br' --role atendente --team 1
```

A senha é pedida sem eco (mínimo 12 caracteres) e armazenada com hash scrypt do Werkzeug. Nunca passe a senha em argumentos para contas reais. Verifique a identidade e a equipe da pessoa antes de criar a conta. Para gestor omita `--team`; para os demais perfis informe uma equipe existente. Na base demonstrativa, 1 = Conectividade e 2 = Cloud e segurança. Em uma implantação nova, o operador provisiona as equipes pela migração administrativa antes das contas. Não reutilize o seed fictício para provisionar a organização.

Sessões usam tokens aleatórios, somente o hash do token fica no banco; cookie HttpOnly/SameSite, expiração de 8 horas, revogação no logout, CSRF em mutações e limitação de tentativas de login. A sessão sempre relê perfil/equipe no banco. Não há segredos em JavaScript. Em HTTPS de produção defina `COOKIE_SECURE=true`. Restrinja a permissão da pasta de instância ao usuário do processo. Não exponha o servidor Flask de desenvolvimento em produção: use um servidor WSGI atrás de proxy HTTPS, limite de upload também no proxy e backups restritos ao banco, sem a pasta temporária.

## Texto e áudio

1. Cliente seleciona um atendimento próprio e digita, grava com MediaRecorder ou anexa áudio.
2. O backend valida extensão, MIME declarado e conteúdo com ffprobe/FFmpeg; aceita MP3, WAV, WebM, OGG e M4A de até **10 MB / 5 minutos**, sem vídeo. Para WebM do navegador sem duração no contêiner, faz decodificação limitada para medi-la.
3. O arquivo recebe nome aleatório em diretório privado; é removido em `finally`, tanto em sucesso como em falha. Nenhum áudio entra no banco, registro de feedback, logs da aplicação, pasta estática ou backup previsto.
4. A transcrição fica em rascunho privado por até 30 minutos, sem criar feedback. O cliente revisa/corrige e confirma explicitamente. Somente o texto confirmado, origem e protocolo entram no feedback; não há player nem link de áudio.
5. Falha mantém o formulário para tentar novamente ou escrever. Não registra conteúdo incompleto automaticamente.

A aplicação limpa arquivos abandonados com mais de 15 minutos em cada nova transcrição. Para cobrir quedas sem novas requisições, configure também um agendador a cada 5 minutos:

```bash
cd /caminho/CIT-solucoes && .venv/bin/flask --app run cleanup-audio
```

O comando remove temporários abandonados, rascunhos e sessões expirados, e também encerra os atendimentos resolvidos há mais de 3 dias em que o cliente não confirmou o resultado (o agendador é o que faz esse prazo valer). `scripts/dev.sh` inicia também um processo de limpeza a cada 60 segundos e encerra ambos os processos ao sair; ao usar outro servidor, configure o agendador acima. O período de 15 minutos evita remover um arquivo que ainda está sendo processado. Configure `AUDIO_TEMP_DIR` em volume temporário privado sem backups; o padrão é privado dentro de `instance/`, que deve ser explicitamente excluído de backups de áudio. Exclusão lógica de arquivos não promete apagamento físico de mídia ou ausência de swap: valide infraestrutura, snapshots e política corporativa antes de usar dados reais. A aplicação não registra corpos de feedback ou áudio em logs; o operador deve desativar captura de corpos no proxy/APM.

## Serviços de análise e transcrição

Consulte [.env.example](.env.example). As variáveis devem ser exportadas no processo ou configuradas no gerenciador seguro do ambiente. Não há leitura automática de `.env` nem chave no repositório.

- `ANALYSIS_MODE=demo` (padrão): regras locais e resumo por trecho. **Não é IA**, não há confiança probabilística e a interface identifica demonstração.
- `TRANSCRIPTION_MODE=demo` (padrão): valida e descarta áudio, retorna um **exemplo explícito sem ouvir/transcrever**. O usuário deve substituir o exemplo por texto próprio; o servidor rejeita envio do marcador original. Isso serve para demonstrar revisão, não para avaliar qualidade de transcrição.
- `ANALYSIS_MODE=openai`: adaptador HTTPS fixo para `api.openai.com/v1/chat/completions`, chave `AI_API_KEY`, modelo configurável `AI_MODEL` (padrão `gpt-4o-mini`), saída com JSON Schema. Retorna sentimento, resumo, assuntos, sinal crítico e evidências literais. Não atribui porcentagem de confiança; exibe **Não disponível**. Feedback é isolado como dado não confiável nas instruções; o modelo não tem ferramentas nem pode executar ações.
- `TRANSCRIPTION_MODE=openai`: `api.openai.com/v1/audio/transcriptions`, mesma chave, `TRANSCRIPTION_MODEL` (padrão `whisper-1`), português. Exige também `TRANSCRIPTION_PRIVACY_ACK=yes`, uma confirmação administrativa após revisão da política do provedor e comunicação aos clientes.

Os dois serviços podem ser configurados de forma independente. Uma chave ausente nunca aciona respostas fictícias no modo real: retorna erro ou mantém análise pendente. Para o modo real libere **api.openai.com** na rede e forneça `AI_API_KEY` nas configurações seguras do servidor. A interface informa o envio ao provedor antes de processar.

**Privacidade e custos:** não foi confirmada nesta entrega uma política contratual de retenção do provedor. Portanto, não afirmamos que OpenAI descarta imediatamente o áudio. Antes de ativar o adaptador real, o responsável deve verificar a documentação vigente, contrato/DPA, elegibilidade de retenção zero, região de processamento e bases legais: https://platform.openai.com/docs/guides/your-data . Documente a conclusão e a data na política da implantação. O custo depende dos modelos, tokens e minutos de áudio; confira https://openai.com/api/pricing/ e configure orçamento/limites na conta. Não há chamadas pagas ou qualidade de IA real verificadas nesta entrega. Dados pessoais não devem ser enviados ao provedor sem autorização e avaliação dessas condições. Não existe anonimização automática garantida; prefira conteúdo fictício até essa avaliação.

## Testes

```bash
.venv/bin/python -m pytest tests/test_platform.py tests/test_services.py -q
# Testes de navegador, com Chromium instalado:
.venv/bin/pip install -r requirements-dev.txt
RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_browser.py -q
# Opcional: CHROMIUM_PATH=/outro/caminho/chromium
```

O backend testa redirecionamentos, bloqueios, isolamento de clientes/atendentes/equipes, CSRF, logout, escalada de privilégio, texto, confirmação, rascunho de áudio, tamanho/duração/formato, limpeza em sucesso/falha, análise pendente, revisão auditável e agregados. O navegador usa Chromium real, dispositivos de áudio fictícios do navegador e banco isolado para verificar menus dos quatro perfis, envio, anexo, **gravação real via MediaRecorder**, revisão humana e operação em tela de 390 px. Áudio de teste é silêncio/sinal fictício, não grava uma pessoa.

Veja [docs/VALIDATION.md](docs/VALIDATION.md) para resultados e limites e [docs/SOURCES.md](docs/SOURCES.md) para fontes e identidade visual.

## Limitações antes de produção

- Identidade provisória: cores e lettering não são apresentados como oficiais. Necessário validar Figma e arquivos oficiais CTI quando o acesso estiver disponível.
- Integrações reais são implementadas, mas não verificadas com credenciais nem dados reais. Modo demonstrativo é o padrão.
- SQLite e análise síncrona são adequados ao MVP acadêmico. Em maior carga, migre para banco servidor e fila com monitoramento de falhas.
- Ao substituir `scripts/dev.sh` por um servidor de produção, o agendador de limpeza deve ser instalado no host. Retenção, exclusão/anonimização de transcrições e backups após 12 meses, consentimento/política LGPD e operação de recuperação de senha exigem definição e implementação adicionais antes de produção.
- Não há publicação, contas produtivas ou integração com sistemas de tickets/telefonia.
