# Prismotor — Danhostel Aarhus City

Foreslår en værelsespris og en sengepris for hver dato 120 dage frem med v4:
bid price på en efterspørgselsfordeling, plus marked, events og faste prisgulve.

Priserne står på en **prisstige**: ni faste trin fra gulv til top pr. dato. V4
vælger trinnet med højest forventet netoomsætning; stigen sørger for træghed,
afrunding, eventtrin og forklaring. Se `REFERENCE.md` → *Version 4*. Du læser
forslagene i browseren og taster dem selv ind i Picasso.

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

Kørt og testet på Python 3.10 til 3.13. Python 3.14 er ikke afprøvet, og flere
af afhængighederne har endnu ikke færdigbyggede pakker til den — vælg en 3.13
hvis `python3` på din maskine peger på noget nyere:

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv .venv
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
sender med, jo mindre gætter modellen — men v4 kan køre på de tre.

**Hvor kommer belægningsfilen fra?** Du behøver ikke lave CSV'en selv. Tag
Picassos **Arrivals**-rapport (Rooms spec.) fra ca. 30 dage tilbage til 120
dage frem, med alle reservationstyper og statusser inkl. In-House, og mail
PDF'en til dig selv. Upload den som den er under **Data**. Servicen tæller
solgte værelser og senge pr. nat og springer tentative, foreløbige og
venteliste over. Samme konvertering fra terminalen:
`python -m app.picasso_belaegning <pdf>`.

PDF'en giver kun de tre første kolonner. Blokeringer og nuværende priser skal
stadig komme fra en planlagt eksport, som AK Techotel sætter op — se
`REFERENCE.md`.

---

## Til daglig

**Resultater** samler prisforslag, priser der er meldt lagt ind, og faktiske
resultater. Upload den sædvanlige Arrivals / Rooms spec.-PDF under Data eller
Resultater. Originalen bevares i `data/validation_pdfs`; loggen bruger PDF'ens
udskriftstidspunkt og gemmer belægning og rumtyper for hver opholdsnat. Samme
PDF giver ikke nye kopier af PDF-observationerne. En gammel rapport bliver
ikke frisk ved at blive uploadet igen.

Rapportens Total Price gemmes særskilt som reservationsbeløb fordelt jævnt
over opholdsnætterne. Det er ikke bogført logiomsætning eller dokumentation
for den offentlige salgspris. Bekræft anvendte salgspriser og normalpriser
under Resultater. Provision, variable omkostninger, særskilte nye bookinger
og annulleringer samt endelige resultater kan suppleres manuelt eller via
CSV-skabelonerne på siden. Ukendt forbliver tomt; nul er en faktisk nulværdi.
Nye bookinger og annulleringer kræver et oplyst tidsinterval og summeres ikke
automatisk på tværs af overlappende intervaller.

Alle registreringer bevares med bruger, kilde og tidspunkt. Godkendelse og
CSV-download opretter aldrig en bekræftelse af, at priserne er lagt ind.
Dækningsbidrag beregnes kun på afsluttede datoer med faktiske omsætnings- og
omkostningstal; bidrag pr. seng kræver også faktisk tilgængelige senge.

Når du henter godkendte priser som CSV, gemmes samme prisfil automatisk i
`dagens_priser/priser_ÅÅÅÅ-MM-DD.csv` og den tilhørende sammenligning i
`skygge-v4-v5/skygge_ÅÅÅÅ-MM-DD.csv`. Begge mapper får også en kopi med
`_run-N` i navnet, så tidligere kørsler bevares. Sammenligningen bruger
beregningens data og omfatter de samme godkendte datoer som prisfilen.
En beregning alene skriver ingen CSV-filer.

På ankomstdagen regnes der også med salg resten af dagen. Skønnet tager
historisk netto-pickup på selve dagen og fordeler det jævnt frem til lokal
midnat, fordi historikken ikke indeholder bookingtidspunkter. Det er markeret
i prisforslagets bemærkning; historiske backtests bruger fortsat slutbelægning.

Efterspørgselsniveauet lærer højst én gang pr. lokal dato, også ved samtidige
kørsler. Ankomstdagens uafsluttede salg indgår ikke i den læring.

Alle dagens kørsler bruger prisstigens seneste trin og udglatning fra før
lokal midnat. Nye data kan ændre dagens forslag; gentagne beregninger på de
samme data bruger ikke flere dagstrin. Næste dag bliver dagens sidste forslag
det nye udgangspunkt.

Belægningsprognosen kommer fra samme efterspørgselsmodel og adaptive niveau som
prisberegningen. Den viser solgte enheder plus forventet resterende salg ved
grundprisen, begrænset til kapaciteten. Prisberegningen vurderer derefter
gæsternes forventede reaktion på hvert prisniveau.

Konkurrentpriser skal være højst `stale_hours` timer gamle (36 som standard).
En tom pris i en ny fil fjerner den tidligere pris for produktet på den dato;
gamle og manglende priser giver en neutral markedsfaktor og en bemærkning.

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

