# snowpea web

`snowpea web` abre el agente en tu navegador. Es la misma pantalla que el IDE de snowpea — la misma barra lateral, conversación, trayectoria (Trajectory) y dock — servida por tu daemon local, así que no hay nada más que instalar y nada sale de tu máquina.

```bash
snowpea web            # start the daemon if needed, print the address, open it
snowpea web --no-open  # print the address only
```

La dirección tiene la forma `http://127.0.0.1:<port>/web/?token=…`. El token es el del propio daemon (el de `$SNOWPEA_HOME/daemon.json`); la página lo guarda para esa pestaña del navegador y lo quita de la barra de direcciones. Quien tenga el token puede manejar el agente, así que no pegues la dirección en chats ni en tickets. Esta dirección solo funciona en la máquina donde corre el daemon.

## Desde otra máquina

Lo más sencillo sigue siendo un túnel ssh: no escucha nada más y la conexión va cifrada.

```bash
ssh -L <port>:127.0.0.1:<port> host
```

Para abrir snowpea web directamente desde otras máquinas —un portátil en la misma red, un teléfono— activa el acceso remoto. Es un segundo listener con usuario y contraseña, aparte del de loopback, que sigue funcionando como siempre.

1. Pon el usuario (y, si quieres, la dirección) en `settings.json`:

   ```json
   { "web": { "remote": { "enabled": true, "username": "me" } } }
   ```

2. Define la contraseña (al menos 8 caracteres). Se pide dos veces y se guarda solo como un hash scrypt con sal en `credentials.json` (modo 0600), nunca en `settings.json`. `--stdin` lee una línea, para scripts, y `--clear` la borra.

   ```bash
   snowpea web password
   snowpea web password --stdin
   snowpea web password --clear
   ```

3. Reinicia el daemon (`snowpea daemon stop` y luego `snowpea web`). La configuración se lee al arrancar el daemon; uno que ya está en marcha no vuelve a enlazar el puerto.

Entonces `snowpea web` imprime también la dirección para otras máquinas, `http://<dirección LAN de esta máquina>:8787/web/`, que muestra un formulario de inicio de sesión. `snowpea web status` muestra si el acceso remoto está activo, en qué dirección escucha, el usuario, si hay contraseña y cuántas sesiones hay abiertas.

| Ajuste (`web.remote.…`) | Por defecto | Significado |
|---|---|---|
| `enabled` | `false` | Arranca el segundo listener. |
| `host` | `0.0.0.0` | Dirección en la que escucha (`0.0.0.0` son todas las interfaces). |
| `port` | `8787` | Puerto en el que escucha. |
| `username` | `""` | La única cuenta que puede iniciar sesión. |
| `sessionHours` | `12` | Cuánto dura un inicio de sesión. |
| `allowedOrigins` | `[]` | Orígenes de página adicionales que acepta el WebSocket, p. ej. `https://snowpea.example.com` detrás de un proxy. |
| `trustProxy` | `false` | Creer `X-Forwarded-Proto` / `X-Forwarded-For` de un proxy inverso que controlas. |

Si el acceso remoto está activado sin usuario o sin contraseña, el daemon arranca solo en loopback, escribe el motivo en su log y `snowpea web` lo dice.

Notas de seguridad:

- **Es HTTP sin cifrar.** La contraseña y todo lo que hagas viajan en claro. Fuera de una red de confianza, ponlo detrás de TLS (un proxy inverso como Caddy o nginx, con `trustProxy: true`) o accede por una VPN o el túnel ssh de arriba.
- Quien inicia sesión maneja el agente y, con él, la shell y los archivos de esta máquina. Usa una contraseña larga.
- Cinco intentos fallidos desde una dirección en cinco minutos, o veinte en total, bloquean el inicio de sesión hasta que pasen esos cinco minutos. Los inicios de sesión y los fallos se registran con la dirección del cliente, nunca con la contraseña.
- La sesión es una cookie (`HttpOnly`, `SameSite=Strict`, `Secure` con HTTPS). Las sesiones viven en la memoria del daemon: reiniciarlo cierra la sesión de todos. Cambiar o borrar la contraseña (`snowpea web password`) cierra la sesión de todos al instante; las demás sesiones duran hasta que caducan o cierras sesión.
- Una página cuya sesión ha terminado (caducó, se cambió la contraseña o se cerró sesión en otro sitio) vuelve a la pantalla de inicio de sesión.
- El listener remoto sirve solo la página, el inicio de sesión y el WebSocket —ni `/health`, ni `/version`, ni el esquema— y el WebSocket rechaza páginas de otro origen.

