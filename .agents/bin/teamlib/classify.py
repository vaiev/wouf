"""teamlib.classify: the INIT classifier of team goal. Pure and deterministic, no model: the same goal text, file list,
symbols and rules give the same class, paths, threat model skeleton and gate checks.

Paths come from the file and folder names the text mentions, matched against the repository's files (`git ls-files`),
and from the identifiers it names, looked up in the definitions the context graph finds (teamlib.graph). The class
comes from keyword rules; a path that matches a security glob or a goal word that matches a security word makes it
security. Rules are read from [classify] in config.ini (security_globs, security_words, keywords.<class>); without that
section the defaults below apply. class_of applies the same rules to paths found elsewhere (bin/project). No class
requires fewer reviews than bin/project's own rule gave before it became a shim (FORMER_WORDS): the one classifier asks
for at least as many reviews as the stricter of the two it replaced.
"""

from __future__ import annotations

import importlib
import re
from fnmatch import fnmatchcase

from teamlib.core import CLASSES, KNOWN, known_classes, mask_secrets
from teamlib.flow import CLASS_DEFAULTS

# The goal text is data: masked first. Paths, symbols and keywords read its first CAP characters; the security words
# read the whole masked goal, FORMER_WORDS the whole unmasked one, as bin/project did (masking protects what is shown,
# not the review floor; former_class shows no goal text).
CAP = 2000
DEFAULT_SECURITY_GLOBS = (".agents/bin/**", "**/auth/**", "**/secret*", ".github/workflows/**")
# Security words: lowercase fnmatch patterns, each matched against every word of the goal; one match makes the class
# security, before every other rule. The stems are bin/project's list before it became a shim over teamlib.
SECURITY_STEMS = ("auth", "login", "logout", "passw", "secret", "token", "credential", "oauth", "session", "cookie",
                  "csrf", "cors", "crypt", "cert", "tls", "ssl", "ssh", "firewall", "guard", "permission", "sandbox",
                  "network", "netzwerk", "anmeld", "schlüssel", "berechtig", "zugang")
DEFAULT_SECURITY_WORDS = (*(f"*{stem}*" for stem in SECURITY_STEMS), "key", "keys")
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
# The first class whose rule holds wins; plan by several areas and security by path are checked between. A class
# configured in config.ini ([class.<name>], words from keywords.<name>) is tried after these, before standard.
KEYWORD_ORDER = ("decision", "analysis", "complex", "mechanical", "standard")
# bin/project's own class words before it became a shim, substrings of the goal: the first class whose words hold, then
# a configured class's keywords.<name>, else complex from FORMER_PATHS paths on, else standard; before them its security
# stems (SECURITY_STEMS, also when security_words replaces the default words). A floor: the class they give wins when
# it requires more reviews, it never lowers one.
FORMER_WORDS = (
    ("decision", ("decide", "choose", "which", "entscheid", "wählen", "auswahl")),
    ("analysis", ("investigate", "analy", "why", "audit", "measure", "untersuch", "warum", "prüfen")),
    ("plan", ("plan ", "packages", "roadmap", "planen", "planung", "pakete")),
    ("complex", ("migrat", "refactor", "architecture", "redesign", "rewrite", "umbau", "architektur")),
    ("mechanical", ("typo", "rename", "bump", "format", "lint", "tippfehler", "umbenenn")),
)
FORMER_PATHS = 8
# Areas: the first two folders of a path. A goal over this many areas is larger than one brief.
PLAN_AREAS = 3
# More files of one name than this: the name is ambiguous and names none of them.
AMBIGUOUS = 3
TOKEN = re.compile(r"[A-Za-z0-9_.*/-]+")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
EDGE = "\"'()[]{}<>,;:!?"


def words_of(section) -> list[str]:
    """[classify] security_words; not set or empty: the defaults, whatever else the section sets (security_globs too).
    A security word only ever raises a class."""
    named = [w.lower() for w in re.split(r"[,\s]+", section.get("security_words", "")) if w]
    return named or list(DEFAULT_SECURITY_WORDS)


