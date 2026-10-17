"""Daily result evidence: no implicit closure, guessed economics or duplicate writes."""
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app import db, validation, economics, picasso_results as reports
from app.result_routes import router

FIXTURES = Path(__file__).parent / 'fixtures' / 'picasso_results'
HOTEL = (FIXTURES/'hotelmanager.txt').read_text()
PRODUCTS = (FIXTURES/'products.txt').read_text()
PARAMS = SimpleNamespace(room_types=('D2','D4','F1','F3','V6','V8','V10'), bed_types=('BSK4','BV8'))
NOW = datetime(2026,10,17)


def parsed(hotel=HOTEL, products=PRODUCTS):
    return dict(hotelmanager=reports.parse_hotelmanager(hotel,'Europe/Copenhagen',NOW),
                products=reports.parse_products(products,'Europe/Copenhagen',PARAMS,NOW))


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db,'_engine',None)
    monkeypatch.setattr(db,'SessionLocal',None)
    monkeypatch.setattr(validation,'PDF_ARCHIVE_ROOT',tmp_path/'pdfs')
    monkeypatch.setattr(db,'utcnow',lambda: NOW)
    monkeypatch.setattr(reports,'pdf_text',lambda data: HOTEL if data.startswith(b'hotel') else PRODUCTS)
    engine=db.init_db(f'sqlite:///{tmp_path}/results.db')
    with db.get_session() as s:
        yield s
    engine.dispose()


def staged(session, **kwargs):
    return reports.stage(session,b'hotel test source',b'products test source',
        SimpleNamespace(timezone='Europe/Copenhagen',params=PARAMS),'test',**kwargs)


def do_confirm(session, record, **kwargs):
    return reports.confirm(session,record,actor='test',
        **dict(final_counts=True,accept_revenue=True,date_confirmed=True,**kwargs))


def test_real_layout_selects_today_not_month_or_pax_and_keeps_rounding_unallocated():
    payload=parsed(); r=reports.review(payload)
    assert payload['hotelmanager']['rooms']==43 and payload['hotelmanager']['beds']==4
    assert payload['hotelmanager']['printed_at']=='2026-10-15T22:11:00Z'
    assert (r['room'],r['bed'],r['breakfast'],r['difference'])==('25248.40','903.45','457.60','0.01')
    assert r['total_lodging']=='26151.86' and r['hotel_difference']=='-0.14'
    assert r['revenue_ready'] and r['date_attestation_required']
    types={v['code']:v for v in r['by_type']}
    assert types['D2']['lodging']=='3232.70' and types['D2']['line_difference']=='-0.01'
    assert types['F3']['lodging']=='7173.19'  # MM line with occupants remains lodging
    assert types['V6']['lodging']=='4139.16'
    assert payload['products']['date_basis']=='Arrival date'


@pytest.mark.parametrize('old,new',[
    ('Danhostel Aarhus City','Another hotel'), ('Product types : Overnight','Product types : Day'),
    ('Out of Hous','Tentative'), ('excl. VAT.','incl. VAT.'), ('Grand Total','Missing Total'),
    ('Show for Arrival date','No date footer'), ('26.609,46','26.610,46'),
    ('3.232,70','3.233,70'),
])
def test_invalid_product_sources_rejected(old,new):
    with pytest.raises(ValueError): parsed(products=PRODUCTS.replace(old,new))


@pytest.mark.parametrize('old,new',[
    ('part 2 (Room/Bed)','part 2'), ('excl. Package','incl. Package'), ('Today','Other'),
    ('16-10-2026 kl. 00:11','20-10-2026 kl. 00:11'),
])
def test_invalid_hotelmanager_sources_rejected(old,new):
    with pytest.raises(ValueError): parsed(hotel=HOTEL.replace(old,new))


def test_unknown_type_or_bad_total_blocks_revenue_but_not_counts():
    p=parsed(products=PRODUCTS.replace('Total BV8','Total XX8'))
    assert not reports.review(p)['revenue_ready']
    p=parsed();p['hotelmanager']['total_price_rounded']='27000.00'
    assert not reports.review(p)['revenue_ready']
    p=parsed();p['hotelmanager']['adr_bed_rounded']='240.00'
    assert not reports.review(p)['revenue_ready']


