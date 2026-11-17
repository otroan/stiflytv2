# Stiflyt2 — byggeplan v3

**Status:** Implementeringsspesifikasjon for Claude Code / Claude Fable 2.  
**Mål:** Fase 1: arbeidsverktøy for å bygge og kvalitetssikre et sammenhengende stinett og DNT-turruter i ett fjellområde.  
**Viktig:** Undersøk Stiflyt v1 før implementering; dette dokumentet beskriver ønsket målarkitektur, ikke verifisert eksisterende kode eller API-tilgang.

## 1. Produktbeslutninger

1. Stiflyt eier **egne ruter, graf, godkjent geometri, metadata og revisjoner**. Eksterne datasett er underlag, ikke operativ sannhet.
2. Turrutebasen importeres som **ruteforslag og kildepåstander**, ikke som en korrekt database som må repareres objekt for objekt. Rekonstruer ruter på nytt når det er raskere.
3. Foretrekk FKB-TraktorvegSti som **geometrikandidat** der den er relevant, men ikke blindt. GPX, Turrutebasen og manuell redigering kan overstyre etter kontroll. NVDB/OSM senere.
4. Et fjellområde velges via ruteprefiks (f.eks. RON, FEM, BRE) og registrert DNT-vedlikeholdsansvar; filteret er ikke fasit, og ekskluderte ruter må kunne legges til manuelt. Beregn justerbart geografisk interesseområde fra kandidatene.
5. Datainnhenting/import er en **egen funksjon med egen UI, API, jobber og migrasjoner**, tilsvarende konseptet i v1, men med streng isolasjon.
6. Publiserte ruter skal aldri endres automatisk ved import, sletting, splitt eller ID-endring i eksterne kilder.
7. Fase 1 prioriterer effektiv manuell rekonstruksjon og topologikontroll, ikke avansert automatisk matching.

## 2. Teknologistakk og deploy

- Backend: **Python 3.12+**, FastAPI, Pydantic v2, SQLAlchemy 2 + psycopg 3, Alembic.
- Geodata: PostgreSQL + PostGIS; Shapely 2, pyproj, evt. GeoPandas for batchjobber; NetworkX kun for algoritmer, ikke som persistensformat.
- Frontend: React + TypeScript + MapLibre GL JS, tegne-/redigeringsverktøy (vurder Terra Draw mot kravene); generert API-klient fra OpenAPI.
- Asynkrone jobber: separat Python worker og databasebasert jobbkø med leasing, retry, idempotens og progresjon. Ikke Celery/Kafka i første versjon uten dokumentert behov.
- Lokal drift: Docker Compose for db, API, worker, frontend; migrations og seed/testdata; CI med lint, typecheck og integrasjonstester.
- Modulær monolitt, ett versjonert REST-API (`/api/v1`), ingen direkte frontend-tilgang til database eller eksterne kilder.

## 3. Streng isolasjon av data

Bruk **separate PostgreSQL-skjemaer, DB-roller og migrasjonsløp**:

| Skjema | Innhold | Skriver | Livssyklus |
|---|---|---|---|
| `source_raw` | Originale nedlastinger, snapshot-manifester, originale egenskaper/geometrier | import worker | Immutable per snapshot; historikk beholdes |
| `source_index` | Normaliserte kildeobjekter, romlig indeks, stedsnavnsøk | import worker | Fullt regenererbar fra `source_raw` |
| `import_control` | Kildeadaptere, importjobber, status, sjekksummer, feil, differanser | import worker | Egen driftshistorikk |
| `core` | Fjellområder, noder, edges, ruter, metadata, kildehenvisninger | domene-API | Stiflyt-eid, aldri overskrevet av import |
| `history` | Immutable domenehendelser, publiserte revisjoner, audit | domene-API | Append-only / revisjonsstyrt |

