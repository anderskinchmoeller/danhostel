"""Accounts evidence: real 16 October layout, signed bridges, no forced allocation."""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app import db, validation, economics, picasso_results as reports, picasso_accounts as accounts
from app.result_routes import router

FIXTURES=Path(__file__).parent/'fixtures'/'picasso_results'
HOTEL=(FIXTURES/'hotelmanager_20261016.txt').read_text()
PRODUCTS=(FIXTURES/'products_20261016.txt').read_text()
ACCOUNTS=(FIXTURES/'accounts_20261016.txt').read_text()
PARAMS=SimpleNamespace(room_types=('D2','D4','F1','F3','FS','V6','V10'),bed_types=('BSK4','BV8'))
SETTINGS=SimpleNamespace(timezone='Europe/Copenhagen',params=PARAMS)
NOW=datetime(2026,10,18)


def payload():
    return dict(hotelmanager=reports.parse_hotelmanager(HOTEL,SETTINGS.timezone,NOW),
        products=reports.parse_products(PRODUCTS,SETTINGS.timezone,PARAMS,NOW),
        accounts=accounts.parse(ACCOUNTS,SETTINGS.timezone,NOW))


@pytest.fixture
def session(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'_engine',None);monkeypatch.setattr(db,'SessionLocal',None)
    monkeypatch.setattr(validation,'PDF_ARCHIVE_ROOT',tmp_path/'pdfs')
    monkeypatch.setattr(db,'utcnow',lambda:NOW)
    monkeypatch.setattr(reports,'pdf_text',lambda b: HOTEL if b.startswith(b'hotel') else ACCOUNTS if b.startswith(b'accounts') else PRODUCTS)
    engine=db.init_db(f'sqlite:///{tmp_path}/accounts.db')
    with db.get_session() as s:yield s
    engine.dispose()


def staged(session,products=True,account=True):
    return reports.stage(session,b'hotel',b'products' if products else None,SETTINGS,'test',
                         account_data=b'accounts' if account else None)


def test_real_account_signs_and_result_do_not_conflate_ledger_with_lodging():
    p=payload();a=p['accounts']
    assert a['day']=='2026-10-16' and a['printed_at']=='2026-10-17T06:57:00Z'
    assert a['summary']==dict(lodging='19688.35',upgrade='1384.72',breakfast='4364.80',other='2079.20')
    assert a['reported_result']=='-27517.06' and a['line_rounding']=='-0.01'
    assert len(a['rows'])==20 and len(a['unallocated_rows'])==11
    room=next(r for r in a['unallocated_rows'] if r['account']=='1002')
    assert room['amount']=='8818.54' and room['revenue']=='-8818.54'
    assert Decimal(room['amount'])==Decimal(room['debit'])-Decimal(room['credit'])


def test_16_october_only_product_groups_reconcile_not_final_stay_date_economics():
    p=payload();r=reports.review(p);bridge=r['accounting']
    assert p['hotelmanager']['rooms']==47 and p['hotelmanager']['beds']==3
    assert bridge['linked_lodging']=='28506.89' and bridge['linked_breakfast']=='422.40'
    assert bridge['product_net']=='28506.88' and bridge['product_net_vs_linked']=='-0.01'
    assert bridge['linked_vs_hotel']=='99.89'
    assert bridge['unallocated_lodging']=='-8818.54' and bridge['unallocated_upgrade']=='1384.72'
    assert bridge['lodging_plus_upgrades']=='21073.07'
    assert {c['difference'] for c in bridge['comparisons']}=={'0.00'}
    mm=next(c for c in bridge['comparisons'] if c['product']=='YP NON-REF MM')
    assert mm['breakfast_unallocated']=='176.00' and mm['accounts_lodging']=='1463.54'
    assert not r['revenue_ready']  # Even a date attestation cannot force these figures to agree.
    assert sum(Decimal(row['amount']) for row in p['products']['rows'] if row['category']=='unknown')==Decimal('941.04')
    assert {row['code']:row['unknown'] for row in r['by_type']}['V10']=='365.04'