def test_stage_is_draft_source_archived_and_repeated_renames_are_idempotent(session):
    record=staged(session,hotel_name='original.pdf');session.commit()
    again=staged(session,hotel_name='renamed.pdf')
    assert again.id==record.id
    assert [r.kind for r in session.scalars(select(db.ValidationRecord))]==['report_pair']
    data=json.loads(record.payload)
    assert (validation.PDF_ARCHIVE_ROOT/(data['hotel_pdf_hash']+'.pdf')).read_bytes()==b'hotel test source'
    assert economics.final_results(session,date(2026,10,1),date(2026,10,20),date(2026,10,17))==[]
    assert session.scalars(select(db.DayState)).all()==[]


def test_mismatch_and_unclosed_date_leave_no_draft(session,monkeypatch):
    monkeypatch.setattr(reports,'pdf_text',lambda b: HOTEL if b.startswith(b'hotel') else PRODUCTS.replace('for 15-10-2026','for 14-10-2026'))
    with pytest.raises(ValueError,match='samme dato'): staged(session)
    assert session.scalars(select(db.ValidationRecord)).all()==[]
    monkeypatch.setattr(reports,'pdf_text',lambda b: HOTEL.replace('Report for: 15-10-2026','Report for: 16-10-2026') if b.startswith(b'hotel') else PRODUCTS.replace('for 15-10-2026','for 16-10-2026'))
    with pytest.raises(ValueError,match='afsluttet'): staged(session)
    assert session.scalars(select(db.ValidationRecord)).all()==[]


def test_archive_failure_leaves_no_database_result(session,monkeypatch):
    monkeypatch.setattr(reports,'archive',lambda _: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError): staged(session)
    session.rollback()
    assert session.scalars(select(db.ValidationRecord)).all()==[]


def test_confirmation_requires_explicit_counts_and_financial_attestation(session):
    record=staged(session)
    with pytest.raises(ValueError,match='Bekræft'): reports.confirm(session,record,actor='t',final_counts=False,accept_revenue=False,date_confirmed=False)
    with pytest.raises(ValueError,match='Økonomi'): reports.confirm(session,record,actor='t',final_counts=True,accept_revenue=True,date_confirmed=False)
    assert reports.confirm(session,record,actor='t',final_counts=True,accept_revenue=False,date_confirmed=False)
    p=json.loads(session.scalar(select(db.ValidationRecord).where(db.ValidationRecord.kind=='outcome')).payload)
    assert p['final'] and p['final_rooms']==43 and p['final_beds']==4
    assert 'room_revenue' not in p and validation.actual_revenue(p) is None
    assert do_confirm(session,record)
    assert not do_confirm(session,record)
    result=economics.final_results(session,date(2026,10,15),date(2026,10,15),date(2026,10,17))[0]
    assert result['revenue']==26151.86 and result['contribution'] is None
    assert 'commission' not in result['data'] and 'available_beds' not in result['data']
    assert do_confirm(session,record,supplements=dict(commission='100',variable_cost='200',available_beds='320'))
    assert not do_confirm(session,record,supplements=dict(commission='100',variable_cost='200',available_beds='320'))
    result=economics.final_results(session,date(2026,10,15),date(2026,10,15),date(2026,10,17))[0]
    assert result['contribution']==25851.86
    assert result['revpab']==26151.86/320
    assert result['data']['revenue_adjustment']==.01
    assert session.scalars(select(db.Recommendation)).all()==[]  # No prices, runs or synthetic snapshots


def test_older_report_cannot_overwrite_newer_manual_result(session):
    record=staged(session)
    validation.append(session,'outcome',record.day,datetime(2026,10,16,12),
        dict(final=True,final_rooms=40,final_beds=3),actor='t',source='manual')
    with pytest.raises(ValueError,match='nyere resultat'): do_confirm(session,record)
    assert len(session.scalars(select(db.ValidationRecord)).all())==2


def test_prior_costs_carried_forward_but_invalid_groups_rejected(session):
    record=staged(session)
    validation.append(session,'outcome',record.day,datetime(2026,10,15,22,15),
        dict(commission=100,variable_cost=200,group_rooms=44),actor='t',source='manual')
    with pytest.raises(ValueError,match='gruppeantal'): do_confirm(session,record)
    # Append a corrected revision; history remains intact.
    validation.append(session,'outcome',record.day,datetime(2026,10,15,22,20),
        dict(commission=100,variable_cost=200,group_rooms=10),actor='t',source='manual')
    assert do_confirm(session,record)
    latest=session.scalar(select(db.ValidationRecord).where(db.ValidationRecord.kind=='outcome').order_by(db.ValidationRecord.id.desc()))
    p=json.loads(latest.payload)
    assert p['commission']==100 and p['variable_cost']==200 and p['group_rooms']==10
    assert validation.contribution(p)==(25851.86,None)