### Una dirección pública con un túnel

`snowpea web` puede abrirte una dirección HTTPS pública a través de un programa de túnel, para que llegues a este equipo desde un teléfono o desde otra red sin abrir un puerto ni montar un proxy. Hay tres proveedores:

| Proveedor | Qué necesitas | Dirección |
|---|---|---|
| `cloudflare-quick` | `cloudflared`; sin cuenta | un `https://….trycloudflare.com` aleatorio, nuevo en cada arranque |
| `cloudflare` | `cloudflared`, un token de túnel de Cloudflare y su nombre de host | tu propio `https://<host>` |
| `ngrok` | `ngrok` y su authtoken | la dirección `https://….ngrok-free.app` que te da ngrok |

El túnel no arranca a menos que **el acceso remoto esté activado y haya una contraseña web** (`snowpea web --host … --port …`, `snowpea web password`): una dirección pública con solo el enlace de acceso no está permitida. El listener debe ser alcanzable desde el loopback de este equipo (`0.0.0.0` o `127.0.0.1`), porque el programa de túnel se conecta a él desde aquí.

```bash
snowpea web tunnel install                      # descarga el programa de túnel, si falta
printf '%s\n' "$TOKEN" | snowpea web tunnel token --provider cloudflare   # o ngrok; se lee de stdin
snowpea web tunnel start --provider cloudflare-quick --save   # --save conserva el proveedor para el próximo arranque
snowpea web tunnel status                       # estado, dirección, programa, si hay token guardado
snowpea web tunnel stop
```

Ajustes (`web.remote.tunnel`, se cambian solo en este equipo): `provider` (`off`, `cloudflare-quick`, `cloudflare`, `ngrok`), `autoStart` (arranca con el daemon cuando hay proveedor y se cumplen las condiciones; activado por defecto), `hostname` (el nombre de host público de un túnel `cloudflare`; ese proveedor no arranca sin él) y `binary` (un programa de túnel que se usa antes que `PATH` y `~/.snowpea/bin`).

- **El programa.** snowpea lo busca en `binary`, luego en `PATH` y después en `~/.snowpea/bin`. `snowpea web tunnel install` descarga una versión fijada de `cloudflared` desde su página oficial de GitHub y solo la instala si su sha256 coincide con el valor fijado; si no coincide, no escribe nada. Donde no hay checksum fijado (macOS, y ngrok en todas partes) se niega a instalar y muestra el comando que debes ejecutar tú (`brew install cloudflared`, `brew install ngrok`, `snap install ngrok`, `winget install …`).
- **Tokens.** El authtoken de ngrok y el token del túnel de Cloudflare se guardan en `credentials.json` (modo 600), nunca en `settings.json`. Establécelos con `snowpea web tunnel token --provider …` (una línea por stdin) o desde los ajustes de un cliente. Llegan al programa de túnel por su entorno, no por su línea de comandos, y se eliminan de cualquier cosa que snowpea registre o muestre.
- **Reinicios.** Si el programa de túnel se detiene, snowpea lo vuelve a arrancar tras 2 s, luego 4 s, 8 s y así hasta 60 s como máximo. Se rinde tras cinco arranques seguidos que nunca mostraron una dirección; `snowpea web tunnel status` dice por qué. Un túnel rápido recibe una dirección nueva tras cada reinicio, y los clientes se enteran por el evento `web.tunnel.changed`.
- **Orígenes y cabeceras de proxy.** Mientras el túnel funciona, su dirección es un origen permitido para el WebSocket de la página, y la dirección del visitante y el esquema `https` se toman de las cabeceras del túnel (`CF-Connecting-IP`, `X-Forwarded-*`), pero solo desde una conexión de este equipo, que es desde donde se conecta el programa de túnel. No se escribe nada en `allowedOrigins`.
- **Terminales a través del túnel.** Una página abierta por el túnel puede ver un terminal después de iniciar sesión, pero arrancar un terminal, escribir en él o usar cualquier opción peligrosa requiere un **desbloqueo de terminal**: la página vuelve a pedir la contraseña web (`web.tunnel.unlock`), y el desbloqueo dura lo que dure esa sesión. Las opciones peligrosas se rechazan por el túnel salvo que `maxMode` sea `auto`.

