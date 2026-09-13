# Setup

[English](../en/setup.md) · [한국어](../ko/setup.md) · [Todas las páginas](../README.md)

`snowpea setup` escribe `$SNOWPEA_HOME/settings.json`. Tiene tres formas.

```bash
snowpea setup            # quick: configure LLM models; defaults for other sections
snowpea setup --full     # every screen, in order
snowpea setup --blank    # asks nothing, writes the defaults
```

Quick es la respuesta correcta la primera vez. Full merece una pasada una vez que sabes qué quieres cambiar. Blank existe para instalaciones scripteadas y para CI.

## Las pantallas

`--full` recorre cinco pantallas y un resumen. Cada pantalla termina con **Skip — keep defaults**, y cada pantalla tiene un flag de línea de comandos para que nunca tengas que ser interactivo.

| Pantalla | Elección | Flag |
|---|---|---|
| Providers | proveedor de LLM, clave, modelo | `--vendor`, `--key`, `--model`, `--base-url` |
| Search | un proveedor de búsqueda web | `--search-provider` |
| Browser | un proveedor de navegador | `--browser-provider` |
| Tools | qué categorías de herramientas están activas | `--tools` |
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token`, `--user-id` |
| Done | resumen de lo que se escribió | — |

Las listas están ordenadas primero gratis-y-sin-clave, luego gratis-pero-necesita-clave o autoalojado, luego de pago. El valor por defecto en cada lista está marcado con una estrella. Nada en la configuración por defecto requiere una cuenta de pago más allá de tu proveedor de LLM: tanto la búsqueda web como el navegador funcionan sin ninguna clave.

## Varios modelos y asignaciones de agentes

Ejecuta `snowpea setup providers` para registrar varios modelos, elegir uno por defecto, y asignar modelos registrados a agentes incorporados o personalizados. Se admiten varios modelos del mismo proveedor. Limpia una asignación para devolver un agente al modelo por defecto.

Un perfil de modelo empareja un proveedor con un ID de modelo. Las credenciales y las URLs base siguen compartidas en `providers.<provider>`; los perfiles no duplican claves de API. Esta configuración ilustra la estructura; sustituye los IDs de modelo de ejemplo por reales.

```json
{
  "models": {
    "default": "daily",
    "profiles": {
      "daily": {"provider": "openai", "model": "your-default-model-id"},
      "reasoning": {"provider": "anthropic", "model": "your-reasoning-model-id"}
    }
  },
  "agents": {
    "max_concurrent": 3,
    "models": {"architect": "reasoning", "critic": "reasoning"}
  }
}
```

La precedencia del enrutado de agentes es **asignación del agente → modelo explícito en la definición del agente → modelo por defecto**. Los agentes sin asignación ni modelo explícito en su definición usan `models.default`. Las sesiones ordinarias nuevas también arrancan con el modelo por defecto; las anulaciones explícitas de proveedor/modelo de una sesión se conservan. Las sesiones existentes no se cambian automáticamente. Las instalaciones sin perfiles de modelo mantienen su comportamiento heredado.

## Proveedores

Once proveedores vienen incluidos en v0.1.

| Vendor id | Etiqueta | Adaptador | Autenticación |
|---|---|---|---|
| `anthropic` | Anthropic | Messages API nativa | Clave de API |
| `openai` | OpenAI | Compatible con OpenAI | Clave de API, inicio de sesión por código de dispositivo |
| `openrouter` | OpenRouter | Compatible con OpenAI | Clave de API, inicio de sesión OAuth PKCE |
| `gemini` | Google Gemini | nativo | Clave de API, OAuth de Google (ADC mediante `gcloud`), token de acceso |
| `xai` | xAI Grok | Compatible con OpenAI | Clave de API |
| `glm` | Zhipu GLM | Compatible con OpenAI | Clave de API |
| `minimax` | MiniMax | Compatible con OpenAI | Clave de API |
| `kimi` | Moonshot Kimi | Compatible con OpenAI | Clave de API |
| `deepseek` | DeepSeek | Compatible con OpenAI | Clave de API |
| `qwen` | Qwen | Compatible con OpenAI | Clave de API |
| `local` | Compatible con OpenAI local (vLLM, Ollama, LM Studio) | Compatible con OpenAI | URL base, clave opcional |

```bash
snowpea provider list
snowpea provider list --json
```

`provider list` muestra los métodos de autenticación de cada proveedor, el modelo por defecto y si está configurado.

En un escritorio, `snowpea provider login gemini` abre el inicio de sesión ADC de
Google. En una máquina remota o sin interfaz, ejecuta `snowpea provider login
gemini --token` y pega el token de acceso OAuth en el prompt oculto. OpenAI admite
la misma forma `--token`. Omite el valor para que el token no aparezca en el
historial del shell.

### Añadir una clave

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

Las variables de entorno también se detectan: si `OPENAI_API_KEY` o `ANTHROPIC_API_KEY` ya están exportadas, setup las ofrece en lugar de pedirte que las pegues.

### Inicio de sesión por navegador

Dos proveedores admiten iniciar sesión a través de un navegador en lugar de pegar una clave.

```bash
snowpea provider login openai        # device code: a code appears, you approve it in the browser
snowpea provider login openrouter    # OAuth PKCE: a local callback receives the code
```

`snowpea setup --login openai` es un alias de lo mismo. Cualquier otro proveedor responde con `login_unsupported` y te indica el comando `--vendor`/`--key` que debes ejecutar en su lugar:

**Resolución de problemas:** un inicio de sesión por código de dispositivo puede fallar con `device authorization failed (HTTP 403)` en algunas redes o cuentas aunque la misma petición funcione en otro sitio; el asistente imprime el texto de error del propio proveedor y vuelve a preguntar la elección de autenticación en lugar de salir, así que elige "1=API key" o "3=OAuth token" para continuar.

```bash
snowpea provider login deepseek
```

### Un modelo local

```bash
snowpea setup --vendor local --base-url http://localhost:11434/v1 --model qwen3:8b
```

Cualquier cosa que hable `/v1/chat/completions` funciona: vLLM, Ollama, LM Studio, el servidor de llama.cpp. El uso de herramientas (tool calling) tiene que estar soportado por el modelo que cargues, o el agente podrá hablar pero no actuar.

### Qué proveedor se usa

En orden de precedencia: la variable de entorno `SNOWPEA_PROVIDER`, luego `--provider` en la línea de comandos o el argumento `provider` de la sesión, luego `providers.default` en settings, luego el primer proveedor configurado.

```bash
snowpea -c "summarize README.md" --provider deepseek
```

## Proveedores de búsqueda

`web_search` y `web_extract` se apoyan en un registro de proveedores. El valor por defecto, `ddgs`, no necesita clave ni cuenta.

```bash
snowpea setup --search-provider ddgs
snowpea setup --search-provider exa --search-key sk-your-exa-key
snowpea setup search
```

`ddgs` es el único proveedor que no necesita absolutamente nada. Los ids `*_free` son *niveles* gratuitos de productos con clave, no endpoints sin clave: Exa responde `402` sin clave, Parallel y Keenable responden `401`, y Tavily también. Están etiquetados como `key required` y no pueden responder una búsqueda hasta que se configura una clave.

| id | etiqueta | necesita |
| --- | --- | --- |
| `ddgs` | gratis, sin clave | nada |
| `firecrawl` | de pago, clave opcional | nada; el endpoint de búsqueda en la nube responde sin clave pero con límite de tasa |
| `brave_free` | gratis, clave requerida | `BRAVE_API_KEY` |
| `exa_free` | sin clave | MCP alojado anónimo y con límite de tasa en `https://mcp.exa.ai/mcp` |
| `exa` | clave requerida | `EXA_API_KEY` (API REST directa) |
| `keenable_free`, `keenable` | clave requerida | `KEENABLE_API_KEY` |
| `parallel_free`, `parallel` | clave requerida | `PARALLEL_API_KEY` |
| `tavily` | gratis, clave requerida | `TAVILY_API_KEY` |
| `xai_grok` | de pago, clave requerida | `XAI_API_KEY` |
| `searxng` | gratis, autoalojado | `SEARXNG_URL` |
| `firecrawl_selfhost` | gratis, autoalojado | `FIRECRAWL_URL` |