def test_blank_count_remains_unknown_not_zero(session,monkeypatch):
    hotel=HOTEL.replace('Room Inhouse                                  43          4', 'Room Inhouse                                  43           ')
    monkeypatch.setattr(reports,'pdf_text',lambda b: hotel if b.startswith(b'hotel') else PRODUCTS)
    record=staged(session)
    assert json.loads(record.payload)['hotelmanager']['beds'] is None
    with pytest.raises(ValueError,match='eksplicit antal'): do_confirm(session,record)


def test_upload_review_confirmation_and_exports_integrate(session):
    # Isolated router avoids the production scheduler; authentication remains a dependency.
    settings=SimpleNamespace(timezone='Europe/Copenhagen',params=PARAMS)
    app=FastAPI()
    templates=Jinja2Templates(directory='app/templates')
    templates.env.globals['freshness']=lambda: dict(stale=False,age_text='Test')
    app.include_router(router(settings,templates,lambda:'test'))
    with TestClient(app) as client:
        response=client.post('/resultater/rapporter',files={
            'hotelmanager':('Hotelmanager.pdf',b'hotel test source','application/pdf'),
            'products':('Products.pdf',b'products test source','application/pdf')},follow_redirects=False)
        assert response.status_code==303
        location=response.headers['location']
        page=client.get(location)
        assert page.status_code==200 and '25248.40' in page.text and 'Arrival date' in page.text
        assert 'Tallene bruges først' in page.text
        response=client.post(location+'/bekraeft',data={'final_counts':'true','accept_revenue':'true','date_confirmed':'false'})
        assert 'Økonomi kræver' in response.text
        response=client.post(location+'/bekraeft',data={'final_counts':'true','accept_revenue':'true','date_confirmed':'true'})
        assert response.status_code==200 and 'Kontrolleret resultat gemt' in response.text
        page=client.get('/resultater?start=2026-10-15&end=2026-10-15')
        assert page.status_code==200 and 'Se kilder og afstemning' in page.text
        exported=client.get('/resultater/export.csv?start=2026-10-15&end=2026-10-15')
        assert exported.status_code==200 and '26151,86' in exported.text
        assert 'omsaetning_afrunding_ufordelt' in exported.text and 'morgenmad_fratrukket' in exported.text
        assert client.get('/resultater/historik.csv').status_code==200
        assert client.get('/resultater/rapporter/9999').status_code==404
        with db.get_session() as s:
            p=json.loads(s.scalar(select(db.ValidationRecord).where(db.ValidationRecord.kind=='report_pair')).payload)
        assert client.get('/resultater/pdf/'+p['hotel_pdf_hash']).content==b'hotel test source'


def test_unresolved_revenue_cannot_be_accepted_even_with_attestation(session,monkeypatch):
    monkeypatch.setattr(reports,'pdf_text',lambda b: HOTEL if b.startswith(b'hotel') else PRODUCTS.replace('Total BV8','Total XX8'))
    record=staged(session)
    with pytest.raises(ValueError,match='Økonomi'): do_confirm(session,record)
    assert reports.confirm(session,record,actor='t',final_counts=True,accept_revenue=False,date_confirmed=False)
    p=json.loads(session.scalar(select(db.ValidationRecord).where(db.ValidationRecord.kind=='outcome')).payload)
    assert 'room_revenue' not in p


def test_confirmed_counts_feed_existing_forecast_validation(session):
    from app import forecast_validation as forecasts
    record=staged(session)
    for product in forecasts.PRODUCTS:
        session.add(db.ForecastSnapshot(day=record.day,lead=7,product=product,run_id=1,
            observed_at=datetime(2026,10,8),payload=json.dumps(dict(
                forecast=10,baseline=12,intervals={'80':[0,50]},capacity=100,weekend=False))))
    session.flush()
    assert forecasts.live_rows(session,date(2026,10,17))[0]==[]  # draft never scores
    reports.confirm(session,record,actor='t',final_counts=True,accept_revenue=False,date_confirmed=False)
    rows,_,_=forecasts.live_rows(session,date(2026,10,17))
    assert {r['product']:r['actual'] for r in rows}=={'rum':43,'senge':4}


@pytest.mark.parametrize('supplements',[{'commission':'-1'},{'variable_cost':'NaN'},{'available_beds':'1.5'}])
def test_bad_manual_supplements_cannot_create_a_partial_outcome(session,supplements):
    record=staged(session)
    with pytest.raises(ValueError): do_confirm(session,record,supplements=supplements)
    assert session.scalars(select(db.ValidationRecord).where(db.ValidationRecord.kind=='outcome')).all()==[]