@pytest.mark.parametrize('old,new',[
    ('to 16-10-2026','to 17-10-2026'),('Corrections included','Corrections excluded'),
    ('-27.517,06','-27.518,06'),('8.818,54','8.818,55'),
    ('NoVat','Other'),('Værelser','Other account'),('Result:','Missing:'),
    ('Danhostel Aarhus City','Other hotel'),
])
def test_ambiguous_unbalanced_or_wrong_accounts_rejected(old,new):
    # One-cent Amount mismatch is tolerated as row rounding; break by two cents instead.
    if old=='8.818,54':new='8.819,54'
    with pytest.raises(ValueError):accounts.parse(ACCOUNTS.replace(old,new),SETTINGS.timezone,NOW)


def test_unknown_account_remains_separate_not_an_automatic_lodging_mapping():
    text=ACCOUNTS.replace('1004','9999')
    parsed=accounts.parse(text,SETTINGS.timezone,NOW)
    assert parsed['summary']['upgrade']=='0.00'
    assert parsed['summary']['other']=='3463.92'
    assert next(r for r in parsed['rows'] if r['account']=='9999')['category']=='other'


def test_short_product_prefix_cannot_absorb_an_unknown_product():
    p=payload();p['products']['rows'][2]['description']='YPX Unknown product'
    result=accounts.reconcile(p['accounts'],p['products'],p['hotelmanager'])
    assert result['unlinked'] and result['issues']


def test_hotel_only_import_supports_counts_and_no_fabricated_money(session):
    record=staged(session,products=False,account=False)
    assert reports.review(json.loads(record.payload))['revenue_ready'] is False
    assert reports.confirm(session,record,actor='test',final_counts=True,accept_revenue=False,date_confirmed=False)
    result=economics.final_results(session,date(2026,10,16),date(2026,10,16),date(2026,10,18))[0]
    assert result['data']['final_rooms']==47 and result['data']['final_beds']==3
    assert result['revenue'] is None and result['contribution'] is None
    with pytest.raises(ValueError,match='Økonomi'):
        reports.confirm(session,record,actor='test',final_counts=True,accept_revenue=True,date_confirmed=True)


def test_three_files_stage_idempotently_and_keep_unallocated_posts_out_of_revenue(session):
    record=staged(session);session.commit()
    assert staged(session).id==record.id
    p=json.loads(record.payload)
    assert (validation.PDF_ARCHIVE_ROOT/(p['account_pdf_hash']+'.pdf')).read_bytes()==b'accounts'
    assert reports.review(p)['revenue_ready'] is False
    with pytest.raises(ValueError,match='Økonomi'):
        reports.confirm(session,record,actor='test',final_counts=True,accept_revenue=True,date_confirmed=True)
    assert reports.confirm(session,record,actor='test',final_counts=True,accept_revenue=False,date_confirmed=False)
    session.commit()
    latest=session.scalar(select(db.ValidationRecord).where(db.ValidationRecord.kind=='outcome'))
    data=json.loads(latest.payload)
    assert data['accounts_report_id']==record.id and len(data['accounts_unallocated'])==11
    assert 'room_revenue' not in data and 'revenue_adjustment' not in data
    assert validation.actual_revenue(data) is None


def test_wrong_account_date_leaves_no_staged_result(session,monkeypatch):
    monkeypatch.setattr(reports,'pdf_text',lambda b:ACCOUNTS.replace('16-10-2026','15-10-2026') if b.startswith(b'accounts') else HOTEL if b.startswith(b'hotel') else PRODUCTS)
    with pytest.raises(ValueError,match='samme dato'):staged(session)
    assert session.scalar(select(db.ValidationRecord.id)) is None


