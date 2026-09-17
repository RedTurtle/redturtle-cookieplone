# AGENTS.md

Note per chi sviluppa questo repo. Il README e' per chi lo usa: qui c'e' il perche',
che li' non serve.

## Cosa e' e cosa non e'

Un overlay su `cookieplone`, non un fork dei template. `cookieplone` genera un backend
gestito da `hatchling` e una CI che pubblica immagini container; RedTurtle usa
`zc.buildout`/setuptools e per gli add-on non pubblica immagini.

Il delta e' stato ricavato **diffando repo reali contro il loro template di origine**:
`rer-linkchecker`, `collective-searchblocks`, `collective-rercaptcha` (tutti
`monorepo_addon`). Era identico in tutti e tre. La parte di release npm viene da
`collective-rercaptcha`, dove era gia' in produzione. Se si aggiunge qualcosa
all'overlay, la domanda da farsi e' se e' vero in tutti gli add-on o solo in uno.

## Layout

```
src/redturtle_cookieplone/
├── cli.py              tutto il codice, un file solo
└── overlay/*.tmpl      file che l'overlay scrive, con segnaposto @NOME@
```

`cli.py` e' volutamente un file unico: e' nato come script a se' stante e la
divisione in moduli non ripagherebbe. I sottocomandi sono `cmd_<nome>` e l'argparse
sta tutto in `main()`.

## Invarianti

Rispettarli o il comando smette di essere quello che e'.

1. **Solo stdlib, nessuna dipendenza.** E' quello che rende `uvx --from git+...`
   istantaneo. `cookieplone` e `repoplone` si invocano come sottoprocessi via `uvx`,
   non si importano.
2. **Idempotenza.** Ogni sottocomando si rilancia senza cambiare niente la seconda
   volta. `Report.already()` serve a dirlo, non a nasconderlo.
3. **Modifiche chirurgiche, mai file interi.** Si tocca la riga che va toccata e si
   lascia il resto com'e', cosi' l'overlay sopravvive ai cambiamenti upstream. E' la
   ragione per cui si preferisce una regex mirata a un round-trip JSON/TOML, che
   riformatterebbe tutto.
4. **Degradare, non indovinare.** Se manca un'informazione (rete, versione di Volto,
   un file), si avvisa con `warn()` e si va avanti. Non si inventa un valore e non si
   aborta l'intero `align` per un pezzo.
5. **I template dell'overlay si leggono con `importlib.resources`** (`overlay_text()`),
   mai da `__file__`: installato da git il codice sta in una directory temporanea di
   uv, senza nessuna `overlay/` sorella.

## cookieplone 2.0

Uscita il 14 settembre 2026. Cambia parecchio, e queste sono le parti che ci toccano.

**Formato della configurazione.** In root `cookiecutter.json` e' diventato
`cookieplone-config.json`; per template `cookiecutter.json` e' diventato
`cookieplone.json`, con un JSON Schema sotto `schema.properties` invece di una mappa
piatta. `cmd_prompts` legge entrambi (`_read_repository_config` prova il v2 e ripiega
sul v1 al 404), perche' `--tag` deve poter leggere le domande di un template vecchio.
Le proprieta' `__*` sono campi calcolati: non si chiedono e non si passano.

**I template sono stati riorganizzati** in `templates/add-ons/`, `templates/projects/`,
`templates/ci/`, `templates/sub/`. `classic_project` non esiste piu'; `seven_addon` e'
un alias nascosto di `aurora_addon`. Non tenere elenchi di template nel codice: c'e'
`prompts`, che li stampa quando gliene passi uno sbagliato.

**La CI e' un subtemplate** (`ci/gh_monorepo_addon`) e usa workflow riusabili:
`main.yml` orchestra `config.yml`, `backend.yml`, `docs.yml`, `frontend.yml`, ognuna
con un job `report` finale che compone una tabella. Togliere un job non basta: va tolta
anche la sua riga dalla tabella, altrimenti resta un `needs.<job>.result` su un job che
non esiste. Lo fa `remove_needs_references()`, e si verifica con `actionlint`.

**Lo scope npm e' nativo.** Il filtro `unscoped_package_name` ricava il nome della
cartella da `npm_package_name`, quindi passando `@redturtle/volto-x` la cartella resta
`volto-x` e i nomi sono scoped. E' per questo che `create --scope` scopa a monte
(`scoped_npm_name()`) invece di rinominare dopo. Il sottocomando `scope` resta per i
repo gia' generati.

**`--answers-file` e `.cookieplone.json`.** Le risposte si passano in un file JSON con
dentro `__template__`, che seleziona anche il template. Sostituisce il vecchio
`chiave=valore` sulla riga di comando ed e' lo stesso formato che cookieplone lascia nel
repo generato.

