"""Applica il set di modifiche RedTurtle a un monorepo generato con cookieplone.

Usa solo la stdlib, quindi non c'e' niente da installare: `uvx` sa costruire
un pacchetto direttamente da un repo git, anche pinnato su un tag.

  uvx --from git+https://github.com/RedTurtle/redturtle-cookieplone redturtle-cookieplone create -o .
  uvx --from git+https://github.com/RedTurtle/redturtle-cookieplone@v1.0.0 redturtle-cookieplone create -o .

Senza --title, `create` lascia fare le domande al wizard interattivo di
cookieplone (dalla 2.0 e' un form tui-forms, con pagina di conferma e
back-navigation) e poi applica la customizzazione: e' il modo da usare a mano.

Sottocomandi, tutti idempotenti (rieseguirli non cambia nulla):

  create --title ... -o <dir-genitore> [--scope @redturtle]
      Il percorso normale per un add-on nuovo: genera con cookieplone, applica
      l'overlay, rinomina sotto lo scope npm e rigenera il lockfile, tutto in
      un colpo solo. Vuole le risposte del wizard come opzioni, cosi' si
      chiedono all'utente una volta sola e poi non si interrompe piu'.

  align <repo>
      Allinea un monorepo appena generato alle convenzioni RedTurtle: backend
      su zc.buildout/setuptools, CI sfoltita dei job che per un add-on non
      servono, e release del frontend delegata alla CI via trusted publishing.

  scope <repo> --scope @redturtle
      Rinomina il pacchetto npm sotto uno scope, toccando solo le occorrenze
      che sono nomi e non path.

  integrate <host> <addon> --theme <nome>
      Collega un add-on non ancora rilasciato a un progetto Volto 17 basato
      su yarn workspaces (io-Comune e derivati).

  prompts [template]
      Estrae le domande del wizard dal template, per non tenerle a memoria.
      Legge il formato v2 di cookieplone 2.0 (cookieplone-config.json in root
      e cookieplone.json per template) e ripiega sul v1 per i tag vecchi.

Il flag globale --dry-run vale per tutti e va prima del sottocomando.

Il delta sul backend e sulla CI e' stato ricavato diffando i repo
rer-linkchecker, collective-searchblocks e collective-rercaptcha contro il
loro template cookieplone di origine: e' identico in tutti e tre. La parte di
release npm viene invece da collective-rercaptcha, dove e' gia' in produzione.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from importlib.resources import files
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

SETUP_PY_TEMPLATE = """\
try:
    from setuptools import setup
except ImportError:
    print("setuptools not found, skipping setup()")

    def setup(**kwargs):
        pass


# python_requires/classifiers are duplicated here (kept in sync with
# pyproject.toml) only so that check-python-versions, which statically
# parses this file, doesn't flag a mismatch against pyproject.toml.
setup(
    python_requires="{requires_python}",
    classifiers=[
{classifiers}
    ],
)
"""

SETUPTOOLS_BLOCK = """\
[tool.setuptools]
package-dir = {{"" = "src"}}

[tool.setuptools.dynamic]
version = {{attr = "{dotted_name}.__version__"}}

