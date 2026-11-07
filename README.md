# DriveFlow Server voor Home Assistant

Officiële publieke Home Assistant-apprepository voor **DriveFlow Server v0.6.3**.

## Installeren

1. Home Assistant → **Instellingen → Apps → App installeren**.
2. Rechtsboven **⋮ → Repositories**.
3. Voeg `https://github.com/Joost05/DriveFlow-Server` toe.
4. Zoek **DriveFlow Server** en installeer.

De server gebruikt poort **8098** en vereist je bestaande DriveFlow API-token (minimaal 32 tekens). Geoapify is optioneel.

## Belangrijk bij een bestaande DriveFlow Server

**Verwijder je huidige DriveFlow Server niet.** Een installatie vanuit een andere repository kan een andere Supervisor-appidentiteit krijgen. Hierdoor worden bestaande private `/data`-gegevens (database, back-ups, tellerstanden, foto’s) **niet automatisch gedeeld**.

Maak eerst een volledige Home Assistant-back-up en een afzonderlijke back-up van je huidige DriveFlow Server. Leg de huidige appidentiteit vast. Migreer de database en bestanden pas nadat het herstelpad is getest, en controleer alle bestaande records voordat je de nieuwe installatie gebruikt.

**Installeer en start niet twee instanties tegelijk op poort 8098.**

Deze openbare repository bevat uitsluitend broncode; geen persoonlijke ritgegevens, API-tokens of historische observatievensters.

## Ontwikkeling

De hoofdbroncode wordt onderhouden in de privérepository `Joost05/DriveFlow`, map `haos/driveflow_server`. Bij een nieuwe versie kopiëren we de geteste wijzigingen naar deze distributierepository.

Road Discovery 2.1 bevat server-side segmentopslag en incrementele synchronisatie-API. Automatische synchronisatie vanuit Home Assistant en de Android-cache volgen in latere versies.
