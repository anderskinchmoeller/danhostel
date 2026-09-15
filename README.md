# Prismotor — Danhostel Aarhus City

Foreslår en værelsespris og en sengepris for hver dato 120 dage frem, ud fra
hvor fyldt huset er på vej til at blive, hvad markedet tager, og hvad der sker i
byen. Du læser forslagene i browseren og taster dem selv ind i Picasso.

**Servicen skriver ingenting til Picasso.** Den kan ikke og skal ikke. Den
regner, og du beslutter.

---

## Kom i gang

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # sæt RMS_USER og RMS_PASSWORD
uvicorn app.main:app --reload
```

Åbn http://127.0.0.1:8000 og gør tre ting:

1. **Data** → upload `samples/belaegning_eksempel.csv` og
   `samples/konkurrentpriser_eksempel.csv`
2. **Priser** → tryk **Kør beregning**
3. Når det virker: skift eksempelfilerne ud med dine egne to fra Picasso

---

## De to filer du uploader

**Belægning** — én række pr. dato, begge solgte antal med, også når de er nul.

```csv
dato;solgte_vaerelser;solgte_senge;blokerede_vaerelser;blokerede_senge;vaerelsespris;sengepris
2026-09-16;24;148;4;0;690;280
```

**Konkurrentpriser** — medianpris for sammenlignelige værelser og senge.

```csv
dato;medianpris_vaerelse;medianpris_seng
2026-09-16;640;285
```

Kolonnenavne genkendes på dansk og engelsk, med og uden æøå. Både semikolon og
komma virker som separator, og datoer må skrives `2026-09-16`, `16-09-2026`
eller `16/09/2026`.

Alt efter de tre første kolonner i belægningsfilen er valgfrit. Jo mere du
sender med, jo mindre gætter modellen — men den kører på de tre.

**Hvor kommer belægningsfilen fra?** Rapportmodulet i Picasso (**F7**) kan give
et øjebliksbillede i hånden, men manualen dokumenterer hverken eksport til fil,
sengetal eller tal fremad i tid. Den daglige fil skal derfor komme fra en
planlagt eksport, som AK Techotel sætter op. `REFERENCE.md` har detaljerne og
hvad du skal bede dem om.

---

## Til daglig

| Hvornår | Hvad |
|---|---|
| Hver morgen, 5 min. | Upload dagens belægning, kør beregningen, kig på **Priser**. Start med de datoer der har en bemærkning |
| Ved gruppeforespørgsler | **Grupper** → antal værelser og datoer → minimumspris |
| Ved tvivl om en dato | Lås den med dine egne priser. Automatikken rører den ikke |
| En gang om måneden | Opdatér **Events** fra VisitAarhus' kalender |
| En gang i kvartalet | Opdatér review-scores under **Indstillinger** |

De fem minutter om morgenen er ikke valgfrie. Et system ingen kigger på bliver
langsomt forkert uden at fejle, og det opdager man først når det har kostet
penge.

---

## Tre ting du skal vide, før du bruger priserne

**1. Lageret i `config/config.yaml` er stadig et gæt.** Antal private værelser,
flex-rum og dormsenge er skrevet ud fra husets offentlige beskrivelse. Er de
forkerte, er alle priser forkerte i samme retning hver eneste dag. Ret dem til
den faktiske opdeling i Picasso og sæt `confirmed: true`. Indtil da siger
dashboardet det selv med rødt.

**2. Parametrene er brancheskøn.** Bookingkurver, sæsonprofil og målbelægning
er gennemsnit fra branchen, ikke fra jeres gæster. Kør
`python -m app.calibration` på jeres egen historik, før tallene bruges til
noget. En god model på forkerte kurver er stadig en forkert model.

**3. Servicen fanger sine egne dårlige data — men ikke sine egne dårlige gæt.**
En uploadet fil bliver afvist hvis den ser plausibel men forkert ud: en kolonne
der er nul hele vejen, flere solgte senge end huset har, et spring på 40
procentpoint på ét døgn. Den kan ikke vide om prisen er for høj. Det kan kun du.

---

## Når noget ser forkert ud

Øverst på hver side står **Sidst opdateret**. Bliver den rød, er tallene på
skærmen gamle, og der lægger sig en advarsel under menuen. Kig altid på den
først — en frossen side og en frisk side ligner ellers hinanden fuldstændig.

Bliver en upload afvist, står grunden i klartekst på **Data**. Ved du at filen
er rigtig alligevel, kan du krydse af og importere den; det bliver skrevet i
loggen med dit navn.

Kør testpakken når du har ændret noget:

```bash
pytest -q
```

---

## Mere

`REFERENCE.md` har detaljerne: prismodellen og dens formler, alle valgfrie
kolonner, kalibrering, guardrails, API-endpoints, sikkerhed ved drift på en
server, og hvad der endnu ikke er bygget.

```
app/engine.py       prismodellen: prognose, to lagre, flex-allokering, RevPAB
app/service.py      kørslen: hent, beregn, gem
app/sanity.py       rimelighedstjek på uploadet belægning
app/main.py         FastAPI: dashboard, grupper, upload, godkendelse
app/adapters/       CSV i dag, Picasso den dag der er API-adgang
app/calibration.py  bookingkurver og sæson fra egen historik
config/config.yaml  lager og alle parametre, ingen kode
```