def rules_of(ini) -> dict:
    """The classifier's rules: [classify] of config.ini over the defaults in code; works without the section. keywords
    has every class of ini (known_classes; the built-ins without ini), in that order; a configured class has none
    unless keywords.<class> names them. security_words replaces the default words; without it the defaults apply, with
    or without security_globs. Either way SECURITY_STEMS stay a floor (former_class)."""
    section = ini["classify"] if ini is not None and ini.has_section("classify") else {}
    globs = [g for g in re.split(r"[,\s]+", section.get("security_globs", "")) if g]
    security_words = words_of(section)
    keywords = {}
    for klass in (known_classes(ini) if ini is not None else CLASSES):
        value = section.get(f"keywords.{klass}")
        found = [w.strip().lower() for w in value.split(",") if w.strip()] if value is not None else None
        keywords[klass] = found if found is not None else list(DEFAULT_KEYWORDS.get(klass, ()))
    return {"security_globs": globs or list(DEFAULT_SECURITY_GLOBS), "security_words": security_words,
            "keywords": keywords}


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
    if security or klass == "security":
        checks.append("threat model: binding for the reviewers; only findings inside it block")
    checks.append("phases: leaving one needs the lead's --yes (autonomy = ask: the human's yes first)")
    return checks


def security_word(text: str, rules: dict) -> str | None:
    """The first word of text that matches one of rules' security words, or None; each distinct word is tried once."""
    patterns = rules.get("security_words", ())
    words = dict.fromkeys(re.findall(r"\w+", text.lower()))
    return next((w for w in words if any(fnmatchcase(w, p) for p in patterns)), None)


def reviews_of(klass: str, rules: dict) -> int:
    """The reviews a class requires: rules' reviews (goal.rules: class_options, as team receipt need counts them), else
    the built-in default, 1 for a configured class."""
    return (rules.get("reviews") or {}).get(klass, int(CLASS_DEFAULTS.get(klass, {}).get("reviews", "1")))


def former_class(whole: str, ordered: list[str], keywords: dict) -> str:
    """The class bin/project's own rule gave the goal and paths before it became a shim: security from its stems in the
    goal or a path (or the word key), whatever security_words says, then its class words."""
    lowered = whole.lower()
    names = [lowered, *(p.lower() for p in ordered)]
    if any(stem in name for stem in SECURITY_STEMS for name in names) or re.search(r"\bkeys?\b", lowered):
        return "security"
    named = [*FORMER_WORDS, *((k, words) for k, words in keywords.items() if k not in CLASSES)]
    return next((k for k, stems in named if any(stem in lowered for stem in stems)),
                "complex" if len(ordered) >= FORMER_PATHS else "standard")


def security_paths(ordered: list[str], files: list[str], globs: list[str]) -> list[str]:
    return [p for p in ordered if covered(p, globs) or (p.endswith("/**") and (
        covered(p[:-3] + "/x", globs) or any(covered(f, globs) for f in files if _matches(f, p))))]


def keyword_class(lowered: str, keywords: dict, reasons: list[str]) -> str:
    """The first configured or built-in keyword class (plan apart), else standard; appends the reason."""
    hit = {k: [w for w in keywords.get(k, []) if has_word(lowered, w)] for k in dict.fromkeys([*CLASSES, *keywords])}
    order = [*(k for k in KEYWORD_ORDER if k != "standard"), *(k for k in hit if k not in CLASSES), "standard"]
    klass = next((k for k in order if hit[k]), "standard")
    reasons.append(f"keyword {hit[klass][0]}" if hit[klass] else "no keyword: standard")
    return klass


