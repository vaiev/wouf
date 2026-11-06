"""teamlib.classify: the INIT classifier of team goal. Pure and deterministic, no model: the same goal text, file list,
symbols and rules give the same class, paths, threat model skeleton and gate checks.

Paths come from the file and folder names the text mentions, matched against the repository's files (`git ls-files`),
and from the identifiers it names, looked up in the definitions the context graph finds (teamlib.graph). The class
comes from keyword rules; a path that matches a security glob makes it security. Rules are read from [classify] in
config.ini (security_globs, keywords.<class>); without that section the defaults below apply.
"""

from __future__ import annotations

import importlib
import re

from teamlib.core import CLASSES, KNOWN, mask_secrets
from teamlib.flow import CLASS_DEFAULTS

# The goal text is data: masked, then cut to this many characters before anything reads it.
CAP = 2000
DEFAULT_SECURITY_GLOBS = (".agents/bin/**", "**/auth/**", "**/secret*", ".github/workflows/**")
# Lowercase words or phrases, matched as whole words; [classify] keywords.<class> replaces a class's list.
DEFAULT_KEYWORDS = {
    "plan": ("design", "architecture", "redesign", "roadmap", "epic", "overhaul"),
    "decision": ("decide", "choose", "pick between", "which of", "should we"),
    "analysis": ("analyse", "analyze", "analysis", "investigate", "measure", "benchmark", "compare", "research",
                 "report on"),
    "complex": ("implement", "refactor", "migrate", "rewrite", "new command", "feature", "integrate", "parallel"),
    "mechanical": ("rename", "typo", "typos", "reformat", "format", "bump", "spelling", "reword", "move",
                   "whitespace"),
    "standard": ("fix", "bug", "add", "update", "change", "adjust", "handle", "remove", "improve"),
}
# The first class whose rule holds wins; plan by several areas and security by path are checked between.
KEYWORD_ORDER = ("decision", "analysis", "complex", "mechanical", "standard")
# Areas: the first two folders of a path. A goal over this many areas is larger than one brief.
PLAN_AREAS = 3
# More files of one name than this: the name is ambiguous and names none of them.
AMBIGUOUS = 3
TOKEN = re.compile(r"[A-Za-z0-9_.*/-]+")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
EDGE = "\"'()[]{}<>,;:!?"


def rules_of(ini) -> dict:
    """The classifier's rules: [classify] of config.ini over the defaults in code; works without the section."""
    section = ini["classify"] if ini is not None and ini.has_section("classify") else {}
    globs = [g for g in re.split(r"[,\s]+", section.get("security_globs", "")) if g]
    keywords = {}
    for klass in CLASSES:
        value = section.get(f"keywords.{klass}")
        found = [w.strip().lower() for w in value.split(",") if w.strip()] if value is not None else None
        keywords[klass] = found if found is not None else list(DEFAULT_KEYWORDS.get(klass, ()))
    return {"security_globs": globs or list(DEFAULT_SECURITY_GLOBS), "keywords": keywords}


def tokens(text: str) -> list[tuple[str, bool]]:
    """The words of text that may name a file, folder or identifier: (token, written in backticks)."""
    found = [(t, True) for t in re.findall(r"`([^`\n]{1,200})`", text)]
    for word in re.sub(r"`[^`\n]{1,200}`", " ", text).split():
        found.append((word, False))
    out = []
    for word, quoted in found:
        word = word.strip(EDGE)
        word = word.rstrip(".") if not word.endswith("/") else word
        word = re.sub(r"\(\)$", "", word)
        if word and TOKEN.fullmatch(word):
            out.append((word, quoted))
    return out


def safe(token: str) -> bool:
    """A repository-relative name: not absolute, no `..`, no empty part."""
    parts = token.rstrip("/").split("/")
    return not token.startswith(("/", "~", "-")) and all(p not in ("", "..") for p in parts)


def folders(files: list[str]) -> set[str]:
    return {"/".join(f.split("/")[:n]) for f in files for n in range(1, f.count("/") + 1)}


def resolve(token: str, quoted: bool, files: list[str], known: set[str], dirs: set[str]) -> list[str]:
    """The paths one token names: a file, a folder (as folder/**), a new file in a known folder, or a file or folder
    by its name (a name with a dot or a capital letter as written, or any name in backticks)."""
    if not safe(token) or "*" in token:
        return []
    if token in known:
        return [token]
    bare = token.rstrip("/")
    if bare in dirs and (quoted or "/" in token):
        return [f"{bare}/**"]
    if "/" in bare:
        parent = bare.rsplit("/", 1)[0]
        return [bare] if parent in dirs and re.search(r"\.[A-Za-z0-9]+$", bare) else []
    if not (quoted or "." in token or token != token.lower()):
        return []
    named = [f for f in files if f.rsplit("/", 1)[-1] == token]
    if not named and len(token) >= 3:
        named = [f for f in files if f.rsplit("/", 1)[-1].split(".", 1)[0] == token]
    if not named and quoted:
        named = [f"{d}/**" for d in sorted(dirs) if d.rsplit("/", 1)[-1] == token]
    return sorted(named) if len(named) <= AMBIGUOUS else []


