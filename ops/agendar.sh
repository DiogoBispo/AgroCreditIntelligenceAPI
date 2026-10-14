#!/bin/sh
# Executa uma tarefa agendada na imagem de ferramentas, sem sobreposição e com registro no syslog.
#
#   ops/agendar.sh <nome-da-tarefa> <módulo e argumentos>
#   ops/agendar.sh ibama agrocredit.pipeline run ibama
#
# Código 75: execução anterior da mesma tarefa ainda em andamento (trava ocupada), não é falha da tarefa.
# Saída diferente de zero vai para o syslog com prioridade "err" e, se AGROCREDIT_ALERTA_CMD estiver
# definido, esse comando recebe a mensagem pela entrada padrão (ex.: integração com o sistema de alertas).
set -u
TAREFA="$1"; shift
DIR="$(cd "$(dirname "$0")/.." && pwd)"
TRAVA="/tmp/agrocredit-${TAREFA}.lock"
# A imagem roda como uid 10001; os volumes ./data e ./archive pertencem ao usuário que agenda
HOST_UID="${HOST_UID:-$(id -u)}"; HOST_GID="${HOST_GID:-$(id -g)}"; export HOST_UID HOST_GID

saida=$(flock -n -E 75 "$TRAVA" docker compose -f "$DIR/docker-compose.yml" --profile tools run --rm -T tools "$@" 2>&1)
codigo=$?
logger -t "agrocredit-${TAREFA}" "saida=${codigo}"
printf '%s\n' "$saida" | tail -n 50 | logger -t "agrocredit-${TAREFA}"
if [ "$codigo" -eq 75 ]; then
  logger -t "agrocredit-${TAREFA}" "execução anterior ainda em andamento; pulada"
  exit 0
fi
if [ "$codigo" -ne 0 ]; then
  logger -p user.err -t "agrocredit-${TAREFA}" "FALHA (código ${codigo})"
  if [ -n "${AGROCREDIT_ALERTA_CMD:-}" ]; then
    printf 'agrocredit %s falhou (código %s)\n%s\n' "$TAREFA" "$codigo" "$(printf '%s' "$saida" | tail -n 20)" \
      | sh -c "$AGROCREDIT_ALERTA_CMD"
  fi
fi
exit "$codigo"
