"""Statistical and workflow tests; all observations live in a temporary database."""
import csv
import hashlib
import io
import itertools
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from scipy.stats import t
from sqlalchemy import select

from app import db, experiments as ab, validation
from app.config import load_settings
from app.experiment_routes import router

TODAY = date(2026,10,17)


def frozen_settings():
    settings=load_settings()
    settings.params=replace(settings.params,v4=replace(settings.params.v4,level_alpha=0))
    return settings


@pytest.fixture
def session(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'_engine',None)
    monkeypatch.setattr(db,'SessionLocal',None)
    engine = db.init_db(f'sqlite:///{tmp_path}/ab.db')
    with db.get_session() as s:
        yield s
    engine.dispose()


def raw(**kw):
    return dict(name='Model mod normal strategi',start='2026-11-23',pairs='6',block_days='7',lead='30',
        capacity='310',sigma='1',delta='5',understood='yes',
        baseline='Samme normale strategi i alle A-blokke. Priser og ansvarlig er beskrevet.',
        safeguards='Stop ved prisfejl. Parspredning er baseret på historiske afstemte par.',
        interference='Afsmitning vurderet før start. Kanaler og gruppepolitik er fælles.',**kw)


def create(session):
    experiment = ab.create(session,raw(),frozen_settings(),'test',TODAY)
    session.commit()
    return experiment


def populate(session,experiment,missing=False):
    p=json.loads(experiment.protocol)
    after=date.fromisoformat(p['end'])+timedelta(days=2)
    for i,b in enumerate(p['blocks']):
        for day in ab.block_dates(b):
            # Controlled + varying treatment effects, with real zero costs allowed.
            value=100+(b['pair'] if b['arm']=='B' else 0)
            data=dict(final=True,final_rooms=10,final_beds=20,room_revenue=value*310,
                bed_revenue=0,commission=0,variable_cost=0,revenue_date_confirmed=True)
            if missing and day==date.fromisoformat(p['start']):
                data.pop('variable_cost')
            validation.append(session,'outcome',day,datetime.combine(after,datetime.min.time()),
                data,actor='test',source='Afstemte logital')
        ab.log_review(session,experiment,dict(block=str(i),adherence='deviations' if i==0 else 'yes',
            note='Kontrolleret i Picasso. Alle afvigelser er beskrevet og bevaret.'),'test',after)
    session.flush()
    return p,after


def test_schedule_is_balanced_reproducible_and_covers_every_date_once():
    first=date(2026,11,23)
    blocks=ab.schedule(first,12,14,123)
    assert blocks==ab.schedule(first,12,14,123)
    assert blocks!=ab.schedule(first,12,14,124)
    dates=[]
    for pair in range(1,13):
        assert {b['arm'] for b in blocks if b['pair']==pair}=={'A','B'}
    for b in blocks:
        assert date.fromisoformat(b['start']).weekday()==0
        dates+=ab.block_dates(b)
    assert len(set(dates))==len(dates)==336
    assert dates[-1]==first+timedelta(days=335)


def test_exact_p_matches_independent_enumeration_and_counts_ties():
    differences=[-2,3,4,0,7,-1]
    obs=abs(sum(differences))
    expected=sum(abs(sum(a*b for a,b in zip(signs,differences)))>=obs
        for signs in itertools.product([-1,1],repeat=6))/64
    assert ab.randomization_p(differences,1)==(expected,'exact',64)
    assert ab.randomization_p([0]*6,1)[0]==1
    assert ab.randomization_p([1]*6,1)[0]==pytest.approx(2/64)
    assert ab.randomization_p([-1]*6,1)[0]==pytest.approx(2/64)


def test_mc_has_plus_one_is_reproducible_and_agrees_with_exact_distribution():
    differences=[1.]*17
    a=ab.randomization_p(differences,123,9999)
    assert a==ab.randomization_p(differences,123,9999)
    assert a[0]>=1/10000 and a[1:] == ('monte_carlo',9999)
    assert ab.randomization_p([1,-1]*9,123,999)[0]==1


