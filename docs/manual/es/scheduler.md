# Planificador

El planificador vive dentro del daemon. Los jobs se ejecutan en el proceso del daemon, en el modo con el que los registraste, y entregan su respuesta al canal que nombres. Nada corre en un daemon de cron aparte y nada necesita que tu terminal esté abierta.

## Registrar un job

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check whether the build is green" --mode plan
snowpea job schedule --every 30m --task "watch the error log" --channel log --workdir ~/src/api
```

Desde dentro de una sesión:

```
/schedule "every day at 9am" "summarize yesterday's commits" --channel telegram:123456
/schedule
```

`--at`, `--in`, `--every`, `--cron` y `--spec` son cinco nombres para la misma opción. Usa el que haga que la línea se lea bien.

## Specs

| Forma | Ejemplo |
|---|---|
| cron, cinco campos | `0 9 * * *` |
| retardo de una sola vez | `in 60s`, `in 10m`, `in 2h` |
| intervalo | `every 30m`, `every 6h` |
| lenguaje natural, inglés | `every day at 9am`, `in 10 minutes` |
| lenguaje natural, coreano | `매일 09:00`, `10분 뒤` |

Un job es `cron`, `once` o `interval` según cómo se haya parseado. `job list` muestra el tipo parseado y la próxima hora de ejecución, que es la forma más rápida de comprobar que una spec en lenguaje natural significó lo que creías.

```bash
snowpea job list
snowpea job list --json
```

## Gestionar jobs

```bash
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`job run` dispara el job de inmediato en el daemon, exactamente como lo haría el temporizador. Es la forma correcta de probar un job antes de confiárselo a una programación.

Cada job registra `last_run` y `last_status`, que es `ok`, `error`, o `denied_by_timeout` cuando una aprobación expiró sin respuesta.

## Cómo se ejecuta un job

El planificador hace tick cada 15 segundos. Cuando un job toca, crea una sesión en el directorio de trabajo y el modo del job, marcada como desatendida, la promptea con la tarea del job, y entrega el mensaje final del asistente al canal. Las notificaciones `job.event` con tipo `started`, `finished`, `failed` o `denied` van a todos los clientes conectados, así que una TUI en marcha muestra el progreso del job.

Dos cosas merece la pena saberlas. Primera, una clave de ocurrencia formada por el id del job más la hora programada es única, así que un job no puede dispararse dos veces para la misma franja aunque el daemon se reinicie a mitad de un tick. Segunda, al arrancar, el planificador recupera los jobs de una sola vez que se perdió mientras el daemon estuvo caído, si llevan menos de una hora de retraso; los más viejos se marcan como perdidos en lugar de ejecutarse.

## Los recordatorios vuelven a la sesión que los pidió

Un job recuerda de dónde vino. La sesión que lo registró —un `/schedule` escrito en una sesión de la TUI, o la herramienta `schedule` usada dentro de un turno— queda estampada en el job como `originSessionId`, y `job list --json` la muestra. Cuando el job se dispara, su respuesta se emite en esa sesión como un evento `message.done` cuyo texto lleva un prefijo:

```text
⏰ Scheduled reminder (job_7f21c0)

Yesterday's commits: 14 across three repositories…
```

Así que el recordatorio aterriza en el hilo desde el que preguntaste, y queda persistido ahí como cualquier otro mensaje: abre esa sesión más tarde y el recordatorio está en su historial.

La sesión no tiene por qué seguir abierta. Una sesión cerrada se restaura únicamente para recibir la entrega y se cierra de nuevo inmediatamente después, así que un job que se dispara nunca deja una sesión corriendo a tus espaldas. Una sesión viva se deja en paz.

Un `--channel` explícito es aditivo, no un reemplazo. La sesión de origen recibe el recordatorio de todos modos, y el canal recibe el mismo texto además. Cuando no hay sesión de origen —un job registrado antes de que esto existiera, o uno cuya sesión se ha borrado— y el canal no se pudo entregar, el texto cae al log de jobs en `$SNOWPEA_HOME/logs/jobs.log`.

## Canales

| Canal | Va a |
|---|---|
| `telegram:<chat_id>` | un chat de Telegram |
| `discord:<channel_id>` | un canal de Discord |
| `slack:<channel>` | un canal de Slack |
| `log` | solo `$SNOWPEA_HOME/logs/daemon.log` |

La plataforma tiene que estar vinculada antes: mira [Gateway](gateway.md). `log` no necesita nada y es la elección correcta mientras todavía estás averiguando qué debería decir un job.

## Modos y aprobaciones

Un job lleva su propio modo, independiente de la sesión desde la que lo registraste. Registrar un job requiere de por sí un permiso `send`, así que en modo accept se te pedirá aprobar el propio registro; eso es deliberado, porque un job programado es una concesión permanente de todo lo que su modo permita.

Cuando el turno de un job se topa con algo que necesita aprobación, la petición es desatendida: aparece en la cola de aprobaciones de la TUI y en el chat vinculado con botones de permitir/denegar al mismo tiempo. Quien responda primero gana, a los demás se les dice qué pasó, y si nadie responde dentro de `approvals.timeoutSec` (300 segundos) la petición se deniega y el `last_status` del job pasa a `denied_by_timeout`.

Un job en modo plan puede leer y buscar pero nunca escribir. Un job en modo auto no pregunta nada en absoluto. Para cualquier cosa desatendida, plan es el valor seguro por defecto y auto merece un contenedor.

## Mantener vivo al daemon

Los jobs habilitados son uno de los cuatro contadores que suprimen el apagado por inactividad, así que un daemon con una programación se queda en pie por su cuenta:

```bash
snowpea daemon status
```

Eso imprime `will not exit` con el motivo. Sobrevivir a un reinicio es otra cuestión: para eso, registra el servicio:

```bash
snowpea service install
snowpea service status
```

## Siguiente

[Gateway](gateway.md) — a dónde van las respuestas.
