from datetime import date, datetime
import json

import pytest
from sqlalchemy import select
from app import db, economics, validation


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_engine', None)
    monkeypatch.setattr(db, 'SessionLocal', None)
    engine=db.init_db(f'sqlite:///{tmp_path}/economics.db')
    with db.get_session() as s:
        yield s
    engine.dispose()


def test_coverage_counts_zero_as_known_and_excludes_other_runs(session):
    run=db.Run(status='done')
    session.add(run);session.flush()
    for day, room, bed in [(date(2026,10,17),0,0),(date(2026,10,18),100,None)]:
        session.add(db.DayState(day=day,booked_room_revenue=room,booked_bed_revenue=bed))
        session.add(db.Recommendation(run_id=run.id,day=day,revenue_basis='estimated'))
    session.add(db.Recommendation(run_id=999,day=date(2026,10,19)))
    session.flush()
    result=economics.coverage(session,run)
    assert result['total']==2 and result['both_known']==1
    assert result['room_known']==2 and result['bed_known']==1
    assert result['missing']==1 and result['estimated']==2
    assert economics.coverage(session,None)['total']==0


def test_actual_contribution_never_estimates_missing_costs(session):
    for day, payload in [(date(2026,10,10),dict(final=True,room_revenue=1000,bed_revenue=0,available_beds=100)),
                         (date(2026,10,11),dict(final=True,room_revenue=1000,bed_revenue=200,commission=0,
                                               variable_cost=100,available_beds=100))]:
        validation.append(session,'outcome',day,datetime(2026,10,12),payload,actor='test',source='accounts')
    rows=economics.final_results(session,date(2026,10,1),date(2026,10,20),date(2026,10,16))
    assert rows[0]['revenue']==1000 and rows[0]['revpab']==10
    assert rows[0]['contribution'] is None
    assert rows[1]['contribution']==1100 and rows[1]['contribution_per_bed']==11


def test_price_confirmation_preserves_assignment_even_if_operator_changed_price(session):
    run=db.Run(status='done',finished=datetime(2026,10,14))
    session.add(run);session.flush()
    assignment=dict(assigned_price=650,assigned_arm='alternative',control_price=600,
                    alternative_price=650,probability_alternative=.5)
    session.add(db.Recommendation(run_id=run.id,day=date(2026,10,15),room_price=650,bed_price=200,
        ladder=json.dumps(dict(rooms=dict(exploration=assignment),beds={}))))
    session.flush()
    prepared=validation.prepare('applied',dict(day='2026-10-15',observed_at='2026-10-16T07:00',
        run_id=str(run.id),applied_room='700'),'Europe/Copenhagen')
    validation.record(session,'applied',prepared,actor='test',source='manual')
    result=json.loads(session.scalar(select(db.ValidationRecord.payload)))
    assert result['exploration_room']['assigned_price']==650
    assert result['exploration_room']['confirmed_applied_price']==700
    assert not result['exploration_room']['applied_matches_assignment']
    assert result['manual_override']