def test_pair_estimator_and_interval_match_known_sample():
    p=dict(pairs=6,inference_seed=123,mc_draws=999,delta=2)
    blocks=[dict(pair=i,arm=arm,value=10+(i if arm=='B' else 0)) for i in range(1,7) for arm in ('A','B')]
    result=ab.analyse(blocks,p)
    assert result['effect']==3.5
    assert result['se']==pytest.approx((3.5/6)**.5)
    assert result['ci'][0]==pytest.approx(3.5-t.ppf(.975,5)*(3.5/6)**.5)
    assert result['relative']==35
    assert result['p']==pytest.approx(2/64)
    for b in blocks:
        b['value']=10
    result=ab.analyse(blocks,p)
    assert result['ci'] is None and not result['positive'] and result['p']==1


def test_preregistration_hash_and_overlap_guard(session):
    experiment=create(session)
    p=json.loads(experiment.protocol)
    assert hashlib.sha256(experiment.protocol.encode()).hexdigest()==experiment.digest
    assert p['blocks']==ab.schedule(date.fromisoformat(p['start']),6,7,p['seed'])
    assert p['primary']=='contribution_per_fixed_bednight'
    with pytest.raises(ValueError,match='overlapper'):
        create(session)
    assert ab.power_pairs(6,5)==12


@pytest.mark.parametrize('key,value', [('start','2026-10-19'),('start','2026-11-24'),
    ('pairs','5'),('capacity','0'),('lead','0'),('sigma','nan'),('delta','inf'),('understood','')])
def test_invalid_or_retrospective_designs_rejected(session,key,value):
    args=raw();args[key]=value
    with pytest.raises(ValueError):
        ab.create(session,args,frozen_settings(),'test',TODAY)
    assert session.scalar(select(db.ABExperiment.id)) is None


def test_underpowered_plan_rejected_before_drawing(session):
    args=raw();args['sigma']='6'
    with pytest.raises(ValueError,match='mindst 12'):
        ab.create(session,args,frozen_settings(),'test',TODAY)


def test_final_analysis_requires_complete_economics_and_keeps_deviations(session):
    experiment=create(session)
    p,after=populate(session,experiment,missing=True)
    with pytest.raises(ValueError,match='Alle planlagte'):
        ab.finalize(session,experiment,frozen_settings(),'test',after,True)
    e=ab.evidence(session,experiment,after)
    assert len(e['missing'])==1
    # Missing costs may not silently become zero.
    assert e['completed']==11 and e['reviewed']==12
    first=date.fromisoformat(p['start'])
    corrected_revenue=(101 if p['blocks'][0]['arm']=='B' else 100)*310
    validation.append(session,'outcome',first,datetime.combine(after+timedelta(days=1),datetime.min.time()),
        dict(final=True,final_rooms=10,final_beds=20,room_revenue=corrected_revenue,bed_revenue=0,
             commission=0,variable_cost=0,revenue_date_confirmed=True),actor='test',source='Afstemning rettet')
    session.flush()
    result=ab.finalize(session,experiment,frozen_settings(),'test',after+timedelta(days=2),True)
    session.commit()
    assert result['effect']==pytest.approx(3.5) and result['deviations']==1
    assert result['n']==6 and len(result['sources'])==84
    frozen=experiment.final_analysis
    validation.append(session,'outcome',first,datetime.combine(after+timedelta(days=3),datetime.min.time()),
        dict(final=True,room_revenue=999999),actor='test',source='Later correction')
    session.commit()
    session.refresh(experiment)
    assert experiment.final_analysis==frozen
    with pytest.raises(ValueError,match='allerede afsluttet'):
        ab.finalize(session,experiment,frozen_settings(),'test',after+timedelta(days=4),True)


def test_later_withdrawal_or_unattested_revenue_blocks_final(session):
    experiment=create(session)
    p,after=populate(session,experiment)
    validation.append(session,'outcome',date.fromisoformat(p['start']),
        datetime.combine(after+timedelta(days=1),datetime.min.time()),dict(final=False),actor='test',source='Withdrawal')
    session.flush()
    assert len(ab.evidence(session,experiment,after+timedelta(days=2))['missing'])==1
    with pytest.raises(ValueError):
        ab.finalize(session,experiment,frozen_settings(),'test',after+timedelta(days=2),True)


def test_stop_and_policy_drift_block_confirmatory_analysis(session):
    experiment=create(session)
    p,after=populate(session,experiment)
    settings=frozen_settings()
    settings.params=replace(settings.params,quality_index=settings.params.quality_index+1)
    with pytest.raises(ValueError,match='Modelgrundlaget'):
        ab.finalize(session,experiment,settings,'test',after,True)
    ab.stop(session,experiment,'Prisfejl kræver stop efter de foruddefinerede regler.','test')
    session.commit()
    session.refresh(experiment)
    assert experiment.status=='stopped' and experiment.final_analysis is None
    with pytest.raises(ValueError):
        ab.log_review(session,experiment,dict(block='0',adherence='yes',note='A'*30),'test',after)


