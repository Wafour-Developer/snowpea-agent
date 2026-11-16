"""Python code read as a syntax tree, for the security lists (review 6, H-3 and M-1).

``ast.parse`` only — nothing here compiles, imports, evaluates or runs the
text it is given.  The tree answers what a regular expression cannot:

* what a name stands for: ``import shutil as s``, ``from shutil import rmtree
  as r``, ``f = shutil.rmtree``, ``__import__("os")``,
  ``importlib.import_module("shutil")``, ``getattr(shutil, "rmtree")`` all
  resolve to the dotted name they are, however the call is spelled or spaced;
* what a path is: literals are decoded by the parser (``"\\x2fetc"`` is
  ``/etc``), and simple constant paths are folded — literal concatenation,
  f-strings of literals, ``os.path.join``, ``Path(...) / ...``,
  ``Path.home()``, ``.parent``, a name assigned once to such a value, a loop
  over a literal ``glob``.

A deleting, moving, writing, copying or re-moding call with a resolved target
becomes the command and the effect it stands for.  The same call with a target
that cannot be resolved is *opaque* (M-1), and so is everything the tree cannot
follow: a parse failure, ``exec``/``eval``/``compile`` of a computed string,
``getattr``/``vars()``/``globals()`` used to reach a call, a computed callee
while a dangerous module is in reach, a star import of ``os``/``shutil``/
``subprocess``/``pathlib``/``sys``, a dangerous callable used as a value the
tree does not follow, ``ctypes``/``pty``, a spawn whose command is computed.
Code that parses and shows none of this has no finding and stays allowed.
"""

from __future__ import annotations

import ast
import posixpath
import shlex
from typing import Any

#: Code larger than this is not analysed (opaque).
MAX_CODE_BYTES = 256 * 1024
#: Most syntax nodes walked before the code counts as too large.
MAX_NODES = 200_000
#: Most path-like literals kept for the control-plane check.
MAX_MENTIONS = 256
#: The word a computed argument of a spawned command is shown as.
UNKNOWN_WORD = "$__computed"

#: Modules whose import puts deleting, writing or spawning calls in reach.
REACH_MODULES: frozenset[str] = frozenset(
    {"os", "shutil", "subprocess", "pathlib", "importlib", "builtins", "ctypes", "pty", "runpy",
     "io", "tempfile", "glob", "asyncio", "multiprocessing"}
)
#: ``from <these> import *`` hides which names are dangerous.
STAR_MODULES: frozenset[str] = frozenset(
    {"os", "shutil", "subprocess", "pathlib", "sys", "builtins", "importlib", "ctypes", "pty",
     "runpy", "io"}
)
#: Modules that reach native code or a terminal: nothing they do can be read.
NATIVE_MODULES: frozenset[str] = frozenset({"ctypes", "_ctypes", "cffi", "pty"})
#: Modules whose bare use as a value (passed along, put in a list) cannot be followed.
VALUE_MODULES: frozenset[str] = frozenset({"os", "shutil", "subprocess", "builtins", "importlib"})
#: snowpea's own packages: code that imports them acts as snowpea itself.
SNOWPEA_MODULES: frozenset[str] = frozenset({"snowpea_core", "snowpea", "snowpea_sdk"})

