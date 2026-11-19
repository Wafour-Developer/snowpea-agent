"""The language of what a person reads in RPC errors (protocol 1.65.0, ``clientLocale``).

A client says its language once, in ``system.hello {locale}``; the connection
keeps it (``RpcConnection.locale``) and the dispatcher puts it in
:data:`CURRENT_LOCALE` for every call that connection makes, so validation
problems and refusals come back in that language without a ``locale``
parameter threaded through every signature.  Which locale a call gets:

1. the call's own ``locale`` parameter, on a method that has one
   (``security.presets.list``, ``security.get``, ``security.check``, …);
2. the connection's hello ``locale``;
3. English (core has no ui language setting of its own).

Only human text is translated.  Stable machine codes (``details.reason``,
``problemItems[].reason`` / ``code``) stay as they are, in English.
Texts pick a language with :func:`snowpea_core.security.catalog.pick`: the tag
itself (``zh-CN``), then its language (``ko-KR`` -> ``ko``), then English.
"""

from __future__ import annotations

import contextvars
import re
from typing import Any

from snowpea_core.security.catalog import DEFAULT_LOCALE, LOCALES, pick

#: Longest tag ``system.hello`` keeps (BCP 47's own practical limit).
MAX_TAG_CHARS = 35
#: A BCP 47 tag, loosely: ``ko``, ``ko-KR``, ``zh-Hans-CN``, ``pt_BR``.
_TAG = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")

#: ``(surface id, locale)`` of the RPC call being handled; set by the dispatcher.
CURRENT_LOCALE: contextvars.ContextVar[tuple[str | None, str | None] | None] = (
    contextvars.ContextVar("snowpea_rpc_locale", default=None)
)


def normalise_locale(value: Any) -> str | None:
    """``"ko_kr"`` -> ``"ko-KR"``, ``"zh-hans-cn"`` -> ``"zh-Hans-CN"``; ``None`` for
    anything that is not a language tag of at most :data:`MAX_TAG_CHARS` characters."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_TAG_CHARS or not _TAG.match(text):
        return None
    parts = text.replace("_", "-").split("-")
    out = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha() or len(part) == 3 and part.isdigit():
            out.append(part.upper())  # region
        elif len(part) == 4 and part.isalpha():
            out.append(part.title())  # script
        else:
            out.append(part.lower())
    return "-".join(out)


def current_locale(conn: Any = None) -> str | None:
    """The locale texts for this call are in (``None``: English).

    ``conn``: the connection a text goes to.  The dispatcher's locale applies
    when it is that connection's own call (or no connection is named); a text
    for another connection — a background task answering a different
    surface — uses that connection's hello ``locale``.
    """
    held = CURRENT_LOCALE.get()
    if held is not None and (conn is None or held[0] == getattr(conn, "surface_id", None)):
        return held[1]
    return getattr(conn, "locale", None) if conn is not None else None


def call_locale(conn: Any, explicit: Any = None) -> str | None:
    """What the dispatcher records for one call: its own ``locale`` parameter when
    that is a language tag, else the connection's hello ``locale``."""
    return normalise_locale(explicit) or getattr(conn, "locale", None)


def text(message: str, /, *, conn: Any = None, locale: str | None = None, **values: Any) -> str:
    """:data:`MESSAGES` ``[message]`` in ``locale`` (default :func:`current_locale`),
    filled in with ``values``."""
    chosen = locale if locale is not None else current_locale(conn)
    return pick(MESSAGES[message], chosen or DEFAULT_LOCALE).format(**values)