"""

# Job delle workflow generate che per un add-on non hanno senso.
CI_JOBS_TO_DROP = {
    "backend.yml": ["release"],
    "frontend.yml": ["storybook", "release"],
}

# devDependencies che i template cookieplone dichiarano con protocollo
# `workspace:*`: risolvibili solo dentro un checkout del monorepo di Volto core.
WORKSPACE_PROTOCOL_DEPS = ["@plone/registry", "@plone/scripts", "@plone/types"]

# Da dove si ricavano le versioni pubblicate delle tre: dal manifesto npm della
# Volto a cui l'add-on e' pinnato, che le dichiara esatte. Due campi diversi,
# `@plone/types` sta fra le devDependencies.
NPM_REGISTRY = "https://registry.npmjs.org"
VOLTO_MANIFEST_FIELDS = ("dependencies", "devDependencies")


def overlay_text(name: str) -> str:
    """Legge un template dell'overlay dai dati del pacchetto.

    Sta qui e non su disco accanto allo script perche' il comando gira
    installato da git (`uvx --from git+...`), dove `__file__` sta in una
    directory temporanea di uv e non c'e' nessuna `overlay/` sorella.
    """
    return (files(__package__) / "overlay" / name).read_text()


# Colori solo su un terminale vero: redirigendo su file, in CI o con NO_COLOR
# impostato le sequenze ANSI sporcherebbero l'output invece di aiutare.
_COLOR = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and os.environ.get("TERM") != "dumb"
)


def _c(code: str) -> str:
    return code if _COLOR else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
CYAN = _c("\033[36m")
RESET = _c("\033[0m")

RULE = "─" * 72


def step(index: int, total: int, title: str, why: str = "") -> None:
    """Intestazione di uno step, con una riga che spiega a cosa serve."""
    print(f"\n{CYAN}{RULE}{RESET}")
    print(f" {BOLD}{CYAN}[{index}/{total}]{RESET} {BOLD}{title}{RESET}")
    if why:
        for line in why.strip().splitlines():
            print(f" {DIM}{line}{RESET}")
    print(f"{CYAN}{RULE}{RESET}", flush=True)


def note(msg: str) -> None:
    print(f"{DIM}   {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠  {msg}{RESET}")


class Report:
    """Raccoglie cosa e' stato toccato e cosa era gia' a posto.

    `base` serve solo a stampare path relativi al repo: le funzioni che
    riportano lo fanno con path assoluti, e a schermo diventano illeggibili.
    """

    def __init__(self, dry_run: bool, base: Path | None = None) -> None:
        self.dry_run = dry_run
        self.base = base
        self.changed: list[str] = []
        self.skipped: list[str] = []

    def _short(self, msg: str) -> str:
        return msg.replace(f"{self.base}/", "") if self.base else msg

    def did(self, msg: str) -> None:
        self.changed.append(self._short(msg))

    def already(self, msg: str) -> None:
        self.skipped.append(self._short(msg))

    def write(self, path: Path, content: str) -> None:
        if path.exists() and path.read_text() == content:
            self.already(f"{path}: gia' a posto")
            return
        verb = "scriverei" if self.dry_run else "scritto"
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.did(f"{path}: {verb}")

    def summary(self) -> int:
        for msg in self.changed:
            print(f"  {GREEN}✓{RESET} {msg}")
        for msg in self.skipped:
            print(f"  {DIM}·  {msg}{RESET}")
        count = len(self.changed)
        print()
        if not count:
            print(f"{DIM}Niente da fare: era gia' tutto a posto.{RESET}")
        elif self.dry_run:
            print(
                f"{YELLOW}{count} modifiche da applicare{RESET} {DIM}(dry-run, nulla scritto){RESET}"
            )
        else:
            print(f"{GREEN}{count} modifiche applicate.{RESET}")
        return 0


# --------------------------------------------------------------------------
# lettura metadati
# --------------------------------------------------------------------------


def read_repository_toml(repo: Path) -> dict:
    """Legge repository.toml, anche dove `python3` e' piu' vecchio di 3.11.

    Nei checkout con un .python-version datato tomllib non c'e', quindi si
    ripiega su un parser minimo: di questo file servono solo stringhe dentro
    sezioni annidate per punto, che e' esattamente cio' che il template genera.
    """
    path = repo / "repository.toml"
    if not path.exists():
        sys.exit(f"{path} non trovato: non sembra un monorepo cookieplone.")
    text = path.read_text()
    if tomllib is not None:
        return tomllib.loads(text)
    return _parse_simple_toml(text)


def _parse_simple_toml(text: str) -> dict:
    data: dict = {}
    section = data
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        header = re.fullmatch(r"\[([^\]]+)\]", line)
        if header:
            section = data
            for part in header.group(1).split("."):
                section = section.setdefault(part, {})
            continue
        pair = re.fullmatch(r'([\w.-]+)\s*=\s*"([^"]*)"', line)
        if pair:
            section[pair.group(1)] = pair.group(2)
    return data


def backend_dotted_name(meta: dict) -> str:
    """Nome puntato del pacchetto Python, es. rer.linkchecker."""
    name = meta.get("backend", {}).get("package", {}).get("name")
    if name:
        return name
    sys.exit("repository.toml non dichiara [backend.package] name.")


# --------------------------------------------------------------------------
# sottocomando: buildout
# --------------------------------------------------------------------------


def patch_pyproject(
    path: Path, dotted_name: str, rep: Report
) -> tuple[str, list[str]]:
    """Rende pyproject.toml installabile da setuptools/zc.buildout.

    Restituisce `requires-python` e le versioni di Python dichiarate nei trove
    classifier, che sono quello che serve a `setup.py`.
    """
    text = path.read_text()
    original = text

    # 1. setuptools vecchi non capiscono la forma PEP 639 `license = "..."`.
    text = re.sub(
        r'^license = "([^"]+)"$',
        lambda m: f'license = {{ text = "{m.group(1)}" }}',
        text,
        count=1,
        flags=re.MULTILINE,
    )

    # 2. Nessun vincolo superiore su requires-python, e via quello che le
    #    versioni precedenti di questo comando ci scrivevano.
    #
    #    Un `<3.x` fisso va fuori sincrono da se': i trove classifier li
    #    aggiorna upstream quando Plone supporta un Python nuovo, e allora
    #    `check-python-versions`, che nel job `Backend: Lint` confronta le due
    #    cose, fa fallire la CI con
    #
    #        pyproject.toml says:    3.11, 3.12, 3.13, 3.14
    #        - python_requires says: 3.11, 3.12, 3.13
    #        mismatch!
    #
    #    Succedeva davvero su collective.rercaptcha. Senza il bound le due
    #    fonti non possono divergere, ed e' anche la forma che genera upstream.
    match = re.search(r'^requires-python = "([^"]+)"$', text, flags=re.MULTILINE)
    if not match:
        sys.exit(f"{path}: requires-python non trovato.")
    requires_python = re.sub(r"\s*,\s*<\s*\d+\.\d+", "", match.group(1))
    if requires_python != match.group(1):
        text = (
            text[: match.start()]
            + f'requires-python = "{requires_python}"'
            + text[match.end() :]
        )

    # 3. Configurazione setuptools accanto a quella hatchling.
    if "[tool.setuptools]" not in text:
        block = SETUPTOOLS_BLOCK.format(dotted_name=dotted_name)
        anchor = "[tool.hatch.build]"
        if anchor in text:
            text = text.replace(anchor, block + anchor, 1)
        else:
            text = text.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"

    # Il template genera il file senza newline finale.
    if not text.endswith("\n"):
        text += "\n"

    if text == original:
        rep.already(f"{path}: gia' adattato a setuptools")
    else:
        rep.write(path, text)

    # I classifier sono di upstream e non si toccano: sono la fonte, e setup.py
    # li ricopia. Prima la direzione era opposta — dal cap ai classifier — ed e'
    # per questo che le due potevano disallinearsi.
    versions = re.findall(r'"Programming Language :: Python :: (\d+\.\d+)"', text)
    if not versions:
        warn(f"{path}: nessun trove classifier di Python, setup.py resta senza.")
    return requires_python, versions


def patch_setup_py(
    path: Path, requires_python: str, versions: list[str], rep: Report
) -> None:
    classifiers = "\n".join(
        f'        "Programming Language :: Python :: {v}",' for v in versions
    )
    rep.write(
        path,
        SETUP_PY_TEMPLATE.format(
            requires_python=requires_python, classifiers=classifiers
        ),
    )


def patch_ci(repo: Path, rep: Report) -> None:
    workflows = repo / ".github" / "workflows"
    if not workflows.is_dir():
        rep.already(".github/workflows: assente, salto la CI")
        return
    for filename, jobs in CI_JOBS_TO_DROP.items():
        path = workflows / filename
        if not path.exists():
            continue
        text = path.read_text()
        original = text
        for job in jobs:
            text = remove_job(text, job)
            text = remove_needs_entry(text, job)
            text = remove_needs_references(text, job)
        if text == original:
            rep.already(f"{path}: job {', '.join(jobs)} gia' rimossi")
        else:
            rep.write(path, text)


def add_npm_workflow(repo: Path, package_path: str, rep: Report) -> None:
    """Installa il workflow che pubblica il frontend su npm via trusted publishing.

    Il token npm scade ogni 90 giorni, quindi il rilascio non passa da chi fa
    la release ma dalla CI, che si autentica via OIDC.
    """
    content = (
        overlay_text("npm.yml.tmpl")
        .replace("@FRONTEND_ROOT@", package_path.split("/")[0])
        .replace("@PACKAGE_PATH@", package_path)
    )
    rep.write(repo / ".github" / "workflows" / "npm.yml", content)


def add_pypi_workflow(repo: Path, backend_path: str, rep: Report) -> None:
    """Installa il workflow che pubblica il backend su PyPI via trusted publishing.

    Gemello di npm.yml, per la stessa ragione: nessun token da custodire ne' da
    rinnovare, e la release non dipende dalla macchina di chi la lancia.

    A differenza di npm, PyPI lascia configurare il trusted publisher *prima*
    che il progetto esista ("pending publisher", da pypi.org -> Publishing), e
    il nome del file di questo workflow fa parte del binding.
    """
    content = overlay_text("pypi.yml.tmpl").replace("@BACKEND_ROOT@", backend_path)
    rep.write(repo / ".github" / "workflows" / "pypi.yml", content)


def disable_publish(repo: Path, section_name: str, workflow: str, rep: Report) -> None:
    """Mette `publish = false` nella sezione data di repository.toml.

    E' quello che impedisce a scripts/release.sh di pubblicare in locale: ai due
    pacchetti pensano i workflow al push del tag. repoplone aggiorna comunque
    versione e changelog, perche' lo fa prima di guardare il flag.
    """
    label = section_name.split(".")[0]
    path = repo / "repository.toml"
    text = path.read_text()
    match = re.search(rf"^\[{re.escape(section_name)}\]\s*$", text, flags=re.MULTILINE)
    if not match:
        rep.already(f"repository.toml: nessun [{section_name}], salto")
        return
    end = text.find("\n[", match.end())
    end = len(text) if end == -1 else end
    section = text[match.start() : end]
    if "publish = false" in section:
        rep.already(f"repository.toml: {label} gia' escluso dalla release locale")
        return
    patched = section.replace(
        "publish = true",
        f"# lo pubblichiamo via github actions ({workflow})\npublish = false",
        1,
    )
    rep.write(path, text[: match.start()] + patched + text[end:])


def add_release_flow(repo: Path, meta: dict, rep: Report) -> None:
    """Installa scripts/release.sh, scripts/bootstrap-npm.sh e i target Makefile.

    Il template non genera nessun flusso di release: senza questi, `publish =
    false` in repository.toml non ha nulla che lo legga.
    """
    frontend = meta["frontend"]["package"]
    package_path = frontend["path"]

    release = (
        overlay_text("release.sh.tmpl")
        .replace("@PROJECT_NAME@", meta.get("repository", {}).get("name", repo.name))
        .replace("@NPM_NAME@", frontend["name"])
        .replace(
            "@BACKEND_NAME@",
            meta.get("backend", {}).get("package", {}).get("name", "il backend"),
        )
        .replace("@FRONTEND_ROOT@", package_path.split("/")[0])
    )
    rep.write(repo / "scripts" / "release.sh", release)

    bootstrap = (
        overlay_text("bootstrap-npm.sh.tmpl")
        .replace("@PACKAGE_PATH@", package_path)
        .replace("@NPM_NAME@", frontend["name"])
        .replace("@GITHUB_SLUG@", _github_slug(repo, meta))
    )
    rep.write(repo / "scripts" / "bootstrap-npm.sh", bootstrap)

    if not rep.dry_run:
        for script in ("release.sh", "bootstrap-npm.sh"):
            path = repo / "scripts" / script
            path.chmod(path.stat().st_mode | 0o111)

    add_makefile_targets(repo, rep)


def add_makefile_targets(repo: Path, rep: Report) -> None:
    """Inserisce la sezione Release nel Makefile, prima di Container images."""
    path = repo / "Makefile"
    if not path.exists():
        rep.already("Makefile: assente, salto i target di release")
        return
    text = path.read_text()
    if re.search(r"^release:", text, flags=re.MULTILINE):
        rep.already("Makefile: target release gia' presente")
        return
    block = overlay_text("makefile-release.mk.tmpl")
    anchor = "###########################################\n# Container images"
    if anchor in text:
        text = text.replace(anchor, block + anchor, 1)
    else:
        text = text.rstrip("\n") + "\n\n" + block
    rep.write(path, text)


def remove_job(text: str, name: str) -> str:
    """Rimuove il blocco `  <name>:` da una workflow GitHub Actions."""
    lines = text.splitlines(keepends=True)
    header = re.compile(rf"^  {re.escape(name)}:[ \t]*$")
    sibling = re.compile(r"^(  \S|\S)")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not header.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        i += 1
        while i < len(lines) and not sibling.match(lines[i]):
            i += 1
        # Non lasciare due righe vuote consecutive dove stava il blocco.
        while out and out[-1].strip() == "" and i < len(lines):
            out.pop()
        if out and i < len(lines):
            out.append("\n")
    return "".join(out)


def remove_needs_entry(text: str, name: str) -> str:
    """Toglie `<name>` dalle liste `needs:`, in tutte le forme che YAML ammette.

    Lasciarne una dentro un `needs:` senza il job corrispondente non e' un
    dettaglio cosmetico: GitHub rifiuta il file con
    *Job 'report' depends on unknown job 'release'* e l'intera workflow non
    parte.

    Le forme gestite sono il blocco

        needs:
          - lint
          - release

    la lista inline `needs: [lint, release]` e lo scalare `needs: release`.

    Il blocco **non** finisce alla prima riga che non e' un elemento: commenti e
    righe vuote ci stanno dentro e vanno attraversati. Su
    `collective.rercaptcha`, dove il job `storybook` era stato commentato a
    mano, un `# - storybook` in mezzo alla lista chiudeva il blocco troppo
    presto e il `- release` successivo sopravviveva, con la CI che si rifiutava
    di partire.
    """
    item = re.compile(rf"^\s*-\s*{re.escape(name)}\s*$")
    # Dentro il blocco ci si resta su elementi, commenti e righe vuote.
    inside = re.compile(r"^\s*(?:-\s|#)|^\s*$")
    out: list[str] = []
    in_needs = False

    for line in text.splitlines(keepends=True):
        block = re.match(r"^(\s*)needs:\s*$", line)
        if block:
            in_needs = True
            out.append(line)
            continue

        inline = re.match(r"^(\s*needs:\s*)\[(.*)\](\s*)$", line)
        if inline:
            kept = [
                part.strip()
                for part in inline.group(2).split(",")
                if part.strip() and part.strip() != name
            ]
            # Senza piu' nessuna dipendenza la chiave va via del tutto: un
            # `needs: []` e' valido ma dice una cosa diversa da "nessun needs".
            if kept:
                out.append(f"{inline.group(1)}[{', '.join(kept)}]{inline.group(3)}")
            continue

        scalar = re.match(r"^\s*needs:\s*([\w-]+)\s*$", line)
        if scalar:
            if scalar.group(1) != name:
                out.append(line)
            continue

        if in_needs:
            if item.match(line):
                continue
            if not inside.match(line):
                in_needs = False
        out.append(line)

    return "".join(out)


def pinned_volto_version(repo: Path, override: str | None) -> str | None:
    """Versione di Volto a cui l'add-on e' pinnato.

    La verita' sta in `frontend/mrs.developer.json`: e' il tag che mrs-developer
    clona in `frontend/core`, quindi e' esattamente la Volto contro cui l'add-on
    viene sviluppato e buildato.
    """
    if override:
        return override
    path = repo / "frontend" / "mrs.developer.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("core", {}).get("tag")
    except (json.JSONDecodeError, AttributeError):
        return None


def volto_published_deps(version: str) -> dict:
    """Versioni pubblicate delle tre, lette dal manifesto npm di quella Volto.

    `@plone/volto@X` dichiara `@plone/registry` e `@plone/scripts` fra le
    dependencies e `@plone/types` fra le devDependencies, tutte con versione
    esatta: sono quelle giuste per definizione, perche' sono quelle con cui
    quella Volto e' stata costruita.

    Cosi' non serve nessuna tabella da tenere aggiornata a mano, e si evita la
    trappola dei dist-tag: `@plone/scripts` 4.x su npm non e' taggata `latest`,
    quindi chiederla per tag darebbe la 3.x.
    """
    url = f"{NPM_REGISTRY}/@plone/volto/{version}"
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310
        manifest = json.loads(response.read().decode())
    found = {}
    for field in VOLTO_MANIFEST_FIELDS:
        for name, spec in manifest.get(field, {}).items():
            if name in WORKSPACE_PROTOCOL_DEPS:
                found[name] = spec
    return found


def pin_workspace_devdeps(
    repo: Path, meta: dict, version: str | None, rep: Report
) -> None:
    """Sostituisce `workspace:*` con le versioni pubblicate, nel package.json dell'add-on.

    Trappola che si manifesta solo alla prima release, cioe' nel momento
    peggiore: `pnpm publish` riscrive ogni spec `workspace:` in una versione
    vera, e puo' farlo solo per i pacchetti che il workspace ha installati. Le
    tre stanno in `frontend/core`, che **non e' in git** — lo clona
    mrs-developer. Il job di release fa solo `pnpm install --frozen-lockfile`,
    quindi core non c'e' e la publish muore con
    ERR_PNPM_CANNOT_RESOLVE_WORKSPACE_PROTOCOL, dopo che il tag e' gia' stato
    creato e il backend gia' pubblicato.

    Si cura qui e non nel workflow: cosi' la CI resta identica per tutti gli
    add-on, senza doversi clonare Volto core solo per pubblicare. Sono
    devDependencies, quindi per chi installa l'add-on non cambia niente. E' come
    stanno gia' rer-linkchecker e collective-rercaptcha, che infatti pubblicano
    senza problemi.

    Il vincolo e' con il caret, non esatto, come in quei due repo.
    """
    package_path = meta.get("frontend", {}).get("package", {}).get("path")
    path = repo / package_path / "package.json"
    if not path.exists():
        rep.already(f"{path.name}: assente, salto le devDependencies")
        return

    text = path.read_text()

    def entry(name: str) -> re.Pattern:
        return re.compile(r'"' + re.escape(name) + r'"(\s*:\s*)"workspace:[^"]*"')

    # Solo quelle ancora a `workspace:`: le altre sono gia' state pinnate, o
    # qualcuno le ha messe a mano e non e' il caso di scavalcarlo.
    pending = [name for name in WORKSPACE_PROTOCOL_DEPS if entry(name).search(text)]
    if not pending:
        rep.already("package.json: nessuna devDependency a workspace:*")
        return

    if not version:
        warn(
            "non so a quale Volto e' pinnato l'add-on "
            "(frontend/mrs.developer.json assente): passa --volto-version, "
            f"altrimenti {', '.join(pending)} restano a workspace:* e la prima "
            "release fallira'."
        )
        return

    try:
        published = volto_published_deps(version)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        warn(
            f"non riesco a leggere le versioni da @plone/volto@{version} ({exc}): "
            f"{', '.join(pending)} restano a workspace:*. Controlla che quella "
            "versione di Volto esista, poi rilancia `align` (serve rete)."
        )
        return

    missing = [name for name in pending if name not in published]
    if missing:
        warn(
            f"@plone/volto@{version} non dichiara {', '.join(missing)}: "
            "li lascio a workspace:*, vanno decisi a mano."
        )

    pinned = {}
    for name in pending:
        if name not in published:
            continue
        # Risostituisce sul testo corrente, non su offset calcolati prima: ogni
        # sostituzione sposta tutto quello che viene dopo.
        text = entry(name).sub(
            lambda m: f'"{name}"{m.group(1)}"^{published[name]}"', text, count=1
        )
        pinned[name] = published[name]

    if not pinned:
        return
    if not rep.dry_run:
        path.write_text(text)
    detail = ", ".join(f"{name} ^{spec}" for name, spec in pinned.items())
    verb = "pinnerei" if rep.dry_run else "pinnate"
    rep.did(f"{package_path}/package.json: {verb} da Volto {version} -> {detail}")

    # Su un repo appena generato non c'e' ancora nessun lockfile e va bene
    # cosi': `make install` arriva dopo. Su uno gia' installato invece il
    # lockfile registra quelle tre come `link:` dentro core, e ora non
    # corrispondono piu' al package.json: la CI gira con --frozen-lockfile e si
    # fermerebbe li'.
    frontend_root = package_path.split("/packages/")[0]
    if (repo / frontend_root / "pnpm-lock.yaml").exists():
        warn(
            "il lockfile e' piu' vecchio di questo cambio e la CI gira con "
            f"--frozen-lockfile: rigeneralo con `make -C {frontend_root} install` "
            "e committalo."
        )


def patch_frontend_makefile(repo: Path, meta: dict, rep: Report) -> None:
    """Corregge il target `ci-test` del frontend, che come generato non gira.

    Due difetti distinti, entrambi nel Makefile del template e visibili solo in
    CI, nel job `Frontend: Unit tests`:

    1. `pnpm run test --passWithNoTests`, mentre lo script `test` di
       package.json passa gia' `--passWithNoTests`. vitest 3.x rifiuta il
       doppione con *Expected a single value for option "--passWithNoTests",
       received [true, true]* e il job fallisce. Si toglie dal Makefile, non
       dallo script, cosi' `pnpm test` da solo continua a comportarsi uguale.

    2. `VOLTOCONFIG=$(pwd)/volto.config.js`: dentro un Makefile `$(pwd)` e'
       un'espansione di **make**, non della shell, e make non ha nessuna
       variabile `pwd`. Diventa la stringa vuota — da cui il
       *warning: undefined variable 'pwd'* e un `VOLTOCONFIG=/volto.config.js`
       che non esiste. Va raddoppiato il `$` perche' lo espanda la shell.

    Riprodotti su un repo pristine, mai toccato dall'overlay: sono bug upstream
    di plone/cookieplone-templates, da togliere di qui quando li correggono.
    """
    package_path = meta.get("frontend", {}).get("package", {}).get("path")
    if not package_path:
        return
    path = repo / package_path.split("/packages/")[0] / "Makefile"
    if not path.exists():
        rep.already("frontend/Makefile: assente, salto")
        return

    text = path.read_text()
    original = text
    text = text.replace("pnpm run test --passWithNoTests", "pnpm run test")
    text = text.replace("VOLTOCONFIG=$(pwd)/", "VOLTOCONFIG=$$(pwd)/")

    if text == original:
        rep.already("frontend/Makefile: ci-test gia' a posto")
        return
    rep.write(path, text)


def remove_needs_references(text: str, name: str) -> str:
    """Toglie le righe che leggono ancora `needs.<name>.…` dopo la rimozione.

    Dalla 2.0 le workflow del template hanno un job `report` che compone una
    tabella riepilogativa con una riga per job, del tipo

        echo '| release | ${{ needs.release.result }} |' >> $GITHUB_STEP_SUMMARY

    Togliere il job e sfilarlo da `needs:` non basta: quella riga resta, e
    `needs.release` non e' piu' una proprieta' valida del contesto. actionlint
    la segnala come errore ("property ... is not defined in object type"), e
    nel caso migliore la tabella mostra una cella vuota.

    Si cancella la riga intera, non solo l'espressione, perche' e' una voce
    della tabella di un job che non esiste piu'.
    """
    reference = re.compile(r"needs\." + re.escape(name) + r"\.")
    return "".join(
        line for line in text.splitlines(keepends=True) if not reference.search(line)
    )


def cmd_align(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    meta = read_repository_toml(repo)
    dotted_name = backend_dotted_name(meta)
    backend = repo / meta.get("backend", {}).get("package", {}).get("path", "backend")
    if not backend.is_dir():
        sys.exit(f"{backend} non trovato.")

    print(f"Allineamento di {repo}  (pacchetto {dotted_name})\n")
    rep = Report(args.dry_run)
    requires_python, python_versions = patch_pyproject(
        backend / "pyproject.toml", dotted_name, rep
    )
    patch_setup_py(backend / "setup.py", requires_python, python_versions, rep)
    patch_ci(repo, rep)

    backend_path = meta.get("backend", {}).get("package", {}).get("path", "backend")
    add_pypi_workflow(repo, backend_path, rep)
    disable_publish(repo, "backend.package", "pypi.yml", rep)

    frontend = meta.get("frontend", {}).get("package", {})
    package_path = frontend.get("path")
    if package_path:
        add_npm_workflow(repo, package_path, rep)
        disable_publish(repo, "frontend.package", "npm.yml", rep)
        add_release_flow(repo, meta, rep)
        pin_workspace_devdeps(
            repo, meta, pinned_volto_version(repo, args.volto_version), rep
        )
        patch_frontend_makefile(repo, meta, rep)

    code = rep.summary()
    # Chiamato da `create`, questo blocco lo stampa lui alla fine: qui sarebbe
    # il passo 2 di 4, e lo scroll di `make install` lo porterebbe via.
    if package_path and not getattr(args, "defer_next_steps", False):
        print_npm_bootstrap(repo, meta)
    return code


def print_npm_bootstrap(repo: Path, meta: dict) -> None:
    """Stampa il bootstrap npm, che va fatto una sola volta per pacchetto.

    npm non permette di configurare un trusted publisher su un pacchetto che
    non esiste ancora, quindi la primissima pubblicazione resta manuale.
    """
    frontend = meta["frontend"]["package"]
    npm_name = frontend["name"]
    slug = _github_slug(repo, meta)

    backend_name = meta.get("backend", {}).get("package", {}).get("name", "il backend")

    print(f"""
