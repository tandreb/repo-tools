# find_branch_repos

Findet für einen gegebenen Branch-Namen alle Repositories eines Google-`repo`-Manifests,
auf denen dieser Branch remote existiert — ohne irgendein Repository lokal auszuchecken,
zu klonen oder zu verändern.

## Funktionsweise

1. Das Root-Manifest (`default.xml`) wird aus dem Manifest-Repo gelesen, rekursiv inklusive
   `<include>`, `<submanifest>`, `<remote>`/`<default>`-Vererbung und `<remove-project>`.
2. Für jedes so gefundene Projekt wird per `git ls-remote --heads` parallel geprüft, ob der
   Branch existiert (asyncio, konfigurierbare Concurrency).
3. Für Treffer werden Commit-Metadaten (Author/Datum/Subject) nachgeladen.
4. Ausgabe als Konsolen-Tabelle und optional als JSON-Datei.

Datei- und Metadaten-Zugriffe laufen über eine Provider-Kette, die HTTP-APIs (Gitiles,
GitHub, GitLab) bevorzugt und nur als letzten Ausweg einen minimalen, temporären
`git fetch --depth=1` in ein Wegwerf-Bare-Repository macht (kein Working Tree, kein
persistenter Zustand). Mit `--strict-no-fetch` lässt sich dieser Fallback komplett
abschalten.

## Nutzung

```bash
python3 -m find_branch_repos \
  --branch feature/xyz \
  --manifest-url https://android.googlesource.com/platform/manifest \
  --manifest-branch main \
  --json-out result.json
```

Alternativ, wenn bereits ein `repo init`-ter Workspace lokal existiert, kann statt
`--manifest-url` einfach auf dessen `.repo`-Verzeichnis (oder den Workspace-Root darüber)
verwiesen werden — Manifest-URL, -Branch und Root-Datei werden dann aus den dort bereits
vorhandenen lokalen Git-Metadaten gelesen (kein Netzwerkzugriff dafür nötig):

```bash
python3 -m find_branch_repos \
  --branch feature/xyz \
  --repo-dir ~/aosp \
  --json-out result.json
```

### Wichtige Optionen

| Option | Beschreibung |
|---|---|
| `--branch` | Gesuchter Branch-Name (erforderlich) |
| `--manifest-url` | Fetch-URL des Manifest-Repos (erforderlich, außer `--repo-dir` ist gesetzt) |
| `--repo-dir` | Pfad zu einem bestehenden `repo`-Workspace (`.repo`-Verzeichnis oder Workspace-Root); Manifest-URL/-Branch/-Datei/`--local-manifest-dir` werden daraus automatisch ermittelt, sofern nicht explizit gesetzt |
| `--manifest-branch` | Ref des Manifest-Repos (Default: `master`, oder aus `--repo-dir` ermittelt) |
| `--manifest-file` | Root-Manifest-Datei (Default: `default.xml`, oder aus `--repo-dir` ermittelt) |
| `--local-manifest-dir` | Optionales Verzeichnis mit lokalen Manifest-XMLs, wird zusätzlich gemerged (aus `--repo-dir` ermittelt, falls `.repo/local_manifests` existiert und nicht explizit gesetzt) |
| `--concurrency` | Max. parallele Remote-Abfragen (Default: 64) |
| `--timeout` | Timeout pro Repo in Sekunden (Default: 15) |
| `--skip-metadata` | Nur SHA ermitteln, keine Author/Datum/Subject-Abfrage |
| `--json-out` | Pfad für die JSON-Ausgabe |
| `--github-token` / `--gitlab-token` | Tokens für die jeweiligen APIs (auch via `$GITHUB_TOKEN`/`$GITLAB_TOKEN`) |
| `--strict-no-fetch` | Verbietet den minimalen `git fetch`-Fallback, nur HTTP-APIs |
| `--verbose` | Debug-Logging |

### Exit-Codes

- `0`: Lauf erfolgreich (auch wenn 0 Treffer)
- `1`: Manifest nicht auflösbar, `.repo`-Workspace nicht auswertbar oder ungültiger Branch-Name — Abbruch
- `2`: Lauf abgeschlossen, aber mit Fehlern bei einzelnen Repos (siehe `errors` im JSON)

## Tests

Reine Standardbibliothek, keine externen Abhängigkeiten. Läuft mit `unittest`:

```bash
python3 -m unittest discover -s tests
```

Die Tests mocken HTTP-Aufrufe (Gitiles/GitHub/GitLab-Provider) und nutzen für den
`git`-Fallback sowie den End-to-End-Test echte, lokal erzeugte Git-Repositories —
es findet kein Netzwerkzugriff statt.
