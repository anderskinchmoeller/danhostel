# Prismotor — reference

Detaljerne. Den korte vejledning står i `README.md`; her står alt det man
kun har brug for når man skal ændre noget eller forstå hvorfor en pris ser
ud som den gør.

---

**Version 3 — prisforslag med manuel vurdering.** Tre ting adskiller den fra en almindelig hotelmodel:

1. **Den forudsiger i stedet for at reagere.** En dato der er 20 % solgt 40 dage
   ude er ikke svag — den er på vej til 85 %, og prisen skal ikke falde.
2. **Den prissætter to lagre.** Private værelser og dormsenge har hver sin
   bookingkurve, hver sin prognose og hver sin pris. Senge bookes senere og er
   mindre weekendfølsomme.
3. **Den træffer beslutningen om flex-rummene.** Et rum der kan sælges som
   familieværelse eller som fire senge er ikke et prisspørgsmål — modellen
   sammenligner dækningsbidraget ved alle mulige fordelinger. Flex er kun et dagsforslag; konkrete rum og hele ophold skal kontrolleres i Picasso.

Nøgletallet er **RevPAB**: omsætning pr. tilgængelig seng i hele huset. Et
hostel der optimerer værelsesprisen alene optimerer den forkerte størrelse.

```
værelsespris = grundpris(sæson × ugedag) × F_pace × F_marked × F_event
sengepris    = grundpris_seng(sæson × ugedag_seng) × F_pace_seng × F_marked_seng × F_event
```

---

## Lageret — det vigtigste at få rigtigt

```yaml
inventory:
  confirmed: false
  private_beds: null       # udfyld fra Picasso
  private_room_types: {}   # antal faste private værelser pr. priskode
  private_rooms: 20        # altid private (typisk dem med eget bad)
  flex_rooms: 16           # kan sælges som familieværelse ELLER som senge
  beds_per_flex_room: 4
  dorm_beds: 216           # faste sovesale
```

Det giver 36 mulige private værelser, 280 mulige senge og 320 senge i alt.
Tallene er kun et eksempel. Danhostel oplyser 72 værelser, heraf 20 med bad/toilet,
og **330 senge**, mens eksemplet giver **320**. De manglende 10 senge må ikke blot
lægges i en vilkårlig kategori. `private_beds` erstatter antagelsen om to senge
i hvert fast privat værelse. `private_room_types` skal summere til `private_rooms`. **Ret dem til den faktiske opdeling i Picasso, før
du bruger priserne til noget.** Er lageret forkert, er alt andet også forkert.

Sæt først `confirmed: true`, når opdelingen er afstemt med Picasso. Indtil da
kan forslag vurderes i dashboardet, men priser kan hverken eksporteres til live
import eller skrives til PMS. Dette kræver en konfigurationsændring og genstart.

Flex-beregningen respekterer solgte enheder, blokeringer og valgfrie
`flex_private_min`/`flex_private_max`. De sidste skal baseres på faktiske rum og
hele ophold: én solgt seng kan binde et helt rum. Daglige totaler kan ikke afsløre
spredte bookinger eller garantere et bestemt rum gennem flere nætter.
**Ingen gæster flyttes, og intet lager ændres automatisk.** CSV-kolonnen
`flex_forslag_til_private` er kun til manuel vurdering.

---

## Sikkerhed før du sætter den på nettet

Servicen kan ændre priser på et rigtigt hostel. Tre ting, ingen af dem valgfri:

1. **Sæt `RMS_USER` og `RMS_PASSWORD` i `.env`.** Uden dem er dashboardet åbent.
2. **Bind til `127.0.0.1` og læg Caddy eller nginx foran med HTTPS.** Så kan
   porten ikke nås direkte udefra. Skal kun du læse siden, er Tailscale
   simplere end begge dele — så er der ingen åben port overhovedet.
3. **Læg aldrig `.env` i versionsstyring.**

