# RedTurtle Cookieplone

Genera un add-on Plone con [cookieplone](https://github.com/plone/cookieplone) e ci
applica le convenzioni RedTurtle: backend su `zc.buildout`/setuptools, CI senza i job
che per un add-on non servono, release di backend e frontend via trusted publishing.

Niente da installare:

```bash
uvx --from git+https://github.com/RedTurtle/redturtle-cookieplone redturtle-cookieplone create -o .
```

Per rigenerare l'identico a mesi di distanza, pinna il tag:

```bash
uvx --from git+https://github.com/RedTurtle/redturtle-cookieplone@v1.0.0 redturtle-cookieplone create -o .
```

Serve `uv >= 0.4`, `git`, e per il frontend `make`, `node`, `jq`.

| comando | cosa fa |
| --- | --- |
| `create` | genera con cookieplone e applica tutto il resto |
| `align` | applica l'overlay a un repo gia' generato |
| `scope` | rinomina il pacchetto npm sotto uno scope |
| `integrate` | collega l'add-on a un progetto Volto 17 ospite |
| `prompts` | stampa le domande del wizard, in JSON |

`--dry-run` e' un flag **globale**: va prima del sottocomando
(`redturtle-cookieplone --dry-run align <repo>`).

## create

Senza opzioni, le domande le fa il wizard di cookieplone, poi lo script prosegue da
solo. E' il modo da usare a mano.

```bash
redturtle-cookieplone create -o .
```

Fa in sequenza, fermandosi al primo errore: `cookieplone` → `align` → `scope` →
`make -C frontend install`.

Non interattivo, con `--title` (servono anche `--description`, `--project-slug`,
`--github-organization`):

```bash
redturtle-cookieplone create \
  --title "RER Linkchecker" --description "..." \
  --project-slug rer-linkchecker --python-package-name rer.linkchecker \
  --github-organization RegioneER \
  --plone-version 6.2.0 --volto-version 19.3.0 \
  --scope @redturtle -o .
```

| opzione | default |
| --- | --- |
| `--scope` | nessuno; con `@redturtle` il nome npm nasce gia' scoped |
| `--template` | `monorepo_addon` |
| `--container-registry` | `github` |
| `--docs` / `--no-docs` | docs generate |
| `--python-max` | `3.13` |
| `--plone-version`, `--volto-version` | l'ultima rilasciata, risolta via rete |
| `--answers-file` | un `.cookieplone.json` da cui partire |
| `--no-install` | salta `make -C frontend install` |

Ogni generazione lascia un `.cookieplone.json` nel repo: ripassandolo con
`--answers-file` si rifa' la stessa cosa, template incluso. Le opzioni sulla riga di
comando vincono su quel file.

**Volto 17 non e' generabile** (cookieplone accetta da `18.0.0-alpha.43` in su), quindi
un add-on nuovo non e' integrabile in io-Comune finche' la migrazione non e' fatta.

## align

```bash
redturtle-cookieplone align <repo>
```

Idempotente. Fa otto cose:

- scrive `backend/setup.py`, lo shim per installarlo come develop egg da buildout;
- adatta `backend/pyproject.toml`: `license` in forma `{ text = ... }`, vincolo
  superiore su `requires-python`, blocchi `[tool.setuptools]`;
- rimuove dalle workflow i job `release` e `storybook`, con i loro riferimenti;
- aggiunge `.github/workflows/npm.yml` e `.github/workflows/pypi.yml`, che pubblicano
  al push di un tag autenticandosi via OIDC;
- mette `publish = false` in `repository.toml`, cosi' `repoplone` non pubblica in
  locale: aggiorna versioni e changelog e crea il tag;
- installa `scripts/release.sh`, `scripts/bootstrap-npm.sh` e i target `release` /
  `bootstrap-npm` nel Makefile;
- sostituisce `workspace:*` con le versioni pubblicate nelle devDependencies
  dell'add-on, ricavandole da `@plone/volto@<versione>`.

Opzioni: `--python-max` (default `3.13`), `--volto-version` (default: il tag di
`frontend/mrs.developer.json`).

Va lanciato **prima** di `make -C frontend install` — e' quello che fa `create`.
Al contrario il lockfile va rigenerato, e `align` avvisa.

## scope

Con `create --scope` non serve. Su un repo gia' generato senza scope:

```bash
redturtle-cookieplone scope <repo> --scope @redturtle
```

Rinomina solo dove il nome e' un nome e non un path: la cartella
`frontend/packages/volto-<slug>/` non si tocca. **Non farlo a mano**, sono otto file.

Dopo, rigenera il lockfile con `make -C frontend install` (la CI gira con
`--frozen-lockfile`).

Fissa anche `"@scope:registry"` in `publishConfig`. Serve: un `@scope:registry`
nell'`~/.npmrc` dirotta `npm publish` su un altro registry senza dare errore.
`--registry` punta altrove (default npmjs.org).

## Prima release: due setup una tantum

### npm

```bash
make bootstrap-npm
```

Pubblica la prima versione e stampa i valori da incollare nel trusted publisher (npm
lo accetta solo su un pacchetto che esiste gia'). Idempotente.

Il trust si configura **dal sito**, non da CLI: pacchetto → Settings → *Trusted
Publisher* → GitHub Actions, organizzazione, repository, file di workflow `npm.yml`.
Lascia attivo il permesso di **publish**: dal 20 maggio 2026 non e' piu' implicito.

Prima di lanciarlo, controlla che `GITHUB_SLUG` in `scripts/bootstrap-npm.sh` sia il
repo dove girano le Actions: il binding OIDC e' sensibile alle maiuscole.

Prerequisito esterno: l'organizzazione `@redturtle` deve esistere su npm e devi avere
i permessi di publish.

### PyPI

Da pypi.org → account → *Publishing*, dichiara un **pending publisher**: nome del
progetto, owner e repository GitHub, file di workflow `pypi.yml`, eventuale
environment (se lo usi, va messo anche nel job). Alla prima pubblicazione PyPI crea il
progetto. Nessuna publish manuale.

Il pending publisher non prenota il nome.

## Rilasciare

```bash
make release
```

Aggiorna versioni e changelog e crea il tag. **Non pubblica**: al push del tag partono
`pypi.yml` e `npm.yml` via OIDC. In locale non serve nessun token; `GITHUB_TOKEN` e'
opzionale, solo per la GitHub release.

Il dist-tag lo deriva il workflow dalla versione: `1.0.0-alpha.23` → `alpha`, `1.0.0` →
`latest`. Finche' pubblichi solo prerelease, `latest` resta alla prima versione
pubblicata; se serve prima, `npm dist-tag add`.

I tre pezzi non sono atomici. Se il tag c'e' ma una publish e' fallita, **non rifare la
release**: rilancia da GitHub Actions il workflow fallito passando il tag.

Per provare la publish senza bruciare una release, replicando la CI:

```bash
git clone <repo> /tmp/citest && cd /tmp/citest/frontend
pnpm install --frozen-lockfile
pnpm --filter <npm-name> publish --dry-run --no-git-checks --tag alpha
```

## integrate

Per sviluppare un add-on non ancora rilasciato dentro io-Comune o derivati. Metti il
checkout sotto `src/addons/` dell'ospite, poi:

```bash
redturtle-cookieplone integrate <progetto-ospite> <path-addon> --theme <addon-tema>
```

Aggiunge il workspace yarn, il path in `jsconfig.json`, la dichiarazione nel tema, le
`resolutions` per le devDependencies `workspace:*` e una riga in `.eslintignore`. Non
tocca il repo dell'add-on.

Poi `yarn install`, e verifica:

```bash
node -e "
const {AddonConfigurationRegistry} = require('@plone/volto/addon-registry');
const reg = new AddonConfigurationRegistry(process.cwd());
console.log(reg.packages['<npm-name>']);
"
```

Deve dare `isRegisteredAddon: true` e `isPublishedPackage: false`.

## prompts

```bash
redturtle-cookieplone prompts [template] [--tag <branch|tag|sha>]
```

JSON con `key`, `prompt`, `help`, `type`, `default`, `choices`, `derived` per ogni
domanda. Riconosce da se' il formato v2 (cookieplone 2.0) e v1.

Template: `project`, `monorepo_addon`, `backend_addon`, `frontend_addon`,
`aurora_addon`, `aurora_cmfplone`, `volto_nick`, `aurora_nick`,
`aurora_nick_embedded`, `documentation_starter`.

## Trappole

- **`make -C frontend build` va lanciato dalla root del repo.** Altrove `make` esce con
  *No such file or directory*, e con una pipe verso `tail` l'exit code diventa 0. Usa
  `cd <repo> && set -o pipefail && ...`.
- **Il bundle non e' in `frontend/build/`** ma in `frontend/core/packages/volto/build/`:
  in un monorepo add-on si compila Volto core con l'add-on dentro.
- **Il repo generato ha un `git init` senza commit**: il primo commit lo fai tu.
- **Per vedere il diff di `align`** serve una baseline, perche' tutto e' untracked:
  `git -C <repo> add -A` (senza commit), poi `align`, poi `git diff`.
- `redturtle-cookieplone align <repo> --dry-run` fallisce: `--dry-run` va prima del
  sottocomando.

## Variabili d'ambiente

`COOKIEPLONE_REPOSITORY` (path locale o repo remoto dei template) e
`COOKIEPLONE_REPOSITORY_TAG` (branch/tag) valgono anche qui; `prompts` legge dalla
prima se punta a una directory.

---

Per lavorare **su** questo repo: [AGENTS.md](AGENTS.md).