En la aplicación, **Settings → Tunnel** hace lo mismo: elige el proveedor, escribe el nombre de host (`cloudflare`), pega un token (**Save token**; el campo indica si hay uno guardado y nunca lo muestra), activa **Start the tunnel with snowpea** y luego **Start tunnel** / **Stop tunnel**. Si falta el programa, ofrece **Install** o el comando que ejecutar. Mientras funciona, la página muestra el estado, la dirección pública con **Copy** y un código QR para abrirla en el teléfono, y el error cuando lo hay. Un inicio de sesión remoto ve una nota en su lugar.

Un túnel hace la página accesible para cualquiera que pueda adivinar la dirección, así que se aplican todas las notas de acceso remoto de arriba: usa una contraseña larga y detén el túnel cuando no lo necesites.

## La pantalla

El diseño sigue el cliente web de DeepSeek Harness: tres columnas, con la conversación en el medio.

| Columna | Qué contiene |
|---|---|
| Izquierda — barra lateral | **New Session** (nueva sesión), Plugins, Automation tasks (tareas de automatización), More (las demás pantallas: skills, programación, ajustes…), y luego **Workspaces** (espacios de trabajo): un grupo por carpeta de proyecto con sus conversaciones, las ancladas primero. Un punto en una fila indica que un turno se está ejecutando, que hay una aprobación esperando o que hay algo sin leer. El menú de la fila ancla, renombra, bifurca, copia el id o archiva. |
| Centro — conversación | Una cabecera con el título de la conversación, los padres de los que se delegó o bifurcó, y dos pestañas: **Chat** (los mensajes, las tarjetas de herramientas y el campo de entrada) y **Trajectory** (trayectoria; ver abajo). |
| Derecha — dock | Pestañas que se abren junto a la conversación: archivos, el árbol de archivos del espacio de trabajo, el plan, el trabajo delegado, los cambios y terminales (en snowpea web, las [sesiones de terminal](terminals.md) del daemon; la aplicación de escritorio también tiene sus propias shells). Los paneles se dividen y las pestañas se mueven entre ellos; el diseño se guarda por conversación en este navegador. |

La barra lateral mide 280 px (arrastra su borde entre 264 y 420), se pliega a un riel por debajo de 1024 px y pasa a ser un cajón por debajo de 768 px; `Ctrl/⌘ B` la alterna. El dock se abre al 45 % de la ventana, conserva el ancho que le des, nunca ocupa más del 70 % y se cierra antes que dejar la conversación en menos de 400 px; `Ctrl/⌘ J` lo alterna.

Un panel vacío del dock muestra una pestaña **Start** con tarjetas para los archivos del espacio de trabajo, los cambios y el plan (la tarjeta de terminal aparece en snowpea web solo cuando el daemon ejecuta [sesiones de terminal](terminals.md)). La barra de estado muestra si la página está conectada, y una pestaña abierta impide que el daemon se apague por inactividad.

## Los archivos se abren en el dock

Un archivo que menciona la conversación se abre como pestaña en el dock, no en una ventana nueva:

- un enlace markdown a un archivo (`[math.ts]` + `(math.ts)`), una ruta relativa o absoluta, una ruta de Windows;
- una línea o un rango: `#L24`, `#L24-L30`, `:24`, `:24-30`;
- código en línea como `` `src/app.ts` ``, pero solo cuando nombra un archivo que el turno leyó o escribió;
- un nombre de archivo suelto en la respuesta, como `notes.md`, cuando el turno leyó o escribió ese archivo. Los demás nombres sueltos quedan como texto plano, y el texto plano solo se enlaza si es una dirección explícita http(s), mailto: o www.
- los chips de archivo y las tarjetas de artefacto bajo una respuesta.

Abrir de nuevo el mismo archivo enfoca su pestaña. La vista previa es la del IDE: código con resaltado, markdown, imágenes, PDF, hojas de cálculo, documentos de office y notebooks. Un archivo HTML se muestra **estático** por defecto (saneado, sin scripts, con una política de seguridad de contenido estricta); cámbialo a **ejecutar scripts** para cargarlo en un marco aislado que puede ejecutar sus propios scripts pero no puede alcanzar la página, tus cookies ni el daemon.

En el navegador, los archivos se leen a través del daemon (`fs.*`), y solo dentro de las carpetas de la conversación: su directorio de trabajo, su espacio de trabajo y sus adjuntos. Una ruta fuera de ellas se rechaza, igual que la rechazarían las propias herramientas del agente.

## Trayectoria

La pestaña **Trajectory** muestra lo que hizo el agente, paso a paso, en lugar de como un chat: cada turno es una banda, cada llamada al modelo dentro de él un paso con su texto, razonamiento y llamadas a herramientas, y cada llamada a una herramienta muestra sus argumentos, su resultado y cuánto tardó. El trabajo delegado aparece donde se inició; al hacer clic se abre la conversación hija en el dock, y un archivo en una llamada a una herramienta se abre en el dock como arriba. Se construye a partir del registro de eventos de la sesión (`session.trajectory`), así que es igual para un turno en curso, uno terminado o una conversación que retomes mañana.

## Bifurcar, anclar y archivar

Desde un turno terminado puedes **bifurcar** la conversación: empieza una conversación nueva con el historial hasta ese turno, y la original queda como está. Su cabecera muestra de dónde viene. Restaurar los archivos a antes de un turno (checkpoints) sigue funcionando como antes; bifurcar sirve para probar otra dirección sin perder esta.

**Anclar** mantiene una conversación arriba de su espacio de trabajo; **archivar** la oculta de la barra lateral sin borrarla (las opciones de vista de la barra lateral vuelven a mostrar las archivadas).

## Lo que el navegador no puede hacer

La página no tiene acceso a tu sistema operativo, así que esto se queda en el IDE y en la interfaz de terminal:

- terminales (Abrir en terminal, Nueva terminal);
- acciones de git que se ejecutan localmente, y Mostrar en la carpeta / Abrir con la aplicación del sistema;
- la terminal, las pantallas de archivos y worktrees, el gestor de conexiones y la autoactualización de la app (esas direcciones vuelven a la conversación);
- el selector de carpetas nativo — añade un espacio de trabajo escribiendo su ruta;

Los botones para estas acciones se ocultan en el navegador en lugar de fallar.

## De dónde sale la página

La build web viaja dentro del paquete de snowpea y el daemon la sirve bajo `/web/`. `index.html` nunca se cachea y cada otro archivo lleva un hash de contenido en su nombre, así que recargar tras una actualización siempre trae la versión nueva. Para desarrollo, apunta el daemon a otra build con `SNOWPEA_WEB_DIST`:

```bash
# in snowpea-ide
npm run build:web
# then, with a separate home so your own daemon is not touched
SNOWPEA_WEB_DIST=$PWD/out/web snowpea --home /tmp/snowpea-dev web
```

Si falta la build, `snowpea web` lo dice y la dirección responde `404`.

## Siguiente

- [Terminal UI](tui.md) — el mismo agente en la terminal
- [Protocol](protocol.md) — los RPC que usa la página (`workspace.*`, `session.trajectory`, `session.fork`, `session.lineage`, `fs.*`)
- [Sesiones remotas](../en/remote.md) — ejecutar el daemon en otra máquina
