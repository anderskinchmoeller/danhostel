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
  flex_room_types: {}      # flex-rum pr. Picasso-kode, fx {V6: 7, V8: 6}
  beds_per_flex_room: 4
  dorm_beds: 216           # faste sovesale
```

Det giver 36 mulige private værelser, 280 mulige senge og 320 senge i alt.
Tallene er kun et eksempel. Danhostel oplyser 72 værelser, heraf 20 med bad/toilet,
og **330 senge**, mens eksemplet giver **320**. De manglende 10 senge må ikke blot
lægges i en vilkårlig kategori. `private_beds` erstatter antagelsen om to senge
i hvert fast privat værelse. `private_room_types` skal summere til `private_rooms`. **Ret dem til den faktiske opdeling i Picasso, før
du bruger priserne til noget.** Er lageret forkert, er alt andet også forkert.

`flex_room_types` skal summere til `flex_rooms`. Er den tom, regnes alle flex-rum
som én type, `familie_4`. Er den udfyldt, får hver sovesalstype sin egen pris,
og et flex-rum solgt privat regnes til gennemsnittet af typerne, vægtet med antal.

### Prislisten

Prislisten (`/export.csv` og `room_types` i `/api/prices`) har én kolonne pr. kode
i `pricing.room_types` og `pricing.bed_types`, i den rækkefølge de står i config.
Rumkoder = værelsesprisen × faktor; sengekoder (Picassos B-typer) = sengeprisen ×
faktor. Skal prislisten passe til Picasso, skal de to lister have præcis Picassos
rumtyper — hverken flere eller færre.

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
  `{"D2": 5, "V8": 2}`. Summen skal matche `solgte_vaerelser`.
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

Den daglige belægning kan tages i hånden fra **Arrivals**-rapporten (Rooms
spec.) som PDF og uploades direkte under **Data** (`app/picasso_belaegning.py`).
In-house-gæster læses i forhold til rapportens udskriftsdato i sidehovedet, så
en rapport kan uploades en dag senere uden at afrejser ruller et år frem. Den
giver solgte værelser og senge, men ikke blokeringer eller nuværende priser.
En fuldt automatisk fil kræver stadig en planlagt eksport. Den skal komme fra en planlagt eksport, som AK Techotel
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

## Prisstigen (version 3)

Slås til og fra under `pricing.ladder.enabled` i `config.yaml`. Slået fra kører
den kontinuerlige faktormodel fra version 2 uændret.

**Idéen.** I stedet for en ny pris hver dag står prisen på ét af ni faste trin
fra 0,80 til 1,48 gange dagens grundpris (sæson x ugedag). Trin 4 er 1,00 —
referencetrinnet. Prisen flytter sig kun, når triggerne samlet siger det
tydeligt nok. I en simulering over 45 dage skiftede faktormodellen pris 15-30
gange pr. dato; stigen 2-5 gange, og altid i den retning efterspørgslen pegede.

**Triggerne**, hver skaleret til [-1; 1]:

| Trigger | Vægt | Hvad den måler | Fuldt tryk ved |
|---|---|---|---|
| Prognose | 0,40 | Forventet slutbelægning mod målbelægning | 20 pp fra målet |
| Tempo | 0,25 | Bookinger siden snapshot for ca. en uge siden, mod hvad kurven forventede i samme vindue — som Poisson-z-score | 2,5 standardafvigelser |
| Marked | 0,20 | Konkurrentmedian x kvalitetsindeks mod vores referencetrin | 20 % over/under |
| Lead time | 0,15 | De sidste 3 dage forstærkes prognosesignalet: ledige senge er værdiløse i morgen | — |

```
tryk     = sum(vægt x trigger)                          i [-1; 1]
position = referencetrin + tryk / 0,15 + eventtrin      (0,15 tryk = ét trin)
position udglattes 50/50 med gårsdagens (ikke de sidste 3 dage)
```

**Regler oven på positionen**, i denne rækkefølge:

1. **Hysterese.** Stigen skifter først når positionen er 0,75 trin fra det
   nuværende, ikke 0,5. Ellers hopper prisen frem og tilbage om den samme grænse.
2. **Trinbegrænsning.** Højst to trin op og ét ned pr. kørsel (to ned de sidste
   tre dage). Op er billigt at fortryde; en for lav pris er solgt.
3. **Skralde.** Ingen prisfald på en dato hvor prognosen er på eller over målet.
4. **Knaphedsbeskyttelse.** Prognosen behandles som en normalfordeling hvis
   spredning vokser med lead time (10 % ved ankomst, 35 % 60+ dage ude).
   Risiko for udsolgt over 25 / 50 / 75 % lukker trinene under
   reference+1 / +2 / +3. En seng solgt billigt i dag kan ikke sælges dyrt i
   morgen — Littlewoods regel i forenklet form.
5. **Ingen dyb rabat langt ude.** Over 60 dage ude aldrig under
   referencetrin - 1. Uden signal er et prisfald et gæt.
6. **Event.** Hver 10 % eventtillæg er ét trin op, og en eventdato går aldrig
   under referencetrinnet.
7. **Ændringsbremsen** (15 %) gælder stadig, men bremser til det nærmeste trin
   inden for vinduet, så prisen bliver på stigen. En låst pris respekteres og
   markeres som uden for stigen.

**Hvad stigen husker.** Forrige trin og udglattet position læses fra seneste
kørsel; tempo-snapshottet fra en kørsel 5-10 dage gammel. De første dage efter
opstart er der intet tempo-signal, og dashboardet siger det.

**På dashboardet.** Kolonnen **Trin v/s** viser værelsestrin / sengetrin, med ▲▼
ved skift. Hold musen over for triggernes tryk, risiko for udsolgt og alle
trinpriser. Begrundelsen for hvert skift står under **Bemærkning**, og
`/api/prices` har det hele som JSON under `ladder`.

**Hvad den ikke ved.** Stigen er ikke klogere end prognosen under den.
Målbelægningen på 55 % ligger over hverdagshistorikken (39 %), så hverdage
starter typisk et til to trin under reference. Det er en beslutning om mål,
ikke en fejl i stigen — sænk `target_occupancy_rooms`, eller sæt forskellige
mål for hverdag og weekend, hvis det ikke er meningen. Trinenes afstand og
triggervægtene er kvalificerede gæt ligesom `k_forecast`; elasticitetsmålingen
i roadmappens punkt 3 er det der gør dem til jeres egne tal.

---

## Version 4 — bid price i stedet for målbelægning

Slås til med `pricing.v4.enabled` i `config.yaml`. Kræver prisstigen tændt og en
estimeret efterspørgselsfordeling. Mangler modelfilen, falder prissætningen
tilbage på version 3 og siger det i `/health`.

### Hvorfor

Version 3 styrer mod `target_occupancy_rooms: 0.55`. Det tal vejer 0,40 i
stigens samlede tryk og er et gæt: historikken for hverdage er 0,39, så hverdage
starter 1-2 trin under reference per konstruktion. Version 4 har ingen
målbelægning. For hvert trin beregnes

    forventet omsætning = nettopris x E[min(efterspørgsel, ledig kapacitet)]

over hele fordelingen af resterende efterspørgsel, og det bedste trin vinder.
Belægningen bliver et resultat, ikke et input.

Stigen bliver: træghed, trinbegrænsning, eventtrin, ændringsbremse og
forklaring er uændret. Knaphedsbeskyttelsen og ratchet'en slås derimod fra, når
version 4 er aktiv — begge forsøger at gætte det bid price regner ud, og de
ville lægge et ekstra gulv oven på et tal der allerede indeholder knapheden.

### Sådan bygges grundlaget

```
python -m app.cube        # kali/cube.csv  — OTB(dato, lead) for hele historikken
python -m app.demand      # config/demand_model.json — fordelingen af netto pickup
python -m app.backtest --ud-af-stikproeve   # version 3 mod version 4
```

`app/cube.py` rekonstruerer hvad der stod på bøgerne enhver historisk dag:
oprettelsesdato og annulleringsdato er begge i reservationshistorikken, så
tilstanden kan regnes eksakt. 731 datoer x 121 lead times. To forbehold står i
modulets dokumentation og gælder hver gang kuben bruges: **ændringer fanges
ikke** (en flyttet reservation ser ud som om den altid lå på den nye dato), og
**udsolgte datoer er censurerede** (den sande efterspørgsel var højere; de er
flaget og udelades).

`app/demand.py` estimerer fordelingen, ikke kun middelværdien:

    netto pickup  ~  middelværdi(lead, ugedag) x sæson(måned) x niveau x form

Formen er den empiriske fordeling af forholdet mellem faktisk og forventet
pickup, poolet pr. lead-interval. Den rummer gruppespring, skæve haler og
negativ pickup (annulleringer der overstiger nysalg, 2 % af observationerne).

### Krympning mod kalenderen

Målingen (`python -m app.evaluate`) viste noget ingen af versionerne kom godt ud
af: fra 60 dage ude slog det rå gennemsnit for ugedagen i måneden begge modeller.
Bookingerne på bøgerne så langt ude tilførte støj frem for information — 43 % af
alle reservationslinjer ender annulleret, og en dato der ser stærk ud fordi en
skoleklasse har booket tre måneder frem, er ikke stærk.

Prognosen krympes derfor mod sæsongennemsnittet:

    prognose = w(lead) x (bøger + forventet pickup) + (1 - w(lead)) x sæsongennemsnit

Vægten er ikke valgt, den er estimeret pr. lead time ved mindste kvadraters
metode på historikken, og bundet til [0; 1]:

| lead | rum | senge |
|---|---|---|
| 3 | 0,87 | 1,00 |
| 14 | 0,62 | 1,00 |
| 30 | 0,49 | 1,00 |
| 60 | 0,29 | 1,00 |
| 120 | 0,21 | 0,11 |

Senge holder vægten længere, fordi sengesalget er så sent, at bøgerne langt ude
alligevel er næsten tomme — der er intet at krympe.

Krympningen afhænger af belægningen på bøgerne, så modellen bindes til datoens
OTB med `model.bind(otb)` før bid price-beregningen. Effekten på prognosefejlen
står i afsnittet om backtesten.

**Tallet der afviser Poisson.** Stigens tempo-trigger bruger en Poisson-z, som
antager at variansen er lig middelværdien. På egne data er forholdet 6-11:

| lead | rum | senge |
|---|---|---|
| 7 | 7,9 | 9,5 |
| 30 | 9,2 | 10,8 |
| 90 | 10,5 | 11,1 |

Nævneren i z-scoren er derfor 2,5-3,5 gange for lille, og triggeren slår ud på
støj. Grunden er grupper: sovesalene sælges samlet, og én skoleklasse flytter
tyve enheder på én dag.

### Hvad backtesten viste

Fordelingen estimeret på 2024 alene og målt på 2025, som den aldrig har set.
Gennemsnitlig absolut fejl i prognosen for antal solgte værelser, med
klyngebootstrap over datoer (`python -m app.evaluate`). "historik" er det rå
gennemsnit for ugedagen i måneden — referencen enhver model skal slå:

| lead | historik | version 3 | version 4 | forskel v3 − v4 | 95 %-interval |
|---|---|---|---|---|---|
| 3 | 10,3 | 4,9 | 4,8 | +0,09 | [−0,39; +0,56] |
| 7 | 10,3 | 7,8 | 6,8 | +0,96 | [+0,21; +1,68] |
| 14 | 10,3 | 10,4 | 7,9 | +2,54 | [+1,71; +3,39] |
| 30 | 10,3 | 12,1 | 9,0 | +3,15 | [+2,10; +4,18] |
| 60 | 10,3 | 13,0 | 9,8 | +3,25 | [+2,07; +4,41] |
| 120 | 10,3 | 15,2 | 9,9 | +5,23 | [+3,93; +6,60] |

Tre dage ude er det uafgjort — intervallet rummer nul, og det er som det skal
være, for der er ikke meget tilbage at forudsige. Fra en uge og ud vinder
version 4 hele vejen. Med krympningen slår version 4 også historik-referencen på
alle lead times; uden den tabte den fra 60 dage og ud.

Kalibreringen, altså om fordelingen har den bredde den lover:

| Lovet | Faktisk |
|---|---|
| 50 % | 49 % |
| 80 % | 79 % |
| 90 % | 89 % |

Det er forudsætningen for bid price. Var fordelingen for smal, ville modellen se
knaphed der ikke er der.

**Om omsætning siger backtesten ingenting.** Historikken indeholder kun de
priser der faktisk blev taget, så enhver sammenligning af omsætning hviler på en
antagelse om hvordan gæsterne ville have reageret på en anden pris. Tallet
beregnes og printes, men det er antagelsen der driver det. Det rigtige svar
kommer fra eksplorationen.

### Hvorfor elasticiteten ikke kan hentes fra historikken

Det nærliggende spørgsmål er, om elasticiteten ikke bare kan estimeres på de to
års historik i stedet for at vente en sæson på eksplorationen. Svaret er nej, og
`python -m app.evaluate` afsnit 4 viser hvorfor frem for at påstå det:

| Metode | Estimat | Standardfejl |
|---|---|---|
| Naiv regression af log salg på log pris | **+1,97** | 0,11 |
| Med kontrol for ugedag × måned | **+1,42** | 0,08 |

Begge har forkert fortegn. Taget for pålydende siger det første, at 10 % højere
pris giver 20 % **flere** solgte værelser. Grunden er, at priserne i historikken
ikke blev sat tilfældigt: et menneske eller den gamle model hævede prisen netop
når efterspørgslen var høj, så pris og salg bevæger sig sammen (korrelation
+0,57). Regressionen måler den sammenhæng, ikke gæsternes prisfølsomhed.

Kontrollen for ugedag og måned hjælper ikke nok, og det er pointen: selv på en
bestemt lørdag i juli vidste den der satte prisen, om netop den lørdag var travl.
Præcis den information står ikke i data og kan derfor ikke trækkes fra.

Bemærk standardfejlene. De er små, så estimatet er ikke støjende — det er
sikkert forkert. Mere historik gør det kun mere sikkert. Det er forskellen på et
datamængdeproblem og et identifikationsproblem, og kun det første løses med tid.

Eksplorationen flytter prisen af en grund der intet har med efterspørgslen at
gøre — et møntkast mellem to trin der står lige godt. Først der er udsvinget
rent. Prisen varierer til gengæld mindre (spredning 0,035 i log mod historikkens
0,225), og det er derfor der skal en sæson til.

### Den ene antagelse

    efterspørgselsfaktor(trin) = (pris(trin) / pris(reference)) ^ (-elasticitet)

Elasticiteten er ikke målt. Den er det eneste ukendte tal tilbage i
prisbeslutningen, og det er med vilje: version 3 havde fire triggervægte, to
målbelægninger og en trinafstand, som alle var gæt. Standardværdierne (rum 1,6,
senge 2,0) er litteraturniveau.

**Derfor er rabatten bundet.** Med en konstant elasticitet over 1 og en lav
variabel omkostning ligger det ubegrænsede optimum under prisgulvet: modellen
vil stå på nederste trin på enhver dato hvor kapaciteten ikke binder. Det er en
ekstrapolation langt væk fra de priser der er observeret, og den rammer den
svageste delscore, værdi for pengene (7,1 mod 8,9). Indtil
`elasticity_measured: true` må version 4 derfor ikke gå dybere end
`max_discount_rungs` under referencetrinnet. Klemmes trinnet, står det i
begrundelsen.

### Bundet eksploration

`pricing.ladder.explore`. Står to nabotrin omtrent lige godt — inden for
`explore_band` af vippepunktet — vælges der tilfældigt mellem dem, og valget
logges. Risikoen er loftet ved ét trin, mindre end den uro modellen laver i
forvejen. Mønten er deterministisk pr. dato, så den samme dato giver det samme
svar hver gang kørslen gentages; ellers flytter prisen sig hver gang nogen
trykker på knappen, og eksperimentet bliver til støj.

Det er roadmap punkt 3 gjort billigt: hundredvis af observationer pr. sæson i
stedet for fyrre, og prisen er valgt tilfældigt, så målingen holder.

### Tre mekanismer bliver til én beregning

**Flex-allokering.** Et flex-rum solgt privat koster otte sengepladser. Sælg det
som rum hvis

    nettopris_rum x P(efterspørgsel_rum >= ledige_rum + 1)
      >= sum over de otte senge af nettopris_seng x P(efterspørgsel_seng >= den seng)

Summen er aftagende, fordi den ottende seng er mindre værd end den første.
Det nuværende RevPAB-check sammenligner gennemsnit og fanger ikke den del.
`flex_min_gain` (25 kr.) er der af driftshensyn, ikke af matematiske: uden den
flytter modellen sovesale på datoer hvor begge lagre er tomme, fordi to en halv
krone er mere end nul. Anbefalinger man lærer at ignorere er værre end ingen.

**Gruppeforskydning.** Gulvet er summen af bid price for hver enkelt enhed over
hver dato. De første værelser er næsten gratis, hvis der er rigeligt tilbage; de
sidste er dyre. Et gennemsnit rammer ingen af delene.

To ting er anderledes her end i trinvalget, og begge skyldes at **afgivne
gruppetilbud er bindende**:

*Ingen elasticitetsdæmpning.* Fortrængningen regnes på den udæmpede
efterspørgsel, så længe `elasticity_measured: false`. Dæmper man med en
antagelse ingen har målt, bliver gulvet lavere end det burde være. Et for højt
gulv koster en forespørgsel; et for lavt gulv er bindende i et år.
(`group_undamped_until_measured`.)

*To tal, ikke ét.* `minimum_rate` er den forventede fortrængning over hele
fordelingen — nogle gange fyldes datoen, nogle gange ikke. `minimum_rate_certain`
er hvad gruppen koster, hvis punktprognosen rammer plet. For ti værelser den
20. november 2026 er det 530 mod 730 kr. Det første er rigtigt i forventning;
det andet viser downsiden. Tabet ved at forære en udsolgt dato væk er større end
gevinsten ved at tage en gruppe med på en halvtom, så mennesket der afgiver
tilbuddet skal se spændet og ikke et gennemsnit af to verdener.

Værktøjet giver et gulv, ikke et tilbud.

**Opholdslængde.** Værdien af et ophold er summen af nætternes bid price
(`bidprice.stay_value`). En enkelt lørdag der blokerer tre nætter afvises af sig
selv, når lørdagens bid price er høj og søndagens er lav. Minimumsophold holder
op med at være en regel nogen skal vedligeholde. Bemærk: funktionen findes, men
er endnu ikke koblet på bookingmotoren.

### Adaptivt efterspørgselsniveau

`app/level.py`, gemt i `data/demand_level.json`. Efter hver kørsel sammenlignes
den pickup der faktisk kom med den fordelingen forventede:

    niveau_ny = niveau_gammel x (faktisk / forventet) ^ 0,07,  bundet til [0,6 ; 1,6]

Det fanger et strukturelt skift som efteråret 2025 (august −21 %, oktober −25 %,
november −35 %) uden at nogen skal finde årsagen. Lille eksponent og hårde
grænser, så en manglende kolonne i Picasso-eksporten ikke kan trække niveauet i
bund. Niveauet erstatter ikke undersøgelsen af hvorfor efteråret faldt.

### Rækkefølge i drift

1. Byg kube og fordeling, kør backtesten og se på dækningen.
2. Kør med `enabled: true` i skyggedrift ved siden af version 3 i mindst fire
   uger, og før log over de datoer hvor I var uenige. `python -m app.skyggedrift`
   viser begge versioners forslag side om side på dagens belægning og skriver
   dem til CSV med `--csv`.
3. Slå `explore: true` til, så elasticitetsmålingen begynder at samle data.
4. Efter en sæson: mål elasticiteten, sæt `elasticity_measured: true`, og
   løft `max_discount_rungs`.

`/health` viser hvilken version der faktisk kører (`pricing_version`), om v4 var
ønsket (`v4_requested`), hvorfor den eventuelt ikke kører (`v4_notes`), og hvor
det adaptive niveau står. Uden den linje kan man tro man kører v4 i ugevis uden
at gøre det.

**Forvent en mærkbar forskel.** På belægningen pr. 21. september 2026 var de to
versioner uenige på 43 af 46 datoer, og version 4 lå i snit 74 kr. lavere pr.
værelse. Det er ikke en finjustering. Ændringsbremsen på 15 % om dagen betyder
at prisen bevæger sig derhen over to-tre døgn og ikke på én nat, men retningen
er tydelig: version 4 holder igen med prisen på datoer, hvor version 3 så en
prognose over målet.

Risikovurderingens punkt 7 gælder uændret. Ingen modelforbedring erstatter
rimelighedstjek, alarm på `/health`, skyggedrift og en aftale om hvem der kigger.


## Rimelighedstjek og eksportformat

Uploadet belægning gennemgår `app/sanity.py` før import. Tjekkene vurderer ikke
om tallene er rigtige — det kan ingen maskine afgøre — men om de overhovedet kan
passe: en kolonne der er nul hele vejen, mere solgt end huset har, spring over 80
procentpoint på et døgn, priser i forkert enhed, datoer uden for horisonten, og
**huller i datoserien**.

Hullet er det nyeste tjek, hentet fra en gennemgang af en ekstern prototype
(`claude/hotel-pricing-sammenligning.md`). Mangler der datoer mellem filens
første og sidste dato, bliver de datoer aldrig prissat — rækkerne står bare ikke
der, og motoren mærker intet. Det er værre end en tom kolonne, fordi der ikke er
noget forkert tal at opdage. At filen slutter før horisontens ende er derimod
normalt og håndteres af dødmandsknappen.

`/export.csv` skriver dansk Excel-format: semikolon som feltseparator,
decimalkomma og UTF-8 med BOM. Uden kommaet læser Excel `900.00` som 90.000,
fordi punktum er tusindtalsseparator på dansk — og det opdages først, når prisen
er tastet ind i Picasso. Uden BOM bliver æ, ø og å forvansket i kolonnenavne og
bemærkninger.

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
  Afledte rum- og sengetyper følger de konfigurerede faktorer.

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
app/ladder.py             prisstigen: trin, træghed, eksploration
app/cube.py               bookingkuben: OTB(dato, lead) fra reservationshistorikken
app/demand.py             fordelingen af resterende efterspørgsel
app/bidprice.py           bid price: trinvalg, flex, gruppegulv, opholdsværdi
app/level.py              adaptivt efterspørgselsniveau
app/backtest.py           version 3 mod version 4 på historikken
app/evaluate.py           afgør hvilken version der er bedst, med usikkerhed
app/skyggedrift.py        begge versioner side om side på dagens belægning
app/service.py            kørslen: hent, beregn, gem, skriv tilbage
app/calibration.py        begge bookingkurver og sæson fra egen historik
app/adapters/             CSV ind; ingen skrivning tilbage til Picasso
app/main.py               FastAPI: dashboard, grupper, upload, godkendelse, API
app/sanity.py             rimelighedstjek på uploadet belægning
config/config.yaml        lager og alle parametre, ingen kode
samples/                  eksempeldata, inkl. reservationshistorik til kalibrering
tests/                    motor-, service- og regressionstests
```