Prossimi passi

  pending publisher     una tantum, a mano su pypi.org -> Publishing: progetto
  (PyPI)                {backend_name}, owner e repository da {slug},
                        workflow `pypi.yml`. PyPI lascia dichiararlo prima che
                        il progetto esista, quindi niente prima publish
                        manuale: la crea il primo tag.

  make bootstrap-npm    una tantum, ora: prima publish di {npm_name}.
                        npm accetta un trusted publisher solo su un pacchetto
                        che esiste gia', da qui la publish manuale. Il trust si
                        configura poi dal sito: lo script stampa a fine corsa i
                        valori esatti da incollare.

  make release          da qui in poi: aggiorna versioni e changelog e crea il
                        tag; al push del tag i due workflow pubblicano backend
                        e frontend via OIDC, senza nessun token.

Lo slug {slug} viene dal remote git. Il binding OIDC e' sensibile alle
maiuscole, quindi se il remote non e' ancora quello definitivo rilancia `align`
dopo averlo sistemato.
""")


def _github_slug(repo: Path, meta: dict) -> str:
    """Slug org/repo dove girano le Actions.

    Il remote git ha la precedenza su container_images_prefix: quest'ultimo e'
    scritto a mano alla generazione e puo' avere maiuscole diverse da quelle
    reali, che per il binding OIDC del trusted publisher fanno differenza.
    """
    remote = _git_remote_slug(repo)
    if remote:
        return remote
    prefix = meta.get("repository", {}).get("container_images_prefix", "")
    match = re.match(r"^[^/]+/([^/]+/[^/]+)$", prefix)
    if match:
        return match.group(1)
    return "<org>/<repo>"


def _git_remote_slug(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(
        r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?$", result.stdout.strip()
    )
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# sottocomando: integrate
# --------------------------------------------------------------------------


def cmd_integrate(args: argparse.Namespace) -> int:
    host = Path(args.host).resolve()
    addon_repo = Path(args.addon).resolve()
    meta = read_repository_toml(addon_repo)
    frontend = meta.get("frontend", {}).get("package", {})
    npm_name = frontend.get("name")
    rel_pkg = frontend.get("path")
    if not npm_name or not rel_pkg:
        sys.exit("repository.toml non dichiara [frontend.package] name/path.")

    pkg_dir = addon_repo / rel_pkg
    try:
        workspace_path = str(pkg_dir.relative_to(host))
    except ValueError:
        sys.exit(f"{pkg_dir} non e' dentro {host}: sposta il checkout in src/addons.")

    version = json.loads((pkg_dir / "package.json").read_text()).get("version", "*")

    print(f"Integrazione di {npm_name} ({version}) in {host}\n")
    rep = Report(args.dry_run)

    # 1. yarn workspace, cosi' node_modules/<addon> punta al checkout locale.
    host_pkg_path = host / "package.json"
    host_pkg = json.loads(host_pkg_path.read_text())
    workspaces = host_pkg.setdefault("workspaces", [])
    if workspace_path in workspaces:
        rep.already(f"package.json: workspace {workspace_path} presente")
    else:
        workspaces.append(workspace_path)
        rep.did(f"package.json: aggiunto workspace {workspace_path}")

    # 2. Le devDependencies `workspace:*` del template non sono risolvibili
    #    fuori dal monorepo di Volto core: le neutralizzo dalla root, senza
    #    toccare il repo dell'add-on (che deve restare buildabile standalone).
    addon_pkg = json.loads((pkg_dir / "package.json").read_text())
    dev = addon_pkg.get("devDependencies", {})
    resolutions = host_pkg.setdefault("resolutions", {})
    for dep in WORKSPACE_PROTOCOL_DEPS:
        if not str(dev.get(dep, "")).startswith("workspace:"):
            continue
        key = f"{npm_name}/{dep}"
        if key in resolutions:
            rep.already(f"package.json: resolution {key} presente")
        else:
            resolutions[key] = "*"
            rep.did(f"package.json: aggiunta resolution {key}")

    rep.write(host_pkg_path, json.dumps(host_pkg, indent=2, ensure_ascii=False) + "\n")

    # 3. jsconfig.json: e' cosi' che l'addon-registry di Volto tratta il
    #    pacchetto come development package e usa i sorgenti locali.
    jsconfig_path = host / "jsconfig.json"
    jsconfig = json.loads(jsconfig_path.read_text())
    paths = jsconfig.setdefault("compilerOptions", {}).setdefault("paths", {})
    base = jsconfig["compilerOptions"].get("baseUrl", "src")
    src_alias = str((pkg_dir / "src").relative_to(host / base))
    if paths.get(npm_name) == [src_alias]:
        rep.already(f"jsconfig.json: path {npm_name} presente")
    else:
        paths[npm_name] = [src_alias]
        rep.did(f"jsconfig.json: aggiunto path {npm_name}")
        rep.write(
            jsconfig_path, json.dumps(jsconfig, indent=2, ensure_ascii=False) + "\n"
        )

    # 4. Catena addon: il tema lo dichiara, la root lo eredita.
    theme_pkg_path = host / "src" / "addons" / args.theme / "package.json"
    if theme_pkg_path.exists():
        theme_pkg = json.loads(theme_pkg_path.read_text())
        addons = theme_pkg.setdefault("addons", [])
        deps = theme_pkg.setdefault("dependencies", {})
        touched = False
        if npm_name in addons:
            rep.already(f"{args.theme}: presente in addons")
        else:
            addons.append(npm_name)
            touched = True
            rep.did(f"{args.theme}: aggiunto ad addons")
        if deps.get(npm_name) == version:
            rep.already(f"{args.theme}: dependency {version} presente")
        else:
            deps[npm_name] = version
            theme_pkg["dependencies"] = dict(sorted(deps.items()))
            touched = True
            rep.did(f"{args.theme}: aggiunta dependency {npm_name}@{version}")
        if touched:
            rep.write(
                theme_pkg_path,
                json.dumps(theme_pkg, indent=2, ensure_ascii=False) + "\n",
            )
    else:
        rep.already(f"{theme_pkg_path}: assente, salto la catena addon")

    # 5. Il .eslintrc.js generato pretende un checkout di Volto core accanto.
    if (addon_repo / "frontend" / ".eslintrc.js").exists() and not (
        addon_repo / "frontend" / "core"
    ).exists():
        ignore_path = host / ".eslintignore"
        entry = f"{addon_repo.relative_to(host)}/**"
        current = ignore_path.read_text() if ignore_path.exists() else ""
        if entry in current:
            rep.already(f".eslintignore: {entry} presente")
        else:
            note = "# lintato dal proprio repo: il suo .eslintrc.js richiede un checkout di Volto core"
            rep.write(ignore_path, current.rstrip("\n") + f"\n{note}\n{entry}\n")

    code = rep.summary()
    if rep.changed and not args.dry_run:
        print("\nProssimo passo:  yarn install")
    return code


# --------------------------------------------------------------------------
# sottocomando: prompts
# --------------------------------------------------------------------------

TEMPLATES_REPO = "plone/cookieplone-templates"


def cmd_prompts(args: argparse.Namespace) -> int:
    """Estrae le domande del wizard leggendo la configurazione del template.

    Serve per non avere le domande duplicate qui dentro: se cookieplone ne
    aggiunge o rinomina una, la si vede al primo giro senza toccare niente.

    Cookieplone 2.0 ha cambiato entrambi i file: in root
    `cookiecutter.json` e' diventato `cookieplone-config.json`, e per ogni
    template `cookiecutter.json` e' diventato `cookieplone.json`, che porta un
    JSON Schema sotto `schema.properties` invece di una mappa piatta. I tag
    vecchi si leggono ancora, perche' `--tag` serve proprio a rigenerare
    l'identico a mesi di distanza.
    """
    index, version = _read_repository_config(args.tag)
    templates = index["templates"]
    if args.template not in templates:
        visible = [k for k, v in templates.items() if not v.get("hidden")]
        sys.exit(
            f"template sconosciuto: {args.template}. Disponibili: {', '.join(visible)}"
        )

    path = templates[args.template]["path"].lstrip("./")
    if version == 2:
        config = json.loads(_read_template_file(f"{path}/cookieplone.json", args.tag))
        questions = _questions_v2(config)
    else:
        config = json.loads(_read_template_file(f"{path}/cookiecutter.json", args.tag))
        questions = _questions_v1(config)

    print(
        json.dumps(
            {
                "template": args.template,
                "path": path,
                "format": version,
                "questions": questions,
            },
            indent=2,
        )
    )
    return 0


def _read_repository_config(tag: str) -> tuple[dict, int]:
    """Indice dei template del repo, nel formato v2 o, se manca, nel v1."""
    try:
        return json.loads(_read_template_file("cookieplone-config.json", tag)), 2
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    except FileNotFoundError:
        pass
    return json.loads(_read_template_file("cookiecutter.json", tag)), 1


def _questions_v2(config: dict) -> list[dict]:
    """Domande dal JSON Schema di cookieplone 2.0.

    Le proprieta' che iniziano con `__` sono campi calcolati: non vengono
    chieste e non vanno passate. `title`/`description` dello schema sono
    l'etichetta e l'aiuto che il wizard mostra davvero.
    """
    questions = []
    for key, prop in config.get("schema", {}).get("properties", {}).items():
        if key.startswith("_"):
            continue
        default = prop.get("default")
        entry = {
            "key": key,
            "prompt": prop.get("title") or key,
            "help": prop.get("description"),
            "type": prop.get("type", "string"),
            "default": default,
            # I default con {{ }} li calcola cookieplone (anche via rete, es.
            # l'ultima Volto rilasciata): passarli a mano li congelerebbe.
            "derived": isinstance(default, str) and "{{" in default,
        }
        if prop.get("enum"):
            entry["choices"] = prop["enum"]
        questions.append(entry)
    return questions


def _questions_v1(config: dict) -> list[dict]:
    """Domande dal cookiecutter.json piatto, per i tag precedenti la 2.0."""
    labels = config.get("__prompts__", {})
    questions = []
    for key, value in config.items():
        if key.startswith("_"):
            continue
        label = labels.get(key)
        prompt = label.get("__prompt__") if isinstance(label, dict) else label
        entry = {
            "key": key,
            "prompt": prompt or key,
            "type": "string",
            "default": value if isinstance(value, str) else None,
            "derived": isinstance(value, str) and "{{" in value,
        }
        if isinstance(value, list):
            entry["choices"] = value
            entry["default"] = value[0]
            if isinstance(label, dict):
                entry["choice_labels"] = {
                    k: v for k, v in label.items() if k != "__prompt__"
                }
        questions.append(entry)
    return questions


def _read_template_file(relative: str, tag: str) -> str:
    """Legge un file del repo dei template, da COOKIEPLONE_REPOSITORY o da GitHub."""
    local = os.environ.get("COOKIEPLONE_REPOSITORY")
    if local and Path(local).is_dir():
        return (Path(local) / relative).read_text()
    url = f"https://raw.githubusercontent.com/{TEMPLATES_REPO}/{tag}/{relative}"
    with urllib.request.urlopen(url) as response:  # noqa: S310 - URL costruita qui
        return response.read().decode()


# --------------------------------------------------------------------------
# helper versioni Python
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# scope npm
# --------------------------------------------------------------------------

# Nel repo lo stesso identificatore compare sia come nome di pacchetto sia come
# path su disco. Qui va rinominato solo il nome: la cartella
# frontend/packages/<nome>/ resta com'e', perche' changelog.yml interpola
# ADDON_NAME in `frontend/packages/${ADDON_NAME}` e npm.yml lo usa come
# working-directory.
SCOPE_FILES = [
    "{pkg}/package.json",  # name
    "{pkg}/tsconfig.json",  # chiave in paths
    "{fe}/package.json",  # dipendenza workspace:* e ogni pnpm --filter
    "{fe}/volto.config.js",  # elemento dell'array addons
    "{fe}/.eslintrc.js",  # chiave dell'alias (il target e' un path)
    "{fe}/README.md",  # badge e istruzioni d'installazione
    "repository.toml",  # name in [frontend.package]
    "scripts/bootstrap-npm.sh",  # NPM_NAME (PACKAGE_PATH e' un path)
]


def _scope_pattern(current: str) -> re.Pattern:
    """Occorrenze che sono NOMI di pacchetto, non path.

    Se il nome attuale e' gia' scoped basta cercarlo per intero: la forma
    `@scope/nome` non compare mai come path, quindi cambiare scope e' una
    sostituzione secca e non serve nessuna guardia.

    Se e' nudo servono le guardie, perche' lo stesso identificatore e' anche il
    nome della cartella: si esclude cio' che e' preceduto da `packages/` (path
    su disco) o da `github.io/` (URL di GitHub Pages), cio' che e' seguito da
    `-dev` (il wrapper di sviluppo, che non si pubblica) e cio' che e' gia'
    dentro uno scope, cosi' non si annida `@a/@b/`.
    """
    if current.startswith("@"):
        return re.compile(re.escape(current))
    return re.compile(
        r"(?<!packages/)(?<!github\.io/)(?<!/)" + re.escape(current) + r"(?!-dev)"
    )


def pin_publish_registry(path: Path, scope: str, registry: str, rep: Report) -> None:
    """Fissa il registry di publish nel package.json dell'add-on.

    Serve perche' un `@scope:registry` nell'~/.npmrc di chi pubblica dirotta
    `npm publish` su un altro registry **senza errori**: e' successo davvero, con
    `@redturtle` mappato sul registry npm di GitLab, e la publish e' finita la'
    invece che su npmjs.org. Messo qui vale per tutti e per la CI, mentre
    sistemare l'npmrc sistemerebbe solo una macchina.

    La chiave deve essere `"@scope:registry"`, non `"registry"`: verificato con
    `npm publish --dry-run` che la seconda **non** ha effetto, perche' la config
    per-scope dell'npmrc ha precedenza piu' alta di `publishConfig.registry`.
    """
    if not path.exists():
        rep.already(f"{path.name}: assente, salto publishConfig")
        return
    key = f"{scope}:registry"
    text = path.read_text()
    if re.search(
        r'"' + re.escape(key) + r'"\s*:\s*"' + re.escape(registry) + '"', text
    ):
        rep.already(f'package.json: "{key}" gia\' fissato')
        return

    # Modifiche chirurgiche, per non riformattare tutto il file con un
    # round-trip JSON.
    plain = re.search(r'\n(\s*)"registry"(\s*:\s*")' + re.escape(registry) + '"', text)
    if plain:
        # Chiave non scoped: inefficace, la si converte invece di aggiungerne una
        # seconda che la contraddice.
        new_text = (
            text[: plain.start()]
            + f'\n{plain.group(1)}"{key}"{plain.group(2)}{registry}"'
            + text[plain.end() :]
        )
        verb = "correggerei" if rep.dry_run else "corretta"
        msg = f'package.json: {verb} "registry" -> "{key}"'
    else:
        match = re.search(r'(\n(\s*)"publishConfig"\s*:\s*\{\n)', text)
        if not match:
            rep.already("package.json: nessun publishConfig, non lo tocco")
            return
        indent = match.group(2) + "  "
        new_text = (
            text[: match.end()]
            + f'{indent}"{key}": "{registry}",\n'
            + text[match.end() :]
        )
        verb = "aggiungerei" if rep.dry_run else "aggiunto"
        msg = f'package.json: {verb} "{key}" = {registry}'

    if not rep.dry_run:
        path.write_text(new_text)
    rep.did(msg)


def cmd_scope(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    meta = read_repository_toml(repo)
    frontend = meta.get("frontend", {}).get("package", {})
    current = frontend.get("name")
    package_path = frontend.get("path")
    if not current or not package_path:
        sys.exit(
            "repository.toml non dichiara [frontend.package]: niente da rinominare."
        )

    scope = args.scope if args.scope.startswith("@") else f"@{args.scope}"
    bare = current.split("/", 1)[1] if current.startswith("@") else current
    target = f"{scope}/{bare}"

    print(f"Scope npm di {repo}\n  {current}  ->  {target}\n")
    already_scoped = current == target
    if already_scoped:
        print(f"Nome gia' sotto {scope}: controllo solo publishConfig.\n")

    fe_root = package_path.split("/packages/")[0]
    pattern = _scope_pattern(current)
    rep = Report(args.dry_run)
    for template in [] if already_scoped else SCOPE_FILES:
        rel = template.format(pkg=package_path, fe=fe_root)
        path = repo / rel
        if not path.exists():
            rep.already(f"{rel}: assente, salto")
            continue
        text = path.read_text()
        new_text, count = pattern.subn(target, text)
        if not count:
            rep.already(f"{rel}: nessun nome da rinominare")
            continue
        if not args.dry_run:
            path.write_text(new_text)
        verb = "rinominerei" if args.dry_run else "rinominate"
        rep.did(f"{rel}: {verb} {count} occorrenze")

    renamed = bool(rep.changed)
    pin_publish_registry(
        repo / package_path / "package.json", scope, args.registry, rep
    )

    code = rep.summary()
    if renamed and not args.dry_run:
        # Solo se un lockfile c'e' davvero. Su un repo appena generato non c'e'
        # (cookieplone non lo genera) e `make install` lo scrive dopo, gia' col
        # nome nuovo: dire "rigeneralo" parlerebbe di un file inesistente.
        if (repo / fe_root / "pnpm-lock.yaml").exists():
            print(f"""
