# Prismotor · teknisk reference

Beskriver den nuværende implementation og konfiguration pr. **17. oktober 2026**.
Start med [README](README.md) for daglig brug eller
[Picasso-rapportguiden](HVORDANMANFINDERBELAEGNIONG.MD) for eksportopsætninger.
Konfiguration og kode er kilden til de aktuelle værdier; tabellerne her er et
øjebliksbillede, ikke selvstændige indstillinger.

## Indhold

- [Status og versioner](#status-og-versioner)
- [Lager og salgsenheder](#lager-og-salgsenheder)
- [Prisberegningen](#prisberegningen)
- [Data og import](#data-og-import)
- [Prisdownload og skyggedrift](#prisdownload-og-skyggedrift)
- [Resultatlog og PDF-import](#resultatlog-og-pdf-import)
- [Prognosevalidering](#prognosevalidering)
- [Omsætning og økonomisk effekt](#omsaetning-og-oekonomisk-effekt)
- [Modelgrundlag og analyser](#modelgrundlag-og-analyser)
- [Opstart](#opstart)
- [Drift og backup](#drift-og-backup)
- [Sider og API](#sider-og-api)
- [Kendte begrænsninger](#kendte-begraensninger)
- [Kodekort](#kodekort)

## Status og versioner

| Område | Nuværende adfærd |
|---|---|
| Aktiv prismotor | V4; `pricing.v4.enabled` og prisstigen skal være slået til |
| Modelgrundlag | `config/demand_model.json`; manglende eller ugyldigt grundlag stopper indlæsningen |
| V5 | Opdateringsvariant af samme motor, beregnet som skyggeforslag |
| Picasso | Manuel overførsel; ingen implementeret API-skrivning |
| Horisont | 120 dage frem; beregningen kan også indeholde ankomstdagen |
| Indlæsning | CSV og understøttede Picasso-PDF’er |
| Planlagt beregning | Konfigureret til 04:15, `Europe/Copenhagen` |
| Friskhed | `stale_hours: 36`; kontrolleres pr. opholdsdato |
| Normal drift | `auto_publish: false` |

V4 og v5 er **ikke to uafhængige strategier til forsøget om merindtjening**.
`app/skyggedrift.py` laver v5 ved at sætte `elasticity_measured=True` i en kopi
af parametrene. Det ophæver blandt andet den midlertidige rabatbinding; det
estimerer ikke en ny elasticitet. Den antagelse må ikke forveksles med en måling.

Den økonomiske sammenligning er **prismodellen mod den normale prisstrategi**.
Skyggeforslag, simuleret omsætning og bedre prognoser dokumenterer ikke denne effekt.

## Lager og salgsenheder

| Pulje | Konfiguration | Antal |
|---|---|---:|
| Faste private værelser | D1: 1, D2: 13, D4: 8, F1: 17, F3: 11 | 50 værelser |
| Senge i faste private værelser | `private_beds` | 148 senge |
| Flexrum | FS: 1, V8: 6, V6: 7, V10: 6 | 20 rum |
| Beregnet størrelse på flexrum | `beds_per_flex_room` | 8 senge pr. rum |
| Faste sovesalspladser | `dorm_beds` | 4 senge |

Lageret er markeret `confirmed: true` i konfigurationen. Det er ikke længere
et ubekræftet eksempellager med 20 private værelser og 216 faste dormsenge.

```text
maksimale værelsesenheder = 50 + 20 = 70
maksimale sengeenheder    = 4 + 20 × 8 = 164
RevPAB-nævner             = 148 + 4 + 20 × 8 = 312
```

De to maksimale salgspuljer er alternative yderpunkter, ikke samtidig kapacitet.
Hvis `n` flexrum sælges som hele rum, regnes før blokeringer med:

```text
værelseskapacitet = 50 + n
sengekapacitet    = 4 + (20 − n) × 8
```

Flextypernes konkrete størrelser summerer til 158 senge; modellen bruger 160.
De 312 er derfor en **tilnærmet beregningskapacitet**, ikke en ny optælling af
fysiske senge. Faktisk RevPAB på økonomisiden kræver oplyst tilgængelig kapacitet.

Modellen tillader stadig, at flexrum sælges som enkelte senge. En kommentar i
YAML om kun at sælge hele værelser ændrer ikke denne adfærd. Hvis den daglige
drift kun tillader hele rum, skal modellen og kapacitetsgrænserne tilpasses.

`room_type_otb` og valgfrie `flex_private_min`/`flex_private_max` begrænser
fordelingen. Kontrollen skal også omfatte konkrete rum og hele ophold; én seng
kan binde et rum på flere datoer. Appen flytter ikke gæster eller lager i Picasso.

Prislisten følger koderne i `pricing.room_types` og `pricing.bed_types`.
Typepris beregnes som basispris × typefaktor og afrundes derefter med motorens
afrundingsfunktion. YP, GRP og andre produktnavne afgør ikke rum/seng.

## Prisberegningen

### Efterspørgsel og trinvalg

V4 bruger en fordeling af resterende **netto-pickup**, inklusive negative
udfald ved annulleringer. Modellen bindes til det aktuelle salg på bøgerne.
Prognosen kombinerer bøger og forventet pickup med et historisk kalenderniveau;
vægten varierer med bookinghorisonten. Kapaciteten begrænser forventet salg.

For hvert tilladt pristrin vurderes skematisk:

```text
efterspørgselsfaktor = (trinpris / referencepris) ^ (−elasticitet)
modelbidrag         = logi ekskl. moms − provision på bekræftet grundlag − omkostning ekskl. moms
forventet bidrag     = modelbidrag × E[min(resterende efterspørgsel, ledig kapacitet)]
```

Motoren sammenligner trinene over alle mulige flexfordelinger. Hvert alternativ
bruger samme fælles lager til værelser og senge. Rumprisen vægtes med det
resterende faste typemiks; ekstra flex-salg værdisættes særskilt, efter de faste
værelser er fyldt. Det er en antagelse om fordeling af samlet rumefterspørgsel;
ukendt miks er fortsat et skøn. Den aktive v4 styrer ikke efter
den ældre vægtede målbelægningsformel, selv om ældre parametre og triggerfelter
stadig findes i konfigurationen og på nogle skærmbilleder.

Fra 23. oktober 2026 er `pricing.v4.uncertainty_mode: minimax_regret` aktiv.
For hvert alternativ (rumtrin, sengetrin, én fælles flexfordeling) beregnes
forventet bidrag ved ni kombinationer af prisfølsomhed. I hvert scenarie findes
det bedste gennemregnede alternativ; motoren vælger alternativet med mindst
største afstand til disse scenarieoptima. Flexfriktionen indgår i alle alternativer.
Ved fastlagte endelige priser bruges samme scenarieoptima til at vælge flex.
Marked, events, udglatning, bremse, prisvariation og manuelle låse gælder stadig
efter trinoptimeringen. Den endelige pris og kapacitet stresstestes derfor igen.

Rumscenarierne er `[0.8, 1.6, 2.4]`, sengescenarierne `[0.8, 2.0, 3.2]`.
Det nominelle estimat medtages altid. Stresspunkterne er lokale arbejdsantagelser,
ikke estimater fra et papir, en sandsynlighedsfordeling eller statistiske grænser.
Det er en endags-scenarietilpasning, ikke en implementering af et publiceret
sekventielt læringssystem eller dets garantier. `diagnostics` giver den tidligere
nominelle optimering med samme stresstest. Ældre konfiguration uden feltet bruger
`diagnostics`. Se [forskningsgennemgangen](docs/research-pricing-2026-10-23.md).

`bid.uncertainty` viser forventet bidragsspænd, bedste pris pr. scenarie og
mindste forskel til referenceprisen ved samme **endelige** kapacitet.
Alternativerne i diagnostikken er tilladte stigepriser før dagens bremse/lås;
det er ikke en påstand om, at alle kunne leveres nu. `bid.optimizer.uncertainty`
bevarer den fælles optimerings beslutning og modeltab før efterbehandlingen.
Minimax-regret kræver ikke, at alle scenarier slår referenceprisen, og valget
kan stadig være dårligere end den gamle politik. Der er ingen målt priseffekt.

Læringen udelader nu produktvinduer med blokeringer eller kapacitetsbinding ved
et af de observerede endepunkter (98 % af muligt lager, inkl. fælles flexbinding).
Tidligere anbefalet kapacitet bruges kun som udeladelsessignal. Antal brugte og
udeladte vinduer logges i niveauhistorikken. Kanaltilgængelighed mellem snapshots
er ukendt; udeladelse løser ikke fuldt ud efterspørgselscensur eller selektionsbias.

Økonomisiden viser desuden en rullende 28-dages prognosekontrol med mindst 14
afsluttede opholdsdatoer pr. produkt og horisont. Den bruger oprindeligt gemte
prognoser og sidste afsluttede facit; duplikater og fremtidige datoer tæller ikke.
Kontrolreglerne er praktiske reviewgrænser, ikke signifikanstests eller bevis for
et strukturelt markedsskift. De genestimerer ikke modellen automatisk.

| Parameter | Nuværende værdi |
|---|---:|
| Elasticitet, værelser / senge | 1,6 / 2,0; antagelser, ikke målt på gæsterne |
| Referencetrin | Faktor 1,00 på dagens grundpris |
| Trinfaktorer | 0,80; 0,86; 0,93; 1,00; 1,07; 1,15; 1,24; 1,35; 1,48 |
| Rabatbinding | Højst ét trin under reference, mens elasticiteten er umålt |
| Prisgulv / loft, basisværelse | 375 / 1.400 kr. |
| Prisgulv / loft, basisseng | 165 / 600 kr. |
| Afrunding | 10 kr., med kontrol af tilladte prisgrænser |
| Antaget provision | 16 % |
| Antaget variabel omkostning, værelse / seng | 111,56 / 39,84 kr. |

De økonomiske satser ovenfor bruges i prisberegningen. De erstatter ikke
faktiske omkostninger i resultatloggen. Modelpriser er bekræftet inklusive
25 % moms og uden morgenmad. `pricing.economics` registrerer særskilt
`price_vat_basis`, `cost_vat_basis` (`ex_vat`, `incl_25` eller `unknown`) og
`commission_basis` (`guest_price`, `ex_vat` eller `unknown`). Når alle tre er
bekræftet, regnes modelbidrag eksklusive moms. Ved ukendt grundlag bevares
det gamle skøn `pris × (1 − provision) − omkostning` med en udtrykkelig
advarsel; der foretages ingen delvis omregning. Gruppens beregnede bidrag
omregnes tilbage til en salgspris på samme grundlag. Modelbidrag er fortsat
ikke målt, regnskabsmæssigt dækningsbidrag.

Aktuelt grundlag, bekræftet 23-10-2026: modelpriser inklusive 25 % moms,
variable omkostninger eksklusive moms og 16 % provision af logi eksklusive moms.
Eksempel: basisværelse til 645 kr. giver modelbidrag
`645 / 1,25 × 0,84 − 111,56 = 321,88 kr.`

Markedet kan flytte v4’s valgte position via stigens markedsvægt og kvalitetsindeks.
Events giver eventtrin og referencegulv; eventfaktoren er begrænset til 1,45.
V4 bruger ikke den ældre knaphedsbeskyttelse og ratchet som ekstra gulve.

### Hukommelse, læring og beskyttelse

- Alle dagens kørsler bruger seneste færdige trin og udglatning fra **før lokal
  midnat**. Gentagne klik bruger ikke flere dagstrin på samme datagrundlag.
- Udglatning og hysterese dæmper skift. Trinbegrænsninger er normalt højst to
  trin op og ét ned; tæt på ankomst kan to trin ned være tilladt.
- Ændringsbremsen er aktuelt 15 % omkring prisankeret, når et relevant anker
  findes. Ankeret holdes i 24 timer, så gentagne importer ikke flytter grænsen.
- Prisgulv, prisloft, bremse og afrunding kontrolleres samlet. Modstridende
  grænser fastfryser datoen. Grænserne gælder basispriserne; typer følger faktorerne.
- Rabatgrænsen er også et gulv gennem marked, udglatning, eksploration og
  ændringsbremse. En manuel prislås kan tilsidesætte det automatiske gulv.
- En manuel lås respekteres; afledte priser og prognose genberegnes ved næste kørsel.
- Det adaptive niveau opdateres højst én gang pr. lokal dato under fil-lås.
  Aktuelt `alpha=0,07`, med niveau begrænset til `[0,6; 1,6]`.
  Forventningen i læringsvinduet bruger det ujusterede historiske niveau,
  og annulleringer modregnes på tværs af datoer. Niveauet udglattes mod
  `(faktisk+1)/(forventet+1)` i log-rum; en stabil afvigelse ophobes ikke
  som en ny procentvis ændring hver dag. Eksisterende niveauer nulstilles
  ikke og kan bevæge sig tilbage fra en grænse med nye observationer.
- På ankomstdagen fordeles historisk samme-dags-pickup over den resterende tid
  til lokal midnat. Det er en tilnærmelse uden bookingtidspunkter. Uafsluttet
  ankomstdag bruges ikke til den daglige niveaulæring.

**Konfigurationsforbehold:** YAML indeholder `pricing.v4.unbounded_daily_change`,
men den nuværende `V4Config` og indlæsning understøtter ikke feltet. Det ophæver
hverken ændringsbremsen eller stigens trinbegrænsninger. Dokumentationen følger
koden; feltet er ikke aktiveret eller rettet som del af dokumentationsoprydningen.

### Flex og grupper

Prisberegningen gennemgår alle mulige flexfordelinger og vælger pristrin
ud fra fælles forventet modelbidrag. `flex_min_gain: 25` fratrækkes pr.
ekstra flex-rum over den tvungne minimumsfordeling, så små forskelle ikke
udløser flytninger. Efter marked, prisbremser og manuelle låse beregnes
fordelingen igen ved de faktiske forslag. Fordelingen respekterer solgte
enheder, blokeringer og oplyste grænser. Bid price, salgsskøn og
udsolgtssandsynlighed beskriver de endelige priser og den valgte kapacitet;
det oprindelige optimeringsvalg bevares særskilt under `optimizer`.
Prisregler kan flytte den endelige pris væk fra det matematiske optimum.

### Sengemodellens træningsperiode

`demand_training.bed_training_from: "2025-03-08"` begrænser ved næste
`python -m app.demand` sengemodellernes pickup, form og krympning til det
nyere salgsniveau. Værelsesmodellerne bruger fortsat hele den brugbare historik.
Der kræves mindst 90 brugbare datoer. Manglende sæsonmåneder låner alene
sæsonfaktoren fra ældre historik; deres gamle pickupniveau genbruges ikke.
Modelfilen gemmer træningens første og sidste dato. Efter genestimering på et
nyt grundniveau skal senges niveau rebaseres til 1,0 med bevaret historik.
Niveaufiler til en tidligere model må ikke uden videre anvendes som ny evidens.

Den særskilte udviklingskontrol på oktober–december 2025 bruger kun træningsdata
fra før 1. oktober. Den erstatter ikke den oprindelige 2024→2025-rapport eller
en ny fremadrettet måling. Elasticiteten er fortsat umålt; en bedre salgsprognose
dokumenterer ikke større indtjening.

Gruppens minimumspris bygger på summen af fortrængningsværdier pr. enhed og nat.
`minimum_rate` er forventet fortrængning; `minimum_rate_certain` viser værdien,
hvis punktprognosen rammer. Før elasticiteten er målt, er gruppefortrængningen
som udgangspunkt udæmpet. Gruppen kan også påvirke flex og kapacitet på tværs
af opholdet. Værktøjet giver et gulv til vurdering, ikke et bindende tilbud.
`bidprice.stay_value` findes, men er ikke koblet på en bookingmotor.

### Prisvariation

`pricing.ladder.explore: true` tillader valg mellem to næsten ligeværdige
nabotrinsforslag. Det er deterministisk pr. opholdsdato og produkt. Nye logs
bevarer begge priser, tildelt valg, sandsynlighed og eventuel senere bremse/lås.
En bekræftet anvendt pris kan sammenholdes med tildelingen.

Det er et observationsgrundlag, ikke et færdigt randomiseret forsøg mod den
normale prisstrategi. Antal kørsler er ikke antal uafhængige forsøgsdatoer.
Sæt ikke `elasticity_measured: true` alene fordi prisvariation er slået til.

## Data og import

### Belægning og konkurrentpriser

Belægning kan indlæses fra understøttet Arrivals-PDF eller CSV. Rapportopsætning
står i [Picasso-guiden](HVORDANMANFINDERBELAEGNIONG.MD).

```csv
dato;solgte_vaerelser;solgte_senge;blokerede_vaerelser;blokerede_senge;vaerelsespris;sengepris
2026-10-18;24;4;0;0;690;280
```

De tre første kolonner er krævet. Begge solgte antal skal være eksplicitte,
hele, ikke-negative tal; også nul. Én række pr. dato. Supplerende felter:

| Felt | Betydning |
|---|---|
| `blokerede_vaerelser`, `blokerede_senge` | Kapacitet ude af salg; solgte grupper må ikke også tælles som blokeret |
| `vaerelsespris`, `sengepris` | Aktuelle salgspriser; kan indgå som prisanker |
| `room_type_otb` | JSON med solgte hele rum pr. kode; summen skal matche solgte værelser |
| `flex_private_min`, `flex_private_max` | Grænser baseret på konkrete rumtildelinger |
| `booked_room_revenue`, `booked_bed_revenue` | Omsætningsbeløb på bøgerne; afgiftsgrundlag skal afklares før blanding med priser |

Konkurrentpriser indlæses som CSV fra sammenlignelige produkter:

```csv
dato;medianpris_vaerelse;medianpris_seng
2026-10-18;640;285
```

Kolonnenavne genkendes på dansk og engelsk med normalisering af æøå.
Separator kan være semikolon eller komma; datoer kan være ISO, `dd-mm-åååå`
eller `dd/mm/åååå`. Priser skal være i DKK pr. salgsenhed og nat.

Der er separate sammenligninger for hele værelser og senge. En tom pris i en
ny fil fjerner den tidligere værdi for produkt/dato. Gamle eller manglende
konkurrentpriser giver neutral markedsfaktor og bemærkning.

### Friskhed og rimelighedstjek

Belægningsdata kontrolleres pr. dato før beregning og igen før eksport.
Gamle/manglende data fastfryser datoen. Ændret belægning siden beregningen kræver
ny beregning. Arrivals’ udskriftstidspunkt bevares; gen-upload gør ikke data friske.

`app/sanity.py` undersøger blandt andet huller, nulkolonner i mindst en uge,
kapacitetsoverskridelser, ugyldige datoer og priser. Daglige belægningsspring
stoppes ved over **80 procentpoint for værelser** og **40 for senge**.
En manuel tilsidesættelse under Data logges. Den tilsidesætter ikke motorens
hårde kapacitets- og prisgrænser.

### Adaptere

Kun `pms_adapter: csv` og `rateshop_adapter: manual` er implementeret.
`RMS_CSV_INBOX` kan pege på en mappe med belægnings-CSV’er. Ukendte adapternavne
afvises; der er ingen afprøvet Picasso- eller Lighthouse-API-integration.
Automatisk hentning af komplette fremtidige data kræver en dokumenteret eksport
eller integration med kapacitet, solgte enheder, typer, blokeringer og salgspriser.

## Prisdownload og skyggedrift

`GET /export.csv` henter som udgangspunkt godkendte priser efter kontroller af
lager og friskhed. Formatet er semikolon, decimalkomma og UTF-8 med BOM.
Eksporten har basispriser, alle konfigurerede typer og et flexforslag.

Ved download gemmes samme fil og matchende skyggefil:

```text
dagens_priser/priser_ÅÅÅÅ-MM-DD.csv
dagens_priser/priser_ÅÅÅÅ-MM-DD_run-N.csv
skygge-v4-v5/skygge_ÅÅÅÅ-MM-DD.csv
skygge-v4-v5/skygge_ÅÅÅÅ-MM-DD_run-N.csv
```

Datoen er lokal downloaddato. Dagens fil kan erstattes af et senere download;
kørselskopier bevarer tidligere kørsler. Skyggen gemmes med beregningens input
og samme udvalgte datoer/status som prislisten. Gamle kørsler uden skygge kræver
matchende genberegning eller en ny godkendt beregning, før eksport kan fortsætte.
En beregning alene skriver ikke disse CSV’er.

CLI til særskilt inspektion af nuværende data:

```sh
.venv/bin/python -m app.skyggedrift --alle
.venv/bin/python -m app.skyggedrift --csv skygge.csv
```

CLI-sammenligningen er en ny beregning på de nuværende data, ikke nødvendigvis
den samme historiske kørsel som et tidligere download.

## Resultatlog og PDF-import

`ValidationRecord` er en historik med opholdsdato, observationstid, registreringstid,
bruger, kilde, eventuel kørselsreference og datagrundlag. Rettelser tilføjes som
nye registreringer. Identiske observationer får ikke dubletter.

| Type | Funktion |
|---|---|
| `inventory` | Historik fra indlæst belægning |
| `pdf` | Arrivals-observation med originalfil og reservationsbeløb som skøn |
| `applied` | Oplyst faktisk anvendt pris, normalpris, tidspunkt og valgt prisforslag |
| `report_pair` | Hotelmanager + produktomsætning som kladde til kontrol |
| `outcome` | Faktiske resultater; endelige når afsluttet og bekræftet |

Alle anvender samme database. Med ét delt login identificerer brugerfeltet
kontoen; det dokumenterer ikke, hvilken fysisk person der brugte den.

### Arrivals

Importen tæller Confirmed, Guaranteed og In-House pr. opholdsnat. Udskriftstidspunkt
og periode bevares. In-House læses i forhold til udskriftsdatoen.
En rapport fra allerede afsluttede datoer viser kun stadig aktive reservationer;
ufuldstændig historik bliver ikke til komplette endelige salgstal.
Total Price fordeles jævnt over nætterne som et reservationsskøn.

### Afsluttede resultater

Den daglige import bruger to PDF’er for samme dato, begge udskrevet efter
opholdsdatoen: Hotelmanager part 1 + part 2 (Room/Bed), og produktomsætning.
Accounts er valgfri til yderligere afstemning. Se [rapportguiden](HVORDANMANFINDERBELAEGNIONG.MD)
for filtre og afkrydsninger.

1. Hotelmanager leverer forslag til antal fra Today / Room Inhouse / Rooms og Beds.
2. Produktlinjer samles under deres efterfølgende rumtypetotal. Koder matches
   mod konfigurationens rum- og sengetyper; YP bruges ikke som enhed.
3. Særskilte morgenmadslinjer uden opholdsantal foreslås fratrukket.
4. Trykte rumtypetotaler bruges i opdelingen. Højst 2 øres forskel mod linjer
   og hovedtotal kan accepteres; ufordelt afrunding lægges kun til samlet omsætning.
5. Logitotal og gennemsnitspris pr. værelse/seng afstemmes mod Hotelmanagers
   hele-kroner-tal med højst 50 øres forskel.
6. Ukendte typer/beløb og større afvigelser spærrer økonomibekræftelsen.

Upload alene afslutter ikke datoen. Endelige antal skal bekræftes, også ved nul;
en tom Hotelmanager-celle er ikke nul. Økonomi kræver særskilt kontrol af
opholdsdato, fuldstændighed, moms og morgenmad. `Arrival date` i fodteksten må
ikke tolkes som automatisk bevis for korrekt periodisering af flerdagesophold.
Flyttede produkter uden reservation og kreditposter kræver særskilt kontrol.

Originalerne gemmes som SHA-256-navngivne PDF’er i `data/validation_pdfs/` og
kan åbnes via beskyttet kildevisning. Ældre rapporter kan ikke stiltiende
erstatte nyere registrerede resultater. Tidligere supplerende oplysninger kan
videreføres med reference til den oprindelige registrering.

Manuel registrering og CSV-skabeloner kan bruges til priser/resultater.
Nye bookinger og annulleringer kræver periode fra/til; overlappende perioder
summeres ikke automatisk. Godkendt CSV er ikke dokumentation for prisændring i Picasso.

## Prognosevalidering

Den første vellykkede beregning pr. opholdsdato, produkt og præcis **1, 7, 14
eller 30 dage før** fryses i `ForecastSnapshot`. Den indeholder modelaftryk,
niveau, kapacitet, punktprognose, reference og 50/80/90 %-intervaller.
Mislykkede kørsler bruger ikke et målepunkt. Gentagne kørsler overskriver ikke
målingen, og manglende målinger rekonstrueres ikke efterfølgende.

Seneste eksplicit afsluttede resultat leverer facit; begge endelige antal
kræves, men omsætning kan tilføjes senere. En senere foreløbig registrering
fjerner ikke et afsluttet facit fra prognosevalideringen.

Grafen `/resultater/afvigelser` viser prognose minus faktisk tal. Den har nulakse,
faktiske datoafstande, tabel og CSV. Huller er ikke nul. Uden afsluttede
driftsmålinger starter siden på den tydeligt mærkede historiske test.

### Mål og fortolkning

| Mål | Fortolkning |
|---|---|
| MAE | Gennemsnitlig absolut fejl; lavere er bedre |
| RMSE | Vægter store fejl mere |
| Skævhed | Gennemsnitlig prognose minus facit; positiv er for højt |
| Referencefordel | Reference-MAE minus model-MAE på præcis samme datoer |
| Intervaldækning | Andel facit inden for fx 80 %-prognoseintervallet |

Referencen er historisk gennemsnit for samme ugedag og måned, kun baseret på
data tilgængelige før prognosen. Manglende reference forbliver ukendt.
Grupper uden oplyst gruppeantal forbliver ukendte. Travle datoer er mindst 90 %
af kapaciteten; facitbaserede delgrupper er efterfølgende fejlanalyse.
Prognoserne gælder referenceprisen, før virkningen af de valgte prisændringer.

P-værdierne tester tosidet nul skævhed og nul forskel i absolut fejl mod
referencen. De er tilnærmede normaltest med Newey–West/HAC og Bartlett-vægte,
lag `max(7, horisont−1)` kalenderdage. Faktiske datoafstande anvendes.
Mindst 60 datoer og 8 kalenderblokke kræves; ingen p-værdi ved nul estimeret variation.

Holm-korrektion reserverer 24 test: 3 målinger × 4 horisonter × 2 spørgsmål,
inden for samme datakilde og periode. 95 %-intervallerne er punktvise, ikke
korrigeret som en familie. Gentagne kig og skiftende perioder er ikke en
sekventiel beslutningsregel. Afhængighed ud over de valgte lag kan stadig påvirke
usikkerheden. Små p-værdier må ikke erstatte praktisk effektstørrelse og dækning.

### Historisk test

Den aktuelle rapport træner på 2024 og tester på 08-03–31-12-2025, 299 opholdsdatoer.
Renoveringen udelades, niveauerne holdes på 1,0, og produktionsmodellens læring
fra 2026 føres ikke tilbage i testen. Det er en salgstest, ikke en indtjeningstest.

Rapport: `data/forecast_validation_history.json`. Grafobservationer:
`data/forecast_validation_observations.json`, med matchende modelhash.
Daterede kopier og CSV ligger i `outputs/prognosevalidering-*`.

Ældre evalueringstal havde andre perioder, horisonter og intervaldefinitioner;
de er ikke samlet til ét kalibreringsresultat her. Den aktuelle rapport skal
læses pr. produkt og horisont. Sengeintervallerne er fortsat væsentligt for snævre
på flere horisonter; fordel over en simpel reference ophæver ikke dette fund.

<a id="omsaetning-og-oekonomisk-effekt"></a>
## Omsætning og økonomisk effekt

Forventet RevPAB er modelberegnet samlet logiomsætning divideret med
`inventory.total_beds`. Allerede solgte enheder bruger indlæste beløb, hvis de
findes; ellers skønnes værdien fra priser. Fremtidigt salg værdisættes med de
foreslåede priser og et vægtet miks af resterende rumtyper. Tallet er en prognose.

Nye checkpoints gemmer også omsætningsskøn og moms-/pakkegrundlag.
`validation.price_vat_basis: incl_25` og `accommodation_only_confirmed: true`
afspejler bekræftelsen af, at modelpriser er inklusive 25 % moms og uden morgenmad.
Sammenlignelige skøn divideres med 1,25 til logi eksklusive moms.

Denne omregning gælder **valideringen**, ikke automatisk motorens RevPAB-visning.
Ældre checkpoints får ikke genskabt manglende økonomiprognoser. Checkpoints med
allerede indlæste omsætningsbeløb spærrer foreløbig omregningen, fordi disse beløbs
momsgrundlag ikke er registreret separat. Økonomisk reference og prognoseinterval
for omsætning er endnu ikke implementeret.

Faktiske økonomital kræver afsluttet og bekræftet logi eksklusive moms og morgenmad:

```text
logi i alt     = værelsesomsætning + sengeomsætning + ufordelt afrunding
dækningsbidrag  = logi i alt − faktisk provision − faktiske variable omkostninger
faktisk RevPAB = logi i alt / faktisk tilgængelige senge
```

Ukendte omkostninger erstattes ikke med standardsatser. Dækningsbidrag er heller
ikke årsoverskud efter husleje, faste lønninger, software og øvrige faste udgifter.

For at måle merindtjening skal en forsøgsplan fastlægge den normale prisstrategi
som reference, tildeling til sammenlignelige perioder, faktisk priseksponering,
bookingvinduer, annulleringer, grupper, events, ophold over flere datoer og
økonomisk resultat. Praktisk relevant forbedring og usikkerhed vurderes sammen.
Der er endnu ikke grundlag for at love merindtjening eller en bestemt dato for den.

## Modelgrundlag og analyser

Kommandoerne nedenfor køres fra projektmappen. Genopbygning af kube/model kan
overskrive analysegrundlag; gennemgå historik, perioder og kapacitet før brug.
Dokumentationsoprydning eller import af dagsresultater genoptræner ikke modellen.

```sh
.venv/bin/python -m app.cube
.venv/bin/python -m app.demand
.venv/bin/python -m app.forecast_validation
.venv/bin/python -m app.evaluate
.venv/bin/python -m app.backtest --ud-af-stikproeve
```

`app.cube` rekonstruerer historisk salg på bøgerne fra oprettelses- og
annulleringsdatoer. Historikken indeholder ikke fuld ændringshistorik;
flyttede reservationer kan derfor ikke rekonstrueres perfekt. Udsolgte datoer
skjuler afvist efterspørgsel. Historiske blokeringer og faktisk flexfordeling
er ikke fuldt kendte. Samme-dags-salg er slut-på-dagen i historiske analyser.

`app.evaluate` og `app.backtest` indeholder også simulationsanalyser af priser.
Resultater afhænger af antaget prisfølsomhed og må ikke kaldes faktisk
meromsætning. `app.calibration` er et separat værktøj til bookingkurver,
kalenderfaktorer og grundpriser; det er ikke et skift til en ældre runtime.
Det kræver eksplicit enhed, antal ved grupper, pris pr. enhed/nat og en komplet,
afsluttet periode med korrekt historisk kapacitet. Se `--help` før brug.

## Opstart

Projektet bruges aktuelt med Python 3.13. En lokal udviklingsinstallation:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Udfyld login i `.env`, før serveren startes på netværket. Miljøfilen indlæses
ikke automatisk af enhver direkte uvicorn-kommando:

```sh
set -a
source .env
set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Lokale eksempelfiler i `samples/` viser formater og kan indeholde gamle datoer;
brug aktuelle, kontrollerede data til drift. Ved opstart indlæses konfiguration
og modelgrundlag. Ændringer i YAML eller `.env` kræver genstart.

## Drift og backup

### Aktuel Mac og netværk

Projektmappe: `/Users/anderskinch/hostel`.
`scripts/run-prismotor.sh` skifter til denne mappe, indlæser `.env` og starter
uvicorn på **0.0.0.0:8000**. Appen kan derfor nås via serverens lokale IP,
men brug den faste navneadresse
[http://MacBook-Pro.local:8000/](http://MacBook-Pro.local:8000/) som bogmærke
på Mac’en og andre enheder på samme lokale netværk. Navnet følger Mac’ens
aktuelle IP-adresse via lokal navneopslag (Bonjour/mDNS), så et nyt IP-nummer
fra routeren ikke kræver et nyt bogmærke. Det lokale værtsnavn skal fortsat
være `MacBook-Pro`; det kan kontrolleres med `scutil --get LocalHostName`.
Andre enheders egen localhost peger ikke på denne Mac. Navneadressen giver
ikke adgang fra internettet.

LaunchAgent ligger under `~/Library/LaunchAgents/`, med label
`dk.danhostel.prismotor`. Genstart den allerede installerede tjeneste med:

```sh
launchctl kickstart -k gui/$(id -u)/dk.danhostel.prismotor
```

IP-adressen kan findes med `ipconfig getifaddr en0`, hvis Wi-Fi/netværket bruger
den grænseflade. Mac’en skal være tændt, vågen og på netværket.
Natkørsel kræver en kørende proces og nye indlæste data for at være nyttig.

### Login og delte ændringer

`RMS_USER` og `RMS_PASSWORD` ligger i `.env`, som er udeladt fra Git.
Der er ét konfigureret login, ikke individuelle roller. Uden `RMS_USER` kræves
intet login. HTTP Basic over lokal HTTP er ikke krypteret; ved adgang uden for
det betroede lokalnet bruges en særskilt løsning med HTTPS eller privat netværk.
Ingen sådan løsning oprettes af appen selv.

Alle browserbrugere skriver til samme SQLite-database på serveren.
Standard er `sqlite:///data/rms.db`; `RMS_DATABASE_URL` kan overstyre det.
Andre miljøvalg er `RMS_CONFIG` og `RMS_CSV_INBOX`.

### Backup og databaseversion

Brug SQLite-backupfunktionen til en kørende database, så en eventuel journal/WAL
håndteres korrekt. Eksempel fra projektmappen, til en ny dateret mappe:

```sh
.venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime
from pathlib import Path

folder = Path('outputs') / ('backup-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
folder.mkdir(parents=True, exist_ok=False)
with sqlite3.connect('data/rms.db') as source:
    with sqlite3.connect(folder / 'rms.db') as target:
        source.backup(target)
print(folder)
PY
```

Kopiér også konfiguration, modelgrundlag, niveaufil og arkiverede PDF’er som del
af en fuld backup; databasen alene indeholder ikke alt dette. Beskyt `.env`
særskilt. `outputs/` indeholder kun de backups, der faktisk er oprettet; mappen
er ikke i sig selv en automatisk backupordning.

Nuværende skemaversion er 3. Version 2 får additive felter og nye tabeller ved
opstart. Version 1 med ét lager afvises og kræver særskilt håndtering; slet
ikke den gamle base som standardløsning. Tag backup før databaseopgraderinger.

## Sider og API

Med undtagelse af `/health` er nedenstående beskyttet af det konfigurerede login.
`GET` på priseksport har den tilsigtede sideeffekt at gemme de downloadede filer.

| Metode og sti | Funktion |
|---|---|
| `GET /` | Prisforslag og prognoser |
| `GET /data` | Indlæsning af belægning og konkurrentpriser |
| `POST /data/upload` | CSV eller understøttet Arrivals-PDF |
| `GET /grupper` | Gruppetilbud til manuel vurdering |
| `GET /events` | Eventkalender |
| `GET /settings` | Parametre, comp set, log og seneste kørsler |
| `POST /run` | Beregn priser |
| `POST /decide` | Godkend eller afvis forslag |
| `POST /lock` | Lås eller frigiv en dato |
| `GET /export.csv` | Prisdownload og lokale kopier |
| `GET /api/prices?days=90` | Priser, prognoser, faktorer og allokering |
| `GET /api/group-quote?rooms=12&start=2026-10-18&nights=3` | Gruppeberegning |
| `GET /resultater` | Resultatlog og import |
| `POST /resultater/registrer` | Manuel pris-/resultatregistrering |
| `POST /resultater/import` | Supplerende CSV-import |
| `POST /resultater/rapporter` | Hotelmanager + produktomsætning til kontrol |
| `GET /resultater/rapporter/{id}` | Afstemning og kontrolside |
| `POST /resultater/rapporter/{id}/bekraeft` | Bekræft dagsresultat |
| `GET /resultater/pdf/{hash}` | Arkiveret original-PDF |
| `GET /resultater/export.csv` | Samlet resultatlog |
| `GET /resultater/historik.csv` | Hele observationshistorikken |
| `GET /resultater/skabelon.csv?kind=outcome` | CSV-skabelon; også `kind=applied` |
| `GET /resultater/prognoser` | Prognosevalidering |
| `GET /resultater/prognoser.csv` | Driftsmålinger |
| `GET /resultater/afvigelser` | Graf og statistik |
| `GET /resultater/afvigelser.csv` | Tal bag den valgte graf |
| `GET /resultater/oekonomi` | Økonomi og datadækning |
| `GET /health` | Driftstilstand og modelstatus uden login |

`/health` viser blandt andet `pricing_version`, `v4_requested`, `v4_notes`,
`exploration`, `elasticity_measured` og et niveau indlæst ved opstart.
Friskhedsstatus i dette endpoint bruger aktuelt en fast 36-timersgrænse;
ved ændring af konfigurationens `stale_hours` skal denne forskel håndteres.
Endpointet er ikke dokumentation for økonomisk effekt eller fuldstændige data.

<a id="kendte-begraensninger"></a>
## Kendte begrænsninger

- **Ingen målt merindtjening eller målt priselasticitet.** Planlagt sammenligning
  mod normal strategi, anvendte priser, salgsperioder og faktiske omkostninger mangler.
- **Sengeintervaller kræver forbedring.** Den aktuelle historiske test viser
  dårlig dækning på flere horisonter, selv når punktprognosen slår referencen.
- **Flex er en tilnærmelse.** Rumstørrelser, spredte sengebookinger og hele
  ophold kan ikke afgøres sikkert ud fra daglige totaler.
- **Momsgrundlag er ikke ens overalt.** Nye sammenlignelige økonomiprognoser
  omregnes; allerede bogførte beløbs grundlag kræver særskilt afklaring.
- **Rapportformat og datoafgrænsning skal kontrolleres.** Andre PDF-layouts,
  flyttede produkter, kredit og flerdagesophold kræver fortsat faglig kontrol.
- **Ingen automatisk Picasso-/Lighthouse-integration.** CSV/PDF er det aktuelle grundlag.
- **Ingen automatiseret minimumsopholdspolitik, segmentprognose eller kontrolleret
  overbooking.** Historisk netto-pickup rummer annulleringer, men er ikke en
  bookingvis annulleringsmodel.
- **Konfigurationskommentarer er ikke funktionalitet.** Se forbeholdet om
  `unbounded_daily_change`, ældre triggere og den aktuelle 15 %-bremse.

## Kodekort

| Fil | Ansvar |
|---|---|
| `app/engine.py` | Prisberegning, typepriser, kapacitet, RevPAB og gruppeberegning |
| `app/bidprice.py` | Valg ud fra efterspørgselsfordeling, marginalværdi og opholdsværdi |
| `app/ladder.py` | Pristrin, udglatning, markeds-/eventtrin og prisvariation |
| `app/demand.py`, `app/cube.py` | Efterspørgselsmodel og rekonstruktion af historik |
| `app/level.py` | Adaptivt niveau og fil-lås |
| `app/service.py` | Indlæsning, beregning, friskhed, prisanker og lagring |
| `app/exports.py`, `app/skyggedrift.py` | Prisdownload og v4/v5-sammenligning |
| `app/picasso_belaegning.py` | Arrivals-PDF til belægning |
| `app/picasso_results.py` | Rapportpar, afstemning og bekræftelse af dagsresultater |
| `app/validation.py` | Resultatlog, historik, CSV og faktiske bidrag |
| `app/forecast_validation.py` | Frosne prognoser og historisk validering |
| `app/forecast_charts.py`, `app/forecast_metrics.py` | Grafgrundlag og statistik |
| `app/economics.py`, `app/result_routes.py` | Økonomi og Resultater-sider |
| `app/main.py`, `app/templates/` | Webapp, planlægning og skærmbilleder |
| `app/static/academic.css`, `academic.js` | Artikellayout med ET Book, varm papirbaggrund, mørkerøde accenter og LaTeX-visning |
| `app/static/fonts/`, `app/static/vendor/katex/` | Lokale skrifter og KaTeX med tilhørende licenser |
| `app/db.py` | Database, log og additive opgraderinger |
| `app/sanity.py`, `app/adapters/` | Importkontroller og dataadgang |
| `app/evaluate.py`, `app/backtest.py`, `app/calibration.py` | Særskilte analyser |

Kør relevante test efter kodeændringer med `.venv/bin/python -m pytest -q`.
Dokumentationsændringer kontrolleres for korrekte links, kommandoer og overensstemmelse
med implementationen; de kræver ikke genoptræning eller en ny prisberegning.

## Præregistrerede A/B-forsøg

`app/experiments.py` og `app/experiment_routes.py` implementerer
`/resultater/ab`, adskilt fra prisstigens lokale prisvariation.
Nye additive tabeller `ab_experiment` og `ab_log` oprettes ved normal opstart.
Ingen eksisterende priser eller valideringsposter omskrives.

Hele huset randomiseres pr. opholdsdato-blok i par af naboblokke på 7/14/28
dage. Én A og én B pr. par; PCG64-seed og kalender gemmes før eksponering.
A er normalstrategien; B er den fastlåste model. Præregistrering kræver
`pricing.v4.level_alpha: 0` og genindlæsning af konfigurationen. Driften ændres
ikke automatisk. Det aktuelle niveaus tal indgår i protokolhashen; fælles online
læring ville skabe afhængighed mellem blokke og er ikke tilladt i dette design.
Protokollen gemmer hash af modelparametre og centrale kodefiler. Der ændres ikke
på prismotorens valg eller Picasso; levering er manuel og skal dokumenteres.

Primærmålet er faktisk dækningsbidrag pr. fast fysisk seng pr. nat, med
præregistreret kapacitet, eksklusive moms og morgenmad. Hele perioden kræves:
seneste registrerede outcome pr. dato, `final`, `revenue_date_confirmed`, begge
omsætningstal, begge salgstal samt provision og variable omkostninger. Nul er
oplyst; manglende data imputeres ikke. Blokgennemgange er append-only, og alle
afvigelser indgår i ITT. Der er ingen efterfølgende datoeksklusion.

Sluttest: tosidet sign-flip-randomisering under den skarpe nulhypotese,
eksakt til og med 16 par, ellers 99.999 PCG64-træk med plus-én-korrektion.
Et separat omtrentligt parret t-interval beregnes med SciPy. Det er ikke et
randomiseringsbaseret interval; antagelser om par-uafhængighed og begrænset
afsmitning er nødvendige. Ved nulspredning tilbageholdes intervallet.
Normaltilnærmet styrkeplanlægning bruger parspredning, α=0,05 og styrke=0,80;
den garanterer ikke randomiseringstestens styrke.

Kun fulde data, alle blokgennemgange, uændret aktuelt modelgrundlag og eksplicit
attest af designantagelser åbner slutanalyse. En betinget databaseopdatering
forhindrer dobbelt afslutning. Den låste JSON gemmer rå kildepayloads,
post-ID'er og kontroller; senere rettelser ændrer ikke konklusionen. Stop er
terminalt uden p-værdi. JSON- og kalender-eksport kræver samme login som appen.
Metode og to figurer er skrevet i `app/static/experiments/*.tex`; SVG-figurerne
er faktisk kompileret fra TikZ. PDF'en og SVG'erne leveres lokalt uden en
runtime-afhængighed af TeX. Byg fra mappen `app/static/experiments` med
`pdflatex` (to gennemløb) og `pdftocairo -svg` til standalone-figurerne.

### Bogføringsafstemning af dagsrapporter

`app/picasso_accounts.py` læser Accounts pr. enkelt dato med produktgrupper,
Amount, Debit og Credit. Rækker, produkttotaler og Result afstemmes med Decimal;
uoverensstemmelser over 2 øre afvises. Omsætningsfortegn er kredit minus debet.
Konto 1002 (Værelser) er logi på tværs af rum- og sengetyper; 1004 er
opgraderinger; 1005 er morgenmad. Andre konti vises separat. Produktnavne er
ikke rumtyper. Result omfatter andre konti og er ikke logi eller dækningsbidrag.

Hotelmanager kan nu indlæses alene til salgstal. Produktomsætning og Accounts
er valgfrie supplerende filer i samme upload. Alle vedlagte rapporter skal dække
samme afsluttede dato. Originaler arkiveres med hash i eksisterende PDF-arkiv;
`report_pair` bevares som posttype af hensyn til historikken. Parser version 2
ændrer ikke gamle kildepayloads.

Accounts sammenholdes med produktudtrækket pr. produktnavn. Ekstra morgenmad
på produktniveau udløser en mangel, når den ikke kan placeres på rumtypernes
fradragslinjer. Ukendte linjer uden opholdsantal bevares som uafklarede;
ingen forholdsmæssig fordeling. “None”-posteringer gemmes særskilt med
originalt beløb og omsætningsfortegn. De indregnes hverken som en ekstra
`revenue_adjustment` eller som omsætning på en gættet rumtype. En ufordelt
160-kroners post er en ufordelt post, ikke afrunding.

Ved bekræftelse af salgstal kan resultatloggen gemme `accounts_report_id`,
`accounts_unallocated` og `accounts_reconciliation`, også når omsætning fortsat
er spærret. Resultat-CSV har separate JSON-kolonner til afstemningen og de
ufordelte poster. `room_revenue`, `bed_revenue` og rumtypebeløb kræver stadig
afklaret total, ADR, kategorisering og udtrykkeligt bekræftet opholdsdato og
moms-/morgenmadsgrundlag. Bogføringsdato lig opholdsdato beviser ikke i sig selv
at rapporterne er sammenlignelige.