**Il wizard interattivo e' `tui-forms`** (renderer `rich`), con pagina di conferma e
back-navigation. E' il motivo per cui `create` senza `--title` e' la modalita' da
preferire, e per cui non serve piu' fare l'intervista da fuori.

**Cose che non sono cambiate:** `VOLTO_MIN_VERSION = "18.0.0-alpha.43"` in
`cookieplone/settings.py`, quindi Volto 17 resta non generabile, e quel minimo sta nel
**pacchetto**, non nei template: `--tag <sha>` da solo non basta a tornare indietro,
servirebbe anche pinnare una cookieplone piu' vecchia.

## Dove va l'output che l'utente deve leggere

`print_npm_bootstrap()` elenca i due setup una tantum (pending publisher PyPI,
`make bootstrap-npm`) senza cui la prima release non funziona. Va stampato **per
ultimo**, non dove viene prodotto: chiamato dentro `create` sarebbe la fine del passo
2 di 4, e lo scroll di `make install` lo porterebbe via. Per questo `cmd_align` lo
salta quando `args.defer_next_steps` e' vero, e `cmd_create` lo stampa in coda.

Stessa ragione per il promemoria in testa a `scripts/release.sh`: la configurazione
del trusted publishing si vede alla generazione del repo, che puo' essere di mesi
prima del primo `make release`. E' un promemoria e non un controllo di proposito —
lo stato npm sarebbe verificabile (`npm view` esce 1 se il pacchetto non c'e'), quello
del pending publisher PyPI no, e una release legittima non deve dipendere da mezza
verifica.

Regola generale: se un'informazione serve **al momento di agire**, va stampata li',
non dove il codice la calcola.

## Il criterio del rename sotto scope

Nel repo lo stesso identificatore compare **sia come nome sia come path**. Si sostituisce
solo dove e' un nome. File in `SCOPE_FILES`; la guardia e' `_scope_pattern()`, che
esclude cio' che e' preceduto da `packages/` o `github.io/` e cio' che e' seguito da
`-dev`.

Restano intatti di proposito: le `working-directory` di `npm.yml` (sono path); il `name`
della root `volto-<slug>-dev` (wrapper di sviluppo non pubblicato); l'URL del badge
Storybook. `ADDON_NAME` del `frontend/Makefile` non va toccato perche' lo ricava a
runtime da `repository.toml`, quindi segue il rename da se'.

Il registry va fissato con la chiave **`"@scope:registry"`**, non `"registry"`:
verificato con `npm publish --dry-run` che la seconda non ha effetto, perche' la config
per-scope dell'`~/.npmrc` ha precedenza piu' alta di `publishConfig.registry`. Tutto il
flusso di release e' specifico di npmjs.org (`npm trust github`, OIDC): su GitLab
`npm.yml` e `bootstrap-npm.sh` andrebbero riscritti, non adattati.

## Il fix di `workspace:*`

Il template dichiara `@plone/registry`, `@plone/scripts`, `@plone/types` a
`workspace:*` **senza condizioni su `volto_version`**: vale identico per 18 e 19. Quei
pacchetti stanno in `frontend/core`, che non e' in git (lo clona mrs-developer), e il
job di release fa solo `pnpm install --frozen-lockfile`: la publish muore con
`ERR_PNPM_CANNOT_RESOLVE_WORKSPACE_PROTOCOL` dopo che il tag e' gia' stato creato e il
backend gia' pubblicato.

Si cura nel `package.json` dell'add-on e non nel workflow, cosi' la CI resta identica
per tutti gli add-on senza doversi clonare Volto core solo per pubblicare.

Le versioni si leggono dal manifesto npm di `@plone/volto@<versione>`, che dichiara
esatte `@plone/registry` e `@plone/scripts` fra le `dependencies` e `@plone/types` fra
le `devDependencies`. Cosi' non c'e' nessuna tabella da aggiornare, e si evita la
trappola dei dist-tag: `@plone/scripts` 4.x su npm non e' taggata `latest`, quindi
chiederla per tag darebbe la 3.x.

**L'ordine conta**: `align` prima di `make -C frontend install`. Al contrario il
lockfile registra le tre come `link:` dentro `core` e non corrisponde piu' al
`package.json`; `pin_workspace_devdeps()` avvisa se trova un lockfile.

### Come si verifica davvero

Il primo controllo che sembra ovvio **e' sbagliato**: se rimetti `workspace:*` nel
`package.json` di un repo dove il lockfile e i `node_modules` vengono dallo stato gia'
corretto, la publish passa lo stesso e ti convinci che il problema non esista. Serve un
repo dove `workspace:*` c'era **prima** che il lockfile venisse generato.