def test_routes_render_both_registration_and_final_snapshot(session,monkeypatch):
    from app import experiment_routes
    experiment=create(session)
    templates=Jinja2Templates(directory=str(Path(__file__).resolve().parents[1]/'app/templates'))
    templates.env.globals['freshness']=lambda:dict(stale=False,age_text='Nu')
    app=FastAPI()
    app.include_router(router(frozen_settings(),templates,lambda:'test'))
    with TestClient(app) as client:
        page=client.get('/resultater/ab')
        assert page.status_code==200 and 'TikZ' in page.text
        assert 'data-latex' in page.text and 'stop' in page.text.lower()
        assert client.get('/resultater/ab?experiment_id=999').status_code==404
        collecting=client.get(f'/resultater/ab?experiment_id={experiment.id}')
        assert collecting.status_code==200 and 'Ingen løbende p-værdier' in collecting.text
        calendar=client.get(f'/resultater/ab/{experiment.id}/kalender.csv')
        assert len(list(csv.DictReader(io.StringIO(calendar.content.decode('utf-8-sig')),delimiter=';')))==84
        protocol=client.get(f'/resultater/ab/{experiment.id}/protokol.json').json()
        assert protocol['sha256']==experiment.digest and protocol['final_analysis'] is None
        p,after=populate(session,experiment)
        result=ab.finalize(session,experiment,frozen_settings(),'test',after,True)
        session.commit()
        page=client.get(f'/resultater/ab?experiment_id={experiment.id}')
        assert page.status_code==200 and 'Slutanalysen er låst' in page.text and '0.03125' in page.text
        assert client.get(f'/resultater/ab/{experiment.id}/protokol.json').json()['final_analysis']['effect']==3.5


def test_shared_learning_is_a_hard_start_condition(session):
    with pytest.raises(ValueError,match='niveaulæring skal pauses'):
        ab.create(session,raw(),load_settings(),'test',TODAY)
    assert session.scalar(select(db.ABExperiment.id)) is None


def test_negative_contribution_is_preserved_and_zero_revenue_is_observed(session):
    experiment=create(session)
    p,after=populate(session,experiment)
    first=date.fromisoformat(p['start'])
    validation.append(session,'outcome',first,datetime.combine(after,datetime.min.time()),
        dict(final=True,final_rooms=0,final_beds=0,room_revenue=0,bed_revenue=0,
             revenue_adjustment=-1,commission=0,variable_cost=100,revenue_date_confirmed=True),
        actor='test',source='Afstemt nulomsætning med omkostninger')
    session.flush()
    e=ab.evidence(session,experiment,after)
    assert e['missing']==[] and e['sources'][0]['payload']['room_revenue']==0
    # A negative day remains in the block; the denominator stays fixed.
    remaining=sum(s['payload']['room_revenue'] for s in e['sources'][1:7])
    assert e['blocks'][0]['value']==pytest.approx((remaining-101)/(7*310))


def test_unconfirmed_date_and_invalid_adjustment_do_not_become_observed_revenue(session):
    experiment=create(session)
    p,after=populate(session,experiment)
    first=date.fromisoformat(p['start'])
    for day,extra in [(first,dict(revenue_date_confirmed=False)),
                      (first+timedelta(days=1),dict(revenue_adjustment='not a number'))]:
        values=dict(final=True,final_rooms=0,final_beds=0,room_revenue=0,bed_revenue=0,
                    commission=0,variable_cost=0,revenue_date_confirmed=True)
        values.update(extra)
        validation.append(session,'outcome',day,datetime.combine(after,datetime.min.time()),values,
            actor='test',source='Unusable')
    session.flush()
    assert len(ab.evidence(session,experiment,after)['missing'])==2


def test_changed_analysis_method_cannot_silently_replace_preregistered_method(session,monkeypatch):
    experiment=create(session)
    p,after=populate(session,experiment)
    old=ab.analysis_method()
    monkeypatch.setattr(ab,'analysis_method',lambda:dict(old,name='different-method'))
    with pytest.raises(ValueError,match='Analysemetoden'):
        ab.finalize(session,experiment,frozen_settings(),'test',after,True)
    assert experiment.final_analysis is None