#: Every text in :data:`LOCALES`; ``{name}`` placeholders match across languages
#: (``tests/test_client_locale.py`` holds them to it).  ``{{``/``}}`` are braces.
MESSAGES: dict[str, dict[str, str]] = {
    # -- security/lists.py: path_problem -----------------------------------
    "path.empty": {
        "en": "a protected path must be a non-empty path",
        "ko": "보호할 경로는 비어 있지 않은 경로여야 합니다",
        "ja": "保護するパスは空でないパスでなければなりません",
        "es": "una ruta protegida no puede estar vacía",
        "zh-CN": "受保护的路径不能为空",
    },
    "path.one_line": {
        "en": "a protected path must be one line of at most {max} characters",
        "ko": "보호할 경로는 {max}자 이하의 한 줄이어야 합니다",
        "ja": "保護するパスは {max} 文字以内の 1 行でなければなりません",
        "es": "una ruta protegida debe ser una sola línea de como máximo {max} caracteres",
        "zh-CN": "受保护的路径必须是一行，且不超过 {max} 个字符",
    },
    "path.not_absolute": {
        "en": "{path} is not an absolute path; write it from the root (/home/me/x) or from "
              "your home (~/x)",
        "ko": "{path}은(는) 절대 경로가 아닙니다. 루트(/home/me/x)나 홈(~/x)부터 적어 주세요",
        "ja": "{path} は絶対パスではありません。ルート (/home/me/x) かホーム (~/x) から"
              "書いてください",
        "es": "{path} no es una ruta absoluta; escríbela desde la raíz (/home/me/x) o desde "
              "tu carpeta personal (~/x)",
        "zh-CN": "{path} 不是绝对路径；请从根目录 (/home/me/x) 或主目录 (~/x) 开始写",
    },
    "path.other_home": {
        "en": "write another user's home as an absolute path, not ~user",
        "ko": "다른 사용자의 홈은 ~user가 아니라 절대 경로로 적어 주세요",
        "ja": "他のユーザーのホームは ~user ではなく絶対パスで書いてください",
        "es": "escribe la carpeta personal de otro usuario como ruta absoluta, no como ~usuario",
        "zh-CN": "其他用户的主目录请写成绝对路径，而不是 ~user",
    },
    "path.whole_disk": {
        "en": "protecting the whole disk would block every change anywhere; the essential "
              "preset already protects / itself",
        "ko": "디스크 전체를 보호하면 어디서든 모든 변경이 막힙니다. essential 프리셋이 이미 "
              "/ 자체를 보호합니다",
        "ja": "ディスク全体を保護するとあらゆる場所の変更がすべて止まります。essential "
              "プリセットがすでに / 自体を保護しています",
        "es": "proteger todo el disco bloquearía cualquier cambio en cualquier sitio; el preset "
              "essential ya protege / en sí",
        "zh-CN": "保护整个磁盘会阻止任何地方的所有更改；essential 预设已经保护了 / 本身",
    },
    # -- security/lists.py: problems_in (settings.set, security.check draft) --
    "security.not_object": {
        "en": "security must be an object {{blockedCommands, protectedPaths}}",
        "ko": "security는 객체 {{blockedCommands, protectedPaths}}여야 합니다",
        "ja": "security はオブジェクト {{blockedCommands, protectedPaths}} でなければなりません",
        "es": "security debe ser un objeto {{blockedCommands, protectedPaths}}",
        "zh-CN": "security 必须是对象 {{blockedCommands, protectedPaths}}",
    },
    "security.unknown_code": {
        "en": "security.unknownCode must be 'ask', 'auto' or 'allow', not {value}",
        "ko": "security.unknownCode는 'ask', 'auto', 'allow' 중 하나여야 합니다({value}은(는) "
              "안 됩니다)",
        "ja": "security.unknownCode は 'ask'、'auto'、'allow' のいずれかです ({value} は"
              "使えません)",
        "es": "security.unknownCode debe ser 'ask', 'auto' o 'allow', no {value}",
        "zh-CN": "security.unknownCode 必须是 'ask'、'auto' 或 'allow'，而不是 {value}",
    },
    "security.enabled": {
        "en": "security.enabled must be true or false, not {value}",
        "ko": "security.enabled는 true나 false여야 합니다({value}은(는) 안 됩니다)",
        "ja": "security.enabled は true か false です ({value} は使えません)",
        "es": "security.enabled debe ser true o false, no {value}",
        "zh-CN": "security.enabled 必须是 true 或 false，而不是 {value}",
    },
    "security.not_a_setting": {
        "en": "security.{key} is not a setting (enabled, blockedCommands, protectedPaths, "
              "unknownCode)",
        "ko": "security.{key}은(는) 없는 설정입니다(enabled, blockedCommands, protectedPaths, "
              "unknownCode)",
        "ja": "security.{key} という設定はありません (enabled、blockedCommands、"
              "protectedPaths、unknownCode)",
        "es": "security.{key} no es un ajuste (enabled, blockedCommands, protectedPaths, "
              "unknownCode)",
        "zh-CN": "security.{key} 不是一个设置项（enabled、blockedCommands、protectedPaths、"
                 "unknownCode）",
    },
    "security.section_not_object": {
        "en": "security.{name} must be an object {{presets, custom}}",
        "ko": "security.{name}은(는) 객체 {{presets, custom}}여야 합니다",
        "ja": "security.{name} はオブジェクト {{presets, custom}} でなければなりません",
        "es": "security.{name} debe ser un objeto {{presets, custom}}",
        "zh-CN": "security.{name} 必须是对象 {{presets, custom}}",
    },
    "security.section_key": {
        "en": "security.{name}.{key} is not a setting (presets, custom)",
        "ko": "security.{name}.{key}은(는) 없는 설정입니다(presets, custom)",
        "ja": "security.{name}.{key} という設定はありません (presets、custom)",
        "es": "security.{name}.{key} no es un ajuste (presets, custom)",
        "zh-CN": "security.{name}.{key} 不是一个设置项（presets、custom）",
    },
    "security.presets_not_list": {
        "en": "security.{name}.presets must be a list of preset ids",
        "ko": "security.{name}.presets는 프리셋 id의 목록이어야 합니다",
        "ja": "security.{name}.presets はプリセット id のリストでなければなりません",
        "es": "security.{name}.presets debe ser una lista de ids de presets",
        "zh-CN": "security.{name}.presets 必须是预设 id 的列表",
    },
    "security.unknown_preset": {
        "en": "security.{name}.presets: unknown preset {item} (known: {known})",
        "ko": "security.{name}.presets: 알 수 없는 프리셋 {item}(있는 프리셋: {known})",
        "ja": "security.{name}.presets: 不明なプリセット {item} (使えるもの: {known})",
        "es": "security.{name}.presets: preset desconocido {item} (conocidos: {known})",
        "zh-CN": "security.{name}.presets：未知的预设 {item}（可用：{known}）",
    },
    "security.custom_not_list": {
        "en": "security.{name}.custom must be a list",
        "ko": "security.{name}.custom은(는) 목록이어야 합니다",
        "ja": "security.{name}.custom はリストでなければなりません",
        "es": "security.{name}.custom debe ser una lista",
        "zh-CN": "security.{name}.custom 必须是列表",
    },
    "security.too_many": {
        "en": "security.{name}.custom holds more than {max} entries",
        "ko": "security.{name}.custom에 항목이 {max}개보다 많습니다",
        "ja": "security.{name}.custom の項目が {max} 個を超えています",
        "es": "security.{name}.custom tiene más de {max} entradas",
        "zh-CN": "security.{name}.custom 的条目超过 {max} 个",
    },
    "security.bad_command": {
        "en": "blocked command {item}: {problem}",
        "ko": "차단할 명령 {item}: {problem}",
        "ja": "ブロックするコマンド {item}: {problem}",
        "es": "comando bloqueado {item}: {problem}",
        "zh-CN": "要阻止的命令 {item}：{problem}",
    },
    "security.path_keys": {
        "en": "protected path entry has unknown keys: {keys}",
        "ko": "보호 경로 항목에 알 수 없는 키가 있습니다: {keys}",
        "ja": "保護パスの項目に不明なキーがあります: {keys}",
        "es": "la entrada de ruta protegida tiene claves desconocidas: {keys}",
        "zh-CN": "受保护路径条目含有未知的键：{keys}",
    },
    "security.path_level": {
        "en": "protected path level must be 'write' or 'all', not {level}",
        "ko": "보호 경로의 level은 'write'나 'all'이어야 합니다({level}은(는) 안 됩니다)",
        "ja": "保護パスの level は 'write' か 'all' です ({level} は使えません)",
        "es": "el nivel de una ruta protegida debe ser 'write' o 'all', no {level}",
        "zh-CN": "受保护路径的 level 必须是 'write' 或 'all'，而不是 {level}",
    },
    "security.bad_path": {
        "en": "protected path {path}: {problem}",
        "ko": "보호 경로 {path}: {problem}",
        "ja": "保護パス {path}: {problem}",
        "es": "ruta protegida {path}: {problem}",
        "zh-CN": "受保护路径 {path}：{problem}",
    },
    # -- security/lists.py: from_raw (security.get problems) -----------------
    "lists.presets_not_list": {
        "en": "{where}: presets must be a list; read as the defaults",
        "ko": "{where}: presets는 목록이어야 합니다. 기본값으로 읽었습니다",
        "ja": "{where}: presets はリストでなければなりません。既定値として読みました",
        "es": "{where}: presets debe ser una lista; se leyó como los valores predeterminados",
        "zh-CN": "{where}：presets 必须是列表；已按默认值读取",
    },
    "lists.unknown_preset": {
        "en": "{where}: unknown preset {item}",
        "ko": "{where}: 알 수 없는 프리셋 {item}",
        "ja": "{where}: 不明なプリセット {item}",
        "es": "{where}: preset desconocido {item}",
        "zh-CN": "{where}：未知的预设 {item}",
    },
    "lists.custom_not_list": {
        "en": "{where}: custom must be a list; {value} was ignored (none of it is in force)",
        "ko": "{where}: custom은 목록이어야 합니다. {value}은(는) 무시되었습니다(아무것도 "
              "적용되지 않습니다)",
        "ja": "{where}: custom はリストでなければなりません。{value} は無視されました "
              "(どれも適用されていません)",
        "es": "{where}: custom debe ser una lista; se ignoró {value} (nada de ello está en "
              "vigor)",
        "zh-CN": "{where}：custom 必须是列表；{value} 已被忽略（其中任何内容都未生效）",
    },
    "lists.pattern_skipped": {
        "en": "{where}: pattern {item} skipped ({problem})",
        "ko": "{where}: 패턴 {item}을(를) 건너뛰었습니다({problem})",
        "ja": "{where}: パターン {item} をスキップしました ({problem})",
        "es": "{where}: se omitió el patrón {item} ({problem})",
        "zh-CN": "{where}：已跳过模式 {item}（{problem}）",
    },
    "lists.entry_skipped": {
        "en": "{where}: entry {item} skipped",
        "ko": "{where}: 항목 {item}을(를) 건너뛰었습니다",
        "ja": "{where}: 項目 {item} をスキップしました",
        "es": "{where}: se omitió la entrada {item}",
        "zh-CN": "{where}：已跳过条目 {item}",
    },
    "lists.path_skipped": {
        "en": "{where}: path {path} skipped ({problem})",
        "ko": "{where}: 경로 {path}을(를) 건너뛰었습니다({problem})",
        "ja": "{where}: パス {path} をスキップしました ({problem})",
        "es": "{where}: se omitió la ruta {path} ({problem})",
        "zh-CN": "{where}：已跳过路径 {path}（{problem}）",
    },
    # -- security.get problems -------------------------------------------------
    "get.disabled": {
        "en": "security checks are off (security.enabled: false): blocked commands and "
              "protected paths are not applied and code snowpea cannot read ahead is not asked "
              "about; snowpea's own controls and the mode rules still apply",
        "ko": "보안 검사가 꺼져 있습니다(security.enabled: false). 차단 명령과 보호 경로가 "
              "적용되지 않고, snowpea가 미리 읽을 수 없는 코드도 묻지 않습니다. snowpea 자체의 "
              "통제와 모드 규칙은 그대로 적용됩니다",
        "ja": "セキュリティチェックはオフです (security.enabled: false)。ブロックするコマンドと"
              "保護パスは適用されず、snowpea が事前に読めないコードも確認しません。snowpea "
              "自身の制御とモードのルールは引き続き適用されます",
        "es": "las comprobaciones de seguridad están desactivadas (security.enabled: false): "
              "no se aplican los comandos bloqueados ni las rutas protegidas y no se pregunta "
              "por el código que snowpea no puede leer de antemano; los controles propios de "
              "snowpea y las reglas de los modos siguen aplicándose",
        "zh-CN": "安全检查已关闭（security.enabled: false）：不应用被阻止的命令和受保护的路径，"
                 "也不会询问 snowpea 无法预先读懂的代码；snowpea 自身的控制和模式规则仍然适用",
    },
    "get.section_invalid": {
        "en": "the security section of settings.json is not valid: the default lists apply "
              "and unknown code asks until it is fixed (the file as it was: "
              "settings.json.invalid)",
        "ko": "settings.json의 security 항목이 올바르지 않습니다. 고칠 때까지 기본 목록이 "
              "적용되고 알 수 없는 코드는 묻습니다(원래 파일: settings.json.invalid)",
        "ja": "settings.json の security セクションが正しくありません。直すまで既定のリストが"
              "適用され、不明なコードは確認されます (元のファイル: settings.json.invalid)",
        "es": "la sección security de settings.json no es válida: se aplican las listas "
              "predeterminadas y el código desconocido pregunta hasta que se corrija (el "
              "archivo tal como estaba: settings.json.invalid)",
        "zh-CN": "settings.json 的 security 部分无效：在修复之前应用默认列表，未知代码会询问"
                 "（原文件：settings.json.invalid）",
    },
    "get.credentials_link": {
        "en": "{path} is a symbolic link: no stored credential is used; replace the link with "
              "the file itself",
        "ko": "{path}은(는) 심볼릭 링크입니다. 저장된 자격 증명을 쓰지 않습니다. 링크를 실제 "
              "파일로 바꿔 주세요",
        "ja": "{path} はシンボリックリンクです。保存された認証情報は使われません。リンクを"
              "ファイルそのものに置き換えてください",
        "es": "{path} es un enlace simbólico: no se usa ninguna credencial guardada; sustituye "
              "el enlace por el propio archivo",
        "zh-CN": "{path} 是符号链接：不会使用任何已保存的凭据；请用文件本身替换该链接",
    },
    "get.unknown_code_migrated": {
        "en": "snowpea 0.2.90 changed 'Commands and scripts snowpea cannot read ahead' from ask "
              "to auto (not asked in auto mode, asked in plan and accept); set it back to ask "
              "on this screen if you want every such command asked",
        "ko": "snowpea 0.2.90에서 'snowpea가 미리 읽을 수 없는 명령과 스크립트'가 ask에서 "
              "auto로 바뀌었습니다(auto 모드에서는 묻지 않고 plan과 accept에서는 묻습니다). "
              "그런 명령을 모두 묻게 하려면 이 화면에서 ask로 되돌리세요",
        "ja": "snowpea 0.2.90 で「snowpea が事前に読めないコマンドとスクリプト」が ask から "
              "auto に変わりました (auto モードでは確認せず、plan と accept では確認します)。"
              "そうしたコマンドをすべて確認したい場合は、この画面で ask に戻してください",
        "es": "snowpea 0.2.90 cambió 'Comandos y scripts que snowpea no puede leer de "
              "antemano' de ask a auto (no se pregunta en el modo auto, sí en plan y accept); "
              "vuelve a ponerlo en ask en esta pantalla si quieres que se pregunte por cada uno",
        "zh-CN": "snowpea 0.2.90 将“snowpea 无法预先读懂的命令和脚本”从 ask 改为 auto（auto "
                 "模式下不询问，plan 和 accept 模式下询问）；如果希望每个此类命令都询问，"
                 "请在此界面改回 ask",
    },
    "get.settings_waiting": {
        "en": "settings.json changed on disk: settings that can run programs or widen access "
              "({keys}) are not applied and wait for you (settings.acceptDiskChange)",
        "ko": "디스크의 settings.json이 바뀌었습니다. 프로그램을 실행하거나 접근을 넓힐 수 있는 "
              "설정({keys})은 적용되지 않고 확인을 기다립니다(settings.acceptDiskChange)",
        "ja": "ディスク上の settings.json が変更されました。プログラムを実行したりアクセスを"
              "広げたりできる設定 ({keys}) は適用されず、確認を待っています "
              "(settings.acceptDiskChange)",
        "es": "settings.json cambió en el disco: los ajustes que pueden ejecutar programas o "
              "ampliar el acceso ({keys}) no se aplican y esperan tu confirmación "
              "(settings.acceptDiskChange)",
        "zh-CN": "磁盘上的 settings.json 已更改：可运行程序或扩大访问权限的设置（{keys}）"
                 "未应用，正在等待你确认（settings.acceptDiskChange）",
    },
    # -- settings.set -----------------------------------------------------------
    "settings.global_only": {
        "en": "{key} is a global setting; a project cannot change it",
        "ko": "{key}은(는) 전역 설정이라 프로젝트에서 바꿀 수 없습니다",
        "ja": "{key} はグローバル設定のため、プロジェクトでは変更できません",
        "es": "{key} es un ajuste global; un proyecto no puede cambiarlo",
        "zh-CN": "{key} 是全局设置；项目无法更改它",
    },
    "settings.project_add_only": {
        "en": "a project can only add to the security lists; it would remove {removed} "
              "(change the global lists instead)",
        "ko": "프로젝트는 보안 목록에 추가만 할 수 있습니다. 이 변경은 {removed}을(를) "
              "없앱니다(전역 목록을 바꿔 주세요)",
        "ja": "プロジェクトはセキュリティリストに追加することしかできません。この変更は "
              "{removed} を削除します (グローバルのリストを変更してください)",
        "es": "un proyecto solo puede añadir a las listas de seguridad; esto quitaría "
              "{removed} (cambia las listas globales en su lugar)",
        "zh-CN": "项目只能向安全列表添加内容；此更改会移除 {removed}（请改为修改全局列表）",
    },
    "settings.hidden_models": {
        "en": "{key} must be a list of model ids (at most {max}, each a non-empty string of at "
              "most {chars} characters)",
        "ko": "{key}은(는) 모델 id 목록이어야 합니다(최대 {max}개, 각각 {chars}자 이하의 "
              "비어 있지 않은 문자열)",
        "ja": "{key} はモデル id のリストでなければなりません (最大 {max} 個、それぞれ "
              "{chars} 文字以内の空でない文字列)",
        "es": "{key} debe ser una lista de ids de modelo (como máximo {max}, cada uno un texto "
              "no vacío de como máximo {chars} caracteres)",
        "zh-CN": "{key} 必须是模型 id 的列表（最多 {max} 个，每个都是不超过 {chars} 个字符的"
                 "非空字符串）",
    },
    # -- person-only refusals a browser shows ----------------------------------
    "refusal.browser": {
        "en": "a browser connection cannot do this: {why}",
        "ko": "브라우저 연결로는 할 수 없습니다: {why}",
        "ja": "ブラウザーの接続ではこの操作はできません: {why}",
        "es": "una conexión de navegador no puede hacer esto: {why}",
        "zh-CN": "浏览器连接无法执行此操作：{why}",
    },
    "refusal.browser_answer": {
        "en": "only a person may answer this; a browser's answer does not count: {why}",
        "ko": "사람만 답할 수 있습니다. 브라우저의 답은 인정되지 않습니다: {why}",
        "ja": "これに答えられるのは人だけです。ブラウザーの回答は数えられません: {why}",
        "es": "solo una persona puede responder a esto; la respuesta de un navegador no "
              "cuenta: {why}",
        "zh-CN": "只有本人可以回答此项；浏览器的回答不算数：{why}",
    },
    "browser_not_allowed": {
        "en": "a browser cannot allow this; use the snowpea app or the terminal",
        "ko": "브라우저에서는 허용할 수 없습니다. snowpea 앱이나 터미널을 쓰세요",
        "ja": "ブラウザーからは許可できません。snowpea アプリかターミナルを使ってください",
        "es": "un navegador no puede permitir esto; usa la app de snowpea o la terminal",
        "zh-CN": "浏览器无法允许此项；请使用 snowpea 应用或终端",
    },
    "browser_remote": {
        "en": "only a browser on this machine counts, not a remote one",
        "ko": "이 컴퓨터의 브라우저만 인정되고, 원격 브라우저는 안 됩니다",
        "ja": "このマシンのブラウザーだけが有効で、リモートのブラウザーは使えません",
        "es": "solo cuenta un navegador de esta máquina, no uno remoto",
        "zh-CN": "只有本机上的浏览器才算数，远程浏览器不行",
    },
    "browser_legacy": {
        "en": "this browser is too old to confirm it (0.4.0 or later is needed); allow it in "
              "the snowpea app or the terminal on this computer, or update the browser",
        "ko": "이 브라우저는 확인할 수 없는 이전 버전입니다(0.4.0 이상 필요). 이 컴퓨터의 "
              "snowpea 앱이나 터미널에서 허용하거나, 브라우저를 업데이트하세요",
        "ja": "このブラウザーは古く確認できません (0.4.0 以降が必要)。このコンピューターの "
              "snowpea アプリかターミナルで許可するか、ブラウザーを更新してください",
        "es": "este navegador es demasiado antiguo para confirmarlo (hace falta la 0.4.0 o "
              "posterior); permítelo en la app de snowpea o en la terminal de este ordenador, "
              "o actualiza el navegador",
        "zh-CN": "此浏览器版本过旧，无法确认（需要 0.4.0 或更高版本）；请在这台电脑上的 "
                 "snowpea 应用或终端中允许，或更新浏览器",
    },
    "browser_unconfirmed": {
        "en": "confirm it in the browser's own confirmation window, or allow it in the "
              "snowpea app or the terminal on this computer",
        "ko": "브라우저 자체 확인 창에서 확인하거나, 이 컴퓨터의 snowpea 앱이나 터미널에서 "
              "허용하세요",
        "ja": "ブラウザー自身の確認ウィンドウで確認するか、このコンピューターの snowpea "
              "アプリかターミナルで許可してください",
        "es": "confírmalo en la ventana de confirmación del propio navegador, o permítelo en "
              "la app de snowpea o en la terminal de este ordenador",
        "zh-CN": "请在浏览器自己的确认窗口中确认，或在这台电脑上的 snowpea 应用或终端中允许",
    },
    "browser_not_owner": {
        "en": "this browser did not open or host that session",
        "ko": "이 브라우저가 열었거나 맡고 있는 세션이 아닙니다",
        "ja": "このブラウザーが開いた、またはホストしているセッションではありません",
        "es": "este navegador no abrió ni aloja esa sesión",
        "zh-CN": "该会话不是此浏览器打开或托管的",
    },
    "messenger_blocked": {
        "en": "only a person at this machine may allow a blocked call; a messenger chat may "
              "only decline it. Allow it in the snowpea app or the terminal on this computer",
        "ko": "차단된 호출은 이 컴퓨터 앞의 사람만 허용할 수 있습니다. 메신저 채팅에서는 거절만 "
              "할 수 있습니다. 이 컴퓨터의 snowpea 앱이나 터미널에서 허용하세요",
        "ja": "ブロックされた呼び出しを許可できるのはこのマシンの前にいる人だけです。"
              "メッセンジャーのチャットからは拒否しかできません。このコンピューターの snowpea "
              "アプリかターミナルで許可してください",
        "es": "solo una persona en esta máquina puede permitir una llamada bloqueada; un chat "
              "de mensajería solo puede rechazarla. Permítelo en la app de snowpea o en la "
              "terminal de este ordenador",
        "zh-CN": "只有这台机器前的本人才能允许被阻止的调用；聊天软件中只能拒绝。请在这台电脑上的 "
                 "snowpea 应用或终端中允许",
    },
    "remote_blocked": {
        "en": "only a person at this machine may allow a blocked call; a remote web login may "
              "only decline it. Allow it in the snowpea app or the terminal on this computer",
        "ko": "차단된 호출은 이 컴퓨터 앞의 사람만 허용할 수 있습니다. 원격 웹 로그인에서는 "
              "거절만 할 수 있습니다. 이 컴퓨터의 snowpea 앱이나 터미널에서 허용하세요",
        "ja": "ブロックされた呼び出しを許可できるのはこのマシンの前にいる人だけです。"
              "リモートの Web ログインからは拒否しかできません。このコンピューターの snowpea "
              "アプリかターミナルで許可してください",
        "es": "solo una persona en esta máquina puede permitir una llamada bloqueada; un "
              "inicio de sesión web remoto solo puede rechazarla. Permítelo en la app de "
              "snowpea o en la terminal de este ordenador",
        "zh-CN": "只有这台机器前的本人才能允许被阻止的调用；远程网页登录只能拒绝。请在这台电脑上的 "
                 "snowpea 应用或终端中允许",
    },
    "trust_changed": {
        "en": "the project's control files are not what you were shown; look at them again",
        "ko": "프로젝트의 제어 파일이 보여 드린 내용과 다릅니다. 다시 확인해 주세요",
        "ja": "プロジェクトの制御ファイルが表示した内容と違います。もう一度確認してください",
        "es": "los archivos de control del proyecto no son los que se te mostraron; vuelve a "
              "revisarlos",
        "zh-CN": "项目的控制文件与之前向你展示的不同；请重新查看",
    },
    "trust.settings_repinned": {
        "en": "{file} changed since the folder's default mode was trusted; trust it again in "
              "the snowpea app or the terminal",
        "ko": "폴더의 기본 모드를 신뢰한 뒤로 {file}이(가) 바뀌었습니다. snowpea 앱이나 "
              "터미널에서 다시 신뢰해 주세요",
        "ja": "フォルダーの既定モードを信頼した後に {file} が変更されました。snowpea アプリか"
              "ターミナルでもう一度信頼してください",
        "es": "{file} cambió desde que se confió en el modo predeterminado de la carpeta; "
              "vuelve a confiar en ella en la app de snowpea o en la terminal",
        "zh-CN": "自信任该文件夹的默认模式以来 {file} 已更改；请在 snowpea 应用或终端中重新"
                 "信任",
    },
}


__all__ = [
    "CURRENT_LOCALE",
    "LOCALES",
    "MAX_TAG_CHARS",
    "MESSAGES",
    "call_locale",
    "current_locale",
    "normalise_locale",
    "text",
]