```bash
# controllo (senza fix)
cp -r <repo-appena-generato> ctrl && cd ctrl
make -C frontend install            # il lockfile registra link: dentro core
git add -A && git commit -m baseline
git clone . ../citest-ctrl && cd ../citest-ctrl/frontend
pnpm install --frozen-lockfile      # niente core, come in CI
pnpm --filter <npm-name> publish --dry-run --no-git-checks --tag alpha
# atteso: ERR_PNPM_CANNOT_RESOLVE_WORKSPACE_PROTOCOL

# con il fix: stesso giro su un repo dove align e' girato PRIMA di make install
# atteso: + <npm-name>@<versione>
```

`pnpm publish` stampa `Catalog file does not exist at: .../frontend/core/catalog.json`
in entrambi i casi: e' rumore, non e' la causa.

## Testare una modifica

Non c'e' una suite. Si prova contro cookieplone vero, che e' l'unico test che conta.

**Durante lo sviluppo, esegui i sorgenti diretti:**

```bash
PYTHONPATH=src python3 -m redturtle_cookieplone <sottocomando> ...
```

`uvx --from .` **non va bene per questo**: serve una wheel in cache e ignora le
modifiche ai sorgenti. Nemmeno `--refresh`, `--reinstall` o un `[tool.uv] cache-keys`
lo smuovono; l'unico modo e' `uv cache clean <nome-pacchetto>` prima di ogni giro. Ci
si perde mezz'ora a inseguire un bug gia' corretto, quindi: sorgenti diretti mentre
sviluppi, `uvx --from git+file://$PWD` solo per la prova finale dopo il commit.

**Baseline pulita da riusare per i diff:**

```bash
uvx cookieplone monorepo_addon -o gen --no-input \
  'author=RedTurtle Technology' email=sviluppo@redturtle.it \
  'title=Demo' description=d project_slug=demo python_package_name=demo.x \
  npm_package_name=volto-demo github_organization=RedTurtle \
  container_registry=github initialize_documentation=0 \
  plone_version=6.2.0 volto_version=19.3.0
```

**I controlli che contano:**

```bash
cp -r gen/demo probe
PYTHONPATH=src python3 -m redturtle_cookieplone align probe
diff -r gen/demo probe -x .git -x .ruff_cache        # il delta dell'overlay

uvx --from actionlint-py actionlint probe/.github/workflows/*.yml
uv pip install --no-deps --target /tmp/x -e probe/backend   # il setup.py shim regge
PYTHONPATH=src python3 -m redturtle_cookieplone align probe # idempotenza
```

Il build del frontend (`make -C frontend build`, minuti) serve solo quando si tocca
qualcosa che riguarda il frontend. Il bundle esce in
`frontend/core/packages/volto/build/`.

**Provare il comando come lo vede l'utente**, dopo il commit:

```bash
uvx --from "git+file://$PWD" redturtle-cookieplone <sottocomando> ...
```

Costruisce dall'**ultimo commit**, non dal working tree.

### I due stati da distinguere sempre

Molti messaggi e molte decisioni dipendono da **se il repo e' stato installato o no**
(`frontend/pnpm-lock.yaml` esiste?) e da **se il nome npm e' gia' scoped**. Un test su
un repo appena generato non dice niente su un repo installato, e viceversa: provale
entrambe.

Esempio concreto di cosa va storto se non lo si fa: il messaggio "rigenera il lockfile"
di `cmd_scope` usciva anche su un repo appena generato, dove nessun lockfile esiste e
`create` sta per crearlo giusto al passo dopo.

## `extends`: la strada non presa

Da cookieplone 2.0 un `cookieplone-config.json` downstream puo' dichiarare
`"extends": "gh:plone/cookieplone-templates"` e sovrascrivere singoli file di un
template upstream, senza forkare. E' tecnicamente il modo "giusto" di fare quello che
fa questo repo.

Non si e' preso perche' **l'override e' a livello di file intero**: vendorizzare
`backend/pyproject.toml`, `backend.yml`, `frontend.yml`, `Makefile` e `repository.toml`
congelerebbe i bump dei `gha_version_*` che Plone fa spesso, e ce ne accorgeremmo solo
quando qualcosa si rompe. Le modifiche di qui sono chirurgiche e si riapplicano su
qualunque versione upstream.

Da rivalutare se il delta cresce, o se upstream stabilizza quei file.

## Cosa guardare quando upstream si muove

- un nuovo job nelle workflow dell'add-on → va in `CI_JOBS_TO_DROP`?
- i trove classifier del backend arrivano a 3.14 → alzare il default di `--python-max`
  (upstream ha gia' `versions.backend_python = 3.14`, i classifier no);
- `SCOPE_FILES` e' un elenco: un file nuovo che contiene il nome npm va aggiunto;
- se le devDependencies `workspace:*` cambiano, `WORKSPACE_PROTOCOL_DEPS`;
- se cookieplone smette di scrivere `.cookieplone.json` o cambia `__template__`,
  `cmd_create` va rivisto.