def symbol_names(text: str) -> list[str]:
    """Identifiers the text names as code: in backticks, with an underscore, or called with ()."""
    names = [t for t, quoted in tokens(text) if (quoted or "_" in t) and IDENTIFIER.fullmatch(t)]
    names += re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\(\)", text)
    return sorted(set(names))


def _matches(path: str, glob: str) -> bool:
    """The context graph's path globs (zero-folder **/ included); graph is a leaf, looked up when used."""
    return importlib.import_module("teamlib.graph")._matches(path, glob)


def covered(path: str, globs: list[str]) -> bool:
    return any(_matches(path, g) for g in globs)


def area(path: str) -> str:
    folder = path[:-3] if path.endswith("/**") else path.rsplit("/", 1)[0] if "/" in path else ""
    return "/".join(folder.split("/")[:2]) if folder else "."


def has_word(text: str, phrase: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?![\w-])", text) is not None


def gate_checks(klass: str, paths: list[str], security: list[str], reviews: int, gate: str = "") -> list[str]:
    """What blocks a result of this class: the pre-gate, the gate command, the reviews, the merge."""
    if klass == "plan":
        checks = ["pre-gate: none; a plan flow commits nothing",
                  "plan: out/PLAN.md and one draft brief per package; nothing is dispatched"]
    elif klass in ("decision", "analysis"):
        checks = [f"review: none (class {klass}); the lead reads the return block"]
    else:
        checks = ["pre-gate: every changed file inside Paths" if paths else "pre-gate: no Paths; name them first",
                  "gate: [flow] gate on the branch head in a sandbox without keys" + (f" ({gate})" if gate else ""),
                  f"review: {reviews} review{'' if reviews == 1 else 's'}, APPROVE <sha> on the exact head",
                  "merge: green gate, APPROVE and the human's yes"]
    if security:
        checks.append("threat model: binding for the reviewers; only findings inside it block")
    checks.append("phases: leaving one needs the lead's --yes (autonomy = ask: the human's yes first)")
    return checks


def classify(text: str, files, symbols: dict | None = None, rules: dict | None = None) -> dict:
    """Class, paths, security paths, threat model skeleton and gate checks for a goal. Pure: reads nothing but its
    arguments (files: the repository's files; symbols: identifier -> files defining it, from the context graph;
    rules: rules_of(config), plus optional reviews {class: n} and gate, the [flow] gate command)."""
    rules = rules or rules_of(None)
    text = mask_secrets(str(text), KNOWN)[:CAP]
    files = sorted(set(files))
    known, dirs = set(files), folders(files)
    # Keywords are read in the prose only: a file name (docs/DESIGN.md) is no keyword.
    lowered = " ".join(w for w in re.sub(r"`[^`\n]{1,200}`", " ", text).split()
                       if "/" not in w and not re.search(r"[A-Za-z0-9]\.[A-Za-z0-9]", w)).lower()
    paths: set[str] = set()
    reasons = []
    for token, quoted in tokens(text):
        paths.update(resolve(token, quoted, files, known, dirs))
    for name in symbol_names(text):
        defined = sorted(set((symbols or {}).get(name, ())) & known)
        if 0 < len(defined) <= AMBIGUOUS:
            paths.update(defined)
            reasons.append(f"symbol {name}")
    if re.search(r"(?<![\w/-])tests?(?![\w/-])", text, re.IGNORECASE):
        for path in sorted(paths):
            stem = path.rsplit("/", 1)[-1][:-3] if path.endswith(".py") else ""
            if stem and not stem.startswith("test_") and f"tests/test_{stem}.py" in known:
                paths.add(f"tests/test_{stem}.py")
    folder_globs = [p for p in paths if p.endswith("/**")]
    paths = {p for p in paths if p in folder_globs or not covered(p, folder_globs)}
    ordered = sorted(paths)
    globs = rules["security_globs"]
    security = [p for p in ordered if covered(p, globs) or (p.endswith("/**") and (
        covered(p[:-3] + "/x", globs) or any(covered(f, globs) for f in files if _matches(f, p))))]
    areas = sorted({area(p) for p in ordered})
    keywords = rules["keywords"]
    hit = {k: [w for w in keywords.get(k, []) if has_word(lowered, w)] for k in CLASSES}
    if hit["plan"]:
        klass = "plan"
        reasons.append(f"keyword {hit['plan'][0]}")
    elif len(areas) >= PLAN_AREAS:
        klass = "plan"
        reasons.append(f"{len(areas)} areas")
    elif security:
        klass = "security"
        reasons.append(f"security path {security[0]}")
    else:
        klass = next((k for k in KEYWORD_ORDER if hit[k]), "standard")
        reasons.append(f"keyword {hit[klass][0]}" if hit[klass] else "no keyword: standard")
    if not ordered and klass not in ("plan", "decision", "analysis"):
        klass = "plan"
        reasons.append("no paths: plan")
    threat = (f"in scope: <who or what could misuse or break {', '.join(security)}>; "
              "out of scope: <what is not, and why>") if security else None
    reviews = (rules.get("reviews") or {}).get(klass, int(CLASS_DEFAULTS[klass]["reviews"]))
    return {"text": text, "class": klass, "paths": ordered, "security": security, "threat_model": threat,
            "gates": gate_checks(klass, ordered, security, reviews, rules.get("gate", "")), "areas": areas,
            "reasons": reasons}