_EXEC = tuple(f"os.{kind}{suffix}" for kind in ("exec", "spawn")
              for suffix in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe"))
#: Classes whose instances run a string as code (``trace.Trace().run("…")``): a
#: call of one resolves to ``<class>()`` so its methods can be named below.
_CODE_CLASSES: frozenset[str] = frozenset(
    {"trace.Trace", "code.InteractiveInterpreter", "code.InteractiveConsole",
     "cProfile.Profile", "profile.Profile", "pdb.Pdb", "bdb.Bdb"}
)
#: Dotted callable -> ``(action, form)``.
CALLS: dict[str, tuple[str, str]] = {
    "shutil.rmtree": ("delete-tree", ""),
    **{f"os.{n}": ("delete", "") for n in ("remove", "unlink", "rmdir", "removedirs")},
    "shutil.move": ("move", ""),
    **{f"os.{n}": ("move", "") for n in ("rename", "renames", "replace")},
    **{f"os.{n}": ("write", "") for n in ("chmod", "lchmod", "chown", "lchown", "truncate",
                                         "chflags", "lchflags", "mkfifo", "mknod", "utime",
                                         "setxattr", "removexattr")},
    # Making a folder: judged when the path is known, like ``Path.mkdir`` (a
    # computed one is not held against the code — ``os.makedirs(out_dir)``).
    "os.mkdir": ("write", "create"), "os.makedirs": ("write", "create"),
    "shutil.chown": ("write", ""),
    **{f"shutil.{n}": ("copy", "") for n in ("copyfile", "copy", "copy2", "copytree",
                                             "copymode", "copystat")},
    "os.link": ("link", ""), "os.symlink": ("link", ""),
    "os.system": ("spawn", "shell"), "os.popen": ("spawn", "shell"),
    **{name: ("spawn", "exec") for name in _EXEC},
    "os.posix_spawn": ("spawn", "exec"), "os.posix_spawnp": ("spawn", "exec"),
    "os.startfile": ("spawn", "shell"),
    **{f"subprocess.{n}": ("spawn", "auto") for n in ("run", "call", "check_call",
                                                      "check_output", "Popen")},
    "subprocess.getoutput": ("spawn", "shell"), "subprocess.getstatusoutput": ("spawn", "shell"),
    "pty.spawn": ("spawn", "auto"), "commands.getoutput": ("spawn", "shell"),
    "asyncio.create_subprocess_shell": ("spawn", "shell"),
    "asyncio.create_subprocess_exec": ("spawn", "words"),
    "builtins.exec": ("code", ""), "builtins.eval": ("code", ""),
    "builtins.compile": ("code", ""),
    # Helpers of the standard library that run a string as code (security review 7,
    # H7-6): a literal is read like ``exec``'s, anything else cannot be.
    **{f"timeit.{n}": ("code", "timeit") for n in ("timeit", "repeat", "Timer")},
    **{f"{m}.{n}": ("code", "") for m in ("cProfile", "profile") for n in ("run", "runctx")},
    **{f"pdb.{n}": ("code", "") for n in ("run", "runeval")},
    **{f"{c}().{n}": ("code", "") for c in ("trace.Trace", "cProfile.Profile",
                                            "profile.Profile", "pdb.Pdb", "bdb.Bdb")
       for n in ("run", "runctx", "runeval")},
    **{f"code.{c}().{n}": ("code", "") for c in ("InteractiveInterpreter", "InteractiveConsole")
       for n in ("runsource", "push")},
    "code.compile_command": ("code", ""), "codeop.compile_command": ("code", ""),
    "code.interact": ("opaque", "code:eval:interactive"),
    **{f"code.{c}().{n}": ("opaque", "code:eval:interactive")
       for c in ("InteractiveInterpreter", "InteractiveConsole") for n in ("interact", "runcode")},
    # Data that is code once loaded.
    **{f"{m}.{n}": ("opaque", "code:eval:pickle")
       for m in ("pickle", "_pickle", "cPickle", "marshal", "dill", "cloudpickle", "joblib")
       for n in ("load", "loads", "Unpickler")},
    "shelve.open": ("opaque", "code:eval:pickle"),
    "runpy.run_module": ("module", ""), "runpy._run_module_as_main": ("module", ""),
    "runpy.run_path": ("script", ""),
    "builtins.open": ("open", ""), "io.open": ("open", ""), "codecs.open": ("open", ""),
    "gzip.open": ("open", ""), "bz2.open": ("open", ""), "lzma.open": ("open", ""),
    "tarfile.open": ("open", ""), "zipfile.ZipFile": ("open", ""),
    "io.FileIO": ("open", ""), "dbm.open": ("open", "dbm"),
    "os.open": ("os-open", ""),
    # More ways to write a file (security review 7, M7-4).
    "shutil.unpack_archive": ("extract", ""), "shutil.make_archive": ("write", ""),
    "sqlite3.connect": ("write", "database"),
    **{f"tempfile.{n}": ("tempdir", "") for n in ("mkstemp", "mkdtemp", "NamedTemporaryFile",
                                                  "TemporaryDirectory", "TemporaryFile",
                                                  "SpooledTemporaryFile", "mktemp")},
    "logging.FileHandler": ("write", ""),
    **{f"logging.handlers.{n}": ("write", "") for n in ("RotatingFileHandler",
                                                        "TimedRotatingFileHandler",
                                                        "WatchedFileHandler")},
    "logging.basicConfig": ("kwfile", "filename"),
    "fileinput.input": ("inplace", ""), "fileinput.FileInput": ("inplace", ""),
    "os.chdir": ("opaque", "code:chdir"), "os.fchdir": ("opaque", "code:chdir"),
    "os.putenv": ("env", ""), "os.unsetenv": ("env", ""),
}
_BUILTINS: frozenset[str] = frozenset(
    {"exec", "eval", "compile", "open", "__import__", "getattr", "vars", "globals", "locals",
     "str", "setattr", "delattr"}
)
#: Builtins that only look at what they are given (a module passed to one is not used).
_LOOKERS: frozenset[str] = frozenset(
    {"hasattr", "dir", "help", "print", "type", "id", "repr", "isinstance", "issubclass",
     "callable"}
)
_PATH_CLASSES: frozenset[str] = frozenset(
    {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath",
     "pathlib.PurePosixPath", "pathlib.PureWindowsPath"}
)
#: ``Path`` methods -> action.
_PATH_METHODS: dict[str, str] = {
    "unlink": "delete", "rmdir": "delete", "write_text": "write", "write_bytes": "write",
    "rename": "move", "replace": "move", "chmod": "write", "lchmod": "write",
    "touch": "create", "mkdir": "create", "symlink_to": "write", "hardlink_to": "write",
    "link_to": "write", "open": "open", "read_text": "read", "read_bytes": "read",
}
#: Method names nothing but a path has: judged on any receiver once pathlib is imported.
_DISTINCT_METHODS: frozenset[str] = frozenset(
    {"unlink", "rmdir", "write_text", "write_bytes", "symlink_to", "hardlink_to", "link_to"}
)
_SAME_PATH: frozenset[str] = frozenset(
    {"os.path.expanduser", "os.path.abspath", "os.path.normpath", "os.path.realpath",
     "os.fspath", "os.path.expandvars", "builtins.str", "os.fsdecode", "os.fsencode"}
)
_SAME_METHODS: frozenset[str] = frozenset(
    {"expanduser", "resolve", "absolute", "as_posix", "__str__", "__fspath__"}
)
_ENV_PATHS: dict[str, str] = {"HOME": "~", "SNOWPEA_HOME": "$SNOWPEA_HOME", "TMPDIR": "$TMPDIR",
                              "PWD": "."}


def _norm(dotted: str) -> str:
    """``posix.remove`` / ``nt.remove`` are ``os.remove``; ``os.path`` has aliases too."""
    head, _, rest = dotted.partition(".")
    if head in ("posix", "nt"):
        dotted = "os" + ("." + rest if rest else "")
    for alias in ("posixpath.", "ntpath."):
        if dotted.startswith(alias):
            dotted = "os.path." + dotted[len(alias):]
    return dotted


def _join(*parts: str) -> str:
    out = ""
    for part in parts:
        if part.startswith(("/", "~", "$")) or not out:
            out = part
        else:
            out = out.rstrip("/") + "/" + part
    return out


def _dirname(text: str) -> str:
    return posixpath.dirname(text.rstrip("/")) or ("/" if text.startswith("/") else ".")


class _Analysis:
    def __init__(self, tree: ast.AST, found: Any, depth: int, path: str | None) -> None:
        self.tree = tree
        self.found = found
        self.depth = depth
        self.path = path
        #: name -> every value it is bound to: ``("ref", dotted)``, ``("const", v)``, ``None``.
        self.cands: dict[str, set[Any]] = {}
        self.pathish: set[str] = set()
        self.parent: dict[int, ast.AST] = {}
        self.reach = False
        self.pathlib = False
        self.archives = False
        #: Nodes used in a way the tree follows (an alias's right-hand side).
        self.followed: set[int] = set()
        #: The code rebinds names behind the tree's back (``__main__.p = …``,
        #: ``setattr`` on a module, ``globals()[…] = …``, ``exec``): no name
        #: stands for a constant any more (security review 7, M7-3).
        self.void = False
        #: The code changes the environment or the folder it runs in: ``~``,
        #: ``$HOME`` and the like no longer stand for the daemon's values.
        self.env_void = False
        #: Expression nodes used as the path of a recognised read (H7-3).
        self.read_nodes: set[int] = set()
        #: Names of functions the code defines.
        self.defs: set[str] = set()

    # -- bindings ---------------------------------------------------------
    def _bind(self, name: str, value: Any) -> None:
        self.cands.setdefault(name, set()).add(value)

    def _module(self, module: str) -> None:
        root = module.split(".", 1)[0]
        root = "os" if root in ("posix", "nt") else root
        if root in REACH_MODULES:
            self.reach = True
        if root == "pathlib":
            self.pathlib = True
        if root in ("tarfile", "zipfile", "shutil"):
            self.archives = True
        if root in NATIVE_MODULES:
            self.found._hide("code:native")
        if root in SNOWPEA_MODULES:
            self.found._control("snowpea-code")
        if module and not module.startswith(".") and module not in self.found.imports:
            # The caller reads a module of that name when the project holds one
            # (``import helper`` runs helper.py like ``python helper.py`` does).
            self.found.imports.append(module)

    def _in_class(self, node: ast.AST) -> bool:
        """``node`` sits directly in a class body: a name bound there is an attribute
        of the class (``A.r``), not a name the tree can follow."""
        current = self.parent.get(id(node))
        while current is not None:
            if isinstance(current, ast.ClassDef):
                return True
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return False
            current = self.parent.get(id(current))
        return False

    def collect(self) -> bool:
        """Parents, imports and every binding; False when the tree is too large."""
        count = 0
        for node in ast.walk(self.tree):
            count += 1
            if count > MAX_NODES:
                return False
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._module(alias.name)
                    if alias.asname:
                        self._bind(alias.asname, ("ref", _norm(alias.name)))
                    else:
                        root = alias.name.split(".", 1)[0]
                        self._bind(root, ("ref", _norm(root)))
            elif isinstance(node, ast.ImportFrom):
                module = ("." * node.level) + (node.module or "")
                self._module(module)
                for alias in node.names:
                    if alias.name == "*":
                        if module.split(".", 1)[0] in STAR_MODULES or _norm(module).split(
                                ".", 1)[0] in STAR_MODULES:
                            self.found._hide("code:star-import")
                        continue
                    self._module(f"{module}.{alias.name}")
                    self._bind(alias.asname or alias.name, ("ref", _norm(f"{module}.{alias.name}")))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._bind(node.name, None)
                if not isinstance(node, ast.ClassDef):
                    self.defs.add(node.name)
            elif isinstance(node, ast.arg):
                self._bind(node.arg, None)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self._bind(node.name, None)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                for name in node.names:
                    self._bind(name, None)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                self._bind(node.name, None)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                self._bind(node.rest, None)
        # Assignments, in source order, twice: an alias of an alias resolves.
        stores = sorted((n for n in ast.walk(self.tree) if isinstance(n, ast.Name)
                         and isinstance(n.ctx, (ast.Store, ast.Del))),
                        key=lambda n: (n.lineno, n.col_offset))
        simple: dict[int, ast.AST] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
                    node.targets[0], ast.Name) and not self._in_class(node):
                simple[id(node.targets[0])] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.value is not None and not self._in_class(node):
                simple[id(node.target)] = node.value
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(
                    node.target, ast.Name):
                simple[id(node.target)] = _Loop(node.iter)
        # A list that is changed after it is written (``cmd.append(x)``, ``cmd[0] = x``)
        # is not the list that was written.
        mutated: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(
                    node.func.value, ast.Name) and node.func.attr not in ("copy", "index",
                                                                         "count"):
                mutated.add(node.func.value.id)
            elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and \
                    isinstance(node.ctx, (ast.Store, ast.Del)):
                mutated.add(node.value.id)
        fixed = {name: set(values) for name, values in self.cands.items()}
        for _round in range(2):
            fresh: dict[str, set[Any]] = {}
            self.followed.clear()
            self.pathish.clear()
            for store in stores:
                value = simple.get(id(store))
                bound: Any = None
                if isinstance(value, _Loop):
                    pattern = self._loop_pattern(value.iter)
                    if pattern is not None:
                        bound = ("const", pattern)
                    self.pathish.add(store.id)
                elif value is not None and isinstance(store.ctx, ast.Store):
                    dotted = self.resolve(value)
                    if dotted is not None:
                        bound = ("ref", dotted)
                        self.followed.add(id(value))
                    else:
                        folded = self.fold(value)
                        words = self.fold_list(value) if folded is None else None
                        if folded is not None:
                            bound = ("const", folded)
                        elif words is not None and store.id not in mutated and not any(
                                dyn for _, dyn in words):
                            bound = ("words", tuple(word for word, _ in words))
                        if self._is_pathish(value):
                            self.pathish.add(store.id)
                fresh.setdefault(store.id, set()).add(bound)
            # Imports, defs and parameters stay; assignments are re-read each round.
            self.cands = {name: set(values) for name, values in fixed.items()}
            for name, values in fresh.items():
                self.cands.setdefault(name, set()).update(values)
        self._rebinding()
        if self.void:
            # No name stands for a constant: a target named through one is unknown.
            self.cands = {
                name: {None if value is not None and value[0] in ("const", "words") else value
                       for value in values}
                for name, values in self.cands.items()
            }
        return True

    def _namespace(self, node: ast.AST | None) -> bool:
        """``node`` is the running code's own namespace: ``__main__``, ``builtins``,
        ``sys.modules[…]``, a module fetched by name, ``globals()``."""
        if node is None:
            return False
        if isinstance(node, ast.Subscript):
            return self._namespace(node.value) or self.resolve(node.value) == "sys.modules"
        if isinstance(node, ast.Call):
            return self.resolve(node.func) in ("builtins.globals", "builtins.locals",
                                               "builtins.vars", "builtins.__import__",
                                               "importlib.import_module")
        return self.resolve(node) in ("__main__", "builtins", "sys.modules")

    def _rebinding(self) -> None:
        """What changes names, the environment or the folder without an assignment
        the tree can follow (security review 7, M7-3)."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                if self._namespace(node.value):
                    self.void = True
                    self.found._hide("code:namespace")
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                if self.resolve(node.value) == "os.environ":
                    self.env_void = True
                elif self._namespace(node.value):
                    self.void = True
                    self.found._hide("code:namespace")
            elif isinstance(node, ast.AugAssign) and self.resolve(node.target) == "os.environ":
                self.env_void = True
            elif isinstance(node, ast.Call):
                dotted = self.resolve(node.func)
                if dotted in ("builtins.setattr", "builtins.delattr"):
                    if node.args and self._namespace(node.args[0]):
                        self.void = True
                        self.found._hide("code:namespace")
                elif dotted == "builtins.exec":
                    self.void = True
                elif dotted in ("os.putenv", "os.unsetenv", "os.chdir", "os.fchdir") or (
                        dotted is not None and dotted.startswith("os.environ.") and
                        dotted.rsplit(".", 1)[1] in ("update", "setdefault", "pop", "popitem",
                                                     "clear", "__setitem__", "__delitem__")):
                    self.env_void = True
                elif isinstance(node.func, ast.Attribute) and node.func.attr in (
                        "update", "__setitem__", "setdefault") and self._namespace(
                            node.func.value) and isinstance(node.func.value, ast.Call):
                    self.void = True  # ``globals().update(…)``

    def _single(self, name: str) -> Any:
        values = self.cands.get(name)
        if values is None or len(values) != 1:
            return None
        return next(iter(values))

    # -- what an expression is ---------------------------------------------
    def resolve(self, node: ast.AST | None, depth: int = 0) -> str | None:
        """The dotted name ``node`` stands for (``shutil.rmtree``), or ``None``."""
        if node is None or depth > 12:
            return None
        if isinstance(node, ast.Name):
            if node.id not in self.cands:
                return f"builtins.{node.id}" if node.id in _BUILTINS else None
            value = self._single(node.id)
            return value[1] if value is not None and value[0] == "ref" else None
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value, depth + 1)
            return _norm(f"{base}.{node.attr}") if base is not None else None
        if isinstance(node, ast.Call):
            func = self.resolve(node.func, depth + 1)
            if func in ("builtins.__import__", "importlib.import_module") and node.args:
                module = self._text(node.args[0])
                if module is not None:
                    self._module(module)
                    # ``__import__("a.b")`` returns ``a``; ``import_module`` returns ``a.b``.
                    name = module.split(".", 1)[0] if func == "builtins.__import__" else module
                    return _norm(name)
            if func == "builtins.getattr" and len(node.args) >= 2:
                base = self.resolve(node.args[0], depth + 1)
                attr = self._text(node.args[1])
                if base is not None and attr is not None:
                    return _norm(f"{base}.{attr}")
            if func in _CODE_CLASSES:
                return f"{func}()"  # an instance: ``trace.Trace().run`` is named in CALLS
        return None

    @staticmethod
    def _text(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            try:
                return node.value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None

    def fold(self, node: ast.AST | None, depth: int = 0) -> str | None:  # noqa: C901
        """``node`` as one constant string (a path or a text), or ``None``."""
        if node is None or depth > 24:
            return None
        text = self._text(node)
        if text is not None:
            return text
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    if value.format_spec is not None or value.conversion not in (-1, 115):
                        return None
                    inner = self.fold(value.value, depth + 1)
                else:
                    inner = self._text(value)
                if inner is None:
                    return None
                parts.append(inner)
            return "".join(parts)
        if isinstance(node, ast.BinOp):
            left, right = self.fold(node.left, depth + 1), self.fold(node.right, depth + 1)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Div):
                return _join(left, right)
            return None
        if isinstance(node, ast.Name):
            if node.id == "__file__" and "__file__" not in self.cands:
                return self.path
            value = self._single(node.id)
            return value[1] if value is not None and value[0] == "const" else None
        if isinstance(node, ast.Attribute):
            dotted = self.resolve(node)
            if dotted in ("os.sep", "os.path.sep"):
                return "/"
            if dotted == "os.curdir":
                return "."
            if dotted == "os.devnull":
                return "/dev/null"
            if dotted == "sys.executable":
                return "python"
            if node.attr == "parent":
                base = self.fold(node.value, depth + 1)
                return _dirname(base) if base is not None else None
            return None
        if isinstance(node, ast.Subscript):
            if self.resolve(node.value) == "os.environ" and not self.env_void:
                key = self._text(node.slice)
                return _ENV_PATHS.get(key) if key is not None else None
            return None
        if isinstance(node, ast.Call):
            return self._fold_call(node, depth)
        return None

    def _fold_call(self, node: ast.Call, depth: int) -> str | None:
        if node.keywords and any(k.arg is None for k in node.keywords):
            return None
        dotted = self.resolve(node.func)
        args = [self.fold(arg, depth + 1) for arg in node.args]
        known = all(arg is not None for arg in args)
        if self.env_void and (
                dotted in ("os.path.expanduser", "os.path.expandvars", "os.getcwd",
                           "pathlib.Path.cwd", "pathlib.Path.home", "tempfile.gettempdir",
                           "os.getenv", "os.environ.get", "os.path.abspath",
                           "os.path.realpath")
                or (isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "expanduser", "resolve", "absolute"))):
            # The code set ``HOME`` (or moved elsewhere) itself: what ``~`` and a
            # relative path stand for is not what the check would assume.
            return None
        if dotted in _PATH_CLASSES or dotted == "os.path.join":
            if not known or any(isinstance(arg, ast.Starred) for arg in node.args):
                return None
            return _join(*[a for a in args if a is not None]) if args else "."
        if dotted in _SAME_PATH:
            return args[0] if len(args) == 1 else None
        if dotted == "os.path.dirname":
            return _dirname(args[0]) if len(args) == 1 and args[0] is not None else None
        if dotted in ("os.getcwd", "pathlib.Path.cwd"):
            return "."
        if dotted == "pathlib.Path.home":
            return "~"
        if dotted == "tempfile.gettempdir":
            return "$TMPDIR"
        if dotted in ("os.getenv", "os.environ.get"):
            return _ENV_PATHS.get(args[0]) if args and args[0] is not None and len(args) == 1 \
                else None
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            base = self.fold(node.func.value, depth + 1)
            if base is None:
                return None
            if method in _SAME_METHODS and not node.args:
                return base
            if method == "joinpath" and known:
                return _join(base, *[a for a in args if a is not None])
            if method == "with_name" and known and len(args) == 1:
                return _join(_dirname(base), args[0] or "")
        return None

    def fold_list(self, node: ast.AST | None, depth: int = 0) -> list[tuple[str, bool]] | None:
        """``node`` as command words ``(word, computed)``; ``None`` when not a known list."""
        if node is None or depth > 8:
            return None
        if isinstance(node, (ast.List, ast.Tuple)):
            out: list[tuple[str, bool]] = []
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    return None
                value = self.fold(item)
                out.append((value, False) if value is not None else (UNKNOWN_WORD, True))
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.fold_list(node.left, depth + 1), self.fold_list(node.right,
                                                                               depth + 1)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.Name):
            value = self._single(node.id)
            if value is not None and value[0] == "words":
                return [(word, False) for word in value[1]]
            return None
        if isinstance(node, ast.Call):
            dotted = self.resolve(node.func)
            if dotted == "shlex.split" and len(node.args) == 1:
                text = self.fold(node.args[0])
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "split" \
                    and not node.args and not node.keywords:
                text = self.fold(node.func.value)
            else:
                return None
            if text is None:
                return None
            try:
                return [(word, False) for word in shlex.split(text)]
            except ValueError:
                return None
        return None

    def _loop_pattern(self, node: ast.AST) -> str | None:
        """``for p in Path("d").glob("*.o")`` / ``glob.glob("d/*.o")``: what ``p`` names."""
        if not isinstance(node, ast.Call):
            return None
        dotted = self.resolve(node.func)
        if dotted in ("glob.glob", "glob.iglob") and node.args:
            return self.fold(node.args[0])
        if isinstance(node.func, ast.Attribute):
            base = self.fold(node.func.value)
            if base is None:
                return None
            if node.func.attr == "iterdir" and not node.args:
                return _join(base, "*")
            if node.func.attr in ("glob", "rglob") and len(node.args) == 1:
                pattern = self.fold(node.args[0])
                if pattern is None:
                    return None
                return _join(base, "**", pattern) if node.func.attr == "rglob" else _join(
                    base, pattern)
        return None

    def _is_pathish(self, node: ast.AST, depth: int = 0) -> bool:
        if depth > 12:
            return False
        if isinstance(node, ast.Name):
            return node.id in self.pathish
        if isinstance(node, ast.Call):
            if self.resolve(node.func) in _PATH_CLASSES | {"pathlib.Path.home",
                                                           "pathlib.Path.cwd"}:
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                    _SAME_METHODS | {"joinpath", "with_name", "with_suffix", "with_stem",
                                     "relative_to"}):
                return self._is_pathish(node.func.value, depth + 1)
            return False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._is_pathish(node.left, depth + 1)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            return self._is_pathish(node.value, depth + 1)
        return False

    # -- the judgement -------------------------------------------------------
    def run(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self._call(node)
        self._values()
        self._mentions()

    def _keyword(self, node: ast.Call, *names: str) -> ast.AST | None:
        for keyword in node.keywords:
            if keyword.arg in names:
                return keyword.value
        return None

    def _arg(self, node: ast.Call, index: int, *names: str) -> ast.AST | None:
        if len(node.args) > index and not any(isinstance(a, ast.Starred)
                                               for a in node.args[: index + 1]):
            return node.args[index]
        return self._keyword(node, *names)

    def _call(self, node: ast.Call) -> None:  # noqa: C901 - one table of what a call is
        found = self.found
        func = node.func
        dotted = self.resolve(func)
        if dotted is not None and dotted in CALLS:
            self._known(node, dotted, *CALLS[dotted])
            return
        if dotted is not None:
            if dotted.startswith("pathlib.Path.") and dotted.rsplit(".", 1)[1] in _PATH_METHODS \
                    and node.args and dotted not in ("pathlib.Path.home", "pathlib.Path.cwd"):
                # ``Path.unlink(Path("x"))``: the method called on the class.
                self._path_method(node, dotted.rsplit(".", 1)[1], node.args[0], node.args[1:],
                                  True)
                return
            if dotted in ("builtins.globals", "builtins.locals"):
                found._hide("code:namespace")
            elif dotted == "builtins.vars" and (not node.args or self.resolve(node.args[0])):
                found._hide("code:namespace")
            elif dotted in ("builtins.__import__", "importlib.import_module"):
                if not node.args or self._text(node.args[0]) is None:
                    found._hide("code:import:non-literal")
            elif dotted == "builtins.getattr":
                base = self.resolve(node.args[0]) if node.args else None
                literal = len(node.args) >= 2 and self._text(node.args[1]) is not None
                if base is not None and not literal and base.split(".", 1)[0] in (
                        REACH_MODULES | {"sys"}):
                    found._hide("code:getattr")
            elif dotted.startswith(("importlib.metadata.", "importlib.resources.")):
                pass  # they read what is installed; nothing is imported or run
            elif dotted.startswith(("importlib.", "runpy.")):
                found._hide("code:import:computed")
            elif isinstance(func, ast.Attribute):
                self._method(node, func)
            return
        # The callee is not a name the tree knows.
        if isinstance(func, ast.Call) and self.resolve(func.func) == "builtins.getattr":
            found._hide("code:getattr")
        elif isinstance(func, (ast.Call, ast.Subscript)):
            if self.reach:
                found._hide("code:computed-callee")
        elif isinstance(func, ast.Name):
            values = self.cands.get(func.id, set())
            if len(values) > 1 and any(v is not None and v[0] == "ref" and self._dangerous(v[1])
                                       for v in values):
                found._hide("code:alias")
        elif isinstance(func, ast.Attribute):
            self._method(node, func)

    def _method(self, node: ast.Call, func: ast.Attribute) -> None:
        if func.attr == "extractall" or (func.attr == "extract" and self.archives):
            # ``TarFile.extractall(path)`` / ``ZipFile.extract(member, path)``:
            # everything below ``path`` is written (security review 7, M7-4).
            where = self._arg(node, 0 if func.attr == "extractall" else 1, "path")
            target = "." if where is None and not any(
                k.arg is None for k in node.keywords) else self.fold(where)
            if target is None:
                self.found._hide("code:write:non-literal")
            else:
                self.found.writes.append((target, False, True))
            return
        action = _PATH_METHODS.get(func.attr)
        if action is None:
            return
        receiver = func.value
        pathish = self._is_pathish(receiver)
        if not pathish and not (self.pathlib and func.attr in _DISTINCT_METHODS):
            return
        self._path_method(node, func.attr, receiver, node.args, pathish)

    @staticmethod
    def _unpacked(node: ast.Call) -> bool:
        """The call takes ``*args`` or ``**kwargs``: any argument may be among them."""
        return any(k.arg is None for k in node.keywords) or any(
            isinstance(a, ast.Starred) for a in node.args)

    def _read(self, node: ast.AST | None, target: str | None) -> None:
        """A recognised read of exactly the expression ``node``."""
        if target is not None:
            self.found.reads.append(target)
        if node is not None:
            self.read_nodes.add(id(node))

    def _path_method(self, node: ast.Call, method: str, receiver: ast.AST, args: list[ast.expr],
                     pathish: bool) -> None:
        found = self.found
        action = _PATH_METHODS[method]
        target = self.fold(receiver)
        if action == "read":
            self._read(receiver, target)
            return
        if action == "open":
            mode_node = args[0] if args else self._keyword(node, "mode")
            mode = self._text(mode_node) if mode_node is not None else "r"
            if self._unpacked(node):
                mode = None  # ``p.open(**{"mode": "w"})``: the mode is not known
            if mode is not None and not any(ch in mode for ch in "wax+"):
                self._read(receiver, target)
                return
            action = "write"
        if action == "create":
            if target is not None:
                found.writes.append((target, False, False))
            return
        if method in ("symlink_to", "hardlink_to", "link_to"):
            # ``Path(new).symlink_to(src)``: a link to ``src`` (review 7, H7-5).
            source = self.fold(args[0]) if args else None
            if source is None:
                found._hide("code:copy:non-literal")
            else:
                found.links.append(source)
        if target is None:
            found._hide(f"code:{action}:non-literal")
            return
        if action == "delete":
            found.commands.append(["rmdir" if method == "rmdir" else "rm", target])
            found.writes.append((target, True, False))
        elif action == "move":
            found.writes.append((target, True, False))
            dest = self.fold(args[0]) if args else None
            if dest is None:
                found._hide("code:move:non-literal")
            else:
                found.writes.append((dest, False, False))
        else:
            found.writes.append((target, False, False))
        del pathish

    def _code_arg(self, source: ast.AST | None, *, callable_ok: bool) -> None:
        """One argument that is run as code: a literal is read like ``exec``'s."""
        found = self.found
        if source is None:
            return
        if callable_ok and (isinstance(source, ast.Lambda) or (
                isinstance(source, ast.Name) and source.id in self.defs)):
            return  # a function of this very code: its body is already read
        text = self.fold(source)
        if text is None or self.depth >= 3:
            found._hide("code:eval:non-literal")
            return
        from snowpea_core.security.code import analyse

        found.merge(analyse(text, "python", depth=self.depth + 1))

    def _known(self, node: ast.Call, dotted: str, action: str, form: str) -> None:  # noqa: C901
        found = self.found
        if action == "opaque":
            found._hide(form)
            return
        if action == "env":
            return  # noted by _rebinding: the environment is not the daemon's any more
        if any(isinstance(arg, ast.Starred) for arg in node.args) and action != "spawn":
            found._hide(f"code:{'eval' if action == 'code' else action}:non-literal")
            return
        if action == "code":
            if self._unpacked(node):
                found._hide("code:eval:non-literal")
                return
            timeit = form == "timeit"
            first_code = self._arg(node, 0, "source", "stmt", "statement", "cmd", "expression",
                                   "expr", "line")
            if first_code is None and not timeit:
                found._hide("code:eval:non-literal")
                return
            self._code_arg(first_code, callable_ok=timeit)
            if timeit:
                self._code_arg(self._arg(node, 1, "setup"), callable_ok=True)
            return
        if action in ("module", "script"):
            # ``runpy.run_module("x")`` is ``python -m x``; ``run_path("x.py")`` is
            # ``python x.py``: walked as that command (snowpea's own modules ask).
            name = self.fold(self._arg(node, 0, "mod_name", "path_name"))
            if name is None:
                found._hide("code:import:non-literal")
            elif action == "module":
                self._module(name)
                found.argv.append([("python", False), ("-m", False), (name, False)])
            else:
                found.argv.append([("python", False), (name, False)])
            return
        if action in ("open", "os-open"):
            self._open(node, dotted, action, form)
            return
        if action == "spawn":
            self._spawn(node, form)
            return
        if action == "tempdir":
            where = self._keyword(node, "dir")
            if where is None and len(node.args) > 2 and dotted in ("tempfile.mkstemp",
                                                                 "tempfile.mkdtemp"):
                where = node.args[2]
            if self._unpacked(node) or (where is not None and self.fold(where) is None):
                if not (isinstance(where, ast.Constant) and where.value is None):
                    found._hide("code:write:non-literal")
            elif where is not None:
                found.writes.append((self.fold(where) or ".", False, False))
            return
        if action == "kwfile":
            where = self._keyword(node, form)
            if self._unpacked(node) or (where is not None and self.fold(where) is None):
                found._hide("code:write:non-literal")
            elif where is not None:
                found.writes.append((self.fold(where) or ".", False, False))
            return
        if action == "inplace":
            flag = self._keyword(node, "inplace")
            if flag is None and len(node.args) > 1:
                flag = node.args[1]
            if flag is None and not self._unpacked(node):
                return
            if isinstance(flag, ast.Constant) and not flag.value:
                return
            files = self._arg(node, 0, "files")
            names = self.fold_list(files)
            single = self.fold(files)
            if single is not None:
                found.writes.append((single, False, False))
            elif names is not None and not any(dyn for _, dyn in names):
                found.writes.extend((name, False, False) for name, _ in names)
            else:
                found._hide("code:write:non-literal")
            return
        if action == "extract":
            where = self._arg(node, 1, "extract_dir")
            target = "." if where is None and not self._unpacked(node) else self.fold(where)
            if target is None:
                found._hide("code:write:non-literal")
            else:
                found.writes.append((target, False, True))
            return
        first = self._arg(node, 0, "path", "src", "fd", "name", "filename", "database",
                          "base_name", "target_is_directory")
        target = self.fold(first)
        if action in ("delete", "delete-tree"):
            if target is None:
                found._hide(f"code:{action}:non-literal")
            elif action == "delete-tree":
                found.commands.append(["rm", "-rf", target])
                found.writes.append((target, True, True))
            else:
                found.commands.append(["rm", target])
                found.writes.append((target, True, False))
            return
        if action == "move":
            dest = self.fold(self._arg(node, 1, "dst"))
            if target is None or dest is None:
                found._hide("code:move:non-literal")
            if target is not None:
                found.writes.append((target, True, True))
            if dest is not None:
                found.writes.append((dest, False, True))
            if target is not None and dest is not None:
                found.commands.append(["mv", target, dest])
            return
        if action == "write":
            if form == "database" and target in (":memory:", ""):
                return
            if target is None and form == "create":
                return
            if target is None:
                found._hide("code:write:non-literal")
            else:
                found.writes.append((target, False, False))
            return
        if action == "link":
            # ``os.symlink(src, dst)`` / ``os.link(src, dst)``: ``dst`` is written and
            # is from then on another name of ``src`` (security review 7, H7-5).
            dest = self.fold(self._arg(node, 1, "dst"))
            if target is None or dest is None:
                found._hide("code:copy:non-literal")
            if target is not None:
                found.links.append(target)
            if dest is not None:
                found.writes.append((dest, False, False))
            return
        if action == "copy":
            dest = self.fold(self._arg(node, 1, "dst"))
            self._read(first, target)
            if dest is None:
                found._hide("code:copy:non-literal")
            else:
                found.writes.append((dest, False, True))

    def _open(self, node: ast.Call, dotted: str, action: str, form: str = "") -> None:
        found = self.found
        where = self._arg(node, 0, "file", "path", "name")
        target = self.fold(where)
        if action == "os-open":
            flags = self._arg(node, 1, "flags")
            flag = self.resolve(flags) if flags is not None else None
            writes = flag != "os.O_RDONLY"
        else:
            mode_node = self._arg(node, 1, "mode", "flag")
            if self._unpacked(node):
                # ``open(p, **{"mode": "w"})``: the mode may be among what is
                # unpacked, so it is not known — a write (security review 7, H7-3).
                writes = True
            elif mode_node is None:
                writes = False
            else:
                mode = self._text(mode_node)
                writes = mode is None or any(ch in mode for ch in
                                             ("wcn" if form == "dbm" else "wax+"))
        if not writes:
            self._read(where, target)
            return
        if target is None:
            # ``tarfile.open(fileobj=…)`` and friends name no file at all.
            if dotted in ("tarfile.open",) and self._arg(node, 0, "name") is None \
                    and not self._unpacked(node):
                return
            found._hide("code:write:non-literal")
        else:
            found.writes.append((target, False, False))

    def _spawn(self, node: ast.Call, form: str) -> None:
        found = self.found
        shell = self._keyword(node, "shell")
        shell_on = isinstance(shell, ast.Constant) and bool(shell.value)
        if shell is not None and not isinstance(shell, ast.Constant):
            shell_on = True  # computed: judged as the stricter of the two
        if form == "words":
            words: list[tuple[str, bool]] = []
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    found._hide("code:spawn:non-literal")
                    return
                value = self.fold(arg)
                words.append((value, False) if value is not None else (UNKNOWN_WORD, True))
            if words:
                found.argv.append(words)
            return
        if node.args and isinstance(node.args[0], ast.Starred):
            found._hide("code:spawn:non-literal")
            return
        first = self._arg(node, 0, "args", "cmd", "command", "path", "file", "argv")
        if first is None:
            found._hide("code:spawn:non-literal")
            return
        if form == "exec":
            program = self.fold(first)
            rest_node = self._arg(node, 1, "args", "argv")
            rest = self.fold_list(rest_node) if rest_node is not None else None
            if rest is None and rest_node is not None:
                rest = []
                for arg in node.args[1:]:
                    value = self.fold(arg)
                    if value is None:
                        rest = None
                        break
                    rest.append((value, False))
            if program is None or rest is None:
                found._hide("code:spawn:non-literal")
                return
            # ``argv[0]`` is the program's own name: the words are what follows it.
            found.argv.append([(program, False), *rest[1:]])
            return
        text = self.fold(first)
        if form == "shell" or shell_on:
            words = self.fold_list(first) if text is None else None
            if text is None and words and not words[0][1]:
                text = words[0][0]  # ``run(["line"], shell=True)``: the first item is the line
            if text is None:
                found._hide("code:spawn:non-literal")
            else:
                found.lines.append(text)
            return
        words = self.fold_list(first)
        if words is not None:
            if not words:
                return
            found.argv.append(words)
        elif text is not None:
            found.lines.append(text)
        else:
            found._hide("code:spawn:non-literal")

    def _dangerous(self, dotted: str) -> bool:
        if dotted in CALLS or dotted in VALUE_MODULES:
            return True
        return dotted.startswith("pathlib.Path.") and dotted.rsplit(".", 1)[1] in (
            _DISTINCT_METHODS | {"rename", "replace", "chmod"})

    def _values(self) -> None:
        """A dangerous callable (or module) used as a value the tree does not follow."""
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(
                    getattr(node, "ctx", None), ast.Load):
                continue
            parent = self.parent.get(id(node))
            if isinstance(node, ast.Name) and node.id == "__builtins__":
                self.found._hide("code:namespace")  # every builtin, by name
                continue
            whole = self.resolve(node)
            if whole is not None and (whole == "sys.modules" or whole.endswith(".sys.modules")):
                # ``sys.modules["shutil"].rmtree``: a module fetched by name (a plain
                # ``"x" in sys.modules`` test fetches nothing).
                fetched = isinstance(parent, ast.Subscript) or (
                    isinstance(parent, ast.Attribute) and parent.attr in (
                        "get", "pop", "setdefault", "values", "items", "__getitem__"))
                if fetched:
                    self.found._hide("code:namespace")
                continue
            if isinstance(parent, ast.Attribute) and parent.value is node:
                if not (parent.attr.startswith("__") and parent.attr.endswith("__")):
                    continue
                # ``shutil.__dict__``, ``os.__getattribute__``: a lookup by name.
                base = self.resolve(node)
                if base is not None and base.split(".", 1)[0] in REACH_MODULES:
                    self.found._hide("code:namespace")
                continue
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            if id(node) in self.followed:
                continue
            dotted = self.resolve(node)
            if dotted is None or not self._dangerous(dotted):
                continue
            if isinstance(parent, ast.Call) and self.resolve(parent.func) in (
                    "builtins.getattr", "builtins.vars") and parent.args and \
                    parent.args[0] is node:
                continue  # judged where the lookup is used
            if dotted in ("builtins.open", "builtins.str", "builtins.compile") and isinstance(
                    node, ast.Name) and isinstance(parent, ast.keyword):
                continue  # ``type=str``, ``opener=open`` keywords of ordinary calls
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) \
                    and parent.func.id in _LOOKERS and parent.func.id not in self.cands:
                continue  # ``hasattr(os, "fork")``, ``print(os)``: looked at, not called
            self.found._hide("code:alias")

    def _mentions(self) -> None:
        """Path-like constants the code names, and which of them it only reads.

        A literal is *read only* when every expression that stands for it is the
        path of a recognised read (``open(p)``, ``Path(p).read_text()``): the
        same literal handed to anything else — ``sqlite3.connect(p)``, a call the
        tree does not know, a list — is not (security review 7, H7-3)."""
        seen = self.found.mentions
        uses: dict[str, list[int]] = {}
        folding = (ast.BinOp, ast.Call, ast.JoinedStr, ast.FormattedValue, ast.Attribute)
        for node in ast.walk(self.tree):
            value: str | None = None
            if isinstance(node, ast.Constant):
                value = self._text(node)
            elif isinstance(node, (ast.BinOp, ast.Call, ast.JoinedStr)) or (
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                value = self.fold(node)
            if not value or len(value) > 4096 or "\n" in value or not (
                    "/" in value or value.startswith("~") or "snowpea" in value.lower()):
                continue
            if value not in seen:
                if len(seen) >= MAX_MENTIONS:
                    return
                seen.append(value)
            parent = self.parent.get(id(node))
            if isinstance(parent, folding) and self.fold(parent) is not None:
                continue  # a piece of a longer constant: that one is the use
            if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
                continue  # a name is bound to it: the name's uses count
            uses.setdefault(value, []).append(id(node))
        for value, nodes in uses.items():
            if nodes and all(node in self.read_nodes for node in nodes):
                self.found.read_only.add(value)


class _Loop:
    """The iterable a loop variable takes its values from."""

    def __init__(self, node: ast.AST) -> None:
        self.iter = node


def analyse_python(code: str, found: Any, *, depth: int = 0, path: str | None = None) -> None:
    """Add to ``found`` what Python ``code`` would do.  Parses; never runs anything."""
    if len(code.encode("utf-8", "replace")) > MAX_CODE_BYTES:
        found._hide("code:too-large")
        return
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        found._hide("code:parse")
        return
    try:
        analysis = _Analysis(tree, found, depth, path)
        if not analysis.collect():
            found._hide("code:too-large")
            return
        analysis.run()
    except RecursionError:
        found._hide("code:too-large")


__all__ = ["CALLS", "MAX_CODE_BYTES", "UNKNOWN_WORD", "analyse_python"]