def test_hotel_and_accounts_without_products_can_be_reviewed_and_exported(session):
    app=FastAPI();templates=Jinja2Templates(directory='app/templates')
    templates.env.globals['freshness']=lambda:dict(stale=False,age_text='Test')
    app.include_router(router(SETTINGS,templates,lambda:'test'))
    with TestClient(app) as client:
        response=client.post('/resultater/rapporter',files={'hotelmanager':('Hotel.pdf',b'hotel','application/pdf'),
            'accounts':('Accounts.pdf',b'accounts','application/pdf')},follow_redirects=False)
        assert response.status_code==303
        location=response.headers['location']
        page=client.get(location)
        assert page.status_code==200 and '47 værelser og 3 senge' in page.text
        assert 'Bogføringsrapport · original' in page.text and 'Produktomsætning er ikke vedlagt' in page.text
        assert 'Uden produktkobling' in page.text
        response=client.post(location+'/bekraeft',data={'final_counts':'true'})
        assert response.status_code==200
        csv=client.get('/resultater/export.csv?start=2026-10-16&end=2026-10-16')
        assert 'bogfoering_ufordelte_poster_json' in csv.text and '-8818.54' in csv.text
        assert client.get('/resultater?start=2026-10-16&end=2026-10-16').status_code==200


def test_real_triple_route_shows_blocked_economics_and_all_sources(session):
    app=FastAPI();templates=Jinja2Templates(directory='app/templates')
    templates.env.globals['freshness']=lambda:dict(stale=False,age_text='Test')
    app.include_router(router(SETTINGS,templates,lambda:'test'))
    with TestClient(app) as client:
        response=client.post('/resultater/rapporter',files={'hotelmanager':('Hotel.pdf',b'hotel','application/pdf'),
            'products':('Products.pdf',b'products','application/pdf'),'accounts':('Accounts.pdf',b'accounts','application/pdf')},follow_redirects=False)
        page=client.get(response.headers['location'])
        assert page.status_code==200
        assert all(s in page.text for s in ('28506.88','99.89','176.00','-8818.54','1384.72','Arrival date'))
        assert 'name="accept_revenue" value="true" disabled' in page.text


def test_a_160_kr_unallocated_post_is_not_rounding_or_assigned_to_an_inventory():
    header=ACCOUNTS.split('\nNone\n')[0]
    original=next(l for l in ACCOUNTS.splitlines() if '1002' in l and '8.818,54' in l)
    row=original.replace('8.818,54','-160,00').replace('11.930,94','0,00').replace('3.112,40','160,00')
    text=header+'\nNone\n'+row+'\n                 -160,00       0,00       160,00\nResult: -160,00\n'
    parsed=accounts.parse(text,SETTINGS.timezone,NOW)
    assert parsed['summary']['lodging']=='160.00'
    assert parsed['unallocated_rows'][0]['revenue']=='160.00'
    assert 'room_type' not in parsed['unallocated_rows'][0]
    bridge=accounts.reconcile(parsed,None,payload()['hotelmanager'])
    assert bridge['unallocated_lodging']=='160.00' and bridge['linked_lodging']=='0.00'
    assert parsed['line_rounding']=='0.00' and bridge['issues']


@pytest.mark.parametrize('unallocated,linked,explained',[
    ('144.00','28885.43',True),
    ('-144.00','28885.43',True),
    ('144.00','28895.43',False),
    ('0.00','28885.43',False),
])
def test_manual_sale_bridge_preserves_ledger_and_does_not_attest_stay_date(unallocated,linked,explained):
    manual=Decimal(unallocated)
    total=Decimal(linked)+manual
    a=dict(by_product=[dict(product='ROOM',unallocated=False,lodging=linked,
                           upgrade='0.00',breakfast='0.00',other='0.00')],
           unallocated_rows=[dict(category='lodging',revenue=unallocated)],
           summary=dict(lodging=str(total),upgrade='0.00'))
    bridge=accounts.reconcile(a,None,dict(total_price_rounded='28885.00'))
    assert bridge['unallocated_explains_difference'] is explained
    assert Decimal(bridge['ledger_vs_hotel'])==total-Decimal('28885.00')
    assert Decimal(bridge['residual_after_unallocated'])==Decimal(linked)-Decimal('28885.00')
    assert Decimal(bridge['lodging_all'])==total
    assert bridge['linked_lodging']==linked
    # A matching amount never clears the unresolved allocation/date check.
    if manual:
        assert bridge['issues']
