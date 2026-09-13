"""Extension -> LSP language id (M13 contract §2).

Ported from opencode's ``packages/opencode/src/lsp/language.ts`` (MIT, commit
95daf90; reference clone ``/tmp/opencode-ref``).  The table is upstream's
verbatim; only the shape changed (a TypeScript ``Record`` became a dict) and
:func:`language_id` was added because the Python client needs the lookup to be
one call rather than an inline ``?? "plaintext"``.

# MIT License
#
# Copyright (c) 2025 opencode
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""

from __future__ import annotations

from pathlib import Path

#: What a server is told a document is when the extension is unknown.
PLAINTEXT = "plaintext"

LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".abap": "abap",
    ".bat": "bat",
    ".bib": "bibtex",
    ".bibtex": "bibtex",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".cljc": "clojure",
    ".edn": "clojure",
    ".coffee": "coffeescript",
    ".c": "c",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".c++": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".h++": "cpp",
    ".cs": "csharp",
    ".csx": "csharp",
    ".css": "css",
    ".d": "d",
    ".pas": "pascal",
    ".pascal": "pascal",
    ".diff": "diff",
    ".patch": "diff",
    ".dart": "dart",
    ".dockerfile": "dockerfile",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".ets": "typescript",
    ".hrl": "erlang",
    ".fs": "fsharp",
    ".fsi": "fsharp",
    ".fsx": "fsharp",
    ".fsscript": "fsharp",
    ".gitcommit": "git-commit",
    ".gitrebase": "git-rebase",
    ".gleam": "gleam",
    ".go": "go",
    ".groovy": "groovy",
    ".hbs": "handlebars",
    ".handlebars": "handlebars",
    ".hcl": "hcl",
    ".hs": "haskell",
    ".lhs": "haskell",
    ".html": "html",
    ".htm": "html",
    ".ini": "ini",
    ".java": "java",
    ".jl": "julia",
    ".js": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".jsx": "javascriptreact",
    ".json": "json",
    ".jsonc": "jsonc",
    ".tex": "latex",
    ".latex": "latex",
    ".less": "less",
    ".lua": "lua",
    ".makefile": "makefile",
    "makefile": "makefile",
    ".md": "markdown",
    ".markdown": "markdown",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".nix": "nix",
    ".pl": "perl",
    ".pm": "perl",
    ".pm6": "perl6",
    ".php": "php",
    ".prisma": "prisma",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".pug": "jade",
    ".jade": "jade",
    ".py": "python",
    ".pyi": "python",
    ".r": "r",
    ".cshtml": "razor",
    ".razor": "razor",
    ".rb": "ruby",
    ".rake": "ruby",
    ".gemspec": "ruby",
    ".ru": "ruby",
    ".erb": "erb",
    ".html.erb": "erb",
    ".js.erb": "erb",
    ".css.erb": "erb",
    ".json.erb": "erb",
    ".rs": "rust",
    ".scss": "scss",
    ".sass": "sass",
    ".scala": "scala",
    ".shader": "shaderlab",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".zsh": "shellscript",
    ".ksh": "shellscript",
    ".sql": "sql",
    ".svelte": "svelte",
    ".swift": "swift",
    ".tf": "terraform",
    ".tfvars": "terraform-vars",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".mts": "typescript",
    ".cts": "typescript",
    ".mtsx": "typescriptreact",
    ".ctsx": "typescriptreact",
    ".typ": "typst",
    ".typc": "typst",
    ".vue": "vue",
    ".xml": "xml",
    ".xsl": "xsl",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".zig": "zig",
    ".zon": "zig",
    ".astro": "astro",
}


def language_id(path: str | Path) -> str:
    """LSP ``languageId`` for ``path``; :data:`PLAINTEXT` when unknown.

    Extension-less names are matched whole and lower-cased, which is how
    ``Makefile`` and ``Dockerfile`` reach a language id at all.
    """
    name = Path(path).name
    suffix = Path(name).suffix.lower()
    if suffix:
        return LANGUAGE_EXTENSIONS.get(suffix, PLAINTEXT)
    return LANGUAGE_EXTENSIONS.get(name.lower(), PLAINTEXT)


def extension_of(path: str | Path) -> str:
    """The suffix a :class:`~snowpea_core.lsp.servers.ServerInfo` is matched on.

    Upstream uses ``path.parse(file).ext || file``; an extension-less file is
    matched by its whole name so ``Dockerfile`` can select a server.
    """
    name = Path(path).name
    return Path(name).suffix or name


__all__ = ["LANGUAGE_EXTENSIONS", "PLAINTEXT", "extension_of", "language_id"]