**Harde regler:**
- Import-DB-rolle har ingen `INSERT/UPDATE/DELETE`-rettigheter i `core` eller `history`. API-rollen kan lese kildeindeks via begrensede views, men ikke endre rådata.
- **Ingen fremmednøkler fra `core` til `source_*`**. Lagre kilde-ID, snapshot-ID og referansetypen som verdier i `core.source_reference`.
- Alle publiserte Stiflyt-edges har **egen materialisert geometri**. Ingen rute krever at et kildeobjekt fortsatt finnes for å kunne vises eller eksporteres.
- Importtransformasjoner er versjonert kode/ETL, **ikke** migrasjoner av `core`. Stedsnavn normaliseres til regenererbar søkeindeks, ikke rutemetadata.
- Oppdatering av `source_index` skjer via staging og atomisk aktivering av et komplett, validert snapshot. Feil/ufullstendig kildehenting må aldri tolkes som sletting.
- Test at `source_index` kan droppes og bygges opp igjen uten endringer i `core` eller publiserte revisjoner.

## 4. Backendmoduler

### 4.1 Areas
`area`: UUID, navn, ruteprefikser, DNT-filterkonfigurasjon, AOI-polygon, buffer, status. Vis inkluderte og ekskluderte rutekandidater; tillat eksplisitt manuell inkludering/ekskludering. Importkriterier er revisjonsstyrte.

### 4.2 Data acquisition & import (egen modul)
Kildeadaptere med kontrakt `discover -> fetch -> validate -> store_raw -> normalize -> index -> diff -> activate`. Første adaptere: Turrutebasen, FKB-TraktorvegSti **når reell tilgang, distribusjon og lisens er bekreftet**, filopplasting GPX/GeoJSON (GeoPackage om hensiktsmessig). Stedsnavn som egen søkeadapter. NVDB/OSM/Strava senere, uten å binde domenemodellen til dem.

- Bevar originalfiler, geometri, metadata, kilde-ID, tidspunkt, CRS, lisens, checksum og transformasjonsversjon.
- For store filer: objektlagring eller filsystem med manifest, ikke nødvendigvis rå bytes i PostgreSQL.
- Jobber: preview, start, progresjon, avbryt, retry, historikk, feil og differanser.
- Differanser: created/updated/missing/split/merged/ambiguous. `missing` betyr ikke automatisk fysisk slettet sti.
- GPX er observasjon/evidens, ikke automatisk korrekt senterlinje. Spor kan ha personopplysninger; tilgang og oppbevaring må vurderes.
- Separate import- og søkeendepunkter; ruteredigering bruker bare aktive, normaliserte kildekandidater.

### 4.3 Graph
`node`, `edge`, `edge_revision`, `edge_source_reference`. Stabile interne UUID-er. Edge har godkjent LineString, fra-/til-node, type (`trail`, `tractor_road`, `road`, `glacier`, `water`, `off_trail`, etc.), usikkerhet, kildeproveniens og status. Eksplisitte topologiske forbindelser; geometrisk kryssing er **ikke** automatisk node. Snap-forslag med toleranse i meter og manuell godkjenning. Deling/sammenslåing må oppdatere referanser atomisk eller blokkere med forklaring.

### 4.4 Routes
`route`, `route_revision`, `route_leg` (ordnet edge-ID, retning, evt. posisjonsintervall), `route_source_reference`. Rute kan rekonstrueres fra importert rutekandidat eller startes blank. Samme edge kan inngå i flere ruter. Lag eksplisitt tilstand: `draft`, `validated`, `published`, `superseded`.

### 4.5 Metadata
Skill `formal_responsibility`, `actual_maintenance`, `marking_responsibility`; importer verdier som **påstander med proveniens**, ikke som bekreftede fakta. Støtt `unknown`, `unverified`, `confirmed`, gyldighetsdato og begrunnelse. Minimumsmetadata for publisering fastsettes i en enkel policy, ikke skjult i UI.

### 4.6 Validation, history & publication
Valider rutekontinuitet med node-ID og retning, geometri/CRS, feilaktige null-lengder, duplikater, ukjente metadata, avvik og spesialpassasjer. Skill **teknisk valid** fra **faglig godkjent/sikker**. Publisering oppretter immutable revisjoner med sporbar bruker og tidspunkt. Egen eksport GeoJSON/GPX.