def path_class(lowered: str, ordered: list[str], areas: list[str], security: list[str], rules: dict,
               reasons: list[str]) -> str:
    """The class teamlib gave before security words existed: plan keywords, then three areas, then security paths, then
    the keywords; a goal without paths is a plan. A floor too: a security word replaces it only when security requires
    at least as many reviews (a config can give plan review*3)."""
    plan = [w for w in rules["keywords"].get("plan", []) if has_word(lowered, w)]
    if plan:
        reasons.append(f"keyword {plan[0]}")
        return "plan"
    if len(areas) >= PLAN_AREAS:
        reasons.append(f"{len(areas)} areas")
        return "plan"
    if security:
        reasons.append(f"security path {security[0]}")
        return "security"
    klass = keyword_class(lowered, rules["keywords"], reasons)
    if not ordered and klass not in ("plan", "decision", "analysis", "security"):
        reasons.append("no paths: plan")
        return "plan"
    return klass


def decide(whole: str, lowered: str, ordered: list[str], files: list[str], rules: dict,
           reasons: list[str]) -> tuple[str, list[str], list[str]]:
    """Class, security paths and areas of a goal with these paths; appends the reasons. whole: the whole goal, unmasked;
    FORMER_WORDS read it as it is, the security words (also in the names of its paths, src/session/** too) read it
    masked, since a reason names the word. lowered: the prose of its first CAP characters, which the keywords read. A
    security word raises the class to security unless the path class requires more reviews, and FORMER_WORDS's class
    wins when it requires more reviews."""
    security = security_paths(ordered, files, rules["security_globs"])
    areas = sorted({area(p) for p in ordered})
    why: list[str] = []
    klass = path_class(lowered, ordered, areas, security, rules, why)
    word = security_word(" ".join([mask_secrets(whole, KNOWN), *ordered]), rules)
    if word and reviews_of("security", rules) >= reviews_of(klass, rules):
        klass, why = "security", [f"security word {word}"]
    reasons.extend(why)
    former = former_class(whole, ordered, rules["keywords"])
    if reviews_of(former, rules) > reviews_of(klass, rules):
        klass = former
        reasons.append(f"at least {former}: bin/project's former rule")
    return klass, security, areas


def prose(text: str) -> str:
    """The goal's words without file names, lowercase: keywords are read in the prose only."""
    return " ".join(w for w in re.sub(r"`[^`\n]{1,200}`", " ", text).split()
                    if "/" not in w and not re.search(r"[A-Za-z0-9]\.[A-Za-z0-9]", w)).lower()


def class_of(text: str, paths: list[str], files, rules: dict | None = None) -> dict:
    """The class teamlib's rules give a goal with paths found elsewhere (bin/project's path finder): class, security
    paths and reasons. The same rules and order as classify."""
    rules = rules or rules_of(None)
    whole = mask_secrets(str(text), KNOWN)
    reasons: list[str] = []
    klass, security, _ = decide(str(text), prose(whole[:CAP]), sorted(set(paths)), sorted(set(files)), rules, reasons)
    return {"class": klass, "security": security, "reasons": reasons}


def classify(text: str, files, symbols: dict | None = None, rules: dict | None = None) -> dict:
    """Class, paths, security paths, threat model skeleton and gate checks for a goal. Pure: reads nothing but its
    arguments (files: the repository's files; symbols: identifier -> files defining it, from the context graph;
    rules: rules_of(config), plus optional reviews {class: n} and gate, the [flow] gate command)."""
    rules = rules or rules_of(None)
    raw = str(text)
    whole = mask_secrets(raw, KNOWN)
    text = whole[:CAP]
    files = sorted(set(files))
    known, dirs = set(files), folders(files)
    # Keywords are read in the prose only: a file name (docs/DESIGN.md) is no keyword.
    lowered = prose(text)
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
    klass, security, areas = decide(str(raw), lowered, ordered, files, rules, reasons)
    threat = (f"in scope: <who or what could misuse or break {', '.join(security) or 'what the goal names'}>; "
              "out of scope: <what is not, and why>") if klass == "security" else None
    reviews = reviews_of(klass, rules)
    return {"text": text, "class": klass, "paths": ordered, "security": security, "threat_model": threat,
            "gates": gate_checks(klass, ordered, security, reviews, rules.get("gate", "")), "areas": areas,
            "reasons": reasons}
