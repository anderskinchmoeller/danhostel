from datetime import date, datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
import json
import math
import statistics

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from app import db, validation, forecast_metrics as metrics, forecast_charts as charts, forecast_validation as forecasts
from app.result_routes import router
from app.config import load_settings
from app.engine import DayInput, price_day


def rows(n=90):
    return [dict(day=(date(2025,4,1)+timedelta(days=i)).isoformat(),product='rum',lead=7,
                 forecast=20+(i%6),actual=17+(i%4),baseline=10,intervals={'80':[0,40]},
                 weekend=False,capacity=70,groups=None) for i in range(n)]


def test_hac_uses_calendar_gaps_and_matches_independent_mean_variance():
    points=[((date(2025,1,1)+timedelta(days=i*30)).isoformat(),float(i%7)) for i in range(90)]
    r=metrics.inference(points,7)
    expected=statistics.stdev(v for _,v in points)/math.sqrt(len(points))
    assert r['se']==pytest.approx(expected)
    assert r['ci'][0]<r['mean']<r['ci'][1]
    assert 0<=r['p']<=1


def test_hac_detects_positive_serial_correlation_and_suppresses_small_samples():
    values=[float((i//10)%2) for i in range(120)]
    points=[((date(2025,1,1)+timedelta(days=i)).isoformat(),v) for i,v in enumerate(values)]
    r=metrics.inference(points,7)
    assert r['se']>statistics.stdev(values)/math.sqrt(len(values))
    assert metrics.inference(points[:30],7)['p'] is None
    assert metrics.inference(points[:90],30)['p'] is None  # insufficient independent calendar span
    assert metrics.inference([(d,1) for d,_ in points],7)['p'] is None
    with pytest.raises(ValueError): metrics.inference(points+[points[0]],7)


def test_holm_correction_is_monotone_and_reserves_missing_tests():
    s=[dict(bias=dict(p=.001),advantage=dict(p=.01)),dict(bias=dict(p=None),advantage=dict(p=.1))]
    metrics.adjust(s)
    assert s[0]['bias']['adjusted_p']==pytest.approx(.024)
    assert s[0]['advantage']['adjusted_p']==pytest.approx(.23)
    assert s[1]['advantage']['adjusted_p']==1
    assert 'adjusted_p' not in s[1]['bias']


def test_zero_mean_does_not_conceal_large_absolute_errors():
    r=rows()
    for i,row in enumerate(r): row.update(actual=20,forecast=30 if i%2 else 10)
    s=metrics.summary(r,7)
    assert s['bias']['mean']==0 and s['mae']==10 and s['rmse']==10
    assert s['bias']['p']==1
    assert s['coverage80']==1


def test_graph_preserves_sign_zero_real_dates_and_no_fake_empty_data():
    assert charts.chart([]) is None
    r=rows(2);r[0].update(forecast=18,actual=20);r[1].update(forecast=20,actual=18,day='2025-04-10')
    chart=charts.chart(r)
    assert [p['error'] for p in chart['points']]==[-2,2]
    assert chart['points'][0]['y']>chart['zero']>chart['points'][1]['y']
    assert chart['first']=='01-04-2025' and chart['last']=='10-04-2025'
    assert charts.chart([dict(r[0],forecast=20)])['zero']>0


def test_period_and_horizon_filters_do_not_pool_repeated_forecasts():
    data=rows()+[dict(r,lead=1,forecast=9999) for r in rows()]
    r=charts.diagnostics(data,date(2025,4,1),date(2025,6,30),'rum',7)
    assert r['summary']['n']==90 and r['summary']['mae']<10
    assert r['summary']['bias']['adjusted_p']>=r['summary']['bias']['p']
    assert charts.diagnostics(data,date(2025,4,1),date(2025,6,30),'senge',7)['chart'] is None


@pytest.fixture
def session(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'_engine',None);monkeypatch.setattr(db,'SessionLocal',None)
    engine=db.init_db(f'sqlite:///{tmp_path}/chart.db')
    with db.get_session() as s: yield s
    engine.dispose()


def test_revenue_checkpoint_freezes_vat_and_does_not_rewrite_old_assumptions(session,monkeypatch):
    settings=load_settings();today=date(2026,10,17)
    monkeypatch.setattr(db,'utcnow',lambda:datetime(2026,10,17,8))
    inputs=[DayInput(today+timedelta(days=7),rooms_otb=10,beds_otb=0)]
    recs=[price_day(inputs[0],settings.params,today=today)]
    assert forecasts.save_checkpoints(session,SimpleNamespace(id=1),recs,inputs,settings.params,today,settings.timezone,
        revenue_settings={'price_vat_basis':'incl_25','accommodation_only_confirmed':True})==2
    session.flush()
    snapshot=session.scalar(select(db.ForecastSnapshot).where(db.ForecastSnapshot.product=='rum'))
    payload=json.loads(snapshot.payload)
    r=payload['revenue_checkpoint']
    assert r['expected_ex_vat']==pytest.approx(recs[0].revpab*settings.params.inventory.total_beds/1.25)
    original=snapshot.payload
    assert forecasts.save_checkpoints(session,SimpleNamespace(id=2),recs,inputs,settings.params,today,settings.timezone,
        revenue_settings={'price_vat_basis':'ex_vat','accommodation_only_confirmed':True})==0
    session.expire_all()
    assert session.get(db.ForecastSnapshot,snapshot.id).payload==original


@pytest.mark.parametrize('policy,booked',[
    ({},None),({'price_vat_basis':'incl_25'},None),
    ({'price_vat_basis':'incl_25','accommodation_only_confirmed':True},100),
])
def test_ambiguous_tax_or_packages_or_booked_mix_never_gets_silent_conversion(session,monkeypatch,policy,booked):
    settings=load_settings();today=date(2026,10,17)
    monkeypatch.setattr(db,'utcnow',lambda:datetime(2026,10,17,8))
    items=[DayInput(today+timedelta(days=7),rooms_otb=10,beds_otb=0,booked_room_revenue=booked)]
    forecasts.save_checkpoints(session,SimpleNamespace(id=1),[price_day(items[0],settings.params,today=today)],
        items,settings.params,today,settings.timezone,revenue_settings=policy)
    payload=json.loads(session.scalar(select(db.ForecastSnapshot).where(db.ForecastSnapshot.product=='rum')).payload)
    assert payload['revenue_checkpoint']['expected_ex_vat'] is None


def test_financial_graph_uses_only_confirmed_matched_comparable_records(session):
    day=date(2026,10,15)
    validation.append(session,'outcome',day,datetime(2026,10,16),
        dict(final=True,final_rooms=43,final_beds=4,room_revenue=25248.40,bed_revenue=903.45,
             revenue_adjustment=.01,revenue_date_confirmed=True),actor='t',source='test')
    common=dict(day=day,product='rum',observed_at=datetime(2026,10,8),run_id=1)
    for lead,checkpoint in [(7,dict(expected_raw=30000,expected_ex_vat=24000,basis='incl_25')),
                             (14,dict(expected_raw=30000,expected_ex_vat=None,basis='unknown'))]:
        session.add(db.ForecastSnapshot(**common,lead=lead,payload=json.dumps(dict(
            forecast=42,baseline=40,weekend=False,intervals={},capacity=70,revenue_checkpoint=checkpoint))))
    session.flush()
    data,pending=charts.live_rows(session,date(2026,10,17))
    finance=[r for r in data if r['product']=='revenue']
    assert len(finance)==1 and pending==1
    assert finance[0]['actual']==26151.86 and finance[0]['forecast']==24000
    assert finance[0]['baseline'] is None


def test_chart_page_and_export_keep_history_separate_and_validate_filters(session,monkeypatch):
    monkeypatch.setattr(charts,'historical_rows',lambda:rows())
    app=FastAPI();settings=load_settings()
    templates=Jinja2Templates(directory='app/templates')
    templates.env.globals['freshness']=lambda:dict(stale=False,age_text='Test')
    app.include_router(router(settings,templates,lambda:'test'))
    with TestClient(app) as client:
        page=client.get('/resultater/afvigelser')
        assert page.status_code==200 and '<svg' in page.text and 'Historisk test.' in page.text
        assert 'Holm' in page.text and 'p-værdi' in page.text
        assert '299' not in page.text  # custom fixture, no hardcoded real data
        live=client.get('/resultater/afvigelser?source=live')
        assert live.status_code==200 and '<svg' not in live.text and 'Ingen sammenlignelige datoer' in live.text
        finance=client.get('/resultater/afvigelser?source=live&measure=revenue')
        assert finance.status_code==200 and 'dividere med 1,25' in finance.text
        for query in ['lead=2','measure=profit','source=wrong','start=bad','start=2026-10-20&end=2026-10-01']:
            assert client.get('/resultater/afvigelser?'+query).status_code==422
        exported=client.get('/resultater/afvigelser.csv?source=historical')
        assert exported.status_code==200 and 'forskel_model_minus_faktisk' in exported.text
        assert exported.content.startswith(b'\xef\xbb\xbf')
        assert len(exported.text.splitlines())==91