**2. V4 kræver sit modelgrundlag.** `config/demand_model.json` skal findes og
være bygget på jeres historik. Kør `python -m app.cube` og derefter
`python -m app.demand`, når historikken ændrer sig væsentligt. Mangler
modelfilen, starter servicen ikke med en ældre prismotor i baggrunden.

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

`REFERENCE.md` har detaljerne: v4-prismodellen og dens formler, alle valgfrie
kolonner, kalibrering, guardrails, API-endpoints, sikkerhed ved drift på en
server, og hvad der endnu ikke er bygget.

```
app/engine.py       prismodellen: v4-priser, to lagre, flex-allokering, RevPAB
app/ladder.py       prisstigen: trin, træghed, eventtrin og forklaring
app/bidprice.py     v4: forventet omsætning, bid price, grupper og flex
app/service.py      kørslen: hent, beregn, gem
app/sanity.py       rimelighedstjek på uploadet belægning
app/main.py         FastAPI: dashboard, grupper, upload, godkendelse
app/adapters/       CSV i dag, Picasso den dag der er API-adgang
app/calibration.py  bookingkurver og sæson fra egen historik
config/config.yaml  lager og alle parametre, ingen kode
```

### Prognosevalidering

Åbn **Resultater → Se prognosernes præcision** (`/resultater/prognoser`). Ved hver
vellykket prisberegning gemmes den første prognose for hver opholdsdato ved
præcis 30, 14, 7 og 1 dags afstand. Værelser og senge gemmes særskilt med
modelaftryk, aktuelt læringsniveau, kapacitet, historisk reference og 50/80/90 %
intervaller. Gentagne kørsler overskriver ikke målingen. Manglende dage
rekonstrueres ikke; mislykkede kørsler optager ikke et målepunkt.

Endeligt solgte enheder indberettes som **afsluttet** i resultatloggen; begge
salgstal kræves, men økonomital kan komme senere. Arrivals med aktive
reservationer er ikke endelige resultater. Den seneste afsluttede rettelse bruges;
en senere foreløbig registrering erstatter ikke et afsluttet resultat.

Driftsmålingen viser MAE, skævhed, parret forskel mod historisk salg på samme
ugedag/måned og intervaldækning. Referencen bruger kun historiske datoer før
beregningen, uden renoveringsperioden. Manglende reference forbliver ukendt.
Weekender følger konfigurationen; grupper uden indberettet gruppeantal forbliver
ukendte. Travle datoer er mindst 90 % af maksimal kapacitet ved prognosen.
Gruppe- og travlhedsopdeling anvender facit som efterfølgende fejlanalyse.
95 %-interval for MAE-forskellen bruger nu tilnærmet Newey–West/HAC med
Bartlett-vægte, lag max(7, horisont−1) kalenderdage, mindst 60 parrede datoer
og 8 kalenderblokke. Afhængighed ud over dette kan stadig være til stede.

Genkør den særskilte historiske test med:

```sh
.venv/bin/python -m app.forecast_validation
.venv/bin/python -m app.evaluate
.venv/bin/python -m app.backtest --ud-af-stikproeve
```

Den nye rapport trænes på 2024 og vurderes på 8. marts–31. december 2025;
renoveringen udelades. Niveauerne holdes på 1,0, så læring fra 2026 ikke lækker
ind i historikken. Produktionsmodellen ændres ikke. Begge lagre og fire
horisonter vises, inklusive kapacitetsmærkede datoer i en separat delgruppe.
Rapporten gemmes i `data/forecast_validation_history.json` og sammen med alle
observationer i en dateret mappe under `outputs/`. Driftsmålinger kan hentes som
CSV fra siden. Prognoserne gælder referenceprisen; prognosepræcision beviser
ikke en omsætningsgevinst ved ændrede priser. Historikkens manglende
ændringshistorik, blokeringer og flexfordeling begrænser også testen.

### Økonomi og prisforsøg

`/resultater/oekonomi` viser datadækning for hele seneste færdige beregning og
faktiske økonomiske resultater fra resultatloggen. Ingen korrelationsanalyse
eller sammenligning mellem prisforslag betegnes som målt merindtjening.
Fremtidige opholdsdatoer har endnu ikke endelig bogført omsætning.

Den eksisterende prisvariation ændres ikke. Nye beregninger gemmer både
oprindelig pris og nabopris, tildelt valg, sandsynlighed, dato-/produktnøgle
samt om bremse eller lås ændrede forslaget. Også valg af oprindelig pris
registreres. Prisbekræftelser med valgt kørsel fryser disse oplysninger og
sammenholder dem med den oplyste anvendte pris. Gamle forsøgsoplysninger
rekonstrueres ikke. Dette er et observationsgrundlag; det erstatter ikke en
fastlagt forsøgsplan eller måling af faktisk priseksponering og salgsperioder.

