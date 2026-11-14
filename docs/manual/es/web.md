# snowpea web

`snowpea web` abre el agente en tu navegador. Es la misma pantalla que el IDE de snowpea — la misma barra lateral, conversación, trayectoria (Trajectory) y dock — servida por tu daemon local, así que no hay nada más que instalar y nada sale de tu máquina.

```bash
snowpea web            # start the daemon if needed, print the address, open it
snowpea web --no-open  # print the address only
```

La dirección tiene la forma `http://127.0.0.1:<port>/web/?token=…`. El token es el del propio daemon (el de `$SNOWPEA_HOME/daemon.json`); la página lo guarda para esa pestaña del navegador y lo quita de la barra de direcciones. Quien tenga el token puede manejar el agente, así que no pegues la dirección en chats ni en tickets. El daemon escucha solo en la dirección de loopback; para usar snowpea web desde otra máquina, abre un túnel del puerto por ssh (`ssh -L <port>:127.0.0.1:<port> host`).

## La pantalla

El diseño sigue el cliente web de DeepSeek Harness: tres columnas, con la conversación en el medio.

| Columna | Qué contiene |
|---|---|
| Izquierda — barra lateral | **New Session** (nueva sesión), Plugins, Automation tasks (tareas de automatización), More (las demás pantallas: skills, programación, ajustes…), y luego **Workspaces** (espacios de trabajo): un grupo por carpeta de proyecto con sus conversaciones, las ancladas primero. Un punto en una fila indica que un turno se está ejecutando, que hay una aprobación esperando o que hay algo sin leer. El menú de la fila ancla, renombra, marca como leído, bifurca, copia el id o archiva. |
| Centro — conversación | Una cabecera con el título de la conversación, los padres de los que se delegó o bifurcó, y dos pestañas: **Chat** (los mensajes, las tarjetas de herramientas y el campo de entrada) y **Trajectory** (trayectoria; ver abajo). |
| Derecha — dock | Pestañas que se abren junto a la conversación: archivos, el árbol de archivos del espacio de trabajo, el plan, el trabajo delegado, los cambios y, en el IDE, terminales. Los paneles se dividen y las pestañas se mueven entre ellos; el diseño se guarda por conversación en este navegador. |

La barra lateral mide 280 px (arrastra su borde entre 264 y 420), se pliega a un riel por debajo de 1024 px y pasa a ser un cajón por debajo de 768 px; `Ctrl/⌘ B` la alterna. El dock se abre al 45 % de la ventana, conserva el ancho que le des, nunca ocupa más del 70 % y se cierra antes que dejar la conversación en menos de 400 px; `Ctrl/⌘ J` lo alterna.

## Los archivos se abren en el dock

Un archivo que menciona la conversación se abre como pestaña en el dock, no en una ventana nueva:

- un enlace markdown a un archivo (`[math.ts]` + `(math.ts)`), una ruta relativa o absoluta, una ruta de Windows;
- una línea o un rango: `#L24`, `#L24-L30`, `:24`, `:24-30`;
- código en línea como `` `src/app.ts` ``, pero solo cuando nombra un archivo que el turno leyó o escribió;
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
- el selector de carpetas nativo — añade un espacio de trabajo escribiendo su ruta;
- leer las respuestas en voz alta.

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
