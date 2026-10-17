# Audit af prisgrundlag og rapportafstemning · 17. oktober 2026

Der er ikke ændret priser, konfiguration, modeltræning eller bekræftede resultater.

## Hvad der kan bekræftes i kalibreringen

- `kali/reservationer_2024_2025.csv` har 34.010 linjer. Kolonnerne er
  bookingdato, ankomstdato, naetter, quantity, pris, enhed, status, cancelled_at,
  rumtype og ref. Produktkode, momsgrundlag og morgenmadsbeløb er ikke bevaret.
- `app/calibration.py:219` beregner en median af de indlæste priser på datoer
  med 75–85 % belægning. Der er ingen moms- eller morgenmadskorrektion.
- `app/cube.py:132` indlæser pris direkte. Efterspørgselsmodellen i
  `app/demand.py` estimerer antal/restsalg, ikke historisk netto-logiomsætning.
  Problemet skal derfor adskilles fra en påstand om, at alle salgsprognoser er
  fejltrænet på pakkebeløb.
- Seks identificerede MM-reservationer i Rooms spec.-kilden for andet halvår
  2025 matcher CSV'en. Kilde:
  `/Users/anderskinch/hostel/kali/Arrivals_on_01-07-2025_to_31-12-202510.pdf`.
  De er ref. 111082 (D2, 470 kr.), 110875 (D4, 802 kr.), 111062 og 111083
  (D4, hver 612 kr.), 110821 (F3, 1.440 kr.) og 111131 (F3, 700 kr.).
  Alle seks har én nat og én enhed i det læste udtræk. Ref. 111131 viser
  direkte 700 kr. Total Price i kilden og 700 kr. pris i CSV'en.
- Dette dokumenterer, at MM-produkter ikke er særskilt renset i dataforløbet.
  Det dokumenterer ikke, hvilket konkret morgenmadsbeløb hver historisk pris
  indeholder, eller om alle historiske beløb er inklusive moms. Rapporternes
  overskrift er ikke en attest af dette grundlag.
- Som afgrænset følsomhedskontrol blev ikke-annullerede linjer med positiv
  pris grupperet pr. rumtype og de seks identificerede MM-linjer udeladt.
  Den uvægtede linjemedian var uændret: D2 502,55, D4 600,00 og F3 782,28 kr.
  Dette er hverken en genkalibrering af alle faktorer eller et bevis for, at
  alle morgenmadspakker er identificeret.
- `config/config.yaml` henviser til historiske D2-medianer omkring 503 kr. og
  senge omkring 208 kr. Aktuelle `bar_base: 645` og `bed_base: 270` er særskilte
  konfigurerede niveauer; kommentarernes medianer er ikke en automatisk garanti
  for deres moms-/morgenmadsgrundlag.

## Rettelser til forklaringen af 16. oktober

1. Produktnavnenes kontototaler før morgenmadsfradrag er 28.929,29 kr.
   Produktudtrækkets Grand Total er 28.929,28 kr.; forskellen er én øre.
   Morgenmad knyttet til de navngivne MM-produkter er 422,40 kr.
2. `Downloads/Report1.pdf` viser under None på konto 1002 en 10 %-rabat på
   100 kr., samme reference 123243 og folio 272221 som en ONLINE-logipost på
   1.000 kr. Produktkoblet logi 28.506,89 minus rabat 100 giver 28.406,89 kr.,
   som afrundes til Hotelmanagers 28.407. Dette er en konkret afstemningsbro,
   ikke et generelt bevis for ens datoafgrænsning eller korrekt typefordeling.
3. Resten af None på konto 1002 er netto 8.718,54 kr. i debet fra manuelle
   posteringer/refusioner. To modposteringspar nettofører nul. De to refusioner
   på 2.295 kr. på bilag 100833 bevares begge; de må ikke deduplikeres uden
   transaktionsbevis. Rapporten angiver bogføringsdato, ikke opholdsdato.
4. Ingen slutning om flytning til faktura/master bill er nødvendig: konkrete
   posteringstekster er nu tilgængelige. Deres regnskabsmæssige behandling og
   opholdsperioder kræver stadig bilagskontrol.
5. I Hotelmanager-reportens side 10 står D4's ene tidlige afrejse under
   **This year to date**, ikke Today. Den række forklarer ikke et dokumenteret
   antal tidlige afrejser den 16. oktober.
6. Prislinjerne uden Rooms/Pax er 246,40 + 576,00 + 365,04 = 1.187,44 kr.
   Men forskellen No. Res 49 mod Rooms 47 kan ikke forklares alene ved disse
   tre linjer: V6-totalen viser 3 mod 2, og BV8-totalen viser også 3 mod 2.
   V10-linjen på 365,04 viser intet reservationsantal.
7. Konto 1004-billedet fra brugeren viser to opgraderingspar á +/-850, som
   udligner hinanden, plus 1.224,72 kr. med teksten Booking.com og 160 kr.
   beskrevet som V8 til V10. Begge sidste poster har reference 123335.
   Teksten Booking.com dokumenterer ikke alene, at beløbet er provision.

## Næste datakrav

Bevar produkt, kildebeløb, antal enheder, nætter, momsgrundlag, pakkegrundlag
og særskilt morgenmadsbeløb i en dokumenteret historisk priseksport. Afstem
et udvalg af MM- og ikke-MM-bookinger mod konkrete opholdsdatoer og bilag.
Genberegn først relevante prismedianer og faktorer, når det grundlag er
bekræftet. Omregn ikke hele historikken med 1,25 alene ud fra de nye rapporter.