### Daglige resultater fra to Picasso-PDF’er

Under **Resultater → Importér afsluttet dagsresultat** uploades Hotelmanager
(part 1 + part 2 med Room/Bed) og produktomsætning pr. rumtype for samme dato.
Begge skal være udskrevet efter opholdsdatoen, med de filtre der står på siden.
Accounts er valgfri til yderligere afstemning.

Importen arkiverer originalerne efter SHA-256 og opretter et rapportpar til
kontrol, ikke et afsluttet resultat. Kontrolsiden viser Today / Room Inhouse,
logi pr. konfigureret rum-/sengekode, foreslåede morgenmadsfradrag og afstemning
mod Hotelmanager-total og ADR. Produkter med morgenmad i navnet og opholdsantal
beholdes som logi; særskilte morgenmadslinjer uden opholdsantal foreslås fratrukket.
Ukendte typer, ukendte beløb og væsentlige afvigelser spærrer økonomibekræftelse.

Trykte rumtypetotaler bruges til opdelingen. Højst 2 øres afrundingsforskel
mod produktlinjer/hovedtotal kan accepteres og vises; en ufordelt forskel lægges
kun til samlet omsætning, aldrig til en rumtype. Hotelmanagers hele-kroner-tal
skal stemme inden for 50 øre, også for gennemsnitspris pr. salgsenhed.

Endelige salgstal og økonomital bekræftes særskilt. Produktudtrækkets
`Arrival date`-fodtekst kræver kontrol af opholdsdato og morgenmad; et match for
én dato dokumenterer ikke generelt korrekt periodisering af flerdagesophold.
Flyttede produkter uden reservation, kredit, provision og øvrige omkostninger
skal kontrolleres særskilt. Dækningsbidrag beregnes først med oplyste faktiske
omkostninger; faktisk tilgængelige senge kræves til RevPAB.

Bekræftede resultater indgår i prognosevalidering og økonomisiden. Importen
opdaterer ikke fremtidig reservationsværdi, træner ikke prismodellen og lægger
ikke priser ind i Picasso. Samme filpar og samme bekræftelse giver ingen dubletter;
rettelser bevares som nye logposter med bruger, observationstid og kilder.


### Graf og statistisk opfølgning

**Resultater → Model mod faktiske tal** (`/resultater/afvigelser`) viser
prognose minus faktisk resultat pr. opholdsdato. Vælg værelser, senge eller
logiomsætning og 1/7/14/30 dage før. Historisk test og ny drift holdes adskilt.
Uden afsluttede driftsmålinger åbnes den tydeligt mærkede historiske test.
SVG-grafen har nulakse, positive/negative fejl, datoafstande og præcise værdier
ved hover samt tabel og CSV. Manglende observationer vises ikke som nul.

Statistikken viser MAE, RMSE, skævhed, referencefordel på præcis samme datoer
og 80 %-intervaldækning. P-værdier er tilnærmede, tosidede HAC-normaltest af
skævhed og gennemsnitlig forskel i absolut fejl mod referencen. Holm-korrektion
reserverer alle 24 planlagte test (3 målinger × 4 horisonter × 2 spørgsmål),
også når nogle endnu ikke kan beregnes. Konfidensintervaller er punktvise.
Filtre/gentagne kig udgør ikke en sekventielt gyldig beslutningsregel, og
samme opholdsdato tæller ikke som flere observationer ved samme horisont.
P-værdier kræver mindst 60 datoer og 8 blokke; det er en forsigtighedsregel,
ikke en garanti for tilstrækkelig styrke eller korrekte tidsserieantagelser.
Ingen p-værdi ved nul estimeret variation. Praktisk betydning og fejlstørrelse
skal altid vurderes sammen; prisgevinst kræver særskilt forsøgsdesign.

Historisk genkørsel skriver nu også `data/forecast_validation_observations.json`
med modelhash. Grafen bruger kun observationer, der matcher rapportens hash.

Fra nye checkpoints fryses desuden omsætningsskøn og moms-/pakkegrundlag.
Anders bekræftede 17-10-2026, at modelpriser er inklusive moms og uden
morgenmad. `validation.price_vat_basis: incl_25` og
`accommodation_only_confirmed: true` giver derfor omregning med 1,25 til
logi ekskl. moms. Det ændrer ikke motorens prisforslag eller RevPAB-visning.
Eksisterende checkpoints overskrives ikke og får ikke genskabt manglende
økonomiprognoser. Et checkpoint med allerede bogførte omsætningsbeløb
spærrer foreløbig omregningen, fordi disse beløbs momsgrundlag ikke endnu er
registreret separat. Ukendt grundlag må aldrig føre til stiltiende blanding.
Kun bekræftede faktiske logital med kontrolleret opholdsdato sammenlignes;
økonomisk reference og usikkerhedsinterval mangler fortsat. Skøn over
værdien af allerede bookede ophold forbliver skøn i omsætningsprognosen.
