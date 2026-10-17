"""Parse paired Picasso result PDFs into reviewable, immutable evidence.

Product labels do not identify inventory. Types are matched to configured room
and bed codes. Arrival-date footers always require explicit date attestation.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from . import db, validation
from .picasso_belaegning import pdf_text

VERSION = 1
MONEY = re.compile(r"-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}")
NUMBER = re.compile(r"-?\d[\d.,]*%?")
PRINTED = re.compile(r"(\d{2}-\d{2}-\d{4})\s+(?:kl\.?|Time:)\s*(\d{2}:\d{2})", re.I)


def amount(value):
    try:
        return Decimal(value.replace('.', '').replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('Ugyldigt beløb i PDF') from exc


def money(value):
    return format(value, '.2f')


def common(text, tz, now=None):
    if 'Danhostel Aarhus City' not in text:
        raise ValueError('Rapporten skal være fra Danhostel Aarhus City')
    matches = set(PRINTED.findall(text))
    if len(matches) != 1:
        raise ValueError('Rapporten skal have ét entydigt udskriftstidspunkt')
    stamp = datetime.strptime(' '.join(next(iter(matches))), '%d-%m-%Y %H:%M')
    observed = stamp.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).replace(tzinfo=None)
    if observed > (now or db.utcnow()) + timedelta(minutes=1):
        raise ValueError('Rapportens udskriftstidspunkt ligger i fremtiden')
    return observed


def day_from(text, pattern):
    dates = set(re.findall(pattern, text))
    if len(dates) != 1:
        raise ValueError('Rapporten skal indeholde én entydig opholdsdato')
    return datetime.strptime(next(iter(dates)), '%d-%m-%Y').date()


def integer(value):
    if value is None:
        return None
    number = amount(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError('Antal i rapporten skal være hele, ikke-negative tal')
    return int(number)


def parse_hotelmanager(text, tz, now=None):
    observed = common(text, tz, now)
    day = day_from(text, r'Report for:\s*(\d{2}-\d{2}-\d{4})')
    if "Hotel Manager's report part 1" not in text or "part 2 (Room/Bed)" not in text:
        raise ValueError('Hotelmanager-PDF skal indeholde både part 1 og part 2 (Room/Bed)')
    first, second = text.split('part 2 (Room/Bed)', 1)
    if not re.search(r'\bToday\b', first) or not re.search(r'\bToday\b', second):
        raise ValueError('Hotelmanager skal indeholde Today-kolonner')
    header = next((l for l in second.splitlines() if l.count('Total:') >= 2 and 'Rooms:' in l), None)
    if header is None:
        raise ValueError('Fandt ikke Today-kolonnerne Total / Rooms / Beds')
    cols = list(re.finditer(r'Total:|Rooms:|Beds:', header))
    if [m.group() for m in cols[:4]] != ['Total:', 'Rooms:', 'Beds:', 'Total:']:
        raise ValueError('Ukendt kolonneopdeling i Hotelmanager')
    centers = [(m.start()+m.end())/2 for m in cols[:4]]
    bounds = [int((a+b)/2) for a,b in zip(centers,centers[1:])]

    def today_cells(label):
        lines = [l for l in second.splitlines() if l.strip().startswith(label)
                 and re.match(r'^\s*'+re.escape(label)+r'\s{2,}', l)]
        if len(lines) != 1:
            raise ValueError('Fandt ikke entydig Hotelmanager-række: '+label)
        line = lines[0]
        fields = [line[len(label)+1:bounds[0]], line[bounds[0]:bounds[1]], line[bounds[1]:bounds[2]]]
        result = []
        for field in fields:
            tokens = NUMBER.findall(field)
            if len(tokens)>1 or (field.strip() and not tokens):
                raise ValueError('Tvetydig Today-celle i '+label)
            result.append(tokens[0] if tokens else None)
        return result

    _, rooms, beds = today_cells('Room Inhouse')
    adr_total, adr_room, adr_bed = today_cells('ADR')
    totals = [l for l in first.splitlines() if re.match(r'^\s*Hotel\s{2,}', l)]
    if len(totals)!=1:
        raise ValueError('Fandt ikke Hotel-totalen på første side')
    values = NUMBER.findall(totals[0])
    if len(values)<4:
        raise ValueError('Hotel-totalen mangler Today-tal')
    if not all(s in first for s in ('excl. VAT', 'excl. Package', 'excl. Credit', 'excl. Out of order')):
        raise ValueError('Brug Hotelmanager eksklusive moms, pakker, kredit og ude af drift som i den aftalte opsætning')
    return dict(day=day.isoformat(), printed_at=observed.isoformat()+'Z',
        rooms=integer(rooms), beds=integer(beds), total_price_rounded=money(amount(values[2])),
        adr_room_rounded=money(amount(adr_room)) if adr_room is not None else None,
        adr_bed_rounded=money(amount(adr_bed)) if adr_bed is not None else None,
        choices='Ekskl. moms, pakker, kredit og ude af drift',
        note='Room Inhouse i Today bruges; Pax, ankomster, Pcs og årstal bruges ikke som endeligt salg.')


def parse_products(text, tz, params, now=None):
    observed = common(text, tz, now)
    day = day_from(text, r'Product Turnover for\s*(\d{2}-\d{2}-\d{4})')
    if 'Product types : Overnight' not in text or 'excl. VAT.' not in text:
        raise ValueError('Produktomsætningsrapporten skal vise Overnight og være eksklusive moms')
    status = re.search(r'Reservation status criteria:\s*(.*?)\s{3,}', text)
    if not status:
        raise ValueError('Rapporten mangler entydige reservationsstatusser')
    statuses = [s.strip() for s in status.group(1).split(',')]
    if any(s not in ('Confirmed','Guaranteed','In-House','Out of Hous','Out of House') for s in statuses):
        raise ValueError('Fravælg venteliste, tentative, provisional, annullerede og no-show i produktudtrækket')
    if not any(s in statuses for s in ('Out of Hous','Out of House')):
        raise ValueError('Produktudtrækket skal medtage Out of House, så afrejste ophold er med')
    header = next((l for l in text.splitlines() if 'Product name' in l and 'Total price' in l), None)
    if not header or not all(k in header for k in ('Adults', 'No. Res', 'Nights', 'Rooms')):
        raise ValueError('Ukendt kolonneopdeling i produktomsætningsrapporten')
    quantity_start, quantity_end = header.index('Adults'), header.index('No. Res')
    descriptive_end = header.index('Adults')
    rows, pending, declared, grand = [], [], {}, None
    for line in text.splitlines():
        total = re.match(r'^\s*Total\s+([A-Z][A-Z0-9]*)\s+', line)
        currencies = list(MONEY.finditer(line))
        if line.strip().startswith('Grand Total'):
            if grand is not None or not currencies:
                raise ValueError('Tvetydig eller manglende hovedtotal')
            grand = amount(currencies[-1].group())
            continue
        if total:
            code=total.group(1)
            if code in declared or not pending or not currencies:
                raise ValueError('Tvetydig eller tom rumtypegruppe: '+code)
            declared[code]=amount(currencies[-1].group())
            for row in pending:
                row['room_type']=code
                row['inventory']='room' if code in params.room_types else 'bed' if code in params.bed_types else 'unknown'
                rows.append(row)
            pending=[]
            continue
        if currencies and line[:descriptive_end].strip() and currencies[-1].end()==len(line.rstrip()):
            description=line[:descriptive_end].strip()
            quantity_tokens=NUMBER.findall(line[quantity_start:quantity_end])
            # Package/breakfast lines have no adults, pax, nights or rooms. The
            # same product WITH quantities is accommodation and must be retained.
            breakfast=(not quantity_tokens and 'morgenma' in description.lower())
            category='breakfast' if breakfast else 'lodging' if quantity_tokens else 'unknown'
            pending.append(dict(description=description, amount=money(amount(currencies[-1].group())),
                                category=category))
    if pending or not rows or grand is None or not declared:
        raise ValueError('Produktudtrækket er ufuldstændigt; medtag hele rapporten og Grand Total')
    # Printed detail/subtotals may round independently. Larger differences must
    # not silently disappear or be assigned to a room type.
    detail_difference=grand-sum(Decimal(r['amount']) for r in rows)
    subtotal_difference=grand-sum(declared.values())
    if abs(detail_difference)>Decimal('.02') or abs(subtotal_difference)>Decimal('.02'):
        raise ValueError('Produktlinjer eller rumtypetotaler stemmer ikke med Grand Total (mere end 2 øre)')
    for code, subtotal in declared.items():
        if abs(subtotal-sum(Decimal(r['amount']) for r in rows if r['room_type']==code))>Decimal('.02'):
            raise ValueError('Produktlinjer stemmer ikke med rumtypetotalen: '+code)
    footer=re.search(r'Show for\s+(.+?)\s+excl\. VAT\.',text)
    if not footer:
        raise ValueError('Rapporten mangler datogrundlag nederst')
    return dict(day=day.isoformat(), printed_at=observed.isoformat()+'Z', statuses=statuses,
        date_basis=footer.group(1).strip(), total=money(grand), rows=rows,
        declared_by_type={k:money(v) for k,v in declared.items()},
        detail_difference=money(detail_difference), subtotal_difference=money(subtotal_difference),
        room_types=list(params.room_types), bed_types=list(params.bed_types))


def review(payload):
    hotel, products = payload['hotelmanager'], payload['products']
    grouped={}
    room=bed=breakfast=unknown=Decimal('0')
    for r in products['rows']:
        value=Decimal(r['amount'])
        bucket=grouped.setdefault(r['room_type'], dict(code=r['room_type'],inventory=r['inventory'],
            lodging=Decimal('0'),breakfast=Decimal('0'),unknown=Decimal('0')))
        bucket[r['category']]+=value
        if r['category']=='breakfast': breakfast+=value
        elif r['category']=='unknown' or r['inventory']=='unknown': unknown+=value
        elif r['inventory']=='room': room+=value
        else: bed+=value
    # Use printed type subtotals. Independent line rounding stays visible;
    # never assign a global rounding difference to an inventory category.
    room=bed=unknown=Decimal('0')
    unresolved=False
    for code, bucket in grouped.items():
        subtotal=Decimal(products['declared_by_type'][code])
        detail=sum(bucket[k] for k in ('lodging','breakfast','unknown'))
        bucket['line_difference']=money(subtotal-detail)
        bucket['lodging']=subtotal-bucket['breakfast']-bucket['unknown']
        if bucket['inventory']=='unknown' or any(r['category']=='unknown' for r in products['rows'] if r['room_type']==code):
            unresolved=True
            unknown+=bucket['lodging']+bucket['unknown']
        elif bucket['inventory']=='room': room+=bucket['lodging']
        else: bed+=bucket['lodging']
    difference=Decimal(products['subtotal_difference'])
    target=Decimal(products['total'])-breakfast
    hotel_difference=target-Decimal(hotel['total_price_rounded'])
    checks=[]
    # Half a krone is the uncertainty from printing a whole-krone amount.
    if abs(hotel_difference)>Decimal('.50'):
        checks.append('Logi efter fradrag af morgenmad afviger fra Hotelmanager med mere end 50 øre.')
    for key, count, revenue in [('room',hotel['rooms'],room),('bed',hotel['beds'],bed)]:
        adr=hotel.get('adr_'+key+'_rounded')
        if count is not None and adr is not None and unknown==0:
            if (count==0 and revenue!=0) or (count>0 and abs(revenue/count-Decimal(adr))>Decimal('.50')):
                checks.append('Prisgennemsnittet for '+('værelser' if key=='room' else 'senge')+' stemmer ikke med Hotelmanager.')
    if unresolved:
        checks.append('Der er uafklarede beløb eller ukendte rumtyper; økonomital kan ikke bekræftes.')
    return dict(by_type=[dict(g, lodging=money(g['lodging']),breakfast=money(g['breakfast']),unknown=money(g['unknown']))
                         for g in grouped.values()], room=money(room),bed=money(bed),breakfast=money(breakfast),
                unknown=money(unknown), difference=money(difference), total_lodging=money(target),
                hotel_difference=money(hotel_difference), issues=checks,
                revenue_ready=not checks, date_attestation_required=True)


def archive(data):
    digest=hashlib.sha256(data).hexdigest()
    validation.PDF_ARCHIVE_ROOT.mkdir(parents=True,exist_ok=True)
    target=validation.PDF_ARCHIVE_ROOT/(digest+'.pdf')
    try:
        with target.open('xb') as f: f.write(data)
    except FileExistsError:
        if target.read_bytes()!=data:
            raise ValueError('Den arkiverede kildefil svarer ikke til sit aftryk')
    return digest


def stage(session, hotel_data, product_data, settings, actor, *, hotel_name='', product_name=''):
    try:
        hotel=parse_hotelmanager(pdf_text(hotel_data),settings.timezone)
        products=parse_products(pdf_text(product_data),settings.timezone,settings.params)
    except Exception as exc:
        if isinstance(exc,ValueError): raise
        raise ValueError('Kun læsbare, komplette Picasso-PDF’er understøttes') from exc
    if hotel['day']!=products['day']:
        raise ValueError('De to rapporter skal gælde samme dato')
    day=datetime.fromisoformat(hotel['day']).date()
    observed=max(validation.timestamp(hotel['printed_at'],settings.timezone),
                 validation.timestamp(products['printed_at'],settings.timezone))
    for r in (hotel,products):
        printed=validation.timestamp(r['printed_at'],settings.timezone)
        if day>=printed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).date():
            raise ValueError('Begge rapporter skal være udskrevet efter opholdsdatoen er afsluttet')
    payload=dict(parser_version=VERSION,hotelmanager=hotel,products=products,
                 hotel_pdf_hash=archive(hotel_data),product_pdf_hash=archive(product_data),
                 timezone=settings.timezone)
    added=validation.append(session,'report_pair',day,observed,payload,actor=actor,source='Hotelmanager + produktomsætning')
    record=session.scalar(select(db.ValidationRecord).where(db.ValidationRecord.kind=='report_pair',
        db.ValidationRecord.day==day, db.ValidationRecord.payload==json.dumps(payload,ensure_ascii=False,sort_keys=True)))
    if record is None:
        raise ValueError('Kunne ikke genfinde rapportparret')
    if added:
        db.log(session,'result_reports_upload',actor=actor,day=day,
               detail=json.dumps(dict(record_id=record.id,hotel_file=hotel_name,product_file=product_name)))
    return record


def confirm(session, record, *, actor, final_counts, accept_revenue, date_confirmed, note='', supplements=None):
    if record.kind!='report_pair': raise ValueError('Ukendt rapportpar')
    payload=json.loads(record.payload)
    report=review(payload)
    hotel=payload['hotelmanager']
    if not final_counts:
        raise ValueError('Bekræft at antal værelser og senge er de endelige salgstal')
    if hotel['rooms'] is None or hotel['beds'] is None:
        raise ValueError('Hotelmanager mangler eksplicit antal for værelser eller senge; registrér resultatet manuelt')
    if accept_revenue and (not report['revenue_ready'] or not date_confirmed):
        raise ValueError('Økonomi kræver afstemte beløb og bekræftet opholdsdato/morgenmad')
    previous=session.scalar(select(db.ValidationRecord).where(db.ValidationRecord.kind=='outcome',
        db.ValidationRecord.day==record.day).order_by(db.ValidationRecord.observed_at.desc(),db.ValidationRecord.id.desc()))
    values={}
    if previous:
        if previous.observed_at>record.observed_at:
            raise ValueError('Der findes et nyere resultat for datoen; brug nyere rapporter eller ret resultatet manuelt')
        values=json.loads(previous.payload)
    values.update(final=True,final_rooms=hotel['rooms'],final_beds=hotel['beds'],
                  result_report_id=record.id,hotel_pdf_hash=payload['hotel_pdf_hash'],
                  product_pdf_hash=payload['product_pdf_hash'],
                  report_counts_confirmed=True)
    if accept_revenue:
        values.update(room_revenue=float(report['room']),bed_revenue=float(report['bed']),
            revenue_adjustment=float(report['difference']),revenue_date_confirmed=True,revenue_report_id=record.id,
            revenue_basis='product_report_attested_stay_date',amounts_by_type=report['by_type'],
            breakfast_excluded=float(report['breakfast']),reported_lodging_total=float(report['total_lodging']))
    for key, raw in (supplements or {}).items():
        if key not in ('commission','variable_cost','available_beds'):
            raise ValueError('Ukendt supplerende oplysning')
        if str(raw).strip():
            _, _, extra=validation.prepare('outcome',dict(day=record.day.isoformat(),
                observed_at=record.observed_at.isoformat()+'Z',**{key:raw}),payload['timezone'])
            values.update(extra)
    for group, total in (('group_rooms','final_rooms'),('group_beds','final_beds')):
        if values.get(group,0)>values[total]:
            raise ValueError('Tidligere gruppeantal overstiger rapportens salgstal; ret gruppeantallet manuelt først')
    if previous: values['carried_forward_from_record']=previous.id
    if note.strip(): values['note']=note.strip()
    # Stable source observation time and payload make repeated confirmation idempotent.
    # Do not add a self-reference when confirming the same source twice.
    if previous and json.loads(previous.payload).get('result_report_id')==record.id:
        values.pop('carried_forward_from_record',None)
        old=json.loads(previous.payload)
        if 'carried_forward_from_record' in old:
            values['carried_forward_from_record']=old['carried_forward_from_record']
    added=validation.append(session,'outcome',record.day,record.observed_at,values,actor=actor,
                             source='Bekræftet Hotelmanager + produktomsætning')
    if added:
        db.log(session,'result_reports_confirm',actor=actor,day=record.day,
               detail=json.dumps(dict(report_id=record.id,revenue=accept_revenue)))
    return added