```nginx
server {
    server_name priser.dithotel.dk;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

---

## Dagligdagen

| Hvornår | Hvad |
|---|---|
| Hver nat kl. 04:15 | Servicen kører selv |
| Hver morgen, 5 min. | Kig på **Priser**. Start med datoer der har en bemærkning |
| Ved gruppeforespørgsler | **Grupper** → antal værelser og datoer → minimumspris |
| Ved tvivl | Lås datoen med dine egne priser. Automatikken rører den ikke |
| En gang om måneden, 30 min. | Opdatér **Events** fra VisitAarhus' kalender |
| En gang i kvartalet | Opdatér review-scores under **Indstillinger** |

---

## Data ind

To filer. **Begge lagre skal med** — leverer eksporten kun værelser, er den
halve model blind.

**Belægning**

```csv
dato;solgte_vaerelser;solgte_senge;blokerede_vaerelser;blokerede_senge;vaerelsespris;sengepris
2026-09-16;24;148;4;0;690;280
```

Begge solgte antal skal være med og må ikke være tomme. Brug eksplicit nul,
og kun én række pr. dato. Negative antal, brøker og ugyldige tal afvises.
Alt efter de tre første kolonner er valgfrit. Blokerede enheder trækkes fra
kapaciteten. Allerede solgte gruppeenheder skal tælles som solgt, ikke samtidig
som blokeret. Umulige kombinationer fastfryser datoen frem for at skjule overbookingen.

Supplerende kolonner:

- `booked_room_revenue`, `booked_bed_revenue`: bogført overnatningsomsætning
  for opholdsdatoen i DKK. Samme afgiftsgrundlag som priserne; uden morgenmad/tillæg.
- `room_type_otb`: JSON-objekt med solgte private rum pr. priskode, fx
  `{"dobbelt_med_bad": 5, "familie_4": 2}`. Summen skal matche `solgte_vaerelser`.
- `flex_private_min`, `flex_private_max`: grænser fra konkrete værelsestildelinger.

RevPAB bruger bogført omsætning på allerede solgte enheder og foreslåede priser
kun på fremtidigt salg. Fremtidige private salg vægtes efter resterende rumtyper.
Ved manglende typemiks, omsætning eller specificerede blokeringstyper markeres
resultatet **skøn**. Selv med alle data er fremtidigt salg en prognose, ikke
realiseret omsætning eller dokumenteret gevinst.

**Konkurrentpriser**

```csv
dato;medianpris_vaerelse;medianpris_seng
2026-09-16;640;285
```

To comp sets: private værelser mod budgethoteller, senge mod andre hostels.
Udelad lukkede rater — en pris du ikke kan booke er ikke en lav pris.

Kolonnenavne genkendes på dansk og engelsk, med og uden æøå. Separator må være
semikolon eller komma, og datoformat `2026-09-16`, `16-09-2026` eller `16/09/2026`.

Mangler en priskolonne, sættes den tilhørende markedsfaktor til 1,000 og
modellen prissætter på prognosen alene. For et hostel er det mindre alvorligt
end for et hotel: rate shopping vejer kun 0,45 på værelser og 0,30 på senge
her, fordi produkterne ikke er sammenlignelige på samme måde.

---

## Data ud af Picasso

Der findes ingen offentlig dokumentation af Picassos eksportfunktioner. Det
nedenstående er læst ud af brugermanualen til **version 8.3** (2022,
techotel.se). I kører formentlig Picasso Digital, så menunavnene kan være
andre — men rapporterne plejer at overleve versionsskift.

**Rapportmodulet** åbnes med **F7** eller rapportikonet i Selector. Manualen
beskriver det som modulet "du bruger til at udtrække diverse rapporter,
arrangementslister, ankomstlister, navnelister og rapport til Danmarks
statistik".

Perioden vælges i kalenderen i højre side: klik datoerne for at markere et
interval, brug `◄►` til at skifte måned, eller brug datointerval-ikonet og
udfyld **Fra** og **Til**. Bekræft med **OK**. *DSL-knapperne* gemmer en
rapportopsætning, så den ikke skal stilles op forfra hver gang.

De rapporter der kommer tættest på det motoren skal bruge:

| Rapport | Hvad den viser |
|---|---|
| **Belægning** | Belægningsoversigt for indeværende år |
| **Rumtype statistik** | Solgte rum pr. værelsestype — svarer til `room_type_otb` |
| **Belæg %** | "Time stat lokaler", belægningsstatistik pr. time |
| **Hotelmanager** | Overordnet hotelstatistik |
| **Leads** | Ugeoversigt med RevPAR, gennemsnitspriser og belægningsprocenter |

### Tre ting manualen ikke nævner

Og det er præcis de tre motoren har brug for.

**Eksport til fil.** Rapportafsnittet handler om udskrivning. Hverken Excel,
CSV eller "gem som" optræder. Der står "Digital rapporter" ét sted, uden
forklaring. Om der findes en eksportknap i jeres version kan kun afgøres ved
at åbne modulet.

**Senge.** Ingen rapport i manualen opgør sovesale eller senge. Alt er pr.
værelse — manualen er skrevet til et hotel. En eksport uden sengetal bliver
afvist af rimelighedstjekket ved import, og det er den rigtige opførsel: uden
sengetal er den halve model blind, og det er billigere at stoppe end at
prissætte videre.

**Fremad i tid.** Rapporterne beskrives som statistik over en valgt, afsluttet
periode. Motoren skal bruge on-the-books **120 dage frem**.

### Hvad det betyder i praksis

Rapportmodulet kan sandsynligvis give et øjebliksbillede i hånden, men ikke
den daglige fil. Den skal komme fra en planlagt eksport, som AK Techotel
sætter op. Spørg dem konkret om: on-the-books pr. ankomstdato, pr.
værelsestype **og pr. seng**, 120 dage frem, som CSV eller Excel, lagt i en
mappe eller sendt på mail hver nat — plus de aktuelle værelses- og sengepriser
pr. dato, så ændringslisten kan sammenligne med det der står i systemet nu.

Kontakt: info@techotel.dk, +45 36 19 21 13.

*Kilde: Picasso brugermanual version 8.3, techotel.se. Læst 15-09-2026.*

---

## Kalibrering — gør det før du stoler på tallene

Sæson-, ugedags- og bookingkurvetallene i `config/config.yaml` er
**brancheskøn**. De er bedre end ingenting og dårligere end dine egne tal.

```bash
python -m app.calibration reservationer.csv --rooms 36 --beds 280 \
  --start 2024-01-01 --end 2025-12-31
