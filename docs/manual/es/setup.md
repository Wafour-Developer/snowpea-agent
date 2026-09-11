# Setup

[English](../en/setup.md) · [한국어](../ko/setup.md) · [Todas las páginas](../README.md)

`snowpea setup` escribe `$SNOWPEA_HOME/settings.json`. Tiene tres formas.

```bash
snowpea setup            # quick: pregunta por un proveedor de LLM, deja el resto en los valores por defecto
snowpea setup --full     # todas las pantallas, en orden
snowpea setup --blank    # no pregunta nada, escribe los valores por defecto
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
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token` |
| Done | resumen de lo que se escribió | — |

Las listas están ordenadas primero gratis-y-sin-clave, luego gratis-pero-necesita-clave o autoalojado, luego de pago. El valor por defecto en cada lista está marcado con una estrella. Nada en la configuración por defecto requiere una cuenta de pago más allá de tu proveedor de LLM: tanto la búsqueda web como el navegador funcionan sin ninguna clave.

## Proveedores

Once proveedores vienen incluidos en v0.1.

| Vendor id | Etiqueta | Adaptador | Autenticación |
|---|---|---|---|
| `anthropic` | Anthropic | Messages API nativa | Clave de API |
| `openai` | OpenAI | Compatible con OpenAI | Clave de API, inicio de sesión por código de dispositivo |
| `openrouter` | OpenRouter | Compatible con OpenAI | Clave de API, inicio de sesión OAuth PKCE |
| `gemini` | Google Gemini | nativo | Clave de API |
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

### Añadir una clave

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

Las variables de entorno también se detectan: si `OPENAI_API_KEY` o `ANTHROPIC_API_KEY` ya están exportadas, setup las ofrece en lugar de pedirte que las pegues.

### Inicio de sesión por navegador

Dos proveedores admiten iniciar sesión a través de un navegador en lugar de pegar una clave.

```bash
snowpea provider login openai        # código de dispositivo: aparece un código, lo apruebas en el navegador
snowpea provider login openrouter    # OAuth PKCE: un callback local recibe el código
```

`snowpea setup --login openai` es un alias de lo mismo. Cualquier otro proveedor responde con `login_unsupported` y te indica el comando `--vendor`/`--key` que debes ejecutar en su lugar:

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
snowpea setup --search-provider tavily
```

Gratis y sin clave: `ddgs` (por defecto), `exa_free`, `keenable_free`, `parallel_free`. Gratis con clave o autoalojado: `brave_free`, `tavily`, `searxng` (configura `SEARXNG_URL`), `firecrawl_selfhost`. De pago: `exa`, `keenable`, `parallel`, `firecrawl`, `xai_grok`. Si el proveedor configurado falla, `web_search` cae en cascada por la cadena gratuita y registra cuál respondió.

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
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token
```

Esto guarda el token en `$SNOWPEA_HOME/credentials.json` (modo `0600`) y nada más: vincular un bot a un agente o sesión es un paso aparte, cubierto en [Gateway](../en/gateway.md).

## Qué termina en disco

`$SNOWPEA_HOME/settings.json` contiene `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `gateway`, `agents.max_concurrent` (3), `team.max_conflict_retries` (2), `approvals.timeoutSec` (300) y `memory.enabled` (true). Las anulaciones por proyecto para modo, allowlist y backend viven en `<project>/.snowpea/settings.json` y tienen prioridad sobre el archivo global. Los secretos nunca se escriben en `settings.json`, y nunca se registran en logs.

## Siguiente

[Modes](../en/modes.md) — decide cuánto puede hacer el agente sin preguntar.