### 4.7 Places search
`GET /places/search` bruker `source_index` for romlige/stedsnavnbaserte søk med PostGIS + pg_trgm; eventuelle Stiflyt-eide steder lagres separat i `core`. Søk må fungere uten å mutere ruter.

### 4.8 Fremtidige moduler (kun utvidelsespunkter nå)
`media` (bilder), `journal` (dagbok), `tasks` (arbeidsoppgaver), `observations`, `organizations/users`, `external_delta`. Bruk stabile `area_id`, `route_id`, `edge_id`, `node_id`, evt. lineær referanse og objekt-revisjon. Ikke bygg funksjonene i fase 1; dokumenter hvordan de kobles på API og autorisasjon.

## 5. API-kontrakt (forslag, konkretiser med OpenAPI før kode)

**Areas**
- `GET/POST /api/v1/areas`; `GET/PATCH /api/v1/areas/{id}`
- `GET /api/v1/areas/{id}/route-candidates`; `POST /api/v1/areas/{id}/route-candidates/{candidate_id}/include`

**Sources / import**
- `GET /api/v1/sources`; `GET /api/v1/sources/{id}/capabilities`
- `POST /api/v1/areas/{id}/imports/preview`; `POST /api/v1/areas/{id}/imports`
- `POST /api/v1/areas/{id}/uploads`; `GET /api/v1/import-jobs/{id}`; `POST /api/v1/import-jobs/{id}/cancel`
- `GET /api/v1/areas/{id}/imports`; `GET /api/v1/imports/{id}/diff`
- `GET /api/v1/areas/{id}/source-features?bbox=...&source=...`

**Graph / routes**
- `GET /api/v1/areas/{id}/graph?bbox=...`; `POST /api/v1/areas/{id}/edges`; `PATCH /api/v1/edges/{id}`
- `POST /api/v1/edges/{id}/split`; `POST /api/v1/graph/connect`
- `GET/POST /api/v1/areas/{id}/routes`; `GET/PATCH /api/v1/routes/{id}`
- `PUT /api/v1/routes/{id}/legs`; `POST /api/v1/routes/{id}/validate`; `POST /api/v1/routes/{id}/publish`
- `GET /api/v1/routes/{id}/revisions`; `GET /api/v1/routes/{id}/export?format=geojson|gpx`

**Places**
- `GET /api/v1/places/search?q=...&area_id=...`

API-standard: Pydantic request/response-DTO-er (aldri ORM-modeller direkte), OpenAPI-generert TS-klient, paginering, BBOX-filtrering, konsekvente feiltyper, auth/roller for mutasjoner, optimistic concurrency (`ETag`/`If-Match` eller versjonsfelt), idempotency keys på jobber, eksplisitte transaksjoner ved grafendringer. Store kartlag kan senere eksponeres som vektortiles bak samme API-grense.

## 6. Frontend og UX

Hovednavigasjon: **Fjellområder | Datakilder og import | Ruteeditor | Kvalitet | Historikk**.

- Fjellområde: angi prefikser, DNT-filter, juster AOI, se kandidatantall og ruter som ble ekskludert.
- Import: egen side som i v1-konseptet; kildeliste, filopplasting, preview, kjøring, status, historikk, diff og feilrapport.
- Editor: venstre ruteliste og kildevelger; kart i midten; høyre geometri-/metadataeditor; nederst valideringsfeil. Lag med tydelig ulik styling: Turrutebasen, FKB, GPX, Stiflyt-draft, publisert.
- Verktøy: velg kildeobjekt -> materialiser edge, tegn, flytt, splitt, koble, reverser, legg til rute, undo/redo. Varsle ved usikker snapping og topologibrudd.
- Ruteoppbygging: følg sammenhengende edges i ordnet retning; foretrekk FKB-kandidater, vis GPX-avvik; aldri automatisk publisering.
- Vis både metadataens verdi og proveniens/konfidens. Ukjent er et gyldig redigeringsutfall, men publiseringspolicy kan kreve avklaring.

## 7. Implementeringsrekkefølge og stoppunkter

