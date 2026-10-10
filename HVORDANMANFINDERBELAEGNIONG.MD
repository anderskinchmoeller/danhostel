F7 - Arrivalvs -> room specs fra (-30, 120dage).

## Under status: 
  Til: WaitingList, Tentativ, Provisional, Confirmed, Guaranteed, In-House
  Fra: Out of House, X-Cancelled, No-Show

## Under Display
  Til: Prices, Guest
  Fra: Alt res

##  og:
  Sort by: kun Room type.

send til dig selv som pdf og upload den som vi kender i Dashboardet. 




# hvis modellen ikke kører lokalt
cd /Users/anderskinch/hostel
./scripts/run-prismotor.sh

# ellers kan du bare åbne hosten og køre beregn for filen fra Picasso

# Hvis du kun vil lave daglig skyggedrift v4 mod v5 til CSV, så kør:

cd /Users/anderskinch/hostel
.venv/bin/python -m app.skyggedrift --alle --csv "skygge/skygge_$(date +%F).csv"







RMS_USER=hotel
RMS_PASSWORD=janesminde6


# Vi skal også finde Konkurrentpriser med medianprisen for værelse og seng pr. dato. 
#
    python -m app.skyggedrift                 # datoer hvor de er uenige
    python -m app.skyggedrift --alle          # hele horisonten
    python -m app.skyggedrift --csv log.csv   # til regneark, så