Elegir un proveedor que requiere clave en `snowpea setup search` pide la clave (enmascarada) y la guarda bajo `search.credentials.<id>.api_key`; dejarla vacía imprime un aviso, porque un proveedor sin su clave no puede responder.

`exa_free` usa las herramientas MCP oficiales alojadas de Exa (`web_search_exa` y `web_fetch_exa`) de forma anónima; no se muestra ningún prompt de clave de API. Los límites de tasa anónimos siguen aplicándose. Elige `exa` cuando quieras la API directa con `EXA_API_KEY` y los límites de una cuenta de pago.

Cuando el proveedor configurado no puede ejecutarse, `web_search` cae a otro y lo dice en lugar de fingir. La sesión recibe un evento `error{code:"search_provider_unavailable"}`, y se le indica al asistente que te repita el motivo.

Comprueba qué proveedor responde de verdad:

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
snowpea tools list --json
```

`snowpea search test` ejecuta una consulta real con tu proveedor configurado e imprime el proveedor que respondió, más el motivo por el que se descartó cada proveedor saltado. `snowpea tools list` muestra `web_search` con su proveedor, escrito como `exa_free → ddgs` cuando el id configurado no puede ejecutarse.

`web_extract` rechaza direcciones privadas, loopback y link-local, y trunca las páginas obtenidas a `tools.max_output_chars` (20000 por defecto).

## Proveedores de navegador

`local_chromium` es el valor por defecto y ejecuta un Chromium headless mediante Playwright en tu propia máquina. La primera ejecución puede pedirte que descargues el binario del navegador. Los demás ids —`camoufox`, `browser_use_local`, `browserbase`, `firecrawl_cloud`— están registrados para que puedas verlos y seleccionarlos, y responden con `browser_provider_unavailable` hasta que estén configurados.

```bash
snowpea setup --browser-provider local_chromium
```

## Categorías de herramientas

Las categorías activan y desactivan grupos enteros de herramientas. Pasa una lista separada por comas; un `-` al inicio desactiva una.

```bash
snowpea setup --tools media,-browser
snowpea tools list
```

Las categorías son `file`, `terminal`, `git`, `web`, `browser`, `delegate`, `schedule`, `memory`, `media`, y `mcp` para cualquier cosa que aporte un servidor `.mcp.json`. Las herramientas de media (`image_generate`, `video_generate`, `music_generate`, `text_to_speech`) siempre están registradas pero permanecen `inactive` hasta que existen credenciales; pasan a `active` sin necesidad de reiniciar en cuanto se configuran, y llamar a una antes de eso devuelve `tool_inactive` con una pista.

## Gateway

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
```

La pantalla interactiva pide ambas cosas: el token del bot, y después tu propio id de cuenta en esa plataforma (Telegram te dice el tuyo si envías `/start` a [@userinfobot](https://t.me/userinfobot)). El id importa porque es la única cuenta autorizada a responder una aprobación desde el chat.

El token se guarda en `$SNOWPEA_HOME/credentials.json` (modo `0600`), y el mensajero empieza a escuchar con el daemon, sin ningún paso de vinculación. Vincular un bot a un agente, sesión o chat *concretos* sigue siendo un paso aparte, cubierto en [Gateway](gateway.md).

## Qué termina en disco

`$SNOWPEA_HOME/settings.json` contiene `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `gateway`, `agents.max_concurrent` (3), `team.max_conflict_retries` (2), `approvals.timeoutSec` (300) y `memory.enabled` (true). Las anulaciones por proyecto para modo, allowlist y backend viven en `<project>/.snowpea/settings.json` y tienen prioridad sobre el archivo global. Los secretos nunca se escriben en `settings.json`, y nunca se registran en logs.

## Siguiente

[Modos](modes.md) — decide cuánto puede hacer el agente sin preguntar.