Ora e' obbligatorio rigenerare il lockfile, perche' la CI gira con
--frozen-lockfile e il rename lo ha invalidato:

  cd {fe_root} && make install
""")
        print(f"""
`publishConfig.access = "public"` e' gia' nel package.json generato, e
npm.yml/bootstrap-npm.sh passano gia' `--access public`: per un pacchetto scoped
serve, altrimenti npm lo pubblicherebbe `restricted`.

Prerequisito esterno: l'organizzazione {scope} deve esistere su npm e l'utente
deve avere i permessi di publish.
""")
    return code


# --------------------------------------------------------------------------
# create: generazione + overlay in un colpo solo
# --------------------------------------------------------------------------

# Le risposte del wizard che `create` sa passare a cookieplone. author/email
# sono fissi e non si chiedono; use_prerelease_versions resta fuori perche'
# serve solo a calcolare i default di plone_version/volto_version, che qui
# arrivano espliciti.
CREATE_CONTEXT_KEYS = [
    "title",
    "description",
    "project_slug",
    "python_package_name",
    "npm_package_name",
    "github_organization",
    "container_registry",
    "initialize_documentation",
    "plone_version",
    "volto_version",
]

RT_AUTHOR = "RedTurtle Technology"
RT_EMAIL = "sviluppo@redturtle.it"


def scoped_npm_name(name: str, scope: str | None) -> str:
    """Nome npm sotto `scope`, sostituendo uno scope gia' presente.

    Dalla 2.0 il nome scoped si puo' passare direttamente a cookieplone: il
    filtro `unscoped_package_name` ricava da li' il nome della cartella, quindi
    `frontend/packages/` resta senza scope mentre package.json, tsconfig.json,
    volto.config.js, .eslintrc.js e repository.toml lo prendono. E' la stessa
    distinzione nome/path che il sottocomando `scope` fa a mano, ma fatta a
    monte: cosi' non c'e' nessun rename da rifare e nessun lockfile da
    rigenerare.
    """
    if not scope:
        return name
    scope = scope if scope.startswith("@") else f"@{scope}"
    bare = name.split("/", 1)[1] if name.startswith("@") else name
    return f"{scope}/{bare}"


def _default_npm_package_name(python_package_name: str | None) -> str | None:
    """Il default che cookieplone calcolerebbe da python_package_name.

    Serve solo quando c'e' --scope ma non --npm-package-name: lo scope va
    applicato a un nome, e lasciarlo derivare a cookieplone lo produrrebbe
    senza scope.
    """
    if not python_package_name:
        return None
    return "volto-" + python_package_name.replace("_", "-").replace(".", "-")


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    printable = shlex.join(cmd)
    print(f"\n$ {printable}\n", flush=True)
    result = subprocess.run(cmd, cwd=cwd, check=False)
    if result.returncode != 0:
        sys.exit(f"\nfallito: {printable}")


def _subdirs(path: Path) -> set[Path]:
    return {p for p in path.iterdir() if p.is_dir()} if path.is_dir() else set()


def _load_answers_file(path: str | None) -> dict:
    """Risposte di partenza da un .cookieplone.json gia' esistente.

    E' il modo per rigenerare un add-on con le stesse risposte di uno vecchio:
    si punta al .cookieplone.json che cookieplone ha lasciato nel suo repo. Le
    opzioni sulla riga di comando vincono su quello che c'e' qui dentro.
    """
    if not path:
        return {}
    source = Path(path)
    if not source.is_file():
        sys.exit(f"answers file non trovato: {source}")
    data = json.loads(source.read_text())
    if not isinstance(data, dict):
        sys.exit(f"{source}: mi aspettavo un oggetto JSON.")
    return data


def format_frontend(repo: Path, fe_root: str) -> None:
    """Formatta il frontend con gli strumenti del repo stesso.

    Serve perche' cookieplone genera un add-on che **non passa il proprio
    lint**: il subtemplate `sub/addon_settings` gira dopo `add-ons/frontend` e
    ne sovrascrive `src/config/settings.ts` con una copia ad apici doppi, mentre
    il `.prettierrc` generato ha `singleQuote: true`. Risultato: il job
    `code-analysis` di `frontend.yml` fallisce al primo push, su un repo appena
    creato e mai toccato a mano.

    Verificato su repo pristine: `make format` sistema quel file e non tocca
    nient'altro. E' un bug upstream in plone/cookieplone-templates; questo passo
    si potra' togliere quando e' corretto.

    Si formatta con `make format` e non con una sostituzione mirata degli apici
    perche' cosi' vale qualunque cosa upstream lasci non formattata, oggi e in
    futuro, e la regola applicata e' quella del repo, non la nostra.

    Non fatale: a questo punto la generazione e' completa e riuscita, e un
    formatter che esce diverso da zero non deve far sembrare fallito tutto.
    """
    print(f"\n--- make -C {fe_root} format")
    result = subprocess.run(["make", "-C", fe_root, "format"], cwd=repo, check=False)
    if result.returncode != 0:
        warn(
            f"`make -C {fe_root} format` e' uscito con {result.returncode}. Il repo "
            "c'e' ed e' a posto, ma controlla `make -C "
            f"{fe_root} lint` prima del primo push."
        )


def cmd_create(args: argparse.Namespace) -> int:
    out_dir = Path(args.output_dir).resolve()
    # Senza --title si lascia fare il wizard a cookieplone: e' il modo comodo a
    # mano, perche' le domande le fa lui e restano sempre allineate al template.
    interactive = args.title is None

    repo = None
    if not interactive:
        # Con --no-input cookieplone riempirebbe da se' cio' che manca, ma senza
        # slug non si sa dove ha generato: meglio dirlo subito e per intero.
        missing = [
            name
            for name in ("project_slug", "description", "github_organization")
            if getattr(args, name) is None
        ]
        if missing:
            sys.exit(
                "con --title servono anche: "
                + ", ".join("--" + m.replace("_", "-") for m in missing)
                + "\n(oppure ometti --title e rispondi al wizard interattivo)"
            )
        repo = out_dir / args.project_slug
        if repo.exists():
            sys.exit(f"{repo} esiste gia': scegli un altro slug o rimuovilo.")

    # Lo scope npm si applica gia' qui, non dopo con un rename: cookieplone 2.0
    # accetta un npm_package_name scoped e sa tenere la cartella unscoped.
    npm_package_name = args.npm_package_name or _default_npm_package_name(
        args.python_package_name
    )
    npm_package_name = (
        scoped_npm_name(npm_package_name, args.scope) if npm_package_name else None
    )

    # Le risposte passano da un answers file invece che da `chiave=valore` sulla
    # riga di comando: cookieplone 2.0 lo legge con --answers-file, e' lo stesso
    # formato del .cookieplone.json che scrive lui nel repo generato, e la
    # chiave __template__ ci mette dentro anche la scelta del template. Cosi'
    # una generazione si rifa' identica ripassando quel file.
    answers = dict(_load_answers_file(args.answers_file))
    answers.setdefault("author", RT_AUTHOR)
    answers.setdefault("email", RT_EMAIL)
    answers["__template__"] = args.template
    given = {
        "title": args.title,
        "description": args.description,
        "project_slug": args.project_slug,
        "python_package_name": args.python_package_name,
        "npm_package_name": npm_package_name,
        "github_organization": args.github_organization,
        "container_registry": args.container_registry,
        "plone_version": args.plone_version,
        "volto_version": args.volto_version,
    }
    answers.update({k: v for k, v in given.items() if v is not None})
    if not interactive:
        answers["initialize_documentation"] = "1" if args.docs else "0"

    answers_path = (
        Path(tempfile.mkdtemp(prefix="redturtle-cookieplone-")) / ".cookieplone.json"
    )
    if not args.dry_run:
        answers_path.write_text(json.dumps(answers, indent=4) + "\n")

    cmd = [
        "uvx",
        "cookieplone",
        "-o",
        str(out_dir),
        "--answers-file",
        str(answers_path),
    ]
    if not interactive:
        cmd.append("--no-input")

    if args.dry_run:
        print("dry-run, eseguirei:\n  " + shlex.join(cmd))
        print("\ncon questo answers file:\n" + json.dumps(answers, indent=4))
        return 0

    print("=" * 70)
    print(
        "1/4  cookieplone"
        + ("  (rispondi alle domande)" if interactive else f"  -> {repo}")
    )
    print("=" * 70)
    before = _subdirs(out_dir)
    _run(cmd)

    if interactive:
        # Lo slug lo decide l'utente nel wizard, quindi la directory si scopre
        # per differenza invece di indovinarla.
        created = sorted(_subdirs(out_dir) - before)
        if len(created) != 1:
            sys.exit(
                "non riesco a capire quale directory ha creato cookieplone "
                f"(trovate: {[p.name for p in created] or 'nessuna'}). "
                "Rilancia i passi separati: align, scope."
            )
        repo = created[0]
        print(f"\ncookieplone ha creato {repo}")
    elif not repo.is_dir():
        sys.exit(f"cookieplone non ha creato {repo}.")

    if interactive and args.scope is None:
        # Senza un terminale non si puo' chiedere: succede se lo si lancia da uno
        # script o dalla CI. Meglio proseguire senza scope che morire qui.
        if not sys.stdin.isatty():
            print("\nstdin non interattivo: salto lo scope npm (passa --scope).")
        else:
            try:
                answer = input(
                    "\nScope npm da applicare (es. @redturtle), invio per nessuno: "
                ).strip()
            except EOFError:
                answer = ""
            args.scope = answer or None

    print("=" * 70)
    print("2/4  overlay RedTurtle")
    print("=" * 70)
    align_args = argparse.Namespace(
        repo=str(repo),
        # Di norma None: la Volto la ridice mrs.developer.json, che cookieplone
        # ha appena scritto con la risposta vera del wizard.
        volto_version=args.volto_version,
        defer_next_steps=True,
        dry_run=False,
    )
    if cmd_align(align_args) != 0:
        return 1

    if args.scope:
        print("=" * 70)
        # Generando non interattivo il nome e' gia' nato scoped, quindi qui
        # resta solo da fissare `@scope:registry` in publishConfig: e' il pezzo
        # che cookieplone non fa, e senza cui un `@scope:registry` nell'~/.npmrc
        # dirotta `npm publish` su un altro registry senza dire niente.
        print(f"3/4  scope npm {args.scope}")
        print("=" * 70)
        scope_args = argparse.Namespace(
            repo=str(repo), scope=args.scope, registry=args.registry, dry_run=False
        )
        if cmd_scope(scope_args) != 0:
            return 1
    else:
        print("\n3/4  scope npm: non richiesto, salto.")

    meta = read_repository_toml(repo)
    package_path = meta.get("frontend", {}).get("package", {}).get("path")
    fe_root = package_path.split("/packages/")[0] if package_path else None
    if fe_root and not args.no_install:
        print("=" * 70)
        print("4/4  make install nel frontend")
        print("=" * 70)
        # Non basta `pnpm install`: su un repo appena generato `@plone/volto` e'
        # dichiarato `workspace:*` e si risolve solo dopo che mrs-developer ha
        # clonato Volto core, cosa che fa il target `install`. Girando dopo il
        # rename, il suo `pnpm i` scrive gia' il lockfile con il nome scoped.
        _run(["make", "-C", fe_root, "install"], cwd=repo)
        format_frontend(repo, fe_root)
    else:
        print("\n4/4  make install: salto.")
        warn(
            f"senza install non posso formattare: lancia `make -C {fe_root} format` "
            "prima del primo push. Il `settings.ts` che genera cookieplone viola il "
            "`.prettierrc` del repo, quindi la CI fallirebbe su `make lint`."
        )

    print("\n" + "=" * 70)
    print(f"Pronto: {repo}")
    print("=" * 70)
    print("""
