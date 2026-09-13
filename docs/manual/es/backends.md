# Backends de ejecución

Toda herramienta que toca un sistema de ficheros o ejecuta un comando pasa por un backend de ejecución. El backend decide *dónde* ocurre eso. El conjunto de herramientas no cambia: `read_file`, `shell`, `grep`, `git_commit` y las demás se comportan igual tanto si se ejecutan en tu portátil, como dentro de un contenedor, o en una máquina al otro lado de la red.

## Cambiar de backend

```
/backend
/backend local
/backend docker {"image": "python:3.11-slim"}
/backend ssh {"host": "10.0.0.5", "user": "build", "key": "~/.ssh/id_ed25519"}
```

`/backend` sin argumento imprime el actual. El cambio se aplica a la sesión de inmediato, incluido el trabajo que ya está en vuelo, a partir de la siguiente llamada a una herramienta. Un proyecto puede fijar el suyo por defecto en `<project>/.snowpea/settings.json` bajo `backend`.

## local

El valor por defecto. Los comandos se ejecutan con tu usuario, en el directorio de trabajo de la sesión, con tu entorno. Nada está aislado: este es el backend que quieres mientras trabajas en tu propio código, y el que no deberías apuntar a un trabajo en modo auto.

## docker

```
/backend docker {"image": "python:3.11-slim"}
```

| Clave | Por defecto | Significado |
|---|---|---|
| `image` | `python:3.11-slim` | imagen a partir de la cual se crea el contenedor |
| `workdir` | el workdir de la sesión | montado por bind en la misma ruta absoluta dentro |

El contenedor lleva el nombre de la sesión, se crea de forma perezosa en la primera llamada a una herramienta, y se elimina cuando la sesión se cierra. El directorio de trabajo se monta por bind en la misma ruta absoluta, así que las rutas sobre las que razona el modelo son las mismas dentro y fuera. Todo lo demás — paquetes instalados, red, entorno — es del contenedor.

Este es el backend correcto para el modo auto. El agente puede instalar lo que quiera y romper lo que quiera, y tú tiras el contenedor.

Requiere un daemon de Docker en ejecución. Sin él, el backend reporta el fallo en lugar de volver silenciosamente a local.

## ssh

```
/backend ssh {"host": "10.0.0.5", "port": 22, "user": "build", "key": "~/.ssh/id_ed25519"}
```

| Clave | Significado |
|---|---|
| `host` | nombre de host o dirección |
| `port` | por defecto 22 |
| `user` | usuario remoto |
| `key` | ruta de la clave privada |
| `password` | alternativa a `key` |
| `cwd` | directorio de trabajo remoto, por defecto el home remoto |

Los comandos se ejecutan por SSH; las lecturas y escrituras de ficheros van por SFTP. Los ficheros que escribe el agente existen solo en la máquina remota: no se replica nada localmente, y un `write_file` bajo un backend ssh deja tu propio disco intacto. La autenticación por clave es muy preferible; una contraseña en la configuración de una sesión es una contraseña en un fichero de configuración.

## Comprobar dónde estás

La prueba honesta es preguntar:

```bash
snowpea -c "run hostname and tell me what it printed"
```

Bajo `local` es tu máquina, bajo `docker` el id del contenedor, bajo `ssh` la máquina remota. Como toda la visión que el agente tiene del sistema de ficheros llega a través del backend, esto no es un detalle en el que pueda equivocarse.

## Qué no se mueve

El backend cubre la ejecución de herramientas, no el daemon. Las sesiones, la memoria, la cola de aprobaciones, el planificador y el gateway siguen en el daemon de tu máquina, use el backend que use una sesión. Las herramientas de red — `web_search`, `web_extract`, las herramientas de navegador, las herramientas de media y los servidores MCP — se ejecutan desde el daemon, no desde la máquina del backend.

## Siguiente

[Headless](headless.md) — usar todo esto desde un script.
