#!/usr/bin/env python3
"""
DuckStation C++ Function DuckTrace Injector - standalone version

No compile_commands.json, CMake, or project dependencies required.

Usage:
    python3 inject_function_logs.py --src /home/lainforall/duckstation/src

Dry run:
    - scans C/C++ source files
    - finds likely function definitions
    - ignores if/else/for/while/switch/try/catch and other non-function blocks
    - shows the functions
    - A = all, S = selected, Q = quit
    - asks for APPLY before changing files
    - creates .bak backups

Injected line:
    DuckTrace(__FUNCTION__);
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


INJECT_LINE = 'DuckTrace(__FUNCTION__);'

DUCKTRACE_HEADER_COMMON = '#include "ducktrace.h"'
DUCKTRACE_HEADER_OTHER = '#include "common/ducktrace.h"'

DUCKTRACE_HEADER_NAME_COMMON = "ducktrace.h"
DUCKTRACE_HEADER_NAME_OTHER = "common/ducktrace.h"

SOURCE_EXTENSIONS = {
    ".cpp", ".cc", ".cxx", ".c++",
    ".h", ".hh", ".hpp", ".hxx", ".h++",
}

SKIP_DIR_NAMES = {
    ".git",
    "build",
    "build-debug",
    "build-debug-new",
    "build-debug-new-detailed",
    "node_modules",
}


@dataclass
class FunctionInfo:
    path: Path
    name: str
    line: int
    open_brace: int
    close_brace: int


# ---------------------------------------------------------------------------
# C++ lexical scanning
# ---------------------------------------------------------------------------

def mask_comments_and_strings(source: str) -> str:
    """
    Replace comments and string/character literal contents with spaces while
    preserving newlines and character positions.

    This lets the brace scanner safely ignore:
        // {
        /* } */
        "}"
        R"( { } )"
        '{'
    """
    out = list(source)
    n = len(source)
    i = 0

    NORMAL = 0
    LINE_COMMENT = 1
    BLOCK_COMMENT = 2
    STRING = 3
    CHAR = 4
    RAW_STRING = 5

    state = NORMAL
    raw_end = ""

    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if state == NORMAL:
            if c == "/" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = LINE_COMMENT
                continue

            if c == "/" and nxt == "*":
                out[i] = out[i + 1] = " "
                i += 2
                state = BLOCK_COMMENT
                continue

            # C++ raw strings: R"delim(... )delim"
            if c == "R" and nxt == '"':
                j = i + 2
                while j < n and source[j] != "(":
                    # A valid raw delimiter is at most 16 chars and cannot
                    # contain whitespace, backslash, parentheses.
                    if j - (i + 2) > 16 or source[j] in " \t\r\n\\()":
                        break
                    j += 1

                if j < n and source[j] == "(":
                    delimiter = source[i + 2:j]
                    raw_end = ")" + delimiter + '"'
                    for k in range(i, j + 1):
                        if source[k] != "\n":
                            out[k] = " "
                    i = j + 1
                    state = RAW_STRING
                    continue

            if c == '"':
                out[i] = " "
                i += 1
                state = STRING
                continue

            if c == "'":
                out[i] = " "
                i += 1
                state = CHAR
                continue

            i += 1
            continue

        if state == LINE_COMMENT:
            if c == "\n":
                state = NORMAL
            else:
                out[i] = " "
            i += 1
            continue

        if state == BLOCK_COMMENT:
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = NORMAL
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
            continue

        if state == STRING:
            if c == "\\":
                if c != "\n":
                    out[i] = " "
                if i + 1 < n:
                    if source[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                else:
                    i += 1
                continue

            if c == '"':
                out[i] = " "
                i += 1
                state = NORMAL
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
            continue

        if state == CHAR:
            if c == "\\":
                if c != "\n":
                    out[i] = " "
                if i + 1 < n:
                    if source[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                else:
                    i += 1
                continue

            if c == "'":
                out[i] = " "
                i += 1
                state = NORMAL
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
            continue

        if state == RAW_STRING:
            if source.startswith(raw_end, i):
                for k in range(i, min(n, i + len(raw_end))):
                    if source[k] != "\n":
                        out[k] = " "
                i += len(raw_end)
                state = NORMAL
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
            continue

    # Fix accidental placeholder logic for line comments.
    for i, ch in enumerate(source):
        if ch == "\n":
            out[i] = "\n"

    return "".join(out)


def matching_pairs(masked: str):
    """
    Return matching delimiter positions for (), [], {}.
    """
    stacks = {"(": [], "[": [], "{": []}
    pairs = {}

    closing = {")": "(", "]": "[", "}": "{"}

    for i, c in enumerate(masked):
        if c in stacks:
            stacks[c].append(i)
        elif c in closing:
            opener = closing[c]
            if stacks[opener]:
                start = stacks[opener].pop()
                pairs[start] = i

    return pairs


# ---------------------------------------------------------------------------
# Function detection
# ---------------------------------------------------------------------------

CONTROL_WORDS = {
    "if",
    "else",
    "for",
    "while",
    "switch",
    "case",
    "default",
    "try",
    "catch",
    "do",
    "sizeof",
    "decltype",
    "requires",
}

NON_FUNCTION_PREFIXES = {
    "namespace",
    "class",
    "struct",
    "union",
    "enum",
    "extern",
    "using",
    "typedef",
    "static_assert",
}


def line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def previous_nonspace(masked: str, pos: int) -> int:
    pos -= 1
    while pos >= 0 and masked[pos].isspace():
        pos -= 1
    return pos


def next_nonspace(masked: str, pos: int) -> int:
    while pos < len(masked) and masked[pos].isspace():
        pos += 1
    return pos


def statement_start(masked: str, pos: int) -> int:
    """
    Find a conservative start of the declaration preceding an opening brace.
    """
    i = pos - 1
    paren = 0
    bracket = 0

    while i >= 0:
        c = masked[i]

        if c == ")":
            paren += 1
        elif c == "(":
            if paren:
                paren -= 1
        elif c == "]":
            bracket += 1
        elif c == "[":
            if bracket:
                bracket -= 1
        elif paren == 0 and bracket == 0 and c in ";{}":
            return i + 1

        i -= 1

    return 0


def normalize_fragment(fragment: str) -> str:
    return re.sub(r"\s+", " ", fragment).strip()


def looks_like_control_block(header: str) -> bool:
    h = normalize_fragment(header)

    # Remove labels/access specifiers at the front.
    h = re.sub(
        r"^(public|private|protected|signals|slots)\s*:\s*",
        "",
        h,
        flags=re.IGNORECASE,
    )

    # Anything beginning with a control keyword is not a function.
    m = re.match(r"^([A-Za-z_]\w*)\b", h)
    if m and m.group(1) in CONTROL_WORDS:
        return True

    # Handle "else if (...)" and "do".
    if re.match(r"^(else|do)\b", h):
        return True

    return False


def looks_like_nonfunction_block(header: str) -> bool:
    h = normalize_fragment(header)

    if not h:
        return True

    if looks_like_control_block(h):
        return True

    # Namespaces/classes/structs/enums/unions are not functions.
    if re.match(
        r"^(?:inline\s+)?(?:namespace|class|struct|union|enum)\b",
        h,
    ):
        return True

    # Lambda expression:
    #   [](...) {
    #   [capture] { ...
    if re.search(r"(?:^|[=\(\[,])\s*\[[^\]]*\]\s*(?:\([^;{}]*\))?\s*(?:mutable\s*)?(?:noexcept\b[^{}]*)?(?:->[^{}]+)?\s*$", h):
        return True

    # Initializer-list / braced initialization.
    if "=" in h and not re.search(r"\boperator\s*=", h):
        # A normal function declaration can contain default arguments with =,
        # so only classify as initialization when no plausible parameter list
        # is present.
        if "(" not in h:
            return True

    # A plain namespace-like qualified name without () isn't a function.
    if "(" not in h and ")" not in h:
        return True

    return False


def extract_function_name(header: str) -> str | None:
    """
    Extract a useful human-readable function name from a likely function
    declaration. This is only used for the dry-run display; the injected
    source uses __FUNCTION__.
    """
    h = normalize_fragment(header)

    # Remove leading attributes/macros in common forms.
    h = re.sub(r"^\s*(?:inline|static|virtual|explicit|constexpr|consteval|constinit)\b\s*", "", h)

    # Constructor/destructor/operator/member function name immediately before
    # the parameter list. Using the final match avoids treating an earlier
    # macro/function call in the declaration as the function name.
    matches = list(re.finditer(
        r"([~A-Za-z_]\w*(?:::[~A-Za-z_]\w*)*|operator\s*[^\s(]+)\s*\(",
        h
    ))
    if matches:
        candidate = matches[-1].group(1).strip()
        if candidate in CONTROL_WORDS or candidate in {"defined"}:
            return None
        return candidate

    return None


def find_function_candidates(source: str) -> list[FunctionInfo]:
    masked = mask_comments_and_strings(source)
    pairs = matching_pairs(masked)

    candidates = []

    for open_brace, close_brace in pairs.items():
        # We only care about braces that appear to start a declaration body.
        prev = previous_nonspace(masked, open_brace)
        if prev < 0:
            continue

        # Header/declaration before this brace.
        start = statement_start(masked, open_brace)
        header = masked[start:open_brace]

        if looks_like_nonfunction_block(header):
            continue

        name = extract_function_name(header)
        if not name:
            continue

        # A real function definition must have its parameter list immediately
        # before the function-body brace, apart from C++ trailing qualifiers,
        # attributes, noexcept, requires, -> return type, override/final, etc.
        #
        # This is important because ordinary calls/macros such as:
        #   defined(...)
        #   tr(...)
        #   width_for(...)
        # can appear in a larger statement that eventually contains a brace.
        close_paren = header.rfind(")")
        if close_paren < 0:
            continue

        after_params = normalize_fragment(header[close_paren + 1:])

        # Things allowed after the parameter list of a C++ function definition.
        # Keep this deliberately conservative: arbitrary source text here is
        # almost certainly not a function definition.
        if after_params:
            allowed_tail = re.compile(
                r"^(?:(?:const|volatile|mutable|override|final|noexcept)"
                r"(?:\\s*\\([^{};]*\\))?\\s*|"
                r"requires\\s+[^{};]+\\s*|"
                r"->\\s*[^{};]+\\s*|"
                r"\\[\\[[^]]*\\]\\]\\s*)+$"
            )
            if not allowed_tail.fullmatch(after_params):
                continue

        # Check that this ')' is actually paired with '('.
        local_pairs = matching_pairs(header)
        open_paren = None
        for op, cl in local_pairs.items():
            if cl == close_paren and header[op] == "(":
                open_paren = op
                break

        if open_paren is None:
            continue

        # Reject obvious control constructs even if their header contains ().
        prefix = normalize_fragment(header[:open_paren])
        first_word_match = re.match(r"^([A-Za-z_]\w*)\b", prefix)
        if first_word_match and first_word_match.group(1) in CONTROL_WORDS:
            continue

        # A function declaration commonly contains a return type / class
        # qualifier before the function name. Require something function-like.
        # This also helps reject arbitrary brace blocks.
        if not re.search(
            r"(?:^|[\w:~*&<>])\s*(?:operator\s*[^\s(]+|~?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\(",
            header,
        ):
            continue

        candidates.append(
            FunctionInfo(
                path=Path(),
                name=name,
                line=line_number(source, open_brace),
                open_brace=open_brace,
                close_brace=close_brace,
            )
        )

    # Remove nested duplicates at the same brace and sort.
    unique = {}
    for c in candidates:
        unique[(c.open_brace, c.close_brace)] = c

    return sorted(unique.values(), key=lambda x: x.open_brace)


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def already_injected(source: str, func: FunctionInfo) -> bool:
    # Only inspect this function's own body.
    body = source[func.open_brace:func.close_brace]
    return INJECT_LINE in body


def indentation_for_body(source: str, brace: int) -> str:
    line_start = source.rfind("\n", 0, brace) + 1
    brace_line = source[line_start:brace]

    # Use tabs if the brace line appears tab-indented, otherwise spaces.
    base = re.match(r"[ \t]*", brace_line).group(0)

    # Look at the first non-empty body line to preserve DuckStation's style.
    pos = brace + 1
    while pos < len(source):
        line_end = source.find("\n", pos)
        if line_end < 0:
            line_end = len(source)

        line = source[pos:line_end]

        if line.strip():
            m = re.match(r"[ \t]*", line)
            if m:
                return m.group(0)
            break

        pos = line_end + 1

    return base + ("\t" if "\t" in base else "    ")



def get_ducktrace_header(path: Path, root: Path) -> str:
    """
    Return the appropriate DuckTrace include for this source file.

    DuckStation source layout:
        src/common/*.cpp  -> #include "ducktrace.h"
        src/<other>/*.cpp -> #include "common/ducktrace.h"
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        # Fallback for safety if path is not below root.
        return DUCKTRACE_HEADER_OTHER

    if relative.parent == Path("common"):
        return DUCKTRACE_HEADER_COMMON

    return DUCKTRACE_HEADER_OTHER


def _preprocessor_depth(line: str, depth: int) -> int:
    """
    Update a simple C/C++ preprocessor nesting depth.

    This is intentionally conservative. We only need to distinguish a
    top-level include from one inside #if/#ifdef/#ifndef/#else/#elif blocks.
    """
    stripped = line.strip()

    if not stripped.startswith("#"):
        return depth

    directive = stripped[1:].lstrip()

    # Ignore line continuations here; DuckTrace's own include is a single line,
    # and normal DuckStation platform guards are simple #if/#ifdef blocks.
    m = re.match(r"(if|ifdef|ifndef)\b", directive)
    if m:
        return depth + 1

    if re.match(r"endif\b", directive):
        return max(0, depth - 1)

    return depth


def _remove_ducktrace_includes(source: str) -> str:
    """
    Remove existing DuckTrace includes.

    This deliberately removes even an include inside #ifdef _WIN32 (or another
    conditional block), because DuckTrace is platform-independent and should
    have exactly one unconditional declaration include.
    """
    lines = source.splitlines(keepends=True)
    filtered = []

    for line in lines:
        if re.match(
            r'^\s*#\s*include\s*[<"](?:common/)?ducktrace\.h[">]',
            line,
        ):
            continue
        filtered.append(line)

    return "".join(filtered)


def has_ducktrace_header(source: str) -> bool:
    """
    Return True only if DuckTrace is included at top level.

    An include inside #ifdef _WIN32, #if, #else, etc. does NOT count.
    """
    depth = 0

    for line in source.splitlines():
        stripped = line.strip()

        if re.match(
            r'^\s*#\s*include\s*[<"](?:common/)?ducktrace\.h[">]',
            line,
        ):
            if depth == 0:
                return True

        depth = _preprocessor_depth(line, depth)

    return False


def add_ducktrace_header(source: str, path: Path, root: Path) -> str:
    """
    Ensure exactly one unconditional DuckTrace include exists.

    DuckStation source layout:
        src/common/*.cpp  -> #include "ducktrace.h"
        src/<other>/*.cpp -> #include "common/ducktrace.h"

    IMPORTANT:
    The include is always placed at top-level, before any #if/#ifdef block.
    This prevents a file such as assert.cpp from hiding the declaration behind
    #ifdef _WIN32 when building DuckStation on Linux.

    Existing DuckTrace includes are removed first so a previously-injected
    conditional include cannot prevent the correct top-level include from
    being added.
    """
    ducktrace_header = get_ducktrace_header(path, root)

    # Remove an old/incorrect include first. This also handles files that were
    # already processed by an older version of this injector.
    source = _remove_ducktrace_includes(source)

    lines = source.splitlines(keepends=True)

    if not lines:
        return ducktrace_header + "\n"

    # Preserve the file's newline convention.
    newline = "\r\n" if "\r\n" in source else "\n"

    # Find the first top-level preprocessor/source boundary.
    #
    # We intentionally put DuckTrace BEFORE the first #if/#ifdef/#ifndef.
    # Includes and harmless #define/#pragma lines may remain before it.
    #
    # Example:
    #
    #   #include "assert.h"
    #   #include "crash_handler.h"
    #
    #   #ifdef _WIN32
    #   ...
    #   #endif
    #
    # becomes:
    #
    #   #include "assert.h"
    #   #include "crash_handler.h"
    #   #include "ducktrace.h"
    #
    #   #ifdef _WIN32
    #   ...
    #   #endif
    #
    insert_at = None

    # First, find the first top-level #if/#ifdef/#ifndef. Putting the include
    # immediately before that is the safest choice.
    depth = 0
    for i, line in enumerate(lines):
        stripped = line.strip()

        if depth == 0 and re.match(r"^#\s*(?:if|ifdef|ifndef)\b", stripped):
            insert_at = i
            break

        depth = _preprocessor_depth(line, depth)

    if insert_at is None:
        # No conditional block before the normal source. Put the include after
        # the initial include/pragma/define area, but never after C++ code.
        last_header_line = -1

        for i, line in enumerate(lines):
            stripped = line.strip()

            if not stripped:
                continue

            if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                continue

            if stripped.startswith("#include") or stripped.startswith("#pragma") or stripped.startswith("#define"):
                last_header_line = i
                continue

            # Stop once ordinary C++ code is encountered.
            break

        insert_at = last_header_line + 1 if last_header_line >= 0 else 0

    lines.insert(insert_at, ducktrace_header + newline)
    return "".join(lines)


def validate_injection_position(source: str, func: FunctionInfo) -> bool:
    """
    Final safety check: the insertion point must be immediately inside the
    detected function body and must not be at file scope.
    """
    if not (0 <= func.open_brace < func.close_brace <= len(source)):
        return False

    # The brace must actually be '{' in the real source.
    if source[func.open_brace] != "{":
        return False

    # There must be a real function body between the braces.
    if func.close_brace <= func.open_brace + 1:
        return False

    # Reject an obvious namespace/class/struct/enum body immediately before
    # the opening brace. This is a final guard against scope-level injection.
    prefix = source[max(0, func.open_brace - 500):func.open_brace]
    masked_prefix = mask_comments_and_strings(prefix)
    if re.search(
        r'\b(?:namespace|class|struct|union|enum)\s+[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*\s*$',
        masked_prefix.strip(),
    ):
        return False

    return True


def inject_into_file(path: Path, funcs: list[FunctionInfo], root: Path) -> tuple[bool, int]:
    source = path.read_text(encoding="utf-8", errors="surrogateescape")
    original_source = source
    changes = []

    # Only .cpp/.cc/.cxx/.c++ files are modified by the normal scanner.
    # Headers are not injected into, even if --include-headers is used.
    if path.suffix.lower() not in {".cpp", ".cc", ".cxx", ".c++"}:
        return False, 0

    valid_funcs = []
    for func in funcs:
        if not validate_injection_position(source, func):
            print(
                f"  SAFETY SKIP: {path.name}:{func.line} "
                f"{func.name} (invalid function-body position)"
            )
            continue

        if already_injected(source, func):
            continue

        valid_funcs.append(func)

    for func in valid_funcs:
        indent = indentation_for_body(source, func.open_brace)

        # If the first character after { is a newline, insert after it.
        if func.open_brace + 1 < len(source) and source[func.open_brace + 1] == "\n":
            insertion = indent + INJECT_LINE + "\n"
            offset = func.open_brace + 2
        else:
            insertion = "\n" + indent + INJECT_LINE + "\n"
            offset = func.open_brace + 1

        changes.append((offset, insertion))

    if not changes:
        return False, 0

    backup = Path(str(path) + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)

    # Apply from the end so offsets remain valid.
    for offset, text_to_insert in sorted(changes, reverse=True):
        source = source[:offset] + text_to_insert + source[offset:]

    # Add the declaration after function-body insertions so the include does
    # not shift the offsets calculated above.
    source = add_ducktrace_header(source, path, root)

    # Final sanity check: never write if the file somehow lost content.
    if not source.strip():
        print(f"  SAFETY ERROR: refusing to write empty file: {path}")
        return False, 0

    path.write_text(source, encoding="utf-8", errors="surrogateescape")
    return True, len(changes)


# ---------------------------------------------------------------------------
# File scanning / UI
# ---------------------------------------------------------------------------

def iter_source_files(root: Path, include_headers: bool):
    extensions = {".cpp", ".cc", ".cxx", ".c++"}
    if include_headers:
        extensions |= {".h", ".hh", ".hpp", ".hxx", ".h++"}

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue

        if path.suffix.lower() in extensions:
            yield path


def parse_selection(text: str, count: int) -> set[int]:
    selected = set()

    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue

        if "-" in token:
            try:
                a, b = token.split("-", 1)
                a, b = int(a), int(b)
                for n in range(min(a, b), max(a, b) + 1):
                    if 1 <= n <= count:
                        selected.add(n)
            except ValueError:
                print(f"  Ignoring invalid range: {token}")
        else:
            try:
                n = int(token)
                if 1 <= n <= count:
                    selected.add(n)
            except ValueError:
                print(f"  Ignoring invalid number: {token}")

    return selected


def main():
    parser = argparse.ArgumentParser(
        description="Find C++ function definitions and inject DuckTrace()."
    )
    parser.add_argument(
        "--src",
        required=True,
        help="Source folder, e.g. /home/lainforall/duckstation/src",
    )
    parser.add_argument(
        "--include-headers",
        action="store_true",
        help="Also scan .h/.hpp/etc. files.",
    )
    args = parser.parse_args()

    root = Path(args.src).expanduser().resolve()

    if not root.is_dir():
        print(f"ERROR: source directory does not exist: {root}")
        sys.exit(1)

    files = sorted(iter_source_files(root, args.include_headers))

    print(f"Source root: {root}")
    print(f"Files to scan: {len(files)}")
    print()

    all_functions: list[FunctionInfo] = []

    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.relative_to(root)}")

        try:
            source = path.read_text(
                encoding="utf-8",
                errors="surrogateescape",
            )

            funcs = find_function_candidates(source)

            for func in funcs:
                func.path = path

            all_functions.extend(funcs)

        except Exception as e:
            print(f"    WARNING: {e}")

    print()
    print("=" * 80)
    print(f"FUNCTIONS FOUND: {len(all_functions)}")
    print("=" * 80)

    for i, func in enumerate(all_functions, 1):
        rel = func.path.relative_to(root)
        print(f"  [{i:5}] {func.name}  ({rel}:{func.line})")

    if not all_functions:
        print()
        print("No functions found.")
        return

    print()
    print("DRY RUN COMPLETE.")
    print()
    print("Choose:")
    print("  A = apply to ALL detected functions")
    print("  S = select individual functions")
    print("  Q = quit without changes")
    print()

    choice = input("> ").strip().lower()

    if choice == "q":
        print("No changes made.")
        return

    if choice == "a":
        selected = all_functions

    elif choice == "s":
        print()
        print("Enter function numbers separated by spaces or commas.")
        print("Ranges are supported, e.g. 1-10.")
        print()

        raw = input("Functions > ")

        # First accept the existing numeric/range syntax.
        numbers = parse_selection(raw, len(all_functions))
        selected = [
            func for i, func in enumerate(all_functions, 1)
            if i in numbers
        ]

        # Also allow exact/substring function-name selection.
        # Example:
        #   System::BootSystem
        #   BootSystem
        if not selected and raw.strip():
            query = raw.strip().lower()
            selected = [
                func for func in all_functions
                if query == func.name.lower()
                or query in func.name.lower()
            ]

            if selected:
                print()
                print(f"Matched {len(selected)} function(s) by name: {raw}")

    else:
        print("Unknown choice. No changes made.")
        return

    if not selected:
        print("Nothing selected. No changes made.")
        return

    print()
    print("=" * 80)
    print(f"SELECTED: {len(selected)}")
    print("=" * 80)

    for i, func in enumerate(selected, 1):
        print(
            f"  [{i:5}] {func.name}  "
            f"({func.path.relative_to(root)}:{func.line})"
        )

    print()
    print("This will modify .cpp source files, add ducktrace.h if needed, and create .bak backups.")
    confirm = input("Type APPLY to continue: ").strip()

    if confirm != "APPLY":
        print("Confirmation not received. No changes made.")
        return

    by_file: dict[Path, list[FunctionInfo]] = {}
    for func in selected:
        by_file.setdefault(func.path, []).append(func)

    print()
    print("Applying changes...")

    modified_files = 0
    injected_count = 0
    skipped_count = 0

    for path, funcs in sorted(by_file.items()):
        try:
            changed, count = inject_into_file(path, funcs, root)

            if changed:
                modified_files += 1
                injected_count += count
                print(
                    f"  MODIFIED: {path.relative_to(root)} "
                    f"({count} injection(s))"
                )
            else:
                skipped_count += 1
                print(
                    f"  SKIPPED:  {path.relative_to(root)} "
                    f"(already injected)"
                )

        except Exception as e:
            print(f"  ERROR:    {path}: {e}")

    print()
    print("=" * 80)
    print("DONE")
    print("=" * 80)
    print(f"Files modified: {modified_files}")
    print(f"DuckTrace lines inserted: {injected_count}")
    print(f"Files skipped: {skipped_count}")
    print()
    print("Original files are backed up as *.bak")
    print("Injected call: DuckTrace(__FUNCTION__);")


if __name__ == "__main__":
    main()