Da verificare (nessuno e' stato lanciato da qui):

  make -C backend install
  make -C frontend build

Il repo ha un `git init` senza commit: il primo commit lo fai tu.
""")
    # In coda, come ultima cosa a schermo: sono i passi che vanno fatti a mano e
    # fuori da qui, e finche' non sono fatti la prima release non funziona.
    if package_path:
        print_npm_bootstrap(repo, meta)
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="redturtle-cookieplone",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="mostra le modifiche senza scriverle"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "prompts", help="estrae le domande del wizard dal template cookieplone"
    )
    p.add_argument(
        "template", nargs="?", default="monorepo_addon", help="nome CLI del template"
    )
    p.add_argument("--tag", default="main", help="branch/tag/sha del repo dei template")
    p.set_defaults(func=cmd_prompts)

    p = sub.add_parser(
        "align", help="allinea un monorepo appena generato alle convenzioni RedTurtle"
    )
    p.add_argument("repo", help="root del monorepo generato")
    p.add_argument(
        "--volto-version",
        dest="volto_version",
        help="Volto da cui ricavare le versioni delle devDependencies "
        "(default: il tag di frontend/mrs.developer.json)",
    )
    p.set_defaults(func=cmd_align)

    p = sub.add_parser(
        "scope", help="rinomina il pacchetto npm sotto uno scope (es. @redturtle)"
    )
    p.add_argument("repo", help="root del monorepo")
    p.add_argument(
        "--scope", required=True, help="scope npm, con o senza @ (es. @redturtle)"
    )
    p.add_argument(
        "--registry",
        default="https://registry.npmjs.org/",
        help="registry da fissare in publishConfig (default: npmjs.org)",
    )
    p.set_defaults(func=cmd_scope)

    p = sub.add_parser(
        "create",
        help="genera con cookieplone e applica overlay + scope + pnpm install, senza interruzioni",
    )
    # Tutte opzionali: senza --title il wizard di cookieplone fa le domande da
    # se'. Passarle serve solo per generare non interattivo (es. da un agente).
    p.add_argument("--title", help="Add-on Title; se manca, wizard interattivo")
    p.add_argument("--description", help="Description of the add-on")
    p.add_argument("--project-slug", dest="project_slug")
    p.add_argument("--python-package-name", dest="python_package_name")
    p.add_argument("--npm-package-name", dest="npm_package_name")
    p.add_argument("--github-organization", dest="github_organization")
    p.add_argument("--plone-version", dest="plone_version")
    p.add_argument("--volto-version", dest="volto_version")
    p.add_argument(
        "--answers-file",
        dest="answers_file",
        help="un .cookieplone.json da cui partire (le opzioni qui sopra vincono)",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        dest="output_dir",
        default=".",
        help="directory genitore (default: quella corrente)",
    )
    p.add_argument(
        "--scope", default=None, help="scope npm da applicare subito (es. @redturtle)"
    )
    p.add_argument(
        "--registry",
        default="https://registry.npmjs.org/",
        help="registry da fissare in publishConfig (default: npmjs.org)",
    )
    p.add_argument("--container-registry", default="github", dest="container_registry")
    p.add_argument("--docs", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--template", default="monorepo_addon")
    p.add_argument(
        "--no-install", action="store_true", help="salta il pnpm install finale"
    )
    p.set_defaults(func=cmd_create)

    p = sub.add_parser(
        "integrate", help="collega l'add-on a un progetto Volto 17 con yarn workspaces"
    )
    p.add_argument("host", help="root del progetto Volto ospite")
    p.add_argument(
        "addon", help="root del checkout dell'add-on, dentro src/addons dell'ospite"
    )
    p.add_argument(
        "--theme",
        required=True,
        help="add-on tema che deve dichiarare la dipendenza (es. rer-theme)",
    )
    p.set_defaults(func=cmd_integrate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
