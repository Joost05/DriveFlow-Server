# DriveFlow Server (publieke HAOS-distributie): projectstatus en changelog

> Laatst bijgewerkt: 2026-11-23 (statusbestand aangemaakt). Dit document is de overdrachtsbron voor volgende chats en agents. Het beschrijft wat aantoonbaar in GitHub staat. Het is **geen bewijs** dat een versie gebouwd, getest of op een Home Assistant-installatie draait.

## 1. Werkafspraken

- **Lees dit bestand eerst** voordat je iets in deze repository wijzigt. Lees daarna de actuele code en de recente commits.
- **Werk dit bestand bij iedere wijziging bij**, bij voorkeur in dezelfde commit of PR. Dat geldt ook als de wijziging nog niet af is. Vermeld datum, branch, commit, versie, gewijzigde bestanden, het doel, de tests met hun resultaat en de vervolgstap.
- **Nooit iets verwijderen, resetten of overschrijven zonder voorafgaande expliciete toestemming van de gebruiker.** Dat geldt voor bestanden, workflows, branches, configuratie en historie. Bestaande gebruikersdata in HAOS `/data` (database, back-ups, tellerstanden, foto's) blijft altijd behouden.
- Introduceer geen nieuwe Supervisor-appidentiteit (`slug`) zonder een goedgekeurd migratieplan.
- Noem niets "geslaagd" of "gepubliceerd" zonder feitelijk bewijs, zoals een run-ID of commit.
- Dit is een **publieke** repository. Zet er nooit API-tokens, persoonsgegevens of ritgegevens in.
- Eerdere changelogregels blijven staan. Nieuwe regels komen bovenaan.

## 2. Basis

| Onderdeel | Waarde (gecontroleerd 2026-11-23) |
| --- | --- |
| Repository | `Joost05/DriveFlow-Server` (publiek), default branch `main` |
| Hoofdbron | Privérepository `Joost05/DriveFlow`, map `haos/driveflow_server`. Projectstatus staat daar in `docs/DRIVEFLOW_STATUS.md` |
| App | `driveflow_server/`, slug `driveflow_server`, poort 8098 |
| Versie in `driveflow_server/config.yaml` | `0.6.11` |
| Versie in `README.md` | `v0.6.3`. **Wijkt af van config.yaml**, nog gelijktrekken |
| Workflows | `catalog-validation.yml`, `discovery-server-tests.yml` |
| Featurebranches | `feature/discovery-place-parity`, `feature/discovery2-national-catalog-alpha1`, `feature/discovery2-reviewed-migration-preview`, `feature/speed-catalog-data-pipeline` |
| Laatste commit `main` | 2026-11-21: `ci: register public NDW catalog branch validations without release` |

## 3. Open punten

| ID | Onderwerp | Status |
| --- | --- | --- |
| DFS-01 | README noemt v0.6.3, `config.yaml` noemt 0.6.11 | Open |
| DFS-02 | Welke versie draait werkelijk op de HAOS-installatie | Niet gecontroleerd |

## 4. Changelog (nieuwe vermeldingen bovenaan)

### 2026-11-23: statusbestand aangemaakt

- **Gewijzigde bestanden:** `PROJECT_STATUS.md` (nieuw) en `AGENTS.md` (nieuw, met de werkafspraken).
- **Behoudscontrole:** alleen documentatie toegevoegd. Code, workflows, branches en data zijn ongewijzigd en er is niets verwijderd.
- **Tests/build:** niet nodig, want het gaat alleen om documentatie.
