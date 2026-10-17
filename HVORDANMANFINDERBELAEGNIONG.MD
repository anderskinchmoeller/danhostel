# Picasso · rapportguide

[Daglig vejledning](README.md) · [Teknisk reference](REFERENCE.md)

Rapporterne har forskellige formål. Arrivals leverer aktuelle reservationer;
Hotelmanager og produktomsætning bruges til at kontrollere afsluttede resultater.
Menunavne kan variere mellem Picasso-versioner. Opsætningerne her bygger på
de rapporter, der er gennemgået fra jeres installation.

## 1. Aktuelle reservationer · Arrivals / Rooms spec.

Åbn **Reports (F7) → Arrivals → Rooms spec.**

| Indstilling | Valg |
|---|---|
| Periode | Cirka 30 dage tilbage til mindst 120 dage frem |
| Status til | WaitingList, Tentativ, Provisional, Confirmed, Guaranteed, In-House |
| Status fra | Out of House, X-Cancelled, No-Show |
| Display til | Prices og Guest |
| Display fra | Alt res. |
| Sortering | Room type |

Gem eller send hele rapporten som PDF og upload under **Data → Belægning**
eller **Resultater → Upload Arrivals fra Picasso**. Bevar sidehovedets periode
og udskriftstidspunkt.

Importen tæller kun **Confirmed, Guaranteed og In-House**. De øvrige aktive
statusser i rapporten tælles ikke som bindende salg. Hver solgt enhed fordeles
på opholdsnætterne fra ankomst til dagen før afrejse.

Perioden skal begynde før ankomsten for gæster, der allerede bor på huset;
30 dage tilbage er ikke nok ved længere ophold. Arrivals’ aktive reservationer
kan ikke dokumentere endeligt salg for allerede afsluttede datoer.

PDF’en giver ikke automatisk faktiske blokeringer eller den offentlige salgspris.
Reservationsbeløb fordelt pr. nat er et skøn, ikke bogført logiomsætning.

## 2. Endeligt salg · Hotelmanager

Vælg én afsluttet opholdsdato efter natkørslen. Gem en PDF med både:

- **Hotel Manager’s report part 1**.
- **Part 2 (Room/Bed)**; brug **Group by Room/Bed** i opsætningen.

Brug den afstemte opsætning **eksklusive VAT, Package, Credit og Out of order**.
Kontrollér eventuelle kreditposteringer særskilt.

Importen læser **Today → Room Inhouse → Rooms / Beds** som forslag til endeligt
salg. Pax, ankomster, Pcs og månedens/årets antal bruges ikke som endeligt salg.
En tom celle tolkes ikke som et eksplicit nul; manglende antal må registreres
manuelt. Bekræft først efter kontrol af, at datoen og tallene er endelige.

## 3. Logi pr. rumtype · Produktomsætning

Åbn **Reports (F7) → Statistik → Produkter**. Vælg samme afsluttede dato som
Hotelmanager. Brug rapporten **Product Turnover**, ikke et fremtidigt omsætningsskøn.

| Indstilling | Valg |
|---|---|
| Overnight Products | Til |
| Day Products | Fra |
| Product name | Til |
| Turnover | Til |
| MIS date | Til; kontrollér også datogrundlaget i PDF’en |
| Sort by | Room type |
| Inclusive VAT | Fra |
| Status til | Confirmed, Guaranteed, In-House, Out of House |
| Øvrige statusser | Fra |

Gem hele PDF’en med alle produktlinjer, rumtypetotaler, **Grand Total**,
udskriftstidspunkt og fodtekst. Den understøttede rapport har blandt andet
kolonnerne Adults, Nights, Rooms, No. Res og Total price.

Rumtypen, eksempelvis D2 eller BV8, afgør værelser/senge efter konfigurationen.
**YP kan dække både hele værelser og enkelte senge.**

Morgenmad foreslås kun fratrukket på linjer med morgenmad i beskrivelsen og
uden opholdsantal. Linjer med samme produktnavn og antal gæster/nætter/rum
beholdes som logi. Kontrollér fradraget i originalen.

Fodteksten kan stadig sige **Arrival date**, selv om MIS date er valgt.
Kontrollér, at beløbene gælder den enkelte opholdsnat, især ved flerdagesophold.
Et match med Hotelmanager på én dato beviser ikke korrekt periodisering på alle
andre datoer. Flyttede produkter uden reservation kan være udeladt af rapporten.

## 4. Upload og bekræft dagsresultatet

1. Gå til **Resultater → Importér afsluttet dagsresultat**.
2. Upload Hotelmanager og produktomsætning for samme dato. Begge rapporter
   skal være udskrevet efter opholdsdatoen.
3. Kontrollér antal, logi pr. rumtype, morgenmadsfradrag og afstemningen.
4. Bekræft endeligt salg. Medtag kun økonomien, når opholdsdato, fuldstændighed,
   momsgrundlag og fradrag er kontrolleret.
5. Tilføj faktisk provision, variable omkostninger og tilgængelige senge,
   når du kender dem. Lad ukendte felter stå tomme.

Upload opretter en kladde. Bekræftelsen gør tallene til et faktisk resultat.
Originalfilerne og rettelserne bevares. Samme filpar giver ikke dubletter.

**Accounts / bogføringsrapport er valgfri til yderligere afstemning** og indgår
ikke som en tredje påkrævet fil i dagsimporten. Kontoens samlede morgenmad må
ikke trækkes fra igen, hvis den ikke indgår i produktudtrækkets beløb.

## Gem som PDF

Brug Picassos fil-/PDF-eksport, hvis den findes i rapportvinduet. Ellers vælg
**Print** og en tilgængelig PDF-printer. Ved fjernskrivebord kan Universal Printer
føre videre til en lokal udskriftsdialog, hvor PDF kan vælges.
Hvis printerlisten kun viser en fysisk printer, er PDF-printeren ikke tilgængelig
i den dialog; undersøg rapportens filfunktion eller fjernskrivebordets printeropsætning.