**Fase 0 – Undersøk v1.** Kartlegg repo, eksisterende import, Turrutebase-massering, migrasjoner, stedsnavnsøk, kildetilganger og lisenser. Lag `docs/architecture/ADR-*.md`, risikoliste og konkret plan. Gjenbruk bare velisolert kode. **Stopp for godkjenning av schema/API.**

**Fase 1A – Fundament og isolasjon.** Opprett FastAPI/PostGIS/React-skjelett, separate skjemaer, roller og Alembic-løp, OpenAPI-klient, auth-minimum, Docker Compose og testmiljø. Test at importrolle ikke kan skrive til `core`.

**Fase 1B – Import som selvstendig funksjon.** Område/prefikser/DNT-filter, GPX/GeoJSON, snapshots, jobber, normalisering, romlig søk og import-UI. Turrutebasen og FKB-adaptere når tilgang er verifisert. Bygg import-preview og kildevisning før ruteeditor.

**Fase 1C – Graf og ruteeditor.** Materialiser FKB-kandidat til intern edge, tegn egne edges, snap/splitt/koble, rute-leg-rekkefølge, metadataeditor og lagre draft. Bruk ett pilotområde, gjerne RON.

**Fase 1D – Kvalitet og publisering.** Topologivalidering, metadata-policy, immutable revisjoner, eksport, undo/redo og regresjonstester.

**Fase 1E – Reimport-sikkerhet.** Staging/diff, slettede eller resegmenterte kilder, konflikter, bevaring av publiserte ruter, rebuild av `source_index`.

Ikke start bilde-, dagbok- eller oppgavemoduler før en komplett vertikal rute-workflow er testet.

## 8. Obligatoriske akseptansetester

1. Opprett RON-område, importer rutekandidater med DNT-filter og inkluder en feilaktig ekskludert rute manuelt.
2. Importer GPX; originalfil og normalisert indeks beholdes separat.
3. Importer samme snapshot to ganger uten duplikater i arbeidsnettet.
4. Importrolle kan ikke skrive i `core`/`history` (database-test).
5. Slett/rebygg `source_index`; publiserte ruter, geometri, metadata og historikk er byte-/semantisk uendret.
6. Simuler at NVDB/FKB-kildeobjekt slettes eller splittes; Stiflyt-ruten består og diff/varsel opprettes.
7. Mislykket/ufullstendig import fører aldri til sletting eller markering som fjernet.
8. Materialiser FKB-edge, splitt den og bruk delene i to ruter uten hengende referanser.
9. Kryssende linjer uten eksplisitt node er ikke koblet; et lite gap oppdages av valideringen.
10. Tegn bre-/vannpassasje med type og usikkerhet, uten å utgi geometri som sikker ferdselsanvisning.
11. Korriger vedlikeholdsansvar og behold importert verdi som kildepåstand.
12. Lagre, publiser, eksporter og gjenåpne rute uten tap av geometri eller ruterekkefølge.
13. Hele brukerreisen fungerer via `/api/v1`; frontend kobler aldri direkte til PostGIS/kildesystemer.
14. Stedsnavnsindeks kan rebygges uten å endre rutene.
15. API-et har dokumenterte stabile objekt-ID-er som fremtidige media/journal/tasks kan referere til.

## 9. Utenfor fase 1

Nasjonal fullimport, Strava, automatisk masse-match, live toveis NVDB/Kartverket-synk, feltapp offline, full rettighetsorganisasjon, bilder, dagbok, oppgaver og avansert ruting. Design utvidelsesmulighet, men unngå premature tabeller og tjenester.

## 10. Ferdig-definisjon

En fagperson kan velge et fjellområde, hente/importere Turrutebasen + FKB + egne GPX, sammenligne kildegeometri, bygge eller rekonstruere en rute på Stiflyts egen graf, korrigere geometri og metadata, validere sammenheng, publisere en versjon og eksportere — uten QGIS. Ny import eller bortfall av kildeindeks kan ikke endre publisert rute. Alle handlinger går gjennom et dokumentert API.
