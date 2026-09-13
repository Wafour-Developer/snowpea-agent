# Gateway de mensajería

El gateway conecta una plataforma de chat con una sesión o con un agente nombrado. Los mensajes de ese chat se convierten en prompts; las respuestas del agente vuelven como mensajes; las aprobaciones llegan como botones. Telegram es la plataforma con soporte completo en v0.1. Discord y Slack implementan la misma interfaz y funcionan para la entrega, con menos horas de campo detrás.

## La vía rápida

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
snowpea            # or: snowpea daemon start
```

Esa es toda la configuración. El asistente registra el mensajero en `settings.json`, y el daemon lo convierte en una vinculación *catch-all* al arrancar: cualquier chat que escriba al bot obtiene su propia sesión, en `$HOME` salvo que fijes `gateway.telegram.workdir`. `snowpea daemon status` dice `messengers   telegram (listening)` una vez está en marcha. El token se copia a `$SNOWPEA_HOME/credentials.json` con modo `0600` y se referencia por nombre en todos los demás sitios, así que nunca aparece en `state.db`, ni en un resultado RPC, ni en una línea de log.

`--user-id` es tu propio id numérico de cuenta en esa plataforma, y es la única cuenta autorizada a responder una aprobación desde el chat. Telegram te dice el tuyo si envías `/start` a [@userinfobot](https://t.me/userinfobot). Si lo omites, el mensajero sigue hablando, pero toda pulsación de un botón de aprobación se rechaza, incluida la tuya.

Apagar el mensajero en el asistente (o con `settings.set`) elimina de nuevo esa vinculación. Las vinculaciones que hiciste a mano nunca se tocan por esto.

## Vincular a mano

Usa esto cuando quieras algo distinto de una única sesión catch-all: un agente nombrado, un chat concreto, o dos cuentas en una misma plataforma.

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN new:~/src/api --channel 123456 --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

Los tres argumentos posicionales son la plataforma, la referencia de credenciales (una clave en `credentials.json` o el nombre de una variable de entorno) y el destino:

| Destino | Significado |
|---|---|
| `agent:<name>` | un agente nombrado y persistente, con su propio espacio de memoria |
| `session:<id>` | una sesión existente |
| `new:<workdir>` | crear una sesión en ese directorio con el primer mensaje |

`--channel` restringe la vinculación a un id de chat. `--user` nombra al usuario de la plataforma autorizado a aprobar cosas. Configura ambos. Sin `--user` no hay nadie autorizado a responder una aprobación, y el gateway ignorará las pulsaciones de botón de cualquiera.

## Conseguir un bot

**Telegram.** Habla con [@BotFather](https://t.me/BotFather), `/newbot`, guarda el token. Tu id de chat es el número que ve el bot cuando le escribes, visible en el log del daemon con el primer mensaje entrante. Se usa long polling, así que no hace falta URL pública ni webhook.

**Discord.** Crea una aplicación, añade un usuario bot, habilita el intent de contenido de mensajes, invítalo a tu servidor, guarda el token del bot. El id del canal es el último segmento de la ruta de la URL de un canal.

**Slack.** Crea una app, añade `chat:write` y los eventos que necesite tu workspace, instálala, guarda el token del bot. Usa el nombre o el id del canal como destino.

## Hablar con él

Envíale un mensaje al bot y ya estás en una sesión. Los comandos slash funcionan exactamente igual que en la terminal, porque el registro de comandos vive en el core:

```
/mode
/ralph fix the failing integration test
/schedule "매일 09:00" "어제 커밋 요약" --channel telegram:123456
```

Las sesiones creadas por un gateway aparecen en `session.list` con su origen, así que una TUI conectada al mismo daemon puede ver lo que está haciendo el chat.

## Aprobaciones desatendidas

Una aprobación levantada por una sesión de gateway o del planificador es desatendida, y se comporta de forma distinta a una que has provocado tú escribiendo:

- Se difunde como una notificación `approval.pending` a todos los clientes conectados, así que la cola de aprobaciones de la TUI la muestra.
- Se envía al chat vinculado con botones de permitir y denegar.
- La primera respuesta desde cualquiera de los dos lados gana. Todos los demás reciben `approval.resolved` con la decisión y quién la tomó.
- Solo el `--user` vinculado puede responder desde el chat. Las pulsaciones de cualquier otra persona se ignoran y se registran.
- Pasados `approvals.timeoutSec` (300 segundos por defecto) sin respuesta, se deniega.

Las aprobaciones que levantas escribiendo en la TUI se quedan en esa superficie y nunca entran en la cola compartida. Esa frontera es justo el objetivo: los diálogos de tu propia terminal no se filtran a un chat de grupo, y una aprobación de chat no puede ser respondida por un desconocido.

Todo lo decidido se añade a `$SNOWPEA_HOME/logs/approvals.jsonl` con el id de la petición, la herramienta, la decisión, quién decidió, el alcance, y si fue desatendida.

## Agentes nombrados

Un agente nombrado sobrevive a los reinicios del daemon con su propia sesión, su propio espacio de memoria, sus propios canales y sus propios jobs. Dos agentes nombrados no pueden leer las memorias del otro, que es lo que hace razonable darle a uno un canal de trabajo y a otro uno personal.

```bash
snowpea agents --json
```

Crea uno con `/agent create "<description>"`, vincúlale un canal con `gateway bind ... agent:<name>`, y será restaurado en el siguiente arranque del daemon junto con sus vinculaciones. Los agentes nombrados son el cuarto contador de keepalive, así que un daemon que tenga uno nunca se apaga por inactividad.

## Seguridad

El gateway es una vía de ejecución remota. Trátalo como tal.

- Vincula `--user`. Siempre.
- Prefiere el modo plan para cualquier cosa alcanzable desde un chat que no controles del todo.
- Las peticiones de aprobación son de un solo uso y están atadas a un id de petición; una pulsación de botón repetida no hace nada.
- Las credenciales viven en un fichero `0600`, y se usa el llavero del sistema operativo donde está disponible.
- Lee `approvals.jsonl` de vez en cuando. Es el registro de lo que se permitió mientras no mirabas.

## Siguiente

[Backends](backends.md) — ejecutar las herramientas en otro sitio.