```

Filen skal have `bookingdato`, `ankomstdato` og `enhed` (`room` eller `bed`).
Valgfrie felter: `naetter` (standard 1), `quantity` (standard 1), `pris`,
`status`, `cancelled_at`. Pris betyder **pr. enhed pr. nat**, aldrig gruppens
samlede pris. Gruppebookinger skal have antal solgte rum/senge i `quantity`.
Brug eksplicit enhed — et navn som "4 bed room" er tvetydigt og afvises.

Annulleringer og no-shows kræver dato, så historiske bookingtal kan rekonstrueres.
En annulleret booking tæller kun før sin annulleringsdato. Ugyldige rækker giver
linjenummer og fejl; de springes ikke tavst over. Der forventes én række pr.
reservation/enhedstype, ikke gentagne ændringshistorik-rækker.

`--start` og `--end` angiver en fuldstændig, afsluttet historisk driftsperiode;
datoer uden salg medtages som nul. Eksporten skal dække hele perioden. Kapaciteten
skal være konstant: adskil perioder med kapacitetsændringer og lukninger.
Variable flex-lagre kræver sammenlignelige historiske kapacitetsdefinitioner.

Ud kommer bookingkurver, sæson/ugedagsprofil og forslag til grundpriser.
Kontrollér resultatet mod en separat historisk periode og sammenlign prognosefejl
med en simpel historisk reference, før parametrene tages i brug. Koden indeholder
ingen dokumentation for priselasticitet eller meromsætning på jeres faktiske data.

---

## Gruppetilbud

Når en skoleklasse beder om 20 værelser, er svaret ikke en mavefornemmelse.
Prognosen ved, hvor mange værelser der alligevel ville være blevet solgt til
almindelig pris — de fortrængte værelser er gruppens reelle omkostning.

**Grupper** → antal værelser, første dato, antal nætter → minimumspris pr.
værelse pr. nat. Ligger den tæt på normalprisen, er datoen ved at være fuld og
en rabat koster rigtige penge.

To forbehold: tallet regner kun på værelser, ikke på de dormsenge et flex-rum
kunne have givet i stedet, og gruppen bringer ofte morgenmad og forbrug med sig.
Brug det som gulv, ikke som tilbud.

---

## Adaptere

Servicen kender ikke Picasso. Den kender en grænseflade i
`app/adapters/base.py`, og bag den kan der sidde en CSV-fil i dag og et REST-API
i morgen, uden at prislogikken ændrer sig en linje.

| Adapter | Bruges til |
|---|---|
| `csv` | Belægning: upload i dashboardet, eller nyeste fil i mappen `RMS_CSV_INBOX` |
| `manual` | Konkurrentpriser uploadet i hånden |

Det er de eneste to. Et andet navn i `config.yaml` giver en fejl ved opstart i
stedet for stille at falde tilbage på CSV.

Der lå tidligere to skrevne, men uafprøvede API-adaptere til Picasso og
Lighthouse. De er fjernet: feltnavne og endpoints var gæt, og kode der ikke har
været kørt mod en rigtig server er ikke et forspring — den ser bare ud som om
arbejdet er gjort. Kommer der API-adgang, skrives adapteren mod den
dokumentation der så findes.

Den viden der var værd at beholde, er de tre spørgsmål AK Techotel skal svare
på, før nogen skriver den kode:

1. Findes der et dokumenteret API en tredjepart kan læse kapacitet og
   on-the-books fra — **pr. værelsestype og pr. seng** — og hvad koster adgangen?
2. Kan priser skrives via API, eller skal de sættes gennem en kanalstyring?
   (Relevant først hvis I en dag vil skrive tilbage. I dag skriver servicen
   ingenting.)
3. Er priser modelleret som én BAR med afledte typepriser, eller uafhængigt pr.
   værelsestype?

---

## Guardrails

- **Rimelighedstjek på import.** En uploadet belægningsfil afvises hvis den er
  plausibel men forkert: en kolonne der er nul over hele horisonten, flere
  solgte enheder end huset har, spring på over 40 procentpoint på ét døgn,
  priser uden for et forventeligt interval, eller datoer der er læst med måned
  og dag byttet om. Intet skrives i basen når importen stoppes. Et menneske kan
  trumfe tjekket igennem med afkrydsningen på **Data**; det står i auditloggen
  med navn. Tjekkene ligger i `app/sanity.py`.
- **Friskhed på skærmen.** Hver side viser hvor gammel seneste kørsel er, og
  skifter til rød advarsel efter `stale_hours`. En frossen side og en frisk side
  ser ellers ens ud.
- **Prisgulv og prisloft** for begge lagre, hver for sig.
- **Afrunding bryder aldrig et gulv.** 165 kr. rundet til nærmeste ti bliver 160
  — fem kroner under gulvet, hver eneste nat. Sådanne fejl er dyre, fordi de
  rammer systematisk og i samme retning.
- **Ændringsbremse** på 15 % i døgnet, på både basis-værelsespris og sengepris. Et fast 24-timers prisanker hindrer, at gentagne kørsler/importer flytter grænsen.
- **Dødmandsknap** — hver dato kontrolleres separat. Gamle/manglende data fastfryser datoen; friske datoer kan stadig beregnes. Eksport og skrivning kontrollerer igen og kræver ny beregning, hvis belægningen er ændret.
- **Eventloft** på 1,45, så en tastefejl ikke løber løbsk.
- **Produktsammenligning** — hvis dormsenge underbyder familieværelset, vises en bemærkning. Den hæver ikke sengeprisen: privatliv og dormsenge er forskellige produkter.
- **Manuel lås** på enkeltdatoer, med dine egne to priser. Afledte rumpriser og
  prognosen genberegnes ved næste kørsel med låsepriserne.
- **Modstridende prisgrænser** fastfryser datoen. Afrunding kan ikke bryde grænserne;
  hvis intervallet ikke indeholder et helt afrundingstrin, beholdes et beløb indenfor intervallet.
- Prisgulv, prisloft og ændringsbremse gælder basis-værelsesprisen og sengeprisen.
  Afledte rumtyper følger de konfigurerede faktorer.

---

## API

| Endpoint | Hvad |
|---|---|
| `GET /api/prices?days=90` | Priser, prognoser, flex-allokering og alle faktorer |
| `GET /api/group-quote?rooms=12&start=2026-10-16&nights=3` | Minimumspris for en gruppe |
| `GET /export.csv` | Godkendte priser til manuel import i Picasso |
| `GET /health` | Til overvågning. `degraded` hvis sidste kørsel er over 36 timer gammel |
| `POST /run` | Kør beregningen nu |

---

## Test

```bash
pytest -q
```

Testpakken kontrollerer blandt andet: at samme
belægning prissættes modsat alt efter lead time, at de to lagre prissættes
uafhængigt, og at flex-allokeringen faktisk flytter kapacitet mellem dem. Dertil
guardrails, gruppeforskydning og hele vejen fra CSV-upload til godkendt pris.

---

## Sådan ved du om det virker

Etablér nulpunkt før du starter, og mål efter seks måneder:

- **RevPAB** — det samlede tal. Værelsesprisen alene kan man pynte på ved at
  sælge færre værelser.
- **RevPAR-indeks mod comp set** (Benchmarking Alliance) — mål: +3 til 5 point.
- **Belægning** — må ikke falde mere end 2 procentpoint mens priserne stiger.
- **Review-score, værdi for pengene** — må ikke falde. Jeres laveste delscore på
  Hostelworld er netop den (7,1 mod 8,9 på personale og beliggenhed). Markedet
  siger allerede, at prisen opleves som høj i forhold til produktet, så hold øje
  med den mens modellen leder efter loftet.

---

## Databaseopgradering

Version 2-databaser opgraderes automatisk med nye, nullable felter. Eksisterende
priser, historik og godkendelser bevares. Tag sædvanlig backup før opgradering.
Version 1 kræver fortsat særskilt håndtering som beskrevet ovenfor.

## Hvad der endnu ikke er bygget

Fra roadmappen, i den rækkefølge det giver mening:

- **Elasticitetsmåling.** `k_forecast` og `k_market` er stadig kvalificerede gæt.
  40 sammenlignelige datoer, halvdelen 5 % op og halvdelen 5 % ned, og efter en
  sæson har I et rigtigt tal om jeres egne gæster.
- **Opholdslængde.** Minimum-ophold på toppe og lukket-for-ankomst på datoer der
  efterlader huller. Modellen advarer allerede når en dato forventes udsolgt.
- **Annulleringsrisiko.** On-the-books er ikke netto. Vægt hver booking efter
  kanal og ratetype, og overbook kontrolleret.
- **Segmentopdelt prognose.** Danske og udenlandske gæster booker med vidt
  forskelligt varsel.

---

## Filer

```
app/engine.py             prismodellen: prognose, to lagre, flex-allokering, RevPAB
app/service.py            kørslen: hent, beregn, gem, skriv tilbage
app/calibration.py        begge bookingkurver og sæson fra egen historik
app/adapters/             CSV ind; ingen skrivning tilbage til Picasso
app/main.py               FastAPI: dashboard, grupper, upload, godkendelse, API
app/sanity.py             rimelighedstjek på uploadet belægning
config/config.yaml        lager og alle parametre, ingen kode
samples/                  eksempeldata, inkl. reservationshistorik til kalibrering
tests/                    motor-, service- og regressionstests
```
